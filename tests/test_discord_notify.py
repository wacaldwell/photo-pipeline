"""Tests for Discord notification support in photo-pipeline.py.

Run with:  python3 -m unittest tests.test_discord_notify -v
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import unittest
import urllib.error
from contextlib import redirect_stderr
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch

# Load photo-pipeline.py as a module despite the hyphen in the filename.
_REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("photo_pipeline", _REPO / "photo-pipeline.py")
photo_pipeline = importlib.util.module_from_spec(_spec)
sys.modules["photo_pipeline"] = photo_pipeline
_spec.loader.exec_module(photo_pipeline)


class DiscordNotifyTests(unittest.TestCase):
    WEBHOOK = "https://discord.example/webhook/test-token"

    def _fake_204(self):
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: False
        resp.status = 204
        resp.read.return_value = b""
        return resp

    def test_posts_json_to_webhook(self):
        with patch("urllib.request.urlopen", return_value=self._fake_204()) as mock_open:
            photo_pipeline.discord_notify(
                webhook_url=self.WEBHOOK,
                title="Bagels",
                edit_url="https://example.com/wp-admin/post.php?post=1&action=edit",
                image_count=12,
            )

        self.assertEqual(mock_open.call_count, 1)
        req = mock_open.call_args[0][0]
        self.assertEqual(req.full_url, self.WEBHOOK)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(mock_open.call_args.kwargs["timeout"], 10.0)

    def test_body_shape_and_mentions_suppressed(self):
        with patch("urllib.request.urlopen", return_value=self._fake_204()) as mock_open:
            photo_pipeline.discord_notify(
                webhook_url=self.WEBHOOK,
                title="Bagels @everyone",
                edit_url="https://example.com/wp-admin/post.php?post=1&action=edit",
                image_count=12,
            )

        body = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(
            body["content"],
            "📸 New gallery: Bagels @everyone (12 photos)\n"
            "✏️ https://example.com/wp-admin/post.php?post=1&action=edit",
        )
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        self.assertEqual(set(body), {"content", "allowed_mentions"})

    def test_swallows_http_error(self):
        err = urllib.error.HTTPError(
            url=self.WEBHOOK,
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(b'{"message":"invalid webhook"}'),
        )
        buf = io.StringIO()
        with patch("urllib.request.urlopen", side_effect=err), redirect_stderr(buf):
            photo_pipeline.discord_notify(
                webhook_url=self.WEBHOOK,
                title="x",
                edit_url="https://e/",
                image_count=1,
            )
        self.assertIn("Discord", buf.getvalue())
        self.assertIn("HTTP 400", buf.getvalue())

    def test_swallows_error_while_reading_http_error_body(self):
        response_body = MagicMock()
        response_body.read.side_effect = TimeoutError("response body timed out")
        err = urllib.error.HTTPError(
            url=self.WEBHOOK,
            code=429,
            msg="Too Many Requests",
            hdrs=Message(),
            fp=response_body,
        )
        buf = io.StringIO()
        with patch("urllib.request.urlopen", side_effect=err), redirect_stderr(buf):
            photo_pipeline.discord_notify(
                webhook_url=self.WEBHOOK,
                title="x",
                edit_url="https://e/",
                image_count=1,
            )
        self.assertIn("HTTP 429", buf.getvalue())
        self.assertIn("error body unavailable", buf.getvalue())

    def test_swallows_transport_error(self):
        buf = io.StringIO()
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("name resolution failure"),
        ), redirect_stderr(buf):
            photo_pipeline.discord_notify(
                webhook_url=self.WEBHOOK,
                title="x",
                edit_url="https://e/",
                image_count=1,
            )
        self.assertIn("Discord", buf.getvalue())

    def test_swallows_invalid_webhook_url(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            photo_pipeline.discord_notify(
                webhook_url="not a URL",
                title="x",
                edit_url="https://e/",
                image_count=1,
            )
        self.assertIn("Discord", buf.getvalue())


class DiscordCliTests(unittest.TestCase):
    def test_discord_is_canonical_and_tele_is_hidden(self):
        result = subprocess.run(
            [sys.executable, str(_REPO / "photo-pipeline.py"), "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("--discord", result.stdout)
        self.assertNotIn("--tele", result.stdout)

    def test_tele_alias_is_still_accepted(self):
        result = subprocess.run(
            [sys.executable, str(_REPO / "photo-pipeline.py"), "--tele", "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("unrecognized arguments", result.stderr)


if __name__ == "__main__":
    unittest.main()
