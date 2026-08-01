#!/usr/bin/env python3
"""Create 15s, 1m, 5m and 15m OHLCV candles from downloaded Parquet files."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

TARGETS = {
    "15s": "15s",
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
}


def read_day(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{path}: missing columns {sorted(missing)}")
    df["date"] = pd.to_datetime(df["date"], utc=True)
    return df.sort_values("date").drop_duplicates("date", keep="last")


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    indexed = df.set_index("date")
    result = indexed.resample(rule, label="left", closed="left", origin="epoch").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    return result.dropna(subset=["open", "high", "low", "close"]).reset_index()


def write_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    df.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Resample FXReplay OHLCV data")
    parser.add_argument("--input", type=Path, required=True, help="Folder containing daily 1s parquet files")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--targets", nargs="+", choices=sorted(TARGETS), default=list(TARGETS))
    parser.add_argument("--csv", action="store_true", help="Also create one CSV.gz per target")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    files = sorted(args.input.glob("*.parquet"))
    if not files:
        raise SystemExit(f"No parquet files found in {args.input}")

    csv_parts: dict[str, list[pd.DataFrame]] = {target: [] for target in args.targets}

    for source in files:
        day = source.stem
        df = read_day(source)
        for target in args.targets:
            destination = args.output / target / f"{day}.parquet"
            if destination.exists() and not args.overwrite:
                print(f"SKIP {target} {day}")
                if args.csv:
                    csv_parts[target].append(read_day(destination))
                continue
            output = resample(df, TARGETS[target])
            write_parquet(output, destination)
            if args.csv:
                csv_parts[target].append(output)
            print(f"OK   {target} {day} rows={len(output)}")

    if args.csv:
        for target, parts in csv_parts.items():
            if not parts:
                continue
            full = pd.concat(parts, ignore_index=True).drop_duplicates("date").sort_values("date")
            csv_path = args.output / f"{target}.csv.gz"
            full.to_csv(csv_path, index=False, compression="gzip", date_format="%Y-%m-%dT%H:%M:%S.%fZ")
            print(f"CSV  {csv_path} rows={len(full)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
