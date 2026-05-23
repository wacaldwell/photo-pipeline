"""Tests for the AI rating + cull feature in photo-pipeline.py.

Run with: python3 -m unittest tests.test_photo_rating -v
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

# Load photo-pipeline.py as a module despite the hyphen in the filename.
_REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("photo_pipeline", _REPO / "photo-pipeline.py")
photo_pipeline = importlib.util.module_from_spec(_spec)
sys.modules["photo_pipeline"] = photo_pipeline
_spec.loader.exec_module(photo_pipeline)


def _fake_gemini_result(payload_obj: dict, fenced: bool = True) -> dict:
    """Build a Gemini generateContent response wrapping payload_obj as JSON text."""
    text = json.dumps(payload_obj)
    if fenced:
        text = "```json\n" + text + "\n```"
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


class SharedGeminiPlumbingTests(unittest.TestCase):
    def test_analyze_image_still_parses_after_refactor(self):
        meta = {
            "seo_filename": "a-photo", "alt_text": "alt", "caption": "cap",
            "tags": ["x", "y"], "description": "desc",
        }
        orig_req = photo_pipeline._gemini_vision_request
        orig_part = photo_pipeline._image_inline_part
        photo_pipeline._gemini_vision_request = lambda payload, api_key, **kw: _fake_gemini_result(meta)
        photo_pipeline._image_inline_part = lambda image_path: {"inline_data": {}}
        try:
            result = photo_pipeline.gemini_analyze_image(Path("/nope.jpg"), "key")
        finally:
            photo_pipeline._gemini_vision_request = orig_req
            photo_pipeline._image_inline_part = orig_part
        self.assertEqual(result["seo_filename"], "a-photo")
        self.assertEqual(result["tags"], ["x", "y"])


class RatingPromptTests(unittest.TestCase):
    def test_rating_keys_are_the_twelve_ppa_elements(self):
        self.assertEqual(len(photo_pipeline.RATING_KEYS), 12)
        for k in ["impact", "technical_excellence", "creativity", "composition",
                  "presentation", "color_harmony", "interest_areas", "light",
                  "technique", "the_story", "style", "subject_matter"]:
            self.assertIn(k, photo_pipeline.RATING_KEYS)

    def test_prompt_mentions_elements_and_json_and_examples(self):
        prompt = photo_pipeline.build_rating_prompt(photo_pipeline.RATING_FEW_SHOT)
        self.assertIn("Impact", prompt)
        self.assertIn("subject_matter", prompt)   # schema key present
        self.assertIn("JSON", prompt)
        self.assertIn("Golden Gate", prompt)       # a few-shot anchor leaked into prompt


class GeminiRateImageTests(unittest.TestCase):
    def _scores(self):
        return {k: 7 for k in photo_pipeline.RATING_KEYS}

    def test_rate_image_parses_fenced_json_response(self):
        body = {"scores": self._scores(),
                "narrative": {"impression": "i", "strongest": "s", "refine": "r",
                              "improvement": "imp", "titles": ["t1", "t2"]}}
        orig_req = photo_pipeline._gemini_vision_request
        orig_part = photo_pipeline._image_inline_part
        photo_pipeline._gemini_vision_request = lambda payload, api_key, **kw: _fake_gemini_result(body)
        photo_pipeline._image_inline_part = lambda image_path: {"inline_data": {}}
        try:
            out = photo_pipeline.gemini_rate_image(Path("/nope.jpg"), "key")
        finally:
            photo_pipeline._gemini_vision_request = orig_req
            photo_pipeline._image_inline_part = orig_part
        self.assertEqual(set(out["scores"].keys()), set(photo_pipeline.RATING_KEYS))
        self.assertEqual(out["scores"]["impact"], 7)
        self.assertEqual(out["narrative"]["titles"], ["t1", "t2"])

    def test_rate_image_coerces_scores_to_int(self):
        scores = {k: "8" for k in photo_pipeline.RATING_KEYS}  # strings from model
        body = {"scores": scores, "narrative": {}}
        orig_req = photo_pipeline._gemini_vision_request
        orig_part = photo_pipeline._image_inline_part
        photo_pipeline._gemini_vision_request = lambda payload, api_key, **kw: _fake_gemini_result(body, fenced=False)
        photo_pipeline._image_inline_part = lambda image_path: {"inline_data": {}}
        try:
            out = photo_pipeline.gemini_rate_image(Path("/nope.jpg"), "key")
        finally:
            photo_pipeline._gemini_vision_request = orig_req
            photo_pipeline._image_inline_part = orig_part
        self.assertEqual(out["scores"]["impact"], 8)
        self.assertIsInstance(out["scores"]["impact"], int)


if __name__ == "__main__":
    unittest.main()
