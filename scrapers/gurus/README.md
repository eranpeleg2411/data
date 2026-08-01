# Trader Video Extractor

This standalone scraper downloads videos from a YouTube channel or playlist,
transcribes them locally with Whisper, and builds a timestamped research index
of trading concepts and candidate strategies.

It is intentionally separate from the Stratradegy web runtime. Generated audio,
transcripts, and summaries are stored under `scrapers/data/` and ignored by Git.

## What was fixed

- Works with any trader instead of hardcoding Frank369.
- Uses YouTube video IDs for stable filenames and reliable resume behavior.
- Loads the selected Whisper model once and honors `--model`.
- Preserves real Whisper segment timestamps in JSON and text transcripts.
- Links every extracted passage back to its exact place in the source video.
- Labels strategy matches as candidates instead of presenting keyword matches as
  verified strategy rules.
- Supports optional trader-specific vocabulary through JSON profile files.
- Checks required programs and reports partial failures in `failures.json`.
- Detects an installed Deno or Node runtime for yt-dlp's YouTube challenge solver.
- Safely merges single-video retries into the existing manifest and master index.
- Rebuilds analysis from saved transcripts without downloading or transcribing again.

## Requirements

- A Python version supported by the current PyTorch release
- `ffmpeg`
- Deno 2.3+ or Node 22+ for current YouTube extraction
- Enough disk space for the selected videos and Whisper model

On macOS:

```bash
brew install ffmpeg
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r scrapers/requirements-trader-video-extractor.txt
```

The requirements install yt-dlp's default dependency group, including its EJS
challenge-solver package. The extractor automatically enables Deno or Node when
one is available.

Whisper's `medium` model is the default and can be slow on CPU. Start with
`--model small --limit 3` when checking a new source.

## Frank369 example

```bash
python scrapers/trader_video_extractor.py \
  --trader "Frank369" \
  --source "https://www.youtube.com/@frankof369/videos" \
  --config scrapers/configs/frank369.json \
  --model small \
  --limit 3
```

## Another trader

Common trading concepts work without a custom profile:

```bash
python scrapers/trader_video_extractor.py \
  --trader "Trader Name" \
  --source "YOUTUBE_CHANNEL_OR_PLAYLIST_URL" \
  --model small
```

The default output is `scrapers/data/<trader-name>/`. Use `--output` to choose
an exact destination.

## Custom trader profile

A profile extends the shared vocabulary. It does not replace it:

```json
{
  "keywords": ["custom phrase"],
  "concept_patterns": {
    "Custom Concept": "\\bcustom phrase\\b"
  },
  "strategy_patterns": {
    "Custom Setup": "\\bcustom phrase\\b.{0,160}\\bentry\\b"
  }
}
```

Patterns are case-insensitive regular expressions. Keep them evidence-based and
avoid encoding rules that the trader has not publicly documented.

## Retry one failed video

Use the failed video's URL as the source. Video-ID filenames let the retry join
the existing trader archive without replacing its manifest or master index:

```bash
python scrapers/trader_video_extractor.py \
  --trader "StoicTA" \
  --source "https://www.youtube.com/watch?v=PgoZxIzY7YM" \
  --model small
```

Upgrade yt-dlp and its EJS package before retrying a JavaScript-runtime error:

```bash
python -m pip install -U "yt-dlp[default]"
```

If the installed package still lacks EJS scripts, yt-dlp can fetch the official
component for that run:

```bash
python scrapers/trader_video_extractor.py \
  --trader "StoicTA" \
  --source "https://www.youtube.com/watch?v=PgoZxIzY7YM" \
  --remote-component ejs:github \
  --model small
```

## Resume and reuse

The extractor automatically reuses existing audio and transcript files unless
`--overwrite` is supplied.

```bash
# Re-transcribe existing ID-named audio without downloading it
python scrapers/trader_video_extractor.py \
  --trader "Trader Name" \
  --source "SOURCE_URL" \
  --skip-download

# Rebuild summaries from existing timestamped transcript JSON
python scrapers/trader_video_extractor.py \
  --trader "Trader Name" \
  --source "SOURCE_URL" \
  --skip-transcribe
```

## Reanalyze after changing keywords

The analysis-only mode reads all existing transcript JSON files and does not
contact YouTube, load Whisper, or touch the audio files:

```bash
python scrapers/trader_video_extractor.py \
  --trader "StoicTA" \
  --analyze-only \
  --keyword "fib geometry" \
  --keyword "2200 sma"
```

For repeatable results, store vocabulary in a JSON profile and pass it to the
same command:

```bash
python scrapers/trader_video_extractor.py \
  --trader "StoicTA" \
  --analyze-only \
  --config scrapers/configs/stoicta.json
```

`keywords` rank relevant passages. Add a named `concept_patterns` entry when a
phrase should also appear in the concept counts, or a `strategy_patterns` entry
when it should produce candidate-strategy evidence.

Use `--cookies-from-browser chrome` only when a video legitimately requires the
YouTube session already present in that browser.

## Output

```text
scrapers/data/trader-name/
├── audio/
├── transcripts/
│   ├── VIDEO_ID.json
│   └── VIDEO_ID.txt
├── summaries/
│   ├── VIDEO_ID.json
│   └── VIDEO_ID.md
├── failures.json
├── manifest.json
└── MASTER_INDEX.md
```

The extractor is a research aid. Transcripts can be inaccurate, regex matches
do not prove a complete trading rule, and none of the output verifies
profitability or constitutes trading advice.

Download and retain media only when you have the right to do so and in
accordance with YouTube's terms and applicable copyright law.
