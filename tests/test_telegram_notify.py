"""Tests for telegram_notify() in photo-pipeline.py.

Run with:  python3 -m unittest tests.test_telegram_notify -v
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

# Load photo-pipeline.py as a module despite the hyphen in the filename.
_REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("photo_pipeline", _REPO / "photo-pipeline.py")
photo_pipeline = importlib.util.module_from_spec(_spec)
sys.modules["photo_pipeline"] = photo_pipeline
_spec.loader.exec_module(photo_pipeline)


class TelegramNotifyTests(unittest.TestCase):
    def _fake_200(self):
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: False
        resp.status = 200
        resp.read.return_value = b'{"ok":true}'
        return resp

    def test_posts_to_sendmessage_url_with_token(self):
        with patch("urllib.request.urlopen", return_value=self._fake_200()) as mock_open:
            photo_pipeline.telegram_notify(
                token="ABC:xyz",
                chat_id=-1001234567890,
                thread_id=42,
                title="Bagels",
                edit_url="https://example.com/wp-admin/post.php?post=1&action=edit",
                image_count=12,
            )
        self.assertEqual(mock_open.call_count, 1)
        req = mock_open.call_args[0][0]
        self.assertEqual(req.full_url, "https://api.telegram.org/botABC:xyz/sendMessage")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Content-type"), "application/json")

    def test_body_shape(self):
        with patch("urllib.request.urlopen", return_value=self._fake_200()) as mock_open:
            photo_pipeline.telegram_notify(
                token="T",
                chat_id=-1001234567890,
                thread_id=42,
                title="Bagels",
                edit_url="https://example.com/wp-admin/post.php?post=1&action=edit",
                image_count=12,
            )
        body = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(body["chat_id"], -1001234567890)
        self.assertEqual(body["message_thread_id"], 42)
        self.assertEqual(body["disable_web_page_preview"], False)
        self.assertNotIn("parse_mode", body)  # plain text — no markdown
        self.assertEqual(
            body["text"],
            "📸 New gallery: Bagels (12 photos)\n✏️ https://example.com/wp-admin/post.php?post=1&action=edit",
        )

    def test_swallows_http_error(self):
        err = urllib.error.HTTPError(
            url="https://api.telegram.org/botT/sendMessage",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(b'{"ok":false,"description":"chat not found"}'),
        )
        buf = io.StringIO()
        with patch("urllib.request.urlopen", side_effect=err), redirect_stderr(buf):
            # Must NOT raise — notification is best-effort.
            photo_pipeline.telegram_notify(
                token="T", chat_id=-1, thread_id=1,
                title="x", edit_url="https://e/", image_count=1,
            )
        self.assertIn("Telegram", buf.getvalue())

    def test_swallows_url_error(self):
        buf = io.StringIO()
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("name resolution failure")), \
             redirect_stderr(buf):
            photo_pipeline.telegram_notify(
                token="T", chat_id=-1, thread_id=1,
                title="x", edit_url="https://e/", image_count=1,
            )
        self.assertIn("Telegram", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
