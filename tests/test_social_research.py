"""Tests for social research/editorial helpers in photo-pipeline.py.

Run with: python3 -m unittest tests.test_social_research -v
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Load photo-pipeline.py as a module despite the hyphen in the filename.
_REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("photo_pipeline", _REPO / "photo-pipeline.py")
photo_pipeline = importlib.util.module_from_spec(_spec)
sys.modules["photo_pipeline"] = photo_pipeline
_spec.loader.exec_module(photo_pipeline)


class SocialResearchTests(unittest.TestCase):
    def test_resolve_last30days_command_prefers_explicit_python_script(self):
        with patch("photo_pipeline.resolve_last30days_python", return_value="/usr/local/bin/python3.12"):
            cmd = photo_pipeline.resolve_last30days_command(
                "/tmp/last30days.py",
                env={},
            )
        self.assertEqual(cmd[0], "/usr/local/bin/python3.12")
        self.assertEqual(cmd[1], "/tmp/last30days.py")

    def test_resolve_last30days_command_returns_none_without_python312(self):
        with patch("photo_pipeline.resolve_last30days_python", return_value=None):
            cmd = photo_pipeline.resolve_last30days_command(
                "/tmp/last30days.py",
                env={},
            )
        self.assertIsNone(cmd)

    def test_resolve_last30days_python_picks_supported_interpreter(self):
        with patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}" if name == "python3.12" else None):
            with patch("subprocess.run", return_value=SimpleNamespace(stdout="3.12\n")):
                cmd = photo_pipeline.resolve_last30days_python()
        self.assertEqual(cmd, "/usr/bin/python3.12")

    def test_resolve_last30days_command_prefers_path_binary(self):
        with patch("shutil.which", side_effect=lambda name: "/usr/local/bin/last30days" if name == "last30days" else None):
            cmd = photo_pipeline.resolve_last30days_command(None, env={})
        self.assertEqual(cmd, ["/usr/local/bin/last30days"])

    def test_resolve_last30days_command_uses_path_last30_fallback(self):
        with patch("shutil.which", side_effect=lambda name: "/usr/local/bin/last30" if name == "last30" else None):
            cmd = photo_pipeline.resolve_last30days_command(None, env={})
        self.assertEqual(cmd, ["/usr/local/bin/last30"])

    def test_derive_research_topic_uses_top_tags(self):
        topic = photo_pipeline.derive_research_topic(
            "Bagels",
            [
                {"metadata": {"tags": ["bagels", "brunch", "nyc"]}},
                {"metadata": {"tags": ["bagels", "food"]}},
            ],
        )
        self.assertEqual(topic, "Bagels photography trends bagels brunch food nyc")

    def test_run_social_research_builds_expected_command(self):
        fake = SimpleNamespace(returncode=0, stdout="brief", stderr="")
        with patch("subprocess.run", return_value=fake) as mock_run:
            result = photo_pipeline.run_social_research(
                topic="bagels photography trends",
                command=["/usr/local/bin/last30"],
                days=30,
                sources="reddit",
                mode="quick",
                timeout=120,
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            mock_run.call_args[0][0],
            [
                "/usr/local/bin/last30",
                "bagels photography trends",
                "--emit",
                "compact",
                "--search=reddit",
                "--days=30",
                "--quick",
            ],
        )

    def test_run_social_research_handles_missing_command(self):
        result = photo_pipeline.run_social_research(
            topic="bagels",
            command=None,
            days=30,
            sources="reddit",
            mode="quick",
            timeout=120,
        )
        self.assertEqual(result["status"], "unavailable")

    def test_build_post_content_joins_non_empty_parts(self):
        content = photo_pipeline.build_post_content(
            "<p>Intro</p>",
            "<figure>Gallery</figure>",
            "<p>Outro</p>",
        )
        self.assertEqual(content, "<p>Intro</p>\n\n<figure>Gallery</figure>\n\n<p>Outro</p>")


if __name__ == "__main__":
    unittest.main()
