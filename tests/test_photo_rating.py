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


class AggregateRatingTests(unittest.TestCase):
    def test_single_pass_preserves_scores_and_marks_single_method(self):
        rating = {
            "scores": _scores_summing_to(8),
            "narrative": {"impression": "steady"},
        }
        out = photo_pipeline.aggregate_ratings([rating])
        self.assertEqual(out["scores"]["impact"], 8)
        self.assertEqual(out["aggregate"]["method"], "single")
        self.assertEqual(out["aggregate"]["overall_spread"], 0.0)
        self.assertEqual(len(out["passes"]), 1)

    def test_multi_pass_uses_median_and_reports_spread(self):
        low = {"scores": _scores_summing_to(6), "narrative": {"impression": "low"}}
        mid = {"scores": _scores_summing_to(7), "narrative": {"impression": "mid"}}
        high = {"scores": _scores_summing_to(9), "narrative": {"impression": "high"}}
        out = photo_pipeline.aggregate_ratings([low, mid, high])
        self.assertEqual(out["scores"]["impact"], 7)
        self.assertEqual(out["aggregate"]["method"], "median")
        self.assertEqual(out["aggregate"]["passes"], 3)
        self.assertEqual(out["aggregate"]["overall_mean"], 7.3)
        self.assertEqual(out["aggregate"]["overall_min"], 6.0)
        self.assertEqual(out["aggregate"]["overall_max"], 9.0)
        self.assertEqual(out["aggregate"]["overall_spread"], 3.0)
        self.assertEqual(out["narrative"]["impression"], "mid")


class RatingEntryTests(unittest.TestCase):
    def test_kept_at_or_above_cutoff(self):
        rating = {"scores": _scores_summing_to(8), "narrative": {}}
        entry = photo_pipeline.make_rating_entry(Path("/a/img.jpg"), rating, 7.0)
        self.assertEqual(entry["decision"], "kept")
        self.assertEqual(entry["overall"], 8.0)
        self.assertEqual(entry["original"], "/a/img.jpg")
        self.assertEqual(entry["passes"], [])

    def test_culled_below_cutoff(self):
        rating = {"scores": _scores_summing_to(6), "narrative": {}}
        entry = photo_pipeline.make_rating_entry(Path("/a/img.jpg"), rating, 7.0)
        self.assertEqual(entry["decision"], "culled")

    def test_rate_only_no_cutoff_keeps(self):
        rating = {"scores": _scores_summing_to(3), "narrative": {}}
        entry = photo_pipeline.make_rating_entry(Path("/a/img.jpg"), rating, None)
        self.assertEqual(entry["decision"], "kept")

    def test_none_rating_is_unrated_and_kept_for_review(self):
        entry = photo_pipeline.make_rating_entry(Path("/a/img.jpg"), None, 7.0)
        self.assertEqual(entry["decision"], "unrated")
        self.assertIsNone(entry["overall"])
        self.assertEqual(entry["passes"], [])


class RatingFlagTests(unittest.TestCase):
    def test_min_rating_implies_rate(self):
        enabled, cutoff = photo_pipeline.resolve_rating_flags(False, 7.0)
        self.assertTrue(enabled)
        self.assertEqual(cutoff, 7.0)

    def test_rate_only(self):
        enabled, cutoff = photo_pipeline.resolve_rating_flags(True, None)
        self.assertTrue(enabled)
        self.assertIsNone(cutoff)

    def test_off_by_default(self):
        enabled, cutoff = photo_pipeline.resolve_rating_flags(False, None)
        self.assertFalse(enabled)
        self.assertIsNone(cutoff)


if __name__ == "__main__":
    unittest.main()
