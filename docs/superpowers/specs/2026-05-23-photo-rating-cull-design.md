# photo-pipeline: AI photo rating + threshold cull

Status: drafted 2026-05-23 (pending review)
Owner: Alex Caldwell

## Goal

Point the pipeline at an album and have it score each photo 1-10 so the weakest shots can be culled before they're SEO-analyzed and published. The rating reproduces the behavior of the "Photo Critique" custom GPT (by Michael Sheehan), which scores against the **PPA 12 Elements of a Merit Image** and reports an overall ~mean score. The cull keeps photos whose overall score meets a user-set threshold and excludes the rest from upload — without ever modifying the source album.

## Background: what we're replicating

"Photo Critique" produces, per photo:
- A short opening impression.
- Twelve element scores, each /10: **Impact, Technical Excellence, Creativity, Composition, Presentation, Color Harmony, Interest Areas, Light, Technique, The Story, Style, Subject Matter** (the PPA 12 Elements of a Merit Image).
- A narrative: strongest elements → main area to refine → improvement suggestions → 1-2 title ideas.
- An overall score.

Confirmed from four real critiques the operator pasted: the overall is **the mean of the twelve elements, nudged up ~+0.1**. We treat `overall = mean(12)` as faithful and compute it ourselves (deterministic), rather than trusting a model's holistic number.

Calibration data points (operator-provided, used as few-shot anchors):

| Subject | mean(12) | PC overall |
|---|---|---|
| Wakeboard action | 7.33 | (none printed) |
| Golden Gate through foliage | 7.67 | 7.8 |
| Child water/pinwheel | 8.00 | 8.1 |
| Winter child portrait | 8.17 | 8.3 |

All four are strong photos (7.3-8.3); the **bottom of the scale is uncalibrated** and is addressed by the calibration step below, not by guessing.

## Decisions (locked)

- **Engine: Gemini**, reusing the existing integration (same single API key, no new dependency, stdlib-only preserved). Not OpenAI.
- **Rating type: absolute** — each photo scored against the fixed rubric, not ranked relative to the album. Required because a fixed threshold is only stable against an absolute scale.
- **Action: threshold cull** — keep `overall >= --min-rating`, exclude the rest.
- **Integration: Approach A** — a separate rating gate that runs after technical validation and before SEO analysis; SEO analysis runs only on survivors.
- **Cull safety: filter-only** — culled photos are excluded from upload and recorded in `ratings.json`. The source album folder is **never modified**. (No file moves in v1; see Out of scope.)

## Non-goals (YAGNI for v1)

- Near-duplicate / burst dedup.
- Multi-pass score averaging or ensemble runs.
- Per-element weighting (overall is a plain mean).
- Top-N mode, interactive review.
- OpenAI / configurable rater engine.
- Physically moving culled originals to `rejected/` (deferred; filter-only is the chosen default).

## Pipeline integration

The rating gate slots in alongside the existing validation pass:

```
find_images → technical validation → ★ rating gate → SEO analysis (keepers only) → manifest → upload → gallery → summary
```

- Runs only on images that passed technical validation (`status != "reject"`); warn-status images are still rated.
- Entirely opt-in. With no rating flags, the pipeline behaves exactly as today.
- Under `--dry-run`, the rating gate still runs (rate + write `ratings.json`), uploads nothing — the "eyeball the album first" workflow.

## CLI surface

```
--rate              Enable the rating pass. Scores every technically-valid
                    photo against the PPA 12 Elements, writes ratings.json.
                    Culls nothing on its own.
--min-rating FLOAT  Cull cutoff on the overall (mean) score. Implies --rate.
                    Photos with overall < cutoff are excluded from upload.
--rate-delay FLOAT  Seconds between rating API calls (default: falls back to
                    --delay). Same 429 backoff as SEO calls.
```

Flag wiring:
- `--rate` off by default → no rating, current behavior preserved.
- `--min-rating` present → implies `--rate`.
- `--rate` without `--min-rating` → rate-and-report only (no cull).

## The rating call

New function, modeled on `gemini_analyze_image()`:

```python
def gemini_rate_image(image_path: Path, api_key: str,
                      few_shot: list[dict]) -> dict
```

Returns:

```json
{
  "scores": {
    "impact": 8, "technical_excellence": 7, "creativity": 7,
    "composition": 7, "presentation": 7, "color_harmony": 8,
    "interest_areas": 7, "light": 6, "technique": 8,
    "the_story": 8, "style": 7, "subject_matter": 8
  },
  "narrative": {
    "impression": "...",
    "strongest": "...",
    "refine": "...",
    "improvement": "...",
    "titles": ["...", "..."]
  }
}
```

