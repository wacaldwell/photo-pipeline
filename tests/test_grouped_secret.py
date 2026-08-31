"""Tests for grouped AWS secret selection."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("photo_pipeline_secret_tests", _REPO / "photo-pipeline.py")
assert _spec is not None and _spec.loader is not None
photo_pipeline = importlib.util.module_from_spec(_spec)
sys.modules["photo_pipeline_secret_tests"] = photo_pipeline
_spec.loader.exec_module(photo_pipeline)


class GroupedSecretTests(unittest.TestCase):
    def test_selects_photo_pipeline_object(self):
        payload = {
            "cloudflare": {"api_token": "not-a-real-token"},
            "photo_pipeline": {
                "gemini_api_key": "not-a-real-key",
                "cmbpix_prod": {"url": "https://example.test", "user": "agent", "app_password": "not-a-real-password"},
            },
        }
        selected = photo_pipeline.select_secret_payload(payload, "photo_pipeline")
        self.assertEqual(selected["cmbpix_prod"]["url"], "https://example.test")
        self.assertNotIn("cloudflare", selected)

    def test_supports_dot_separated_object_path(self):
        payload = {"publishing": {"photo_pipeline": {"gemini_api_key": "not-a-real-key"}}}
        selected = photo_pipeline.select_secret_payload(payload, "publishing.photo_pipeline")
        self.assertEqual(set(selected), {"gemini_api_key"})

    def test_missing_key_error_does_not_include_secret_values(self):
        payload = {"photo_pipeline": {"gemini_api_key": "sensitive-test-marker"}}
        with self.assertRaisesRegex(ValueError, "missing required JSON object: missing") as caught:
            photo_pipeline.select_secret_payload(payload, "missing")
        self.assertNotIn("sensitive-test-marker", str(caught.exception))

    def test_rejects_scalar_selection(self):
        with self.assertRaisesRegex(ValueError, "is not an object: cloudflare.api_token"):
            photo_pipeline.select_secret_payload(
                {"cloudflare": {"api_token": "sensitive-test-marker"}},
                "cloudflare.api_token",
            )

    def test_no_selector_preserves_legacy_shape(self):
        payload = {"gemini_api_key": "not-a-real-key"}
        self.assertIs(photo_pipeline.select_secret_payload(payload, None), payload)


if __name__ == "__main__":
    unittest.main()
