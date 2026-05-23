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

    def test_prompt_is_strict_with_bands_and_no_few_shot(self):
        prompt = photo_pipeline.build_rating_prompt()  # no args now
        self.assertIn("Impact", prompt)
        self.assertIn("subject_matter", prompt)        # schema key present
        self.assertIn("JSON", prompt)
        self.assertIn("strict", prompt.lower())        # strictness directive
        self.assertIn("portfolio", prompt.lower())     # band semantics present
        self.assertIn("cull", prompt.lower())          # band semantics present
        self.assertIn("2 to 10", prompt)               # 2-10 scale
        self.assertNotIn("Golden Gate", prompt)        # few-shot anchors removed

    def test_few_shot_constant_removed(self):
        self.assertFalse(hasattr(photo_pipeline, "RATING_FEW_SHOT"))


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

    def test_rate_image_raises_on_missing_key(self):
        incomplete = {k: 7 for k in photo_pipeline.RATING_KEYS[:-1]}  # drop one element
        body = {"scores": incomplete, "narrative": {}}
        orig_req = photo_pipeline._gemini_vision_request
        orig_part = photo_pipeline._image_inline_part
        photo_pipeline._gemini_vision_request = lambda payload, api_key, **kw: _fake_gemini_result(body)
        photo_pipeline._image_inline_part = lambda image_path: {"inline_data": {}}
        try:
            with self.assertRaises(KeyError):
                photo_pipeline.gemini_rate_image(Path("/nope.jpg"), "key")
        finally:
            photo_pipeline._gemini_vision_request = orig_req
            photo_pipeline._image_inline_part = orig_part

    def test_rate_image_raises_on_non_numeric_score(self):
        bad = {k: "great" for k in photo_pipeline.RATING_KEYS}
        body = {"scores": bad, "narrative": {}}
        orig_req = photo_pipeline._gemini_vision_request
        orig_part = photo_pipeline._image_inline_part
        photo_pipeline._gemini_vision_request = lambda payload, api_key, **kw: _fake_gemini_result(body)
        photo_pipeline._image_inline_part = lambda image_path: {"inline_data": {}}
        try:
            with self.assertRaises(ValueError):
                photo_pipeline.gemini_rate_image(Path("/nope.jpg"), "key")
        finally:
            photo_pipeline._gemini_vision_request = orig_req
            photo_pipeline._image_inline_part = orig_part

    def test_rate_image_clamps_scores_to_2_10(self):
        scores = {k: 7 for k in photo_pipeline.RATING_KEYS}
        scores["impact"] = 11   # above range
        scores["light"] = 1     # below range
        body = {"scores": scores, "narrative": {}}
        orig_req = photo_pipeline._gemini_vision_request
        orig_part = photo_pipeline._image_inline_part
        photo_pipeline._gemini_vision_request = lambda payload, api_key, **kw: _fake_gemini_result(body)
        photo_pipeline._image_inline_part = lambda image_path: {"inline_data": {}}
        try:
            out = photo_pipeline.gemini_rate_image(Path("/nope.jpg"), "key")
        finally:
            photo_pipeline._gemini_vision_request = orig_req
            photo_pipeline._image_inline_part = orig_part
        self.assertEqual(out["scores"]["impact"], 10)  # clamped down
        self.assertEqual(out["scores"]["light"], 2)    # clamped up


def _scores_summing_to(per):  # helper: all 12 elements = per
    return {k: per for k in photo_pipeline.RATING_KEYS}


class ScoringTests(unittest.TestCase):
    def test_compute_overall_is_rounded_mean(self):
        # wakeboard example: sum 88 over 12 = 7.333 -> 7.3
        wake = {"impact": 8, "technical_excellence": 7, "creativity": 7, "composition": 7,
                "presentation": 7, "color_harmony": 8, "interest_areas": 7, "light": 6,
                "technique": 8, "the_story": 8, "style": 7, "subject_matter": 8}
        self.assertEqual(photo_pipeline.compute_overall(wake), 7.3)

    def test_compute_overall_uniform(self):
        self.assertEqual(photo_pipeline.compute_overall(_scores_summing_to(8)), 8.0)


class RatingEntryTests(unittest.TestCase):
    # bands used in these tests: cull_below=6.0, keep_above=8.0
    def test_keep_at_or_above_keep_floor(self):
        rating = {"scores": _scores_summing_to(8), "narrative": {}}
        entry = photo_pipeline.make_rating_entry(Path("/a/img.jpg"), rating, 6.0, 8.0)
        self.assertEqual(entry["decision"], "keep")
        self.assertEqual(entry["overall"], 8.0)
        self.assertEqual(entry["original"], "/a/img.jpg")

    def test_maybe_between_floors(self):
        rating = {"scores": _scores_summing_to(7), "narrative": {}}  # overall 7.0
        entry = photo_pipeline.make_rating_entry(Path("/a/img.jpg"), rating, 6.0, 8.0)
        self.assertEqual(entry["decision"], "maybe")

    def test_maybe_at_cull_floor_boundary(self):
        rating = {"scores": _scores_summing_to(6), "narrative": {}}  # overall 6.0, == cull_below
        entry = photo_pipeline.make_rating_entry(Path("/a/img.jpg"), rating, 6.0, 8.0)
        self.assertEqual(entry["decision"], "maybe")  # >= cull_below is not culled

    def test_cull_below_cull_floor(self):
        rating = {"scores": _scores_summing_to(5), "narrative": {}}  # overall 5.0
        entry = photo_pipeline.make_rating_entry(Path("/a/img.jpg"), rating, 6.0, 8.0)
        self.assertEqual(entry["decision"], "cull")

    def test_none_rating_is_unrated(self):
        entry = photo_pipeline.make_rating_entry(Path("/a/img.jpg"), None, 6.0, 8.0)
        self.assertEqual(entry["decision"], "unrated")
        self.assertIsNone(entry["overall"])


class RatingFlagTests(unittest.TestCase):
    def test_rate_enables_with_default_bands(self):
        enabled, cull, keep = photo_pipeline.resolve_rating_flags(True, None, None)
        self.assertTrue(enabled)
        self.assertEqual(cull, 6.0)
        self.assertEqual(keep, 8.0)

    def test_off_by_default_but_bands_still_returned(self):
        enabled, cull, keep = photo_pipeline.resolve_rating_flags(False, None, None)
        self.assertFalse(enabled)
        self.assertEqual(cull, 6.0)
        self.assertEqual(keep, 8.0)

    def test_cull_below_implies_rate(self):
        enabled, cull, keep = photo_pipeline.resolve_rating_flags(False, 5.0, None)
        self.assertTrue(enabled)
        self.assertEqual(cull, 5.0)
        self.assertEqual(keep, 8.0)

    def test_keep_above_implies_rate(self):
        enabled, cull, keep = photo_pipeline.resolve_rating_flags(False, None, 9.0)
        self.assertTrue(enabled)
        self.assertEqual(cull, 6.0)
        self.assertEqual(keep, 9.0)

    def test_keep_floor_clamped_up_to_cull_floor(self):
        enabled, cull, keep = photo_pipeline.resolve_rating_flags(True, 8.0, 6.0)
        self.assertEqual(cull, 8.0)
        self.assertEqual(keep, 8.0)  # degenerate keep<cull clamped


if __name__ == "__main__":
    unittest.main()