Prompt contents:
- The 12 elements, each with a one-line definition.
- A strict-JSON instruction matching the schema above (no markdown fences).
- **Text few-shot anchors** built from the four real critiques (subject description + the 12 scores + overall) to pin Gemini's numeric scale and narrative voice. If calibration shows drift, upgrade to *image* few-shot (send the reference photos); starting text-only to keep per-call cost down.
- `temperature: ~0.1` for scoring stability; `maxOutputTokens: ~1500` for the narrative.

Shared plumbing: the image-POST + 429-backoff retry loop currently inside `gemini_analyze_image()` is factored into one helper used by both the analyze and rate functions, so the retry logic isn't duplicated. (Surgical: behavior of the existing SEO call is unchanged.)

The pipeline computes `overall = round(mean(scores.values()), 1)` — not taken from the model.

## Scoring & cull mechanics

- `overall = mean(12)`. Keep iff `overall >= --min-rating`.
- **Fail-open on error:** if a rating call fails after retries, mark the photo `unrated` and **keep** it (never cull because of an API failure), flagged in the report for manual review. Same defensive philosophy as the SEO fallback.
- Culled photos are dropped from the set that proceeds to SEO analysis and upload. No file is moved or deleted.

## Artifacts

- **`ratings.json`** (work dir): array, one entry per rated photo:
  ```json
  {
    "original": "/path/IMG_1234.jpg",
    "scores": { ...12... },
    "overall": 7.3,
    "decision": "kept" | "culled" | "unrated",
    "narrative": { ... }
  }
  ```
- **Console:** a table sorted by score (`overall  decision  filename`) and a summary line: `Rating: N kept, M culled, K unrated (cutoff X.X)`.
- **`manifest.json`:** each kept entry gains a `rating` block (scores + overall).
- **`summary.json`:** a `rating` section — `{ "enabled", "cutoff", "kept", "culled", "unrated" }` with the per-file overall scores.

## Calibration plan (do this before relying on the cutoff)

1. Operator provides the actual image files for the four critiques (or any folder already Photo-Critique-scored).
2. Run `--rate --dry-run` on them; diff Gemini's computed overall against Photo Critique's known overall.
3. Tune the prompt / few-shot until agreement is within ~±0.5 across the sample, paying attention to the low end (add a low-scoring anchor if one becomes available).
4. Only after calibration passes do we trust `--min-rating` for real culls.

## Testing (TDD, Gemini call mocked)

Following the repo's `tests/` unittest + importlib pattern:
- `overall = mean(12)` computation (incl. rounding).
- Threshold filter: photos below cutoff are culled, at/above are kept.
- Fail-open: a rating exception marks the photo `unrated` and keeps it.
- Rating-JSON parsing: handles clean JSON and fenced/dirty JSON (mirrors the SEO parser's fence stripping).
- CLI wiring: `--min-rating` implies `--rate`; rating is off when neither flag is passed.

## Failure modes

| Scenario | Behavior |
|---|---|
| No rating flags | Rating gate skipped entirely; current behavior. |
| `--rate` only | Rate + write `ratings.json`; cull nothing. |
| `--rate --min-rating N` | Rate + cull below N. |
| `--dry-run --rate` | Rate + write report; upload nothing. |
| Single rating call fails (after retries) | Photo marked `unrated`, kept, flagged. |
| All photos culled | Print clear error and exit non-zero (nothing to publish), same as validation's all-rejected path. |
| `--min-rating` set but Gemini key missing | Existing key check already exits early before any pass. |

## Files touched

- `photo-pipeline.py` — add `--rate` / `--min-rating` / `--rate-delay`; factor shared Gemini-vision retry helper; add `gemini_rate_image()`; add the rating gate in `main()` between validation and the analysis loop; extend `manifest.json` / `summary.json`; write `ratings.json`.
- `tests/test_photo_rating.py` — new unit tests (per Testing).
- `README.md` — document the three flags + the rate-and-review workflow.
- `CLAUDE.md` — add a "Rating / cull" subsection (rubric, mean-overall, fail-open, filter-only) and note the gate's position in the Architecture list.
- AGENTS.md / agent contract — note rating is operator-facing and off by default; agents do not cull unless asked.

## Open follow-ups (post-v1, only if needed)

- Image few-shot if text anchors don't calibrate.
- A low-end calibration anchor once a panned example exists.
- `--cull-move` to archive culls into `album/rejected/`.
