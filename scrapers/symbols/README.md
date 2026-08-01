# FXReplay NQ yearly downloader

Downloads your authorized FXReplay NQ candles for a full calendar year and then creates 15-second, 1-minute, 5-minute and 15-minute datasets.

> FXReplay's endpoint is private and undocumented. It may change. Use only with your own account and according to the service terms. Do not commit your bearer token.

## 1. Install

```bash
cd fxreplay_nq_downloader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Set a fresh token

The token pasted into chat should be treated as exposed. Sign out/in to FXReplay and copy a fresh bearer token from DevTools.

```bash
export FXREPLAY_TOKEN='PASTE_FRESH_TOKEN_HERE'
```

Optional, only if FXReplay requires the same IDs seen in the browser request:

```bash
export FXREPLAY_SESSION_ID='...'
export FXREPLAY_TAB_ID='...'
```

## 3. Download a calendar year

Both 1-second and native 1-minute data:

```bash
python download_fxreplay.py \
  --year 2025 \
  --resolutions 1s 1m \
  --output ./data
```

The downloader saves one Parquet file per UTC day and resumes automatically by skipping existing files.

Expected layout:

```text
data/
  CME_MINI_NQ1/
    2025/
      1s/2025-01-02.parquet
      1m/2025-01-02.parquet
```

To retry and replace existing files:

```bash
python download_fxreplay.py --year 2025 --resolutions 1s 1m --output ./data --overwrite
```

## 4. Create 15s, 1m, 5m and 15m

This uses the downloaded 1-second files as the source, producing consistent OHLCV aggregation:

```bash
python resample_candles.py \
  --input ./data/CME_MINI_NQ1/2025/1s \
  --output ./data/CME_MINI_NQ1/2025/resampled \
  --targets 15s 1m 5m 15m \
  --csv
```

Outputs:

```text
resampled/15s/*.parquet
resampled/1m/*.parquet
resampled/5m/*.parquet
resampled/15m/*.parquet
resampled/15s.csv.gz
resampled/1m.csv.gz
resampled/5m.csv.gz
resampled/15m.csv.gz
```

Every output contains:

```text
date,open,high,low,close,volume
```

## Notes

- Timestamps are UTC.
- Daily files keep memory usage low and allow resume.
- Empty weekends/holidays are saved as no file and reported with `rows=0`.
- If FXReplay returns an unfamiliar response shape, the downloader stops rather than silently writing incorrect candles.
- Start with a small test year/day by temporarily changing the date loop or run the script and inspect the first generated Parquet file before downloading the whole year.

## FXReplay array format

FXReplay currently returns each candle as:

```text
[timestamp_ms, open, high, low, close, volume, metadata]
```

The final metadata value may be `null` and is ignored.

## Downloading a date range

Dates are inclusive.

From a date through the end of a supplied year:

```bash
python download_fxreplay.py \
  --symbol "CME_MINI:MNQ1" \
  --year 2026 \
  --start-date 2026-05-13 \
  --resolutions 1s \
  --output ./data_mnq \
  --overwrite
```

Exact range:

```bash
python download_fxreplay.py \
  --symbol "CME_MINI:MNQ1" \
  --start-date 2026-05-13 \
  --end-date 2026-07-29 \
  --resolutions 1s \
  --output ./data_mnq \
  --overwrite
```

When only `--start-date` is supplied, the end date defaults to today's UTC date.
