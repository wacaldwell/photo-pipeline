# Photo Rating + Threshold Cull Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in Gemini rating gate to `photo-pipeline.py` that scores each photo against the PPA 12 Elements, computes an overall (mean) score, and culls below-threshold photos before SEO analysis/upload — without ever modifying the source album.

**Architecture:** A new rating pass mirrors the existing technical-validation pass: it runs after validation and before the SEO-analysis loop, rating every validated image via Gemini, writing `ratings.json`, and filtering the processing set so only survivors reach analysis/upload. Shared Gemini-vision plumbing (image encoding + 429 retry) is factored out so the rating call and the existing SEO call reuse it. Culling is filter-only (no file moves).

**Tech Stack:** Python 3.10+ stdlib only (`urllib`, `json`, `base64`, `argparse`), Google Gemini 2.0 Flash REST API, `unittest` + `importlib` test loader.

**Reference spec:** `docs/superpowers/specs/2026-05-23-photo-rating-cull-design.md`

---

## Preconditions (read before Task 1)

**The working tree is dirty with unrelated in-flight work** (standard-control + social-research changes in `photo-pipeline.py`, plus untracked `standards/` and existing test files). Every task below ends with `git add photo-pipeline.py tests/...` which would bundle that unrelated work into rating commits.

**Before starting, isolate the in-flight work** so rating commits stay clean. Pick one (operator decision):
- Commit the existing standard-control/social work first (preferred — gives a clean base), **or**
- `git stash push -m "wip: standard-control" photo-pipeline.py` and restore after, **or**
- Accept bundling and clean up history later (not recommended).

Do not proceed to Task 1 until the tree is clean enough that `git add photo-pipeline.py` stages only rating-related changes.

## File structure

- **`photo-pipeline.py`** (modify) — all production code:
  - New shared helpers: `_image_inline_part()`, `_gemini_vision_request()`, `_gemini_response_json()`.
  - Refactor `gemini_analyze_image()` to use those helpers (no behavior change).
  - New rating module-level constants: `RATING_ELEMENTS`, `RATING_KEYS`, `RATING_FEW_SHOT`.
  - New functions: `build_rating_prompt()`, `gemini_rate_image()`, `compute_overall()`, `make_rating_entry()`, `resolve_rating_flags()`.
  - New CLI flags: `--rate`, `--min-rating`, `--rate-delay`.
  - New rating gate inside `main()` between validation and the analysis loop; `ratings.json` write; `manifest.json` and `summary.json` extensions.
- **`tests/test_photo_rating.py`** (create) — unit tests, Gemini mocked.
- **`README.md`**, **`CLAUDE.md`**, **`AGENTS.md`** (modify) — docs.

---

### Task 1: Factor shared Gemini-vision plumbing (refactor, no behavior change)

