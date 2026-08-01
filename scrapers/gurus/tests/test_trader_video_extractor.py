import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRAPERS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRAPERS_DIR))

import trader_video_extractor as extractor


class UtilityTests(unittest.TestCase):
    def test_slugify_and_upload_date_are_stable(self):
        self.assertEqual(extractor.slugify("  Linda B. Raschke  "), "linda-b-raschke")
        self.assertEqual(extractor.normalize_upload_date("20260725"), "2026-07-25")
        self.assertEqual(extractor.normalize_upload_date(None), "unknown")

    def test_timestamp_url_replaces_existing_timestamp(self):
        result = extractor.timestamp_url(
            "https://www.youtube.com/watch?v=abc123&t=5s",
            91.8,
        )
        self.assertEqual(
            result,
            "https://www.youtube.com/watch?v=abc123&t=91s",
        )

    def test_video_entry_uses_id_when_flat_metadata_has_no_url(self):
        video = extractor.video_from_entry(
            {
                "id": "abc123",
                "title": "A setup",
                "upload_date": "20260725",
            }
        )
        self.assertEqual(video["url"], "https://www.youtube.com/watch?v=abc123")
        self.assertEqual(video["upload_date"], "2026-07-25")

    def test_js_runtime_auto_detects_node(self):
        paths = {"deno": None, "node": "/opt/homebrew/bin/node", "qjs": None}
        with patch.object(extractor.shutil, "which", side_effect=paths.get):
            runtime = extractor.resolve_js_runtime("auto")

        self.assertEqual(runtime, "node:/opt/homebrew/bin/node")

    def test_parser_allows_analysis_without_source(self):
        args = extractor.create_parser().parse_args(
            ["--trader", "StoicTA", "--analyze-only"]
        )
        self.assertTrue(args.analyze_only)
        self.assertIsNone(args.source)


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.transcript = {
            "video": {
                "id": "abc123",
                "title": "NQ morning reversal",
                "url": "https://www.youtube.com/watch?v=abc123",
                "upload_date": "2026-07-25",
                "channel": "Example Trader",
            },
            "model": "small",
            "language": "en",
            "text": (
                "NQ swept sell-side liquidity during the 90-minute time cycle. "
                "ES did not make a lower low, creating SMT divergence. "
                "Bullish displacement caused a market structure shift and formed an FVG."
            ),
            "segments": [
                {
                    "id": 0,
                    "start": 60.0,
                    "end": 66.0,
                    "text": "NQ swept sell-side liquidity during the 90-minute time cycle.",
                },
                {
                    "id": 1,
                    "start": 66.0,
                    "end": 72.0,
                    "text": "ES did not make a lower low, creating SMT divergence.",
                },
                {
                    "id": 2,
                    "start": 72.0,
                    "end": 80.0,
                    "text": (
                        "Bullish displacement caused a market structure shift and formed an FVG."
                    ),
                },
            ],
        }

    def test_analysis_retains_timestamped_evidence(self):
        profile = extractor.load_profile_config(None)
        summary = extractor.analyze_transcript(self.transcript, profile)

        self.assertIn("Liquidity", summary["concept_mentions"])
        self.assertIn("SMT Divergence", summary["concept_mentions"])
        self.assertTrue(summary["candidate_strategies"])
        self.assertEqual(summary["key_passages"][0]["start"], 60.0)

    def test_custom_profile_extends_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "profile.json"
            config_path.write_text(
                json.dumps(
                    {
                        "keywords": ["zeussy"],
                        "concept_patterns": {"Zeussy": r"\bzeussy\b"},
                        "strategy_patterns": {},
                    }
                ),
                encoding="utf-8",
            )
            profile = extractor.load_profile_config(config_path)

        self.assertIn("liquidity", profile["keywords"])
        self.assertIn("zeussy", profile["keywords"])
        self.assertIn("Zeussy", profile["concept_patterns"])

    def test_saved_summary_and_index_link_to_video_timestamp(self):
        profile = extractor.load_profile_config(None)
        summary = extractor.analyze_transcript(self.transcript, profile)

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            json_path, markdown_path = extractor.save_summary(summary, output_dir)
            index_path = extractor.generate_master_index(
                "Example Trader",
                output_dir,
                [summary],
            )

            markdown = markdown_path.read_text(encoding="utf-8")
            index = index_path.read_text(encoding="utf-8")

        self.assertTrue(json_path.name.endswith(".json"))
        self.assertIn("t=60s", markdown)
        self.assertIn("Example Trader Video Research Index", index)
        self.assertIn("summaries/abc123.md", index)

    def test_single_video_retry_merges_manifest_and_clears_failure(self):
        first_video = self.transcript["video"]
        second_video = {
            "id": "second",
            "title": "Second video",
            "url": "https://www.youtube.com/watch?v=second",
            "upload_date": "2026-07-24",
            "channel": "Example Trader",
        }

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            extractor.merge_manifest(
                "Example Trader",
                "https://www.youtube.com/@example/videos",
                output_dir,
                [first_video, second_video],
            )
            extractor.merge_manifest(
                "Example Trader",
                first_video["url"],
                output_dir,
                [{**first_video, "title": "Updated title"}],
            )
            extractor.write_json_atomic(
                output_dir / "failures.json",
                [{"video_id": "abc123", "title": "Old", "error": "403"}],
            )
            extractor.update_failure_log(output_dir, ["abc123"], [])

            manifest = json.loads(
                (output_dir / "manifest.json").read_text(encoding="utf-8")
            )
            failure_exists = (output_dir / "failures.json").exists()

        self.assertEqual(len(manifest["videos"]), 2)
        self.assertEqual(manifest["videos"][0]["title"], "Updated title")
        self.assertEqual(len(manifest["sources"]), 2)
        self.assertFalse(failure_exists)

    def test_analysis_only_rebuilds_existing_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            extractor.write_json_atomic(
                output_dir / "transcripts" / "abc123.json",
                self.transcript,
            )
            profile = extractor.load_profile_config(None)
            attempted, failures = extractor.analyze_saved_transcripts(
                output_dir,
                profile,
                fail_fast=False,
            )

            summary_exists = (output_dir / "summaries" / "abc123.json").exists()

        self.assertEqual(attempted, ["abc123"])
        self.assertEqual(failures, [])
        self.assertTrue(summary_exists)


if __name__ == "__main__":
    unittest.main()
