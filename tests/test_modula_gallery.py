"""Tests for Modula gallery payload construction in photo-pipeline.py.

Run with: python3 -m unittest tests.test_modula_gallery -v

Regression guard: Modula's server-side defaults set the hover effect to
"pufrobo", which renders a text overlay (.figc with .jtg-title + .description)
over every image. We must explicitly send effect="none" so Modula uses the
item-none.php template (image only, no caption block). The pipeline used to
omit `effect` entirely, so every gallery shipped with the overlay.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

# Load photo-pipeline.py as a module despite the hyphen in the filename.
_REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("photo_pipeline", _REPO / "photo-pipeline.py")
photo_pipeline = importlib.util.module_from_spec(_spec)
sys.modules["photo_pipeline"] = photo_pipeline
_spec.loader.exec_module(photo_pipeline)


class ModulaOverlayTests(unittest.TestCase):
    def test_default_settings_disable_hover_effect(self):
        # effect="none" is the only value that selects Modula's item-none.php
        # template. An empty string or a missing key falls back to item.php,
        # which renders the title/description overlay.
        self.assertEqual(photo_pipeline.MODULA_DEFAULT_SETTINGS.get("effect"), "none")

    def test_create_gallery_sends_effect_none(self):
        captured = {}

        def fake_request(method, url, auth, body=None, **kwargs):
            captured["body"] = body
            return {"id": 123}

        orig = photo_pipeline.wp_rest_request
        photo_pipeline.wp_rest_request = fake_request
        try:
            photo_pipeline.wp_create_modula_gallery(
                title="Test",
                status="draft",
                modula_images=[{"id": 1}],
                wp_url="https://example.com",
                auth="Basic x",
            )
        finally:
            photo_pipeline.wp_rest_request = orig

        self.assertEqual(captured["body"]["modulaSettings"].get("effect"), "none")

    def test_settings_overrides_can_still_re_enable_effect(self):
        captured = {}

        def fake_request(method, url, auth, body=None, **kwargs):
            captured["body"] = body
            return {"id": 123}

        orig = photo_pipeline.wp_rest_request
        photo_pipeline.wp_rest_request = fake_request
        try:
            photo_pipeline.wp_create_modula_gallery(
                title="Test",
                status="draft",
                modula_images=[{"id": 1}],
                wp_url="https://example.com",
                auth="Basic x",
                settings_overrides={"effect": "pufrobo"},
            )
        finally:
            photo_pipeline.wp_rest_request = orig

        self.assertEqual(captured["body"]["modulaSettings"].get("effect"), "pufrobo")


if __name__ == "__main__":
    unittest.main()
