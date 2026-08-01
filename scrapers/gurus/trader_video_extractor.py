#!/usr/bin/env python3
"""Download, transcribe, and index trading videos with timestamped evidence."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


DEFAULT_OUTPUT_ROOT = Path("./scrapers/data")
DEFAULT_WHISPER_MODEL = "medium"
DEFAULT_AUDIO_FORMAT = "mp3"

DEFAULT_KEYWORDS = [
    "levels",
    "poi",
    "indicator",
    "volume",
    "sma",
    "ema",
    "entry",
    "exit",
    "stop loss",
    "take profit",
    "liquidity",
    "sweep",
    "order block",
    "fair value gap",
    "fvg",
    "inverse fair value gap",
    "ifvg",
    "market structure",
    "change of character",
    "choch",
    "market structure shift",
    "mss",
    "smart money",
    "ict",
    "algorithm",
    "time cycle",
    "accumulation",
    "manipulation",
    "distribution",
    "power of three",
    "ipda",
    "killzone",
    "nasdaq",
    "nq",
    "es",
    "ym",
    "futures",
    "risk management",
    "position size",
    "confluence",
    "divergence",
    "smt",
    "premium",
    "discount",
    "mitigation",
    "displacement",
    "breaker",
    "imbalance",
    "reversal",
    "continuation",
    "higher timeframe",
    "lower timeframe",
    "session",
    "swing"
]

DEFAULT_CONCEPT_PATTERNS = {
    "Liquidity": r"\bliquidity\b|\bstop hunt\b|\bliquidity (?:sweep|grab|run)\b",
    "Fair Value Gap": r"\bfair value gap\b|\bfvg\b|\bimbalance\b",
    "Inverse Fair Value Gap": r"\binverse fair value gap\b|\bifvg\b",
    "Market Structure Shift": (
        r"\bmarket structure shift\b|\bmss\b|\bchange of character\b|\bchoch\b"
    ),
    "Order Blocks and Breakers": r"\border blocks?\b|\bbreaker blocks?\b",
    "SMT Divergence": r"\bsmt\b|\bsmart money technique\b|\bdivergence\b",
    "Time Cycles": r"\btime cycles?\b|\b90[-\s]?minute\b|\btime of interest\b",
    "Power of Three": (
        r"\bpower of three\b|\bpo3\b|"
        r"\baccumulation\b.{0,160}\bmanipulation\b.{0,160}\bdistribution\b"
    ),
    "Premium and Discount": r"\bpremium\b|\bdiscount\b|\bequilibrium\b",
    "Risk Management": (
        r"\brisk management\b|\bposition siz(?:e|ing)\b|\brisk[-\s]?reward\b|\br:r\b"
    ),
}

DEFAULT_STRATEGY_PATTERNS = {
    "Liquidity Reversal": (
        r"\bliquidity\b.{0,180}\b(?:reversal|shift|displacement|sweep)\b|"
        r"\b(?:reversal|shift|displacement|sweep)\b.{0,180}\bliquidity\b"
    ),
    "Market Maker Buy Model": r"\bmarket maker buy model\b|\bmm buy model\b",
    "Market Maker Sell Model": r"\bmarket maker sell model\b|\bmm sell model\b",
    "High or Low of Day": (
        r"\bhigh of (?:the )?day\b|\blow of (?:the )?day\b|\bhod\b|\blod\b"
    ),
    "Trend Continuation": (
        r"\btrend continuation\b|\bcontinuation\b.{0,140}\b(?:entry|setup|trade)\b"
    ),
    "IFVG Entry": (
        r"\b(?:inverse fair value gap|ifvg)\b.{0,140}\b(?:entry|retrace|continuation)\b|"
        r"\b(?:entry|retrace|continuation)\b.{0,140}\b(?:inverse fair value gap|ifvg)\b"
    ),
    "SMT-Confirmed Reversal": (
        r"\bsmt\b.{0,180}\b(?:reversal|divergence|shift)\b|"
        r"\b(?:reversal|divergence|shift)\b.{0,180}\bsmt\b"
    ),
    "Power of Three / AMD": (
        r"\bpower of three\b|\bpo3\b|\baccumulation\b.{0,160}\bmanipulation\b"
    ),
}

SPOKEN_TIME_PATTERNS = [
    r"\b\d{1,2}:\d{2}\b",
    r"\b\d{1,2}(?:th|st|nd|rd)\s+minute\b",
    r"\b\d{1,2}\s*(?:am|pm)\b",
    r"\b(?:new york|london|asia)\s+(?:open|close|session)\b",
    r"\b(?:killzone|kill zone)\b",
]


class ExtractorError(RuntimeError):
    """A recoverable pipeline error with a useful user-facing message."""


def slugify(value: str) -> str:
    """Create a stable ASCII directory name."""
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "trader"


def normalize_upload_date(value: Any) -> str:
    """Convert yt-dlp's YYYYMMDD value into ISO format when possible."""
    text = str(value or "").strip()
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text or "unknown"


