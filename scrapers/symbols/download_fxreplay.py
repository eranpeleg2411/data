#!/usr/bin/env python3
"""Download FXReplay candles for a full calendar year.

Uses the user's own FXReplay bearer token from the FXREPLAY_TOKEN environment
variable. Data is written incrementally as Parquet files, one file per day and
resolution, so the process can resume safely and does not load a full year into RAM.

The FXReplay endpoint is private/undocumented and may change. This downloader
therefore supports several common JSON/CSV/ZIP response shapes and fails loudly
when it cannot identify OHLCV fields.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import random
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import pandas as pd
import requests

BASE_URL = "https://awf.fxreplay.com/archives-zip-opt"
DEFAULT_SYMBOL = "CME_MINI:NQ1"


@dataclass(frozen=True)
class Resolution:
    api_value: str
    milliseconds: int
    folder: str


RESOLUTIONS = {
    "1s": Resolution("1S", 1_000, "1s"),
    "1m": Resolution("1", 60_000, "1m"),
}


def utc_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def parse_utc_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def iter_date_range(start: datetime, end_inclusive: datetime) -> Iterator[tuple[datetime, datetime]]:
    """Yield UTC day intervals for an inclusive calendar-date range."""
    current = start
    end_exclusive = end_inclusive + timedelta(days=1)
    while current < end_exclusive:
        nxt = current + timedelta(days=1)
        yield current, nxt
        current = nxt


def retry_sleep(attempt: int, retry_after: str | None = None) -> None:
    if retry_after:
        try:
            delay = min(float(retry_after), 120.0)
        except ValueError:
            delay = 0.0
    else:
        delay = 0.0
    delay = max(delay, min(2 ** attempt + random.random(), 60.0))
    time.sleep(delay)


def decode_response(response: requests.Response) -> Any:
    raw = response.content
    content_type = response.headers.get("content-type", "").lower()
    # requests normally decompresses gzip automatically but may preserve the
    # Content-Encoding header. Only decompress when the gzip magic bytes are
    # actually present; otherwise a JSON body such as b"[[..." would fail.
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)

    if raw[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
            if not names:
                return []
            parts: list[Any] = []
            for name in names:
                member = archive.read(name)
                parts.append(decode_bytes(member, name))
            return parts

    return decode_bytes(raw, content_type)


def decode_bytes(raw: bytes, hint: str = "") -> Any:
    text = raw.decode("utf-8-sig", errors="replace").strip()
    if not text:
        return []

    if "json" in hint or text[:1] in "[{":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Some APIs return newline-delimited JSON.
            rows = []
            for line in text.splitlines():
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
            return rows

    # CSV fallback.
    return list(csv.DictReader(io.StringIO(text)))


def flatten_payload(payload: Any) -> list[Any]:
    """Locate candle-like records inside several likely response envelopes."""
    if payload is None:
        return []
    if isinstance(payload, list):
        out: list[Any] = []
        for item in payload:
            if isinstance(item, (list, dict)):
                # Preserve a direct numeric OHLCV row.
                if isinstance(item, list) and item and all(
                    isinstance(x, (int, float, type(None))) for x in item
                ):
                    out.append(item)
                else:
                    out.extend(flatten_payload(item))
        return out
    if isinstance(payload, dict):
        # Direct candle object.
        keys = {str(k).lower() for k in payload.keys()}
        if keys & {"open", "o"} and keys & {"close", "c"}:
            return [payload]

        preferred = (
            "data",
            "candles",
            "bars",
            "items",
            "results",
            "archive",
            "records",
            "s",
        )
        for key in preferred:
            if key in payload:
                found = flatten_payload(payload[key])
                if found:
                    return found

        out: list[Any] = []
        for value in payload.values():
            if isinstance(value, (list, dict)):
                out.extend(flatten_payload(value))
        return out
    return []


def first_present(row: dict[str, Any], names: Iterable[str]) -> Any:
    lowered = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def normalize_timestamp(value: Any) -> int | None:
    """Convert a plausible Unix timestamp to epoch milliseconds.

    Reject ordinary market prices such as 21161.5. The previous parser treated
    any small number as epoch seconds, so a seven-column FXReplay row was
    mistakenly interpreted as [index, timestamp, ...].
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            value = float(stripped)
        except ValueError:
            parsed = pd.to_datetime(stripped, utc=True, errors="coerce")
            if pd.isna(parsed):
                return None
            millis = int(parsed.timestamp() * 1000)
            return millis if 946_684_800_000 <= millis <= 4_102_444_800_000 else None
    if isinstance(value, (int, float)):
        number = float(value)
        # Plausible epoch seconds: 2000-01-01 through 2100-01-01.
        if 946_684_800 <= number <= 4_102_444_800:
            return int(number * 1000)
        # Plausible epoch milliseconds for the same range.
        if 946_684_800_000 <= number <= 4_102_444_800_000:
            return int(number)
        return None
    return None