**Files:**
- Modify: `photo-pipeline.py` (add helpers above `gemini_analyze_image`; rewrite that function's body)
- Test: `tests/test_photo_rating.py`

- [ ] **Step 1: Write the failing characterization test**

Create `tests/test_photo_rating.py` with the standard importlib loader and one test proving `gemini_analyze_image` still parses a (fenced) Gemini response after the refactor, with the network call mocked:

```python
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_photo_rating -v`
Expected: FAIL — `AttributeError: module 'photo_pipeline' has no attribute '_gemini_vision_request'`.

- [ ] **Step 3: Add the three shared helpers**

Insert immediately **above** `def gemini_analyze_image` (currently line ~409) in `photo-pipeline.py`:

```python
def _image_inline_part(image_path: Path) -> dict:
    """Build a Gemini inline_data part (base64 image + mime type) for a file."""
    image_bytes = image_path.read_bytes()
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    mime_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }
    mime_type = mime_map.get(image_path.suffix.lower(), "image/jpeg")
    return {"inline_data": {"mime_type": mime_type, "data": b64_image}}


def _gemini_vision_request(payload: dict, api_key: str,
                           model: str = "gemini-2.0-flash",
                           timeout: int = 60) -> dict:
    """POST a generateContent payload with 429 backoff. Return the parsed response JSON."""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    data = json.dumps(payload).encode("utf-8")
    max_retries = 7
    for attempt in range(max_retries):
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code == 429 and attempt < max_retries - 1:
                # Prefer server's Retry-After; otherwise exponential backoff capped
                # at 60s. Jitter avoids herd-retry on concurrent runs.
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if retry_after and retry_after.strip().isdigit():
                    wait = min(60.0, float(retry_after.strip()))
                else:
                    wait = min(60.0, 2 ** (attempt + 1) + 1)
                wait += random.uniform(0, 1.5)
                print(f"  Rate limited (429), waiting {wait:.1f}s (attempt {attempt + 1}/{max_retries}) ...", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"  ERROR: Gemini API returned {e.code}: {body[:300]}", file=sys.stderr)
            raise
    raise RuntimeError("Gemini request exhausted retries")


def _gemini_response_json(result: dict) -> dict:
    """Extract the model's text from a Gemini response and parse it as JSON, tolerating ``` fences."""
    text = result["candidates"][0]["content"]["parts"][0]["text"].strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()
    return json.loads(text)
```

- [ ] **Step 4: Rewrite `gemini_analyze_image` to use the helpers**

Replace the entire body of `gemini_analyze_image` (currently lines ~409-505, from `image_bytes = ...` through `return json.loads(text)`) with:

```python
def gemini_analyze_image(image_path: Path, api_key: str) -> dict:
    """Send an image to Gemini and get SEO metadata back."""
    prompt = (
        "Analyze this photograph and return a JSON object with exactly these keys:\n"
        '- "seo_filename": a lowercase, hyphenated, SEO-friendly filename '
        "(no extension, max 60 chars, descriptive of the image content)\n"
        '- "alt_text": concise alt text for accessibility (max 125 chars)\n'
        '- "caption": a short, engaging caption suitable for a photo blog '
        "(1-2 sentences, max 200 chars)\n"
        '- "tags": array of 3-7 relevant SEO keyword tags (lowercase)\n'
        '- "description": a brief 1-sentence description of what the image shows\n'
        "\n"
        "Return ONLY valid JSON, no markdown fences, no extra text."
    )
    payload = {
        "contents": [
            {"parts": [{"text": prompt}, _image_inline_part(image_path)]}
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 512,
        },
    }
    result = _gemini_vision_request(payload, api_key)
    return _gemini_response_json(result)
```

- [ ] **Step 5: Run the full test file + existing suite to verify no regression**

Run: `python3 -m unittest tests.test_photo_rating tests.test_modula_gallery -v`
Expected: all PASS (the new characterization test passes; Modula tests unaffected).

- [ ] **Step 6: Commit**

```bash
git add photo-pipeline.py tests/test_photo_rating.py
git commit -m "[pipeline]: factor shared Gemini-vision helpers (no behavior change)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Rating rubric constants + prompt builder

**Files:**
- Modify: `photo-pipeline.py` (add constants + `build_rating_prompt` near the other Gemini functions)
- Test: `tests/test_photo_rating.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_photo_rating.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_photo_rating.RatingPromptTests -v`
Expected: FAIL — `AttributeError: ... has no attribute 'RATING_KEYS'`.

- [ ] **Step 3: Add the constants + prompt builder**

Insert **above** `def gemini_analyze_image` (after the shared helpers from Task 1):

```python
# PPA "12 Elements of a Merit Image" — the rubric the "Photo Critique" GPT uses.
# (json_key, Display Label, one-line definition)
RATING_ELEMENTS = [
    ("impact",              "Impact",              "the sense one gets upon viewing; emotional or intellectual response"),
    ("technical_excellence","Technical Excellence","print/image quality: exposure, sharpness, noise, artifacts, retouching"),
    ("creativity",          "Creativity",          "original, fresh, imaginative expression of the subject"),
    ("composition",         "Composition",         "arrangement of elements; balance, flow, use of the frame"),
    ("presentation",        "Presentation",        "how the image is finished/framed; nothing distracts from it"),
    ("color_harmony",       "Color Harmony",       "colors work together to support the image (or effective monochrome)"),
    ("interest_areas",      "Interest Areas",      "a clear center of interest; the eye is led there, not distracted"),
    ("light",               "Light",               "quality and use of lighting to shape the subject and mood"),
    ("technique",           "Technique",           "skillful use of tools/approach (DOF, motion, lens choice, etc.)"),
    ("the_story",           "The Story",           "the image tells a story or evokes a narrative/feeling"),
    ("style",               "Style",               "a distinctive, intentional visual approach"),
    ("subject_matter",      "Subject Matter",      "appropriateness and appeal of the chosen subject"),
]
RATING_KEYS = [key for key, _label, _defn in RATING_ELEMENTS]

# Real "Photo Critique" outputs (operator-provided), used as text few-shot anchors
# to pin Gemini's numeric scale and narrative voice. overall = mean(12), ~+0.1.
RATING_FEW_SHOT = [
    {
        "desc": "Wakeboarder mid-action behind a boat: wake, spray, diagonal tow rope, wide lake, big sky, shot in strong midday sun (shadowed face).",
        "scores": {"impact": 8, "technical_excellence": 7, "creativity": 7, "composition": 7,
                   "presentation": 7, "color_harmony": 8, "interest_areas": 7, "light": 6,
                   "technique": 8, "the_story": 8, "style": 7, "subject_matter": 8},
        "overall": 7.3,
    },
    {
        "desc": "Golden Gate Bridge through soft out-of-focus foreground foliage; longer-lens compression stacking bridge, hills, water, sky; tower slightly cropped at top.",
        "scores": {"impact": 8, "technical_excellence": 7, "creativity": 8, "composition": 7,
                   "presentation": 7, "color_harmony": 8, "interest_areas": 7, "light": 7,
                   "technique": 8, "the_story": 8, "style": 8, "subject_matter": 9},
        "overall": 7.8,
    },
    {
        "desc": "Child playing in a backyard water spray with a colorful pinwheel; suspended droplets, warm golden light, shallow depth of field, genuine expression.",
        "scores": {"impact": 8, "technical_excellence": 8, "creativity": 8, "composition": 7,
                   "presentation": 8, "color_harmony": 8, "interest_areas": 8, "light": 7,
                   "technique": 8, "the_story": 9, "style": 8, "subject_matter": 9},
        "overall": 8.1,
    },
    {
        "desc": "Tight winter portrait of a child with sharp expressive eyes, blue coat, olive hat, snow crystals, soft high-key snowy background; crop tight above the hat.",
        "scores": {"impact": 9, "technical_excellence": 8, "creativity": 8, "composition": 7,
                   "presentation": 8, "color_harmony": 9, "interest_areas": 8, "light": 8,
                   "technique": 8, "the_story": 8, "style": 8, "subject_matter": 9},
        "overall": 8.3,
    },
]


def build_rating_prompt(few_shot: list[dict]) -> str:
    """Build the Gemini rating prompt: PPA rubric + JSON schema + text few-shot anchors."""
    lines = [
        "You are an expert photography judge using the PPA \"12 Elements of a Merit "
        "Image\". Score the photograph on each element from 1 to 10 (integers).",
        "",
        "Elements:",
    ]
    for _key, label, defn in RATING_ELEMENTS:
        lines.append(f"- {label}: {defn}")
    lines += [
        "",
        "Return ONLY a JSON object (no markdown fences, no extra text) with exactly:",
        '{"scores": {' + ", ".join(f'"{k}": <1-10>' for k in RATING_KEYS) + "},",
        '  "narrative": {"impression": "...", "strongest": "...", "refine": "...",'
        ' "improvement": "...", "titles": ["...", "..."]}}',
        "",
        "Calibration examples (subject -> scores -> overall = mean of the twelve):",
    ]
    for ex in few_shot:
        score_str = ", ".join(f"{k}:{ex['scores'][k]}" for k in RATING_KEYS)
        lines.append(f"- {ex['desc']}")
        lines.append(f"    scores: {score_str}  (overall {ex['overall']})")
    lines += [
        "",
        "Judge the provided image on the same scale and in the same voice.",
    ]
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_photo_rating.RatingPromptTests -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add photo-pipeline.py tests/test_photo_rating.py
git commit -m "[pipeline]: add PPA 12-element rating rubric + prompt builder

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `gemini_rate_image()` — the rating call

**Files:**
- Modify: `photo-pipeline.py` (add `gemini_rate_image` after `build_rating_prompt`)
- Test: `tests/test_photo_rating.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_photo_rating.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_photo_rating.GeminiRateImageTests -v`
Expected: FAIL — `AttributeError: ... has no attribute 'gemini_rate_image'`.

- [ ] **Step 3: Implement `gemini_rate_image`**

Insert after `build_rating_prompt` in `photo-pipeline.py`:

```python
def gemini_rate_image(image_path: Path, api_key: str,
                      few_shot: list[dict] | None = None) -> dict:
    """Rate one photo against the PPA 12 Elements. Returns {"scores": {...}, "narrative": {...}}."""
    if few_shot is None:
        few_shot = RATING_FEW_SHOT
    prompt = build_rating_prompt(few_shot)
    payload = {
        "contents": [
            {"parts": [{"text": prompt}, _image_inline_part(image_path)]}
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 1500,
        },
    }
    result = _gemini_vision_request(payload, api_key)
    data = _gemini_response_json(result)
    raw_scores = data.get("scores", {})
    scores = {k: int(raw_scores[k]) for k in RATING_KEYS}  # KeyError/ValueError -> caller fail-open
    narrative = data.get("narrative", {})
    return {"scores": scores, "narrative": narrative}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_photo_rating.GeminiRateImageTests -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add photo-pipeline.py tests/test_photo_rating.py
git commit -m "[pipeline]: add gemini_rate_image() PPA rating call

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `compute_overall()` + `make_rating_entry()` + `resolve_rating_flags()`

**Files:**
- Modify: `photo-pipeline.py` (add the three pure functions after `gemini_rate_image`)
- Test: `tests/test_photo_rating.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_photo_rating.py`:

```python
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
    def test_kept_at_or_above_cutoff(self):
        rating = {"scores": _scores_summing_to(8), "narrative": {}}
        entry = photo_pipeline.make_rating_entry(Path("/a/img.jpg"), rating, 7.0)
        self.assertEqual(entry["decision"], "kept")
        self.assertEqual(entry["overall"], 8.0)
        self.assertEqual(entry["original"], "/a/img.jpg")

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_photo_rating.ScoringTests tests.test_photo_rating.RatingEntryTests tests.test_photo_rating.RatingFlagTests -v`
Expected: FAIL — `AttributeError: ... has no attribute 'compute_overall'`.

- [ ] **Step 3: Implement the three functions**

Insert after `gemini_rate_image` in `photo-pipeline.py`:

```python
def compute_overall(scores: dict) -> float:
    """Overall rating = mean of the 12 element scores, rounded to 1 decimal."""
    vals = [scores[k] for k in RATING_KEYS]
    return round(sum(vals) / len(vals), 1)


def make_rating_entry(original: Path, rating: dict | None,
                      min_rating: float | None) -> dict:
    """Build a ratings.json entry with a kept/culled/unrated decision.

    Fail-open: a None rating (API failure) is 'unrated' and kept for manual review.
    No cutoff (rate-only mode) keeps everything.
    """
    if rating is None:
        return {"original": str(original), "scores": None, "overall": None,
                "decision": "unrated", "narrative": None}
    overall = compute_overall(rating["scores"])
    if min_rating is None:
        decision = "kept"
    else:
        decision = "kept" if overall >= min_rating else "culled"
    return {"original": str(original), "scores": rating["scores"],
            "overall": overall, "decision": decision,
            "narrative": rating.get("narrative")}


def resolve_rating_flags(rate: bool, min_rating: float | None) -> tuple[bool, float | None]:
    """--min-rating implies --rate. Returns (rating_enabled, cutoff)."""
    enabled = bool(rate or min_rating is not None)
    return enabled, min_rating
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_photo_rating -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add photo-pipeline.py tests/test_photo_rating.py
git commit -m "[pipeline]: add overall/score-entry/flag-resolution helpers

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: CLI flags

**Files:**
- Modify: `photo-pipeline.py` (argparse block in `main()`)

- [ ] **Step 1: Add the three arguments**

In the argument definitions in `main()`, add:

```python
    parser.add_argument("--rate", action="store_true",
        help="Rate each image 1-10 against the PPA 12 Elements before SEO analysis; "
             "writes ratings.json. Culls nothing on its own.")
    parser.add_argument("--min-rating", type=float, default=None,
        help="Cull cutoff on the overall (mean) rating; implies --rate. "
             "Images scoring below this are excluded from upload (source files untouched).")
    parser.add_argument("--rate-delay", type=float, default=None,
        help="Seconds between rating API calls (default: same as --delay).")
```

- [ ] **Step 2: Verify the parser accepts them**

Run: `python3 photo-pipeline.py --help`
Expected: `--rate`, `--min-rating`, and `--rate-delay` appear in the help output; exit 0.

- [ ] **Step 3: Commit**

```bash
git add photo-pipeline.py
git commit -m "[pipeline]: add --rate / --min-rating / --rate-delay flags

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Wire the rating gate into `main()`

**Files:**
- Modify: `photo-pipeline.py` (`main()` — insert gate after the standard-report write at line ~1359 / before `analysis_results = []` at line ~1361; extend the manifest entry and the `summary` dict)

- [ ] **Step 1: Insert the rating gate**

Find, in `main()`, the lines (currently ~1361-1362):

```python
    analysis_results = []
    all_tags = []
```

Insert **immediately above** them:

```python
    # -----------------------------------------------------------------------
    # Rating gate — mirrors the validation pass. Rate every (technically valid)
    # image against the PPA 12 Elements, then drop sub-threshold photos from
    # the processing set BEFORE the expensive SEO-analysis loop. Filter-only:
    # source files are never moved or deleted. Fail-open: a failed rating call
    # keeps the photo (marked 'unrated') for manual review.
    # -----------------------------------------------------------------------
    rate_enabled, rate_cutoff = resolve_rating_flags(args.rate, args.min_rating)
    ratings: list[dict] = []
    rating_map: dict[Path, dict] = {}
    if rate_enabled:
        rate_delay = args.rate_delay if args.rate_delay is not None else args.delay
        cutoff_str = "none" if rate_cutoff is None else f"{rate_cutoff}"
        print(f"Rating {len(images)} image(s) against PPA 12 Elements (cutoff {cutoff_str}) ...")
        for i, img in enumerate(images, 1):
            if i > 1 and rate_delay > 0:
                time.sleep(rate_delay)
            print(f"[rate {i}/{len(images)}] {img.name} ...")
            try:
                rating = gemini_rate_image(img, gemini_key)
            except Exception as e:
                print(f"  RATING FAILED (kept for manual review): {e}", file=sys.stderr)
                rating = None
            entry = make_rating_entry(img, rating, rate_cutoff)
            rating_map[img] = entry
            ratings.append(entry)
            ov = "n/a" if entry["overall"] is None else f"{entry['overall']:.1f}"
            print(f"  overall={ov}  {entry['decision']}")

        ratings_path = work_dir / "ratings.json"
        ratings_path.write_text(json.dumps(ratings, indent=2))
        print(f"Ratings written: {ratings_path}")

        print()
        print("Rating results (high to low):")
        for e in sorted(ratings, key=lambda e: (e["overall"] is not None, e["overall"] or 0), reverse=True):
            ov = "n/a" if e["overall"] is None else f"{e['overall']:.1f}"
            print(f"  {ov:>4}  {e['decision']:<7}  {Path(e['original']).name}")
        n_kept = sum(1 for e in ratings if e["decision"] == "kept")
        n_culled = sum(1 for e in ratings if e["decision"] == "culled")
        n_unrated = sum(1 for e in ratings if e["decision"] == "unrated")
        suffix = f" (cutoff {rate_cutoff})" if rate_cutoff is not None else ""
        print(f"Rating: {n_kept} kept, {n_culled} culled, {n_unrated} unrated{suffix}")
        print()

        # Filter-only cull: drop 'culled'; keep 'kept' and (fail-open) 'unrated'.
        images = [img for img in images if rating_map[img]["decision"] != "culled"]
        if not images:
            print("ERROR: all images culled by rating. Lower --min-rating or review the album.", file=sys.stderr)
            sys.exit(1)

```

- [ ] **Step 2: Attach the rating to each manifest entry**

Find the `analysis_results.append({...})` block (currently ~1407-1413):

```python
        analysis_results.append({
            "original": str(img),
            "prepared": str(dst),
            "filename": dst.name,
            "metadata": metadata,
            "validation": validation_map.get(img, {"status": "ok", "issues": []}),
        })
```

Add one line so it becomes:

```python
        analysis_results.append({
            "original": str(img),
            "prepared": str(dst),
            "filename": dst.name,
            "metadata": metadata,
            "validation": validation_map.get(img, {"status": "ok", "issues": []}),
            "rating": rating_map.get(img),
        })
```

- [ ] **Step 3: Add the `rating` section to `summary`**

Find the `summary = {` dict (currently ~1673) and add this key (e.g. after `"images_analyzed": ...`):

```python
        "rating": {
            "enabled": rate_enabled,
            "cutoff": rate_cutoff,
            "kept": sum(1 for e in ratings if e["decision"] == "kept"),
            "culled": sum(1 for e in ratings if e["decision"] == "culled"),
            "unrated": sum(1 for e in ratings if e["decision"] == "unrated"),
            "scores": [
                {"file": Path(e["original"]).name, "overall": e["overall"], "decision": e["decision"]}
                for e in ratings
            ],
        },
```

- [ ] **Step 4: Smoke-test the wiring (no real API, no upload)**

Verify the module still imports and `main()` parses with the new flags by running help and a dry-run that fails fast on a missing album (proves the gate code is syntactically wired without burning API calls):

Run: `python3 -c "import importlib.util,sys;from pathlib import Path;s=importlib.util.spec_from_file_location('pp',Path('photo-pipeline.py'));m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print('import OK')"`
Expected: `import OK` (no SyntaxError).

Run: `python3 -m unittest tests.test_photo_rating tests.test_modula_gallery -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add photo-pipeline.py
git commit -m "[pipeline]: wire rating gate into main (ratings.json, manifest, summary)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Documentation

**Files:**
- Modify: `README.md`, `CLAUDE.md`, `AGENTS.md`

- [ ] **Step 1: README — document the flags + rate-and-review workflow**

In `README.md`, locate the flag/options list and add entries for `--rate`, `--min-rating`, and `--rate-delay`. Then add a short usage block (place it near the existing dry-run example):

```markdown
### Rate & cull an album

Score every photo 1-10 against the PPA "12 Elements of a Merit Image" and keep
only the strong ones. Source files are never moved or deleted — culling just
excludes weak photos from the upload.

```bash
# Rate everything and review (no cull, no upload):
python3 photo-pipeline.py /path/to/album --rate --dry-run
#   -> writes ratings.json (12 element scores + overall + narrative per photo)

# Cull below 7.0, then publish the survivors:
AWS_PROFILE=hermes-photo-pipeline python3 photo-pipeline.py /path/to/album \
  --title "Album Title" --secret wordpress-mcp/photo-pipeline \
  --target cmbpix_prod --status draft --rate --min-rating 7
```

`--min-rating` implies `--rate`. A failed rating call keeps the photo (marked
`unrated`) for manual review rather than dropping it.
```

- [ ] **Step 2: CLAUDE.md — Architecture + a Rating subsection**

In `CLAUDE.md`, in the numbered Architecture list, update the analysis step ordering to mention the rating gate (insert between Discovery/validation and the SEO Analysis step), e.g. add:

```markdown
3b. **Rating gate (optional)** — when `--rate`/`--min-rating` is passed, `gemini_rate_image()` scores each validated image against the PPA 12 Elements; `compute_overall()` = mean of the 12. Sub-threshold images are filtered out (filter-only — source untouched) before SEO analysis. Fail-open: a failed rating keeps the image as `unrated`. Writes `ratings.json`.
```

Then add a new subsection after "Modula specifics":

```markdown
## Rating / cull specifics

`--rate` enables a PPA "12 Elements" rating pass (Impact, Technical Excellence,
Creativity, Composition, Presentation, Color Harmony, Interest Areas, Light,
Technique, The Story, Style, Subject Matter), reusing the existing Gemini key.
The overall score is the **mean of the 12** (computed in-code, not by the model)
— this mirrors the "Photo Critique" GPT, whose overall is the mean +~0.1.

`--min-rating N` culls photos with overall < N. Culling is **filter-only**: weak
photos are dropped from the processing set, never moved or deleted. A failed
rating call is **fail-open** (kept, marked `unrated`). `RATING_FEW_SHOT` holds
four real Photo-Critique examples used as text anchors to calibrate Gemini's
scale; if the scale drifts, switch them to image few-shot.
```

- [ ] **Step 3: AGENTS.md — note rating is operator-facing and off by default**

In `AGENTS.md`, under the agent behavior guidance, add a bullet:

```markdown
- **Do not pass `--rate` / `--min-rating` unless the user asks to cull.** Rating is operator-facing and off by default. If asked to cull, prefer `--rate --dry-run` first so the user can review `ratings.json` before any photos are excluded.
```

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md AGENTS.md
git commit -m "[docs]: document --rate / --min-rating cull feature

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Calibration run (manual — requires operator + real images)

**Files:** none (verification only)

> This is the gate from the spec's Calibration plan. It needs the operator and a real `GEMINI_API_KEY`. Do NOT trust `--min-rating` for real culls until this passes.

- [ ] **Step 1: Gather a known-scored set**

Operator provides the four critique images (or any folder already Photo-Critique-scored) in a local directory, e.g. `~/cal-album/`.

- [ ] **Step 2: Run rate-only**

Run: `GEMINI_API_KEY=<key> python3 photo-pipeline.py ~/cal-album --rate --dry-run`
Expected: prints a rating table; writes `ratings.json` in the temp work dir (path shown in output).

- [ ] **Step 3: Compare to known overalls**

Open the printed `ratings.json`. Compare Gemini's `overall` per photo against Photo Critique's known overall (Golden Gate 7.8, water/pinwheel 8.1, winter portrait 8.3, wakeboard ~7.3).
Expected (target): agreement within ~±0.5. If consistently off, tune `build_rating_prompt` wording and/or upgrade `RATING_FEW_SHOT` to image few-shot, then re-run. Record the result.

- [ ] **Step 4: (No commit unless tuning changed code.)** If you edited the prompt/few-shot, commit:

```bash
git add photo-pipeline.py
git commit -m "[pipeline]: calibrate rating prompt against known Photo-Critique scores

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Self-review

**Spec coverage:**
- Engine = Gemini, reuse integration → Tasks 1, 3. ✓
- Absolute rating, PPA 12 Elements → Task 2. ✓
- overall = mean(12) computed in-code → Task 4 (`compute_overall`). ✓
- Approach A gate after validation, before SEO analysis → Task 6. ✓
- Filter-only cull, source never modified → Task 6 (list filter; no file ops). ✓
- Opt-in flags; `--min-rating` implies `--rate` → Tasks 5, 4 (`resolve_rating_flags`). ✓
- `--dry-run` = rate-and-review → gate runs before the existing dry-run return; ratings.json written in Task 6. ✓
- Fail-open on rating error → Task 6 try/except + `make_rating_entry(None)`. ✓
- ratings.json / manifest.json / summary.json artifacts → Task 6. ✓
- Calibration plan → Task 8. ✓
- Tests (mean, threshold, fail-open, JSON parse incl. fences, CLI implication) → Tasks 1-4. ✓
- Docs (README, CLAUDE.md, AGENTS.md) → Task 7. ✓
- "All photos culled" error path → Task 6. ✓

**Placeholder scan:** none — every code/doc step shows the actual content.

**Type consistency:** `RATING_KEYS` (Task 2) used by `gemini_rate_image` (Task 3), `compute_overall` (Task 4), gate + summary (Task 6) — consistent. `make_rating_entry`/`resolve_rating_flags`/`compute_overall` signatures match call sites in Task 6. `_gemini_vision_request` / `_image_inline_part` / `_gemini_response_json` defined in Task 1, reused in Task 3. Mock targets in tests match those exact names.

**Note on ordering:** Task 5 (flags) must precede Task 6 (gate reads `args.rate` / `args.min_rating` / `args.rate_delay`). Tasks 2-4 add code only referenced by Task 6's gate, so the suite stays green between tasks.
