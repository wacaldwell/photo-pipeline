# Session note — AI photo rating / cull feature (2026-05-23 → 24)

Audience: future agents (and humans) picking up the photo-rating work. Read this before
touching the `--rate` code paths or re-attempting score calibration.

## TL;DR

We added an opt-in AI rating + cull step to `photo-pipeline.py` that scores each photo
against the PPA "12 Elements of a Merit Image" via Gemini and culls the weak ones before
SEO analysis/upload. It works mechanically and is well-tested. **But the headline finding
is that absolute per-photo scoring does not help Alex's real albums, because those albums
are already culled to "the good ones" — a set of peers with no quality spread for absolute
scoring to discriminate.** Alex decided a relative forced-ranking mode was not worth
building. The feature is left in place; do not re-chase prompt tuning to "fix" the
compression — it's a structural limit, not a prompt bug.

## What was built

A rating gate that mirrors the existing technical-validation pass: it runs **after**
validation and **before** the (expensive) SEO-analysis loop, so culled photos never get
analyzed or uploaded. Cull is **filter-only** — source files are never moved or deleted.
Fail-open — if a Gemini rating call fails, the photo is marked `unrated` and kept for
manual review (never culled on error). `overall = mean of the 12 element scores`, computed
in code (not taken from the model). Writes `ratings.json` (12 sub-scores + a
Photo-Critique-style narrative per photo) into the run's temp work dir, and adds a `rating`
section to `summary.json` + a `rating` block to each `manifest.json` entry.

Reuses the existing Gemini integration and single API key (no new deps; stdlib-only).
Shared Gemini-vision plumbing was factored into `_image_inline_part`,
`_gemini_vision_request`, `_gemini_response_json` (used by both `gemini_analyze_image` and
`gemini_rate_image`).

## ⚠️ Branch state — IMPORTANT, two versions exist

There are **two versions** of this feature. They differ in scale, decision model, and flags.

| | `main` (tip `718b8f9`) | `feat/rating-strict-bands` (tip `faee885`, NOT merged) |
|---|---|---|
| Scale | 1–10 | 2–10, "be strict" |
| Few-shot anchors | Yes (4 real critiques) — **these compress scores into 7–8** | None (removed) — strict band rubric drives it |
| Decision | Two-way: keep / cull | Three-way: keep / maybe / cull / unrated |
| Flags | `--rate`, `--min-rating N` | `--rate`, `--cull-below N` (def 6.0), `--keep-above N` (def 8.0) |
| Score clamp | no | yes (clamps to 2–10) |
| Tests | 20 pass | 25 pass |

- `main` is **12 commits ahead of `origin/main` and UNPUSHED.** Pushing is Alex's call.
  (Per the repo's wrapper-parity rule in CLAUDE.md, agent hosts won't get the feature until
  it's pushed to GitHub and clones updated.)
- `feat/rating-strict-bands` is the **better** version (stricter, honest scoring, three-way,
  cleaner docs) but was **not merged** because, even strict, it didn't solve the real
  problem (see findings). If absolute rating is ever used on *raw/unculled* shoots, merging
  this branch first is recommended.
- `wip/standard-control-and-modula-fix` (tip `a855730`) holds **unrelated** parked in-flight
  work: the standard-control feature + the *completed/verified* Modula `effect:"none"`
  overlay fix (cherry-pickable to `main` independently). Don't conflate it with rating work.

## The key finding (don't re-litigate)

Two live runs on a 25-photo album (`.../galleries/2025/2025-07-19-Lake-Keowee`, all
competent lake/water-sports snapshots):

- **Run 1** — `main` prompt (anchored, 1–10): every photo 7.0–8.0, mean 7.52, **stdev 0.24**.
- **Run 2** — strict prompt (2–10, no anchors): every photo shifted down to 6.5–7.4, mean
  ~6.89, **stdev ~0.18**; with default bands all 25 were "maybe" — **0 keep, 0 cull**.

"Be strict" lowered the mean but did **not** add spread. Root cause: **the albums Alex
points at are already hand-culled to good peers, so there is no quality range for absolute
scoring to find.** Scoring photos in isolation cannot manufacture spread that isn't there.
Sorting by these compressed absolute scores is also unreliable (boundary photos flip
run-to-run at temperature 0.1).

The real "Photo Critique" GPT sidesteps this by seeing the batch together and doing a
**forced-ranking** pass on the maybes — relative comparison, not isolated grading. That is
the only thing that would discriminate a peer set. It was scoped but **deliberately not
built** (Alex: "not sure it's worth spending more time on this").

When absolute rating *does* work well: **raw, unculled shoots** that contain a genuine mix
of keepers and throwaways. Point it at one of those and the keep/maybe/cull split is real.

## How to run / test

```bash
# Rate-and-review (no upload), pulling the Gemini key from the AWS secret:
AWS_PROFILE=hermes-photo-pipeline python3 photo-pipeline.py "/path/to/album" \
  --rate --dry-run --secret wordpress-mcp/photo-pipeline

# main:  add --min-rating 7.5   to cull below 7.5
# feat/rating-strict-bands: add --cull-below 6 / --keep-above 8 (or rely on defaults)
```

- The `hermes-photo-pipeline` AWS profile (long-lived key, scoped to the one secret) is
  valid on this machine and is the right way for agents to get the Gemini key. Never use the
  human `clownshow` SSO profile for agent runs.
- `--dry-run` makes the gate a pure rate-and-review: it rates, writes `ratings.json`, and
  stops before upload. Each rating run makes one Gemini call per image (paced by
  `--rate-delay`, default = `--delay` = 2s), so ~3–4 min for 25 images.
- Tests: `python3 -m unittest discover -s tests -v` (test_photo_rating.py covers prompt,
  rating call, scoring/bands/flags, fail-open). All green on both branches.

## Open decisions for whoever picks this up

1. **Push `main` to origin?** (12 commits unpushed.) Alex's call.
2. **Merge `feat/rating-strict-bands` into `main`?** It's the better absolute version. Not
   merged pending a reason to keep iterating.
3. **Relative forced-ranking / keep-top-N mode?** The only thing that would make this useful
   on already-good albums. Explicitly deferred as "not worth it" for now — revisit only if
   the use case changes (e.g., culling raw shoots, or wanting "best N of these peers").
4. **Branch cleanup:** the merged `feat/photo-rating-cull` pointer may still linger (a local
   safety hook blocks `git branch -d`); delete by hand when convenient.

## Pointers

- Spec:  `docs/superpowers/specs/2026-05-23-photo-rating-cull-design.md`
- Plan:  `docs/superpowers/plans/2026-05-23-photo-rating-cull.md`
- Rubric note: `## Rating / cull specifics` in `CLAUDE.md` (the strict-bands version is on
  `feat/rating-strict-bands`).
- Agent memory: `memory/project_rating_cull_calibration.md` (the calibration finding).