def normalize_record(record: Any) -> dict[str, Any] | None:
    if isinstance(record, dict):
        timestamp = first_present(record, ["date", "time", "timestamp", "t", "datetime"])
        open_ = first_present(record, ["open", "o"])
        high = first_present(record, ["high", "h"])
        low = first_present(record, ["low", "l"])
        close = first_present(record, ["close", "c"])
        volume = first_present(record, ["volume", "v", "vol"])
    elif isinstance(record, list):
        # FXReplay returns:
        # [timestamp_ms, open, high, low, close, volume, metadata]
        # Other providers sometimes return:
        # [index, timestamp, open, high, low, close, volume]
        # Always prefer a plausible timestamp in column 0.
        if len(record) >= 6 and normalize_timestamp(record[0]) is not None:
            timestamp, open_, high, low, close, volume = record[:6]
        elif len(record) >= 7 and normalize_timestamp(record[1]) is not None:
            timestamp, open_, high, low, close, volume = record[1:7]
        else:
            return None
    else:
        return None

    ts = normalize_timestamp(timestamp)
    if ts is None:
        return None
    try:
        return {
            "date": pd.to_datetime(ts, unit="ms", utc=True),
            "open": float(open_),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": float(volume or 0),
        }
    except (TypeError, ValueError):
        return None


def normalize_payload(payload: Any) -> pd.DataFrame:
    rows = []
    for record in flatten_payload(payload):
        normalized = normalize_record(record)
        if normalized:
            rows.append(normalized)
    if not rows:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    return df