def format_timestamp(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def timestamp_url(url: str, seconds: float) -> str:
    """Return a YouTube URL with a stable timestamp query parameter."""
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "t"]
    query.append(("t", f"{max(0, int(seconds))}s"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def markdown_cell(value: Any) -> str:
    return str(value).replace("\n", " ").replace("|", r"\|").strip()


def trim_text(text: str, limit: int = 420) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rsplit(" ", 1)[0] + "..."


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def write_json_atomic(path: Path, payload: Any) -> None:
    write_text_atomic(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def require_command(command: str, install_hint: str) -> None:
    if shutil.which(command) is None:
        raise ExtractorError(f"{command} was not found. {install_hint}")


def run_command(command: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise ExtractorError(f"Command timed out after {timeout} seconds: {command[0]}") from error
    except subprocess.CalledProcessError as error:
        details = (error.stderr or error.stdout or "No diagnostic output").strip()
        raise ExtractorError(f"{command[0]} failed: {details[-1200:]}") from error


def yt_dlp_auth_args(cookies_from_browser: str | None) -> list[str]:
    if not cookies_from_browser:
        return []
    return ["--cookies-from-browser", cookies_from_browser]


def resolve_js_runtime(requested_runtime: str) -> str | None:
    """Resolve an available yt-dlp JavaScript runtime."""
    if requested_runtime != "auto":
        return None if requested_runtime == "none" else requested_runtime

    for runtime_name, executable in (
        ("deno", "deno"),
        ("node", "node"),
        ("quickjs", "qjs"),
    ):
        executable_path = shutil.which(executable)
        if executable_path:
            return f"{runtime_name}:{executable_path}"
    return None


def yt_dlp_environment_args(
    cookies_from_browser: str | None,
    js_runtime: str | None,
    remote_components: Sequence[str],
) -> list[str]:
    args = yt_dlp_auth_args(cookies_from_browser)
    if js_runtime:
        args.extend(["--js-runtimes", js_runtime])
    for component in remote_components:
        args.extend(["--remote-components", component])
    return args


def video_from_entry(entry: Mapping[str, Any]) -> dict[str, str] | None:
    video_id = str(entry.get("id") or "").strip()
    if not video_id:
        return None

    raw_url = str(entry.get("webpage_url") or entry.get("original_url") or "").strip()
    if not raw_url.startswith(("http://", "https://")):
        raw_url = f"https://www.youtube.com/watch?v={video_id}"

    title = str(entry.get("title") or video_id).strip()
    return {
        "id": video_id,
        "title": title,
        "url": raw_url,
        "upload_date": normalize_upload_date(entry.get("upload_date")),
        "channel": str(entry.get("channel") or entry.get("uploader") or "").strip(),
    }


def extract_video_urls(
    source_url: str,
    limit: int = 0,
    cookies_from_browser: str | None = None,
    js_runtime: str | None = None,
    remote_components: Sequence[str] = (),
) -> list[dict[str, str]]:
    """Extract video metadata without downloading media."""
    command = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--no-warnings",
        "--quiet",
        *yt_dlp_environment_args(
            cookies_from_browser,
            js_runtime,
            remote_components,
        ),
    ]
    if limit > 0:
        command.extend(["--playlist-end", str(limit)])
    command.append(source_url)

    result = run_command(command, timeout=600)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ExtractorError("yt-dlp returned metadata that was not valid JSON.") from error

    entries = payload.get("entries")
    if not isinstance(entries, list):
        entries = [payload]

    videos: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        video = video_from_entry(entry)
        if video and video["id"] not in seen_ids:
            videos.append(video)
            seen_ids.add(video["id"])
        if limit > 0 and len(videos) >= limit:
            break

    if not videos:
        raise ExtractorError("No public videos were found at the supplied source URL.")
    return videos


def find_existing_audio(audio_dir: Path, video_id: str) -> Path | None:
    candidates = sorted(
        path
        for path in audio_dir.glob(f"{video_id}.*")
        if path.is_file() and not path.name.endswith(".part")
    )
    return candidates[0] if candidates else None


def download_audio(
    video: Mapping[str, str],
    output_dir: Path,
    audio_format: str,
    overwrite: bool,
    cookies_from_browser: str | None = None,
    js_runtime: str | None = None,
    remote_components: Sequence[str] = (),
) -> Path:
    """Download one video's audio using the video ID as its stable filename."""
    audio_dir = output_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    existing = find_existing_audio(audio_dir, video["id"])
    if existing and not overwrite:
        return existing

    command = [
        "yt-dlp",
        "--no-playlist",
        "--no-progress",
        "-x",
        "--audio-format",
        audio_format,
        "--audio-quality",
        "0",
        "--print",
        "after_move:filepath",
        "-o",
        str(audio_dir / f"{video['id']}.%(ext)s"),
        *yt_dlp_environment_args(
            cookies_from_browser,
            js_runtime,
            remote_components,
        ),
    ]
    if overwrite:
        command.append("--force-overwrites")
    command.append(video["url"])

    result = run_command(command, timeout=1800)
    reported_paths = [
        Path(line.strip()).expanduser()
        for line in result.stdout.splitlines()
        if line.strip()
    ]
    for path in reversed(reported_paths):
        if path.exists():
            return path.resolve()

    downloaded = find_existing_audio(audio_dir, video["id"])
    if downloaded:
        return downloaded.resolve()
    raise ExtractorError(f"Audio download completed but no file was found for {video['id']}.")


def load_whisper_model(model_name: str, requested_device: str) -> tuple[Any, str]:
    """Load Whisper once so a channel does not reload the model for every video."""
    try:
        import torch
        import whisper
    except ImportError as error:
        raise ExtractorError(
            "Whisper is not installed. Run: pip install -r "
            "scrapers/requirements-trader-video-extractor.txt"
        ) from error

    device = requested_device
    if requested_device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"

    print(f"Loading Whisper model '{model_name}' on {device}...")
    try:
        model = whisper.load_model(model_name, device=device)
    except Exception as error:
        raise ExtractorError(f"Could not load Whisper model '{model_name}': {error}") from error
    return model, device


def transcript_paths(output_dir: Path, video_id: str) -> tuple[Path, Path]:
    transcript_dir = output_dir / "transcripts"
    return transcript_dir / f"{video_id}.json", transcript_dir / f"{video_id}.txt"


def load_existing_transcript(output_dir: Path, video_id: str) -> dict[str, Any]:
    json_path, _ = transcript_paths(output_dir, video_id)
    if not json_path.exists():
        raise ExtractorError(
            f"No timestamped transcript exists for {video_id}. "
            "Run without --skip-transcribe first."
        )
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ExtractorError(f"Transcript file is invalid: {json_path}") from error
    if not isinstance(payload.get("segments"), list):
        raise ExtractorError(f"Transcript has no timestamped segments: {json_path}")
    return payload


def transcribe_audio(
    model: Any,
    device: str,
    model_name: str,
    audio_path: Path,
    output_dir: Path,
    video: Mapping[str, str],
    language: str,
    overwrite: bool,
) -> dict[str, Any]:
    json_path, text_path = transcript_paths(output_dir, video["id"])
    if json_path.exists() and not overwrite:
        return load_existing_transcript(output_dir, video["id"])

    options: dict[str, Any] = {
        "verbose": False,
        "fp16": device.startswith("cuda"),
    }
    if language != "auto":
        options["language"] = language

    try:
        result = model.transcribe(str(audio_path), **options)
    except Exception as error:
        raise ExtractorError(f"Whisper failed for {video['title']}: {error}") from error

    segments = [
        {
            "id": int(segment.get("id", index)),
            "start": round(float(segment.get("start", 0.0)), 3),
            "end": round(float(segment.get("end", 0.0)), 3),
            "text": str(segment.get("text", "")).strip(),
        }
        for index, segment in enumerate(result.get("segments") or [])
        if str(segment.get("text", "")).strip()
    ]
    transcript = {
        "schema_version": 1,
        "video": dict(video),
        "model": model_name,
        "device": device,
        "language": str(result.get("language") or language),
        "text": str(result.get("text") or "").strip(),
        "segments": segments,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    timestamped_text = "\n".join(
        f"[{format_timestamp(segment['start'])}] {segment['text']}" for segment in segments
    )
    write_json_atomic(json_path, transcript)
    write_text_atomic(text_path, timestamped_text + ("\n" if timestamped_text else ""))
    return transcript


def validate_patterns(patterns: Mapping[str, str], source_name: str) -> None:
    for label, pattern in patterns.items():
        try:
            re.compile(pattern, re.IGNORECASE | re.DOTALL)
        except re.error as error:
            raise ExtractorError(
                f"Invalid regular expression for '{label}' in {source_name}: {error}"
            ) from error


def load_profile_config(config_path: Path | None) -> dict[str, Any]:
    keywords = list(DEFAULT_KEYWORDS)
    concept_patterns = dict(DEFAULT_CONCEPT_PATTERNS)
    strategy_patterns = dict(DEFAULT_STRATEGY_PATTERNS)

    if config_path:
        try:
            profile = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ExtractorError(f"Could not read profile config: {config_path}") from error

        if not isinstance(profile, dict):
            raise ExtractorError("Profile config must contain a JSON object.")
        extra_keywords = profile.get("keywords", [])
        extra_concepts = profile.get("concept_patterns", {})
        extra_strategies = profile.get("strategy_patterns", {})
        if not isinstance(extra_keywords, list):
            raise ExtractorError("Profile config 'keywords' must be a list.")
        if not isinstance(extra_concepts, dict) or not isinstance(extra_strategies, dict):
            raise ExtractorError(
                "Profile config concept_patterns and strategy_patterns must be objects."
            )
        keywords.extend(str(keyword) for keyword in extra_keywords)
        concept_patterns.update(
            {str(label): str(pattern) for label, pattern in extra_concepts.items()}
        )
        strategy_patterns.update(
            {str(label): str(pattern) for label, pattern in extra_strategies.items()}
        )

    keywords = list(dict.fromkeys(keyword.strip().lower() for keyword in keywords if keyword))
    validate_patterns(concept_patterns, str(config_path or "built-in concepts"))
    validate_patterns(strategy_patterns, str(config_path or "built-in strategies"))
    return {
        "keywords": keywords,
        "concept_patterns": concept_patterns,
        "strategy_patterns": strategy_patterns,
    }


def count_pattern_mentions(text: str, patterns: Mapping[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label, pattern in patterns.items():
        matches = re.findall(pattern, text, re.IGNORECASE | re.DOTALL)
        if matches:
            counts[label] = len(matches)
    return counts


def build_segment_windows(
    segments: Sequence[Mapping[str, Any]],
    concept_patterns: Mapping[str, str],
    strategy_patterns: Mapping[str, str],
    keywords: Sequence[str],
    window_size: int = 3,
) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for index in range(len(segments)):
        selected = segments[index : index + window_size]
        text = re.sub(
            r"\s+",
            " ",
            " ".join(str(segment.get("text", "")) for segment in selected),
        ).strip()
        if not text:
            continue

        concepts = [
            label
            for label, pattern in concept_patterns.items()
            if re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        ]
        strategies = [
            label
            for label, pattern in strategy_patterns.items()
            if re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        ]
        lower_text = text.lower()
        keyword_hits = sum(lower_text.count(keyword) for keyword in keywords)
        score = len(concepts) * 3 + len(strategies) * 5 + min(keyword_hits, 10)
        if score < 4:
            continue

        windows.append(
            {
                "start": float(selected[0].get("start", 0.0)),
                "end": float(selected[-1].get("end", 0.0)),
                "text": trim_text(text),
                "concepts": concepts,
                "strategies": strategies,
                "score": score,
            }
        )
    return windows


def select_non_overlapping(
    windows: Iterable[Mapping[str, Any]],
    limit: int,
    minimum_separation: float = 15.0,
) -> list[dict[str, Any]]:
    ranked = sorted(windows, key=lambda item: (-int(item["score"]), float(item["start"])))
    selected: list[dict[str, Any]] = []
    for window in ranked:
        start = float(window["start"])
        if any(abs(start - float(existing["start"])) < minimum_separation for existing in selected):
            continue
        selected.append(dict(window))
        if len(selected) >= limit:
            break
    return sorted(selected, key=lambda item: float(item["start"]))


def spoken_time_references(text: str) -> list[str]:
    references: set[str] = set()
    for pattern in SPOKEN_TIME_PATTERNS:
        references.update(
            match.group(0) for match in re.finditer(pattern, text, re.IGNORECASE)
        )
    return sorted(references, key=str.lower)[:30]


def analyze_transcript(
    transcript: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> dict[str, Any]:
    video = dict(transcript["video"])
    segments = transcript.get("segments") or []
    full_text = str(transcript.get("text") or " ".join(s.get("text", "") for s in segments))
    concepts = count_pattern_mentions(full_text, profile["concept_patterns"])
    windows = build_segment_windows(
        segments,
        profile["concept_patterns"],
        profile["strategy_patterns"],
        profile["keywords"],
    )
    key_passages = select_non_overlapping(windows, limit=12)

    candidate_strategies = []
    for strategy_name in profile["strategy_patterns"]:
        evidence = select_non_overlapping(
            (window for window in windows if strategy_name in window["strategies"]),
            limit=3,
        )
        if evidence:
            candidate_strategies.append(
                {
                    "name": strategy_name,
                    "evidence": evidence,
                }
            )

    duration = max(
        (float(segment.get("end", 0.0)) for segment in segments),
        default=0.0,
    )
    return {
        "schema_version": 1,
        "video": video,
        "transcript": {
            "model": transcript.get("model"),
            "language": transcript.get("language"),
            "file": f"../transcripts/{video['id']}.json",
        },
        "stats": {
            "word_count": len(full_text.split()),
            "duration_seconds": round(duration, 3),
            "segments": len(segments),
            "concepts_detected": len(concepts),
            "candidate_strategies": len(candidate_strategies),
        },
        "concept_mentions": concepts,
        "spoken_time_references": spoken_time_references(full_text),
        "candidate_strategies": candidate_strategies,
        "key_passages": key_passages,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def passage_markdown(passage: Mapping[str, Any], video_url: str) -> str:
    stamp = format_timestamp(float(passage["start"]))
    link = timestamp_url(video_url, float(passage["start"]))
    tags = list(passage.get("concepts", [])) + list(passage.get("strategies", []))
    tag_line = f"  \n_Tags: {', '.join(tags)}_" if tags else ""
    return f"- **[{stamp}]({link})** {passage['text']}{tag_line}"


def save_summary(summary: Mapping[str, Any], output_dir: Path) -> tuple[Path, Path]:
    summary_dir = output_dir / "summaries"
    video = summary["video"]
    json_path = summary_dir / f"{video['id']}.json"
    markdown_path = summary_dir / f"{video['id']}.md"
    write_json_atomic(json_path, summary)

    lines = [
        f"# {markdown_cell(video['title'])}",
        "",
        f"**Video:** {video['url']}  ",
        f"**Upload date:** {video['upload_date']}  ",
        f"**Transcript:** [`{video['id']}.json`](../transcripts/{video['id']}.json)",
        "",
        "> Candidate strategies and concepts are generated from transcript evidence. "
        "Verify every rule against the linked video segment before using it.",
        "",
        "## Stats",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Duration | {format_timestamp(summary['stats']['duration_seconds'])} |",
        f"| Words | {summary['stats']['word_count']:,} |",
        f"| Timestamped segments | {summary['stats']['segments']:,} |",
        f"| Concepts detected | {summary['stats']['concepts_detected']} |",
        f"| Candidate strategies | {summary['stats']['candidate_strategies']} |",
        "",
        "## Concept mentions",
        "",
    ]

    if summary["concept_mentions"]:
        lines.extend(
            f"- **{name}:** {count}"
            for name, count in sorted(
                summary["concept_mentions"].items(),
                key=lambda item: (-item[1], item[0]),
            )
        )
    else:
        lines.append("- No configured concepts were detected.")

    lines.extend(["", "## Candidate strategies", ""])
    if summary["candidate_strategies"]:
        for strategy in summary["candidate_strategies"]:
            lines.extend([f"### {strategy['name']}", ""])
            lines.extend(
                passage_markdown(passage, video["url"])
                for passage in strategy["evidence"]
            )
            lines.append("")
    else:
        lines.extend(
            [
                "No configured strategy pattern had enough transcript evidence.",
                "",
            ]
        )

    lines.extend(["## Key transcript passages", ""])
    if summary["key_passages"]:
        lines.extend(
            passage_markdown(passage, video["url"])
            for passage in summary["key_passages"]
        )
    else:
        lines.append("- No high-confidence trading passages were detected.")

    if summary["spoken_time_references"]:
        lines.extend(["", "## Times mentioned in speech", ""])
        lines.extend(f"- {reference}" for reference in summary["spoken_time_references"])

    lines.extend(
        [
            "",
            "---",
            "",
            "Educational research only. This analysis does not verify profitability or "
            "constitute trading advice.",
            "",
        ]
    )
    write_text_atomic(markdown_path, "\n".join(lines))
    return json_path, markdown_path


def merge_manifest(
    trader_name: str,
    source_url: str,
    output_dir: Path,
    videos: Sequence[Mapping[str, str]],
) -> Path:
    """Merge a channel scan or single-video retry into the existing manifest."""
    manifest_path = output_dir / "manifest.json"
    existing: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}

    sources = existing.get("sources")
    if not isinstance(sources, list):
        previous_source = existing.get("source")
        sources = [previous_source] if previous_source else []
    if source_url not in sources:
        sources.append(source_url)

    existing_videos = existing.get("videos")
    if not isinstance(existing_videos, list):
        existing_videos = []
    video_by_id = {
        str(video.get("id")): dict(video)
        for video in existing_videos
        if isinstance(video, Mapping) and video.get("id")
    }
    ordered_ids = list(video_by_id)
    for video in videos:
        video_id = str(video["id"])
        if video_id not in video_by_id:
            ordered_ids.append(video_id)
        video_by_id[video_id] = dict(video)

    write_json_atomic(
        manifest_path,
        {
            "trader": trader_name,
            "sources": sources,
            "videos": [video_by_id[video_id] for video_id in ordered_ids],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return manifest_path


def load_saved_summaries(output_dir: Path) -> list[dict[str, Any]]:
    """Load every valid summary, preserving manifest order when available."""
    summaries_by_id: dict[str, dict[str, Any]] = {}
    for summary_path in sorted((output_dir / "summaries").glob("*.json")):
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        video = summary.get("video") if isinstance(summary, dict) else None
        if isinstance(video, dict) and video.get("id"):
            summaries_by_id[str(video["id"])] = summary

    ordered_ids: list[str] = []
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        manifest_videos = manifest.get("videos") if isinstance(manifest, dict) else []
        if isinstance(manifest_videos, list):
            ordered_ids.extend(
                str(video["id"])
                for video in manifest_videos
                if isinstance(video, dict) and str(video.get("id")) in summaries_by_id
            )

    ordered_ids.extend(video_id for video_id in summaries_by_id if video_id not in ordered_ids)
    return [summaries_by_id[video_id] for video_id in ordered_ids]


def update_failure_log(
    output_dir: Path,
    attempted_video_ids: Iterable[str],
    failures: Sequence[Mapping[str, str]],
) -> None:
    """Replace failure entries for attempted videos and clear successful retries."""
    failure_path = output_dir / "failures.json"
    existing_failures: list[dict[str, str]] = []
    if failure_path.exists():
        try:
            loaded = json.loads(failure_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing_failures = [
                    dict(failure)
                    for failure in loaded
                    if isinstance(failure, Mapping) and failure.get("video_id")
                ]
        except (OSError, json.JSONDecodeError):
            existing_failures = []

    attempted = set(attempted_video_ids)
    merged = [
        failure
        for failure in existing_failures
        if str(failure["video_id"]) not in attempted
    ]
    merged.extend(dict(failure) for failure in failures)
    if merged:
        write_json_atomic(failure_path, merged)
    else:
        failure_path.unlink(missing_ok=True)


def analyze_saved_transcripts(
    output_dir: Path,
    profile: Mapping[str, Any],
    fail_fast: bool,
) -> tuple[list[str], list[dict[str, str]]]:
    """Rebuild summaries using existing transcript JSON without network or Whisper."""
    transcript_paths_found = sorted((output_dir / "transcripts").glob("*.json"))
    if not transcript_paths_found:
        raise ExtractorError(f"No transcript JSON files were found in {output_dir}.")

    attempted_ids: list[str] = []
    failures: list[dict[str, str]] = []
    for index, transcript_path in enumerate(transcript_paths_found, 1):
        video_id = transcript_path.stem
        attempted_ids.append(video_id)
        try:
            transcript = load_existing_transcript(output_dir, video_id)
            summary = analyze_transcript(transcript, profile)
            _, markdown_path = save_summary(summary, output_dir)
            print(f"[{index}/{len(transcript_paths_found)}] Rebuilt {markdown_path.name}")
        except (ExtractorError, KeyError, TypeError, ValueError) as error:
            failures.append(
                {
                    "video_id": video_id,
                    "title": video_id,
                    "error": str(error),
                }
            )
            print(f"Skipped {video_id}: {error}", file=sys.stderr)
            if fail_fast:
                break
    return attempted_ids, failures


def generate_master_index(
    trader_name: str,
    output_dir: Path,
    summaries: Sequence[Mapping[str, Any]],
) -> Path:
    index_path = output_dir / "MASTER_INDEX.md"
    concept_totals: Counter[str] = Counter()
    for summary in summaries:
        concept_totals.update(summary["concept_mentions"])

    lines = [
        f"# {trader_name} Video Research Index",
        "",
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}  ",
        f"**Videos processed:** {len(summaries)}",
        "",
        "> This is a transcript evidence index. Candidate concepts and strategies must "
        "be checked against the source videos.",
        "",
        "## Videos",
        "",
        "| # | Video | Date | Duration | Top concepts |",
        "|---:|---|---|---:|---|",
    ]

    for index, summary in enumerate(summaries, 1):
        video = summary["video"]
        top_concepts = ", ".join(
            name
            for name, _ in sorted(
                summary["concept_mentions"].items(),
                key=lambda item: (-item[1], item[0]),
            )[:3]
        )
        title = markdown_cell(video["title"])
        lines.append(
            f"| {index} | [{title}](summaries/{video['id']}.md) "
            f"([video]({video['url']})) | {video['upload_date']} | "
            f"{format_timestamp(summary['stats']['duration_seconds'])} | "
            f"{markdown_cell(top_concepts or 'None')} |"
        )

    lines.extend(["", "## Concept frequency", ""])
    if concept_totals:
        lines.extend(
            f"- **{name}:** {count}"
            for name, count in concept_totals.most_common()
        )
    else:
        lines.append("- No configured concepts were detected.")

    lines.extend(
        [
            "",
            "## Folder structure",
            "",
            "```text",
            f"{output_dir.name}/",
            "├── audio/          # Video-ID-based audio files",
            "├── transcripts/    # Timestamped JSON and text",
            "├── summaries/      # Evidence indexes in JSON and Markdown",
            "├── manifest.json   # Source video metadata",
            "└── MASTER_INDEX.md",
            "```",
            "",
        ]
    )
    write_text_atomic(index_path, "\n".join(lines))
    return index_path


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download, transcribe, and index videos from any trading educator."
    )
    parser.add_argument("--trader", required=True, help="Trader or channel display name")
    parser.add_argument(
        "--source",
        "--playlist",
        "-p",
        dest="source",
        help="YouTube channel, playlist, or video URL",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Exact output directory (default: scrapers/data/<trader>)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Optional JSON file with trader-specific keywords and regex patterns",
    )
    parser.add_argument(
        "--keyword",
        action="append",
        default=[],
        help="Extra passage-ranking keyword; repeat this option to add more",
    )
    parser.add_argument(
        "--analyze-only",
        action="store_true",
        help="Rebuild summaries from existing transcript JSON without YouTube or Whisper",
    )
    parser.add_argument("--limit", "-l", type=int, default=0, help="Maximum videos (0 = all)")
    parser.add_argument(
        "--model",
        default=DEFAULT_WHISPER_MODEL,
        help=f"Whisper model name (default: {DEFAULT_WHISPER_MODEL})",
    )
    parser.add_argument(
        "--device",
        default="auto",
        help="Whisper device: auto, cpu, cuda, or another PyTorch device",
    )
    parser.add_argument(
        "--language",
        default="en",
        help="Spoken language code, or 'auto' for detection (default: en)",
    )
    parser.add_argument(
        "--audio-format",
        choices=["mp3", "m4a", "opus", "wav"],
        default=DEFAULT_AUDIO_FORMAT,
    )
    parser.add_argument(
        "--cookies-from-browser",
        help="Optional yt-dlp browser name for videos that require your YouTube session",
    )
    parser.add_argument(
        "--js-runtime",
        default="auto",
        help=(
            "yt-dlp JavaScript runtime, such as auto, node, deno, "
            "or node:/path/to/node (default: auto)"
        ),
    )
    parser.add_argument(
        "--remote-component",
        action="append",
        choices=["ejs:github", "ejs:npm"],
        default=[],
        help="Allow yt-dlp to fetch an EJS component; repeat to enable more than one",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Use existing audio files named with their YouTube video IDs",
    )
    parser.add_argument(
        "--skip-transcribe",
        action="store_true",
        help="Use existing timestamped transcript JSON files",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing audio and transcripts",
    )
    parser.add_argument(
        "--delete-audio-after-transcribe",
        action="store_true",
        help="Delete an audio file after its transcript has been saved",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop on the first video that fails",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    output_dir = (
        Path(args.output).expanduser()
        if args.output
        else DEFAULT_OUTPUT_ROOT / slugify(args.trader)
    ).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    profile = load_profile_config(args.config)
    if args.keyword:
        profile["keywords"] = list(
            dict.fromkeys(
                [
                    *profile["keywords"],
                    *(keyword.strip().lower() for keyword in args.keyword if keyword.strip()),
                ]
            )
        )

    if args.analyze_only:
        attempted_ids, failures = analyze_saved_transcripts(
            output_dir,
            profile,
            args.fail_fast,
        )
        update_failure_log(output_dir, attempted_ids, failures)
        all_summaries = load_saved_summaries(output_dir)
        if not all_summaries:
            print("No summaries were generated.", file=sys.stderr)
            return 1
        index_path = generate_master_index(args.trader, output_dir, all_summaries)
        print(f"\nAnalyzed {len(attempted_ids) - len(failures)} transcript(s). Index: {index_path}")
        if failures:
            print(f"{len(failures)} transcript(s) failed. See failures.json.", file=sys.stderr)
            return 2
        return 0

    require_command("yt-dlp", "Install it from the scraper requirements file.")
    if not args.skip_download:
        require_command("ffmpeg", "On macOS, run: brew install ffmpeg")

    js_runtime = resolve_js_runtime(args.js_runtime)
    if not js_runtime:
        raise ExtractorError(
            "No supported JavaScript runtime was found. Install Deno or Node 22+, "
            "or pass --js-runtime RUNTIME[:PATH]."
        )

    print(f"Finding videos for {args.trader}...")
    videos = extract_video_urls(
        args.source,
        limit=max(0, args.limit),
        cookies_from_browser=args.cookies_from_browser,
        js_runtime=js_runtime,
        remote_components=args.remote_component,
    )
    merge_manifest(args.trader, args.source, output_dir, videos)
    print(f"Found {len(videos)} video(s). Output: {output_dir}")

    model = None
    device = args.device
    needs_transcription = not args.skip_transcribe and (
        args.overwrite
        or any(not transcript_paths(output_dir, video["id"])[0].exists() for video in videos)
    )
    if needs_transcription:
        model, device = load_whisper_model(args.model, args.device)

    summaries: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    attempted_ids: list[str] = []
    for index, video in enumerate(videos, 1):
        attempted_ids.append(video["id"])
        print(f"\n[{index}/{len(videos)}] {video['title']}")
        audio_path: Path | None = None
        try:
            if args.skip_transcribe:
                transcript = load_existing_transcript(output_dir, video["id"])
            elif transcript_paths(output_dir, video["id"])[0].exists() and not args.overwrite:
                transcript = load_existing_transcript(output_dir, video["id"])
            else:
                if args.skip_download:
                    audio_path = find_existing_audio(output_dir / "audio", video["id"])
                    if not audio_path:
                        raise ExtractorError(
                            f"No existing audio file was found for {video['id']}."
                        )
                else:
                    audio_path = download_audio(
                        video,
                        output_dir,
                        args.audio_format,
                        args.overwrite,
                        args.cookies_from_browser,
                        js_runtime,
                        args.remote_component,
                    )
                if audio_path is None:
                    raise ExtractorError(f"No audio file is available for {video['id']}.")
                transcript = transcribe_audio(
                    model,
                    device,
                    args.model,
                    audio_path,
                    output_dir,
                    video,
                    args.language,
                    args.overwrite,
                )
                if args.delete_audio_after_transcribe and audio_path.exists():
                    audio_path.unlink()

            summary = analyze_transcript(transcript, profile)
            _, markdown_path = save_summary(summary, output_dir)
            summaries.append(summary)
            print(f"Saved {markdown_path.name}")
        except ExtractorError as error:
            failures.append(
                {
                    "video_id": video["id"],
                    "title": video["title"],
                    "error": str(error),
                }
            )
            print(f"Skipped: {error}", file=sys.stderr)
            if args.fail_fast:
                break

    update_failure_log(output_dir, attempted_ids, failures)

    if not summaries:
        print("No videos were processed successfully.", file=sys.stderr)
        return 1

    all_summaries = load_saved_summaries(output_dir)
    index_path = generate_master_index(args.trader, output_dir, all_summaries)
    print(f"\nProcessed {len(summaries)} video(s). Index: {index_path}")
    if failures:
        print(f"{len(failures)} video(s) failed. See failures.json.", file=sys.stderr)
        return 2
    return 0


def main() -> int:
    parser = create_parser()
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit cannot be negative")
    if not args.analyze_only and not args.source:
        parser.error("--source is required unless --analyze-only is used")
    try:
        return run(args)
    except ExtractorError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