def request_page(
    session: requests.Session,
    token: str,
    symbol: str,
    resolution: Resolution,
    start_ms: int,
    end_ms: int,
    cursor_ms: int,
    limit: int,
    timeout: int,
    retries: int,
) -> pd.DataFrame:
    url = f"{BASE_URL}/{symbol}/{resolution.api_value}"
    headers = {
        "accept": "application/json, text/plain, */*",
        "authorization": f"Bearer {token}",
        "origin": "https://app.fxreplay.com",
        "referer": "https://app.fxreplay.com/",
        "user-agent": "Mozilla/5.0 FXReplay-year-downloader/1.0",
        "x-request-id": str(uuid.uuid4()),
        "x-session-id": os.getenv("FXREPLAY_SESSION_ID", str(uuid.uuid4())),
        "x-tab-id": os.getenv("FXREPLAY_TAB_ID", str(uuid.uuid4())),
    }
    params = {
        "from": start_ms,
        "to": end_ms,
        "sessionType": os.getenv("FXREPLAY_SESSION_TYPE", "undefined"),
        "limit": limit,
        "currentDate": cursor_ms,
    }

    for attempt in range(retries + 1):
        try:
            response = session.get(url, headers=headers, params=params, timeout=timeout)
            if response.status_code in (401, 403):
                raise RuntimeError(
                    f"FXReplay rejected the token ({response.status_code}). "
                    "Create a fresh token and export it as FXREPLAY_TOKEN."
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= retries:
                    response.raise_for_status()
                retry_sleep(attempt, response.headers.get("retry-after"))
                continue
            response.raise_for_status()
            payload = decode_response(response)
            df = normalize_payload(payload)
            if df.empty and os.getenv("FXREPLAY_DEBUG") == "1":
                preview = repr(payload)[:2000]
                print(
                    f"DEBUG empty payload status={response.status_code} "
                    f"content-type={response.headers.get('content-type')} "
                    f"url={response.url} payload={preview}",
                    file=sys.stderr,
                )
            return df
        except (requests.RequestException, zipfile.BadZipFile, json.JSONDecodeError) as exc:
            if attempt >= retries:
                raise RuntimeError(f"Request failed after {retries + 1} attempts: {exc}") from exc
            retry_sleep(attempt)

    raise AssertionError("unreachable")


def download_day(
    session: requests.Session,
    token: str,
    symbol: str,
    resolution: Resolution,
    day_start: datetime,
    day_end: datetime,
    output_file: Path,
    limit: int,
    timeout: int,
    retries: int,
    pause: float,
) -> int:
    start_ms = utc_ms(day_start)
    end_ms = utc_ms(day_end) - 1

    # FXReplay pages backwards from currentDate. Start at the end of the day
    # and move the cursor to just before the oldest returned candle.
    cursor = end_ms
    chunks: list[pd.DataFrame] = []
    previous_cursor = end_ms + resolution.milliseconds

    while cursor >= start_ms:
        df = request_page(
            session=session,
            token=token,
            symbol=symbol,
            resolution=resolution,
            start_ms=start_ms,
            end_ms=end_ms,
            cursor_ms=cursor,
            limit=limit,
            timeout=timeout,
            retries=retries,
        )
        if df.empty:
            break

        day_mask = (df["date"] >= pd.Timestamp(day_start)) & (df["date"] < pd.Timestamp(day_end))
        df = df.loc[day_mask]
        if df.empty:
            break
        chunks.append(df)

        min_ms = int(df["date"].min().timestamp() * 1000)
        previous_cursor, cursor = cursor, min_ms - resolution.milliseconds
        if cursor >= previous_cursor:
            raise RuntimeError("FXReplay backward pagination made no progress; endpoint semantics may have changed.")
        if min_ms <= start_ms:
            break
        if pause:
            time.sleep(pause)

    if not chunks:
        return 0

    result = pd.concat(chunks, ignore_index=True)
    result = result.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    output_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_file.with_suffix(output_file.suffix + ".part")
    result.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(output_file)
    return len(result)


def main() -> int:
    parser = argparse.ArgumentParser(description="Download FXReplay candles by year or date range")
    parser.add_argument(
        "--year",
        type=int,
        help="Calendar year. Supplies Jan 1-Dec 31 defaults; --start-date/--end-date can override them.",
    )
    parser.add_argument("--start-date", help="First date to download, inclusive (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="Last date to download, inclusive (YYYY-MM-DD)")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--resolutions", nargs="+", choices=sorted(RESOLUTIONS), default=["1s", "1m"])
    parser.add_argument("--output", type=Path, default=Path("data"))
    parser.add_argument("--limit", type=int, default=329)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--pause", type=float, default=0.15, help="Pause between pages")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    try:
        if args.year is not None:
            default_start = datetime(args.year, 1, 1, tzinfo=timezone.utc)
            default_end = datetime(args.year, 12, 31, tzinfo=timezone.utc)
        else:
            default_start = None
            default_end = None

        start_date = parse_utc_date(args.start_date) if args.start_date else default_start
        end_date = parse_utc_date(args.end_date) if args.end_date else default_end
    except ValueError as exc:
        parser.error(f"Invalid date. Use YYYY-MM-DD: {exc}")

    if start_date is None:
        parser.error("Provide --year or --start-date")
    if end_date is None:
        # A start-only range downloads through today's UTC date.
        now = datetime.now(timezone.utc)
        end_date = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    if end_date < start_date:
        parser.error("--end-date must be on or after --start-date")

    token = os.getenv("FXREPLAY_TOKEN")
    if not token:
        print("Missing FXREPLAY_TOKEN environment variable.", file=sys.stderr)
        return 2

    with requests.Session() as session:
        for resolution_name in args.resolutions:
            resolution = RESOLUTIONS[resolution_name]
            for day_start, day_end in iter_date_range(start_date, end_date):
                output_file = (
                    args.output
                    / args.symbol.replace(":", "_")
                    / str(day_start.year)
                    / resolution.folder
                    / f"{day_start.date().isoformat()}.parquet"
                )
                if output_file.exists() and not args.overwrite:
                    print(f"SKIP {resolution_name} {day_start.date()} (already exists)")
                    continue
                try:
                    count = download_day(
                        session=session,
                        token=token,
                        symbol=args.symbol,
                        resolution=resolution,
                        day_start=day_start,
                        day_end=day_end,
                        output_file=output_file,
                        limit=args.limit,
                        timeout=args.timeout,
                        retries=args.retries,
                        pause=args.pause,
                    )
                    print(f"OK   {resolution_name} {day_start.date()} rows={count}")
                except Exception as exc:
                    print(f"FAIL {resolution_name} {day_start.date()}: {exc}", file=sys.stderr)
                    return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
