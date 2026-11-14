# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## What This Is

A standalone Python 3.10+ CLI tool (single file, stdlib only — no pip dependencies) that automates photo album processing: analyzes images with Google Gemini vision AI, generates SEO metadata (filenames, alt text, captions, tags), optionally resizes, uploads to WordPress via REST API, and creates a draft post — either a standard WP post with an inline gallery block, a generic CPT post, or a **Modula Gallery** (the primary path for cmbpix.com).

## Running the Tool

```bash
# cmbpix.com Modula flow (most common)
AWS_PROFILE=clownshow python3 photo-pipeline.py /path/to/album \
  --title "Bagels" \
  --cpt modula-gallery \
  --category food \
  --featured \
  --secret wordpress-mcp/photo-pipeline \
  --target cmbpix_prod \
  --status draft

# Dry run (analyze + rename only, no WordPress upload)
python3 photo-pipeline.py /path/to/album --dry-run

# Legacy standard-post flow (no --cpt)
python3 photo-pipeline.py /path/to/album --title "My Photo Post"
```

Config resolution: CLI arg > env var > AWS secret (via `--secret`) > `.env` file > default. The secret stores Gemini key + multi-target WP credentials as a single JSON blob; `--target` selects which site's creds to use (`cmbpix_local`, `cmbpix_prod`).

## Agent Invocation Contract

`photo-pipeline.py` is the canonical implementation. Agents, skills, and local wrapper scripts should invoke this CLI directly instead of copying or reimplementing pipeline logic.

Minimum input:
- An album directory containing supported image files: jpg, jpeg, png, webp, tif, or tiff.
- Credentials supplied through CLI flags, environment variables, AWS Secrets Manager, or `.env`.

Recommended cmbpix production invocation (for agents):

```bash
AWS_PROFILE=hermes-photo-pipeline python3 photo-pipeline.py /path/to/album \
  --title "Album Title" \
  --secret wordpress-mcp/photo-pipeline \
  --target cmbpix_prod \
  --status draft
```

For `cmbpix_*` targets, the CLI automatically defaults to `--cpt modula-gallery` if no CPT is supplied.

### Credentials for agents

Use the dedicated IAM profile `hermes-photo-pipeline`, **never** the human admin profile (`clownshow` / SSO). The agent profile has exactly one permission: `secretsmanager:GetSecretValue` on `wordpress-mcp/photo-pipeline*`. No IAM access, no other secrets, no console login. Blast radius is one read-only secret.

Provisioning + rotation of this IAM user is managed in [`clownshow-infra`](https://github.com/wacaldwell/clownshow-infra) → `iam/hermes-photo-pipeline/` (Terraform). If the profile doesn't exist on the agent host, follow that module's README to provision and harvest credentials into `~/.aws/credentials`.

If `AWS_PROFILE=hermes-photo-pipeline` is unavailable for any reason, fall back to env vars resolved by the pipeline directly: `GEMINI_API_KEY`, `WP_URL`, `WP_USER`, `WP_APP_PASSWORD`. Drop `--secret` and `--target` from the command when going this route.

Agent behavior:
- Treat the album path as the only required user-provided input.
- Derive `--title` from the directory name unless the user gives a better title.
- Prefer `--status draft` so the user can review before publishing.
- Add `--category <slug>` only when the user provides a known curated category.
- Add `--featured` only when the user asks for a featured gallery.
- Report the final `post_id`, `post_title`, `edit_url`, `preview_url`, `images_uploaded`, and `images_analyzed` from `summary.json`.
- On failure, report the command, exit status, and the relevant stderr/stdout lines. Do not retry with altered publishing flags unless the user asks.
- **Do not pass `--rate` / `--min-rating` unless the user asks to cull.** Rating is operator-facing and off by default. If asked to cull, prefer `--rate --dry-run` first so the user can review `ratings.json` before any photos are excluded.

## Architecture

Everything lives in `photo-pipeline.py`. Sequential, functional, no classes:

1. **Config** — `fetch_aws_secret()` / `load_env()`. CLI > env > secret > default.
2. **Discovery** — `find_images()` finds jpg/jpeg/png/webp/tif/tiff.
3. **Analysis** — `gemini_analyze_image()` → Gemini 2.0 Flash → `{seo_filename, alt_text, caption, tags, description}`. 429 backoff, max 4 retries. Falls back to original filename on failure.
4. **Preparation** — Rename to SEO filenames in a temp dir. Optional `sips_resize()` on macOS (off by default).
5. **Manifest** — Writes `manifest.json`. `--dry-run` stops here.
6. **Upload** — `wp_upload_media()` uploads each image via `/wp/v2/media` (two requests: POST bytes, PUT metadata). `attach_to` param only used for legacy CPT path.
7. **Gallery/post creation** — Three modes:
   - `--cpt modula-gallery`: `wp_create_modula_gallery()` builds `modulaSettings` + `modulaImages` and POSTs to `/wp/v2/modula-gallery` in one shot; then PATCHes for `featured_media`, `_cmbpix_featured`, taxonomy, `menu_order`, final `status`.
   - `--cpt <other>`: legacy generic CPT path — pre-creates draft, uploads media with `post_parent`, PATCHes to finalize.
   - No `--cpt`: `wp_create_draft_post()` creates a standard post with an inline WP gallery block.
8. **Output** — Writes `summary.json` with `post_id`, `post_title`, `edit_url`, `preview_url`, `images_uploaded`, `images_analyzed`, `working_directory`.

All HTTP via `urllib` (no `requests`). **Global UA override** installed at module import: `cmbpix-photo-pipeline/1.0`. This is required because Cloudflare bot-fight mode blocks `Python-urllib/*`. If you add new HTTP code, use the installed opener or set the same UA explicitly.

## Modula specifics

`MODULA_DEFAULT_SETTINGS` hardcodes `creative-gallery` type, FancyBox lightbox, 10px gutter, 800px height, white captions. Override via `settings_overrides` param to `wp_create_modula_gallery()` if you add a CLI flag for it.

`build_modula_images()` takes the media REST responses + per-image Gemini metadata and produces Modula's image-object list. Image `id` must be a WP attachment ID. Filter tags (Modula Pro `filters` field) are not wired via CLI — add there if needed; the Pro extension's `modula_gallery_image_attributes` hook will accept/whitelist them only when Pro is licensed.

## Deployment

The pipeline runs from any host that has AWS credentials for the `clownshow` profile with read access to the `wordpress-mcp/photo-pipeline` secret. It uploads over HTTPS to the target WordPress REST API. There is no CI/CD; the old `deploy-to-vm.yml` workflow was deleted on 2026-04-16 (never worked after Feb 19, no self-hosted runner registered).

Deployment target:
- **Mac** (user-driven, interactive): AWS SSO profile `clownshow`, Modula gallery via `cmbpix-publish` skill.
- **Mac** (agent-driven, e.g. Hermes): dedicated IAM user `hermes-photo-pipeline` (long-lived access key in `~/.aws/credentials`, scoped to one secret). Same CLI for draft creation. IAM provisioned via `clownshow-infra` Terraform — see the "Credentials for agents" subsection above.

## Orchestration

The live cmbpix orchestrator is the **Mac** `cmbpix-publish` skill in the cmbpix theme repo (`~/code/websites/cmbpix.com-new/.Codex/skills/cmbpix-publish/`). It handles target selection, draft review, status flip, and Cloudflare purge.

The old OpenClaw VM / Malory / Cheryl / incoming SMB flow is retired for cmbpix. Do not use it as the publishing path.

## Agent wrapper parity

For agent use, wrappers such as Hermes skills or the `media/photo-pipeline` skill in `crawdad-skills` may clone or pull this repo. Wrappers should call this repo's CLI and must not ship a forked copy of `photo-pipeline.py`, so the tool and wrapper contract cannot drift. When you change the pipeline:

1. Commit here.
2. Push to `main` on GitHub (`wacaldwell/photo-pipeline`).
3. On agent hosts, update the clone or rerun the wrapper's install step so it fast-forwards to the new version.

Only touch the skill repo if you're changing the **invocation contract or agent docs** (SKILL.md, install.sh) — not the pipeline code itself.


<claude-mem-context>
# Memory Context

# [photo-pipeline] recent context, 2026-09-09 1:46pm EDT

Legend: 🎯session 🔴bugfix 🟣feature 🔄refactor ✅change 🔵discovery ⚖️decision 🚨security_alert 🔐security_note
Format: ID TIME TYPE TITLE
Fetch details: get_observations([IDs]) | Search: mem-search skill

Stats: 50 obs (18,889t read) | 1,448,778t work | 99% savings

### May 23, 2026
S125 Photo rating pipeline planning — scoring mechanics confirmed, overall score derivation resolved (May 23 at 9:34 AM)
S126 Photo rating pipeline planning — three critiques analyzed, architecture approaches proposed (May 23 at 9:36 AM)
S127 Photo rating + threshold cull design spec drafted, committed, and sent for review (May 23 at 9:36 AM)
S128 Photo rating + cull deep planning complete — design spec and implementation plan both committed (May 23 at 9:40 AM)
S131 Photo-pipeline rating/cull feature complete — awaiting album path from user for Task 8 calibration run (May 23 at 9:49 AM)
549 12:21p ✅ Task 2 Marked Complete; Task 3 Now In Progress
550 " 🔵 photo-pipeline Repo Has Three Active Branches
551 " 🟣 Task 3 TDD Red Phase: GeminiRateImageTests Added, Failing as Expected
557 " 🔵 gemini_analyze_image Payload Structure: Pattern for gemini_rate_image
552 12:24p 🟣 Task 3 TDD: GeminiRateImageTests Revised with Int-Coercion Requirement
575 12:36p 🟣 gemini_rate_image() added — PPA 12-Element rating call
576 " ⚖️ gemini_rate_image raises on malformed scores — fail-open handled by caller
583 12:39p 🟣 Task 4: Rating aggregation pure functions added to photo-pipeline
584 " 🟣 Negative-path tests lock gemini_rate_image raise-on-malformed contract
588 1:19p 🟣 Task 5: Add --rate / --min-rating / --rate-delay CLI flags to photo-pipeline
594 1:21p 🟣 Task 6: Rating gate wired into main() — photo-pipeline.py
597 3:05p ⚖️ photo-pipeline rating feature isolated to feat/photo-rating-cull branch
599 3:09p 🔵 --min-rating cutoff string uses bare f-string, showing full float precision
600 3:12p 🔵 Table formatting verified correct for all rating score ranges
601 " 🔵 Second code-review of Task 6 confirms APPROVE with 3 non-blocking issues
598 3:13p 🔵 Rating leaderboard sort lambda handles None overalls correctly
603 3:20p 🔴 Fixed cutoff_str float formatting to use :.1f consistently
604 3:21p ⚖️ Dirty working-tree changes deferred to separate feature branch
602 6:09p 🔄 Hoisted rating count variables to outer scope (MEDIUM fix)
605 6:31p ⚖️ Dirty changes to be isolated in new WIP feature branch
606 " 🔴 RATING_FEW_SHOT overalls corrected to true compute_overall() means
607 " 🔴 CLAUDE.md architecture step ordering fixed: rating gate now 2b. (before SEO analysis)
608 " 🔵 wip/standard-control-and-modula-fix branch already exists in repo
609 " 🔵 Calibration environment: AWS profiles present, no GEMINI_API_KEY or sample images
610 6:49p 🔵 hermes-photo-pipeline IAM credentials confirmed valid on local Mac
S130 Photo-pipeline photo rating/culling feature (Tasks 1–7) complete; awaiting album path for calibration run (Task 8) (May 23 at 6:49 PM)
S133 Photo-pipeline AI rating/cull feature: full implementation complete — observer session recording calibration finding from Photo Critique GPT scoring instructions (2–10 scale, 5-tier decision framework) (May 23 at 6:50 PM)
611 6:53p 🟣 Calibration run (Task 8) started with Lake Keowee album — 25 real photos
612 " 🔵 Task 8 not visible in observer task list despite TaskUpdate success in primary session
613 6:57p 🟣 Task 8 calibration run completed: 25 Lake Keowee images rated 7.0–8.0 with no errors
615 " 🟣 --min-rating 7.5 cull demo: 9/25 images culled, SEO analysis skipped for culled set
614 6:58p 🔵 Rating scale distribution: mean=7.52, stdev=0.24 — tight clustering expected for quality recreational albums
616 7:19p 🔵 Gemini score compression documented as known limitation: all scores cluster in 7.0–8.0 band
617 " ✅ Task 8 (calibration run) marked completed; project memory updated with calibration findings
618 " 🔵 Photo Critique GPT canonical scoring instructions confirmed: 2–10 scale, 5-tier decision framework, "Maybe" second-pass workflow
S132 Photo-pipeline AI rating/cull feature (Tasks 1–8) fully complete — awaiting user decision on git push to origin (May 23 at 7:19 PM)
S2165 Is photo-pipeline a portable skill usable anywhere in Claude or Codex? (May 23 at 7:20 PM)
619 7:45p 🟣 New branch feat/rating-strict-bands created to fix score compression in build_rating_prompt()
620 " ⚖️ Task 9: Redesign rating system — strict 2-10 prompt, drop few-shot anchors, three-way Keep/Maybe/Cull with dual-threshold flags
621 " 🔵 Test file audit: 5 of 20 tests will break under Task 9 redesign — identified before edits begin
622 7:47p 🔄 RATING_FEW_SHOT constant deleted and build_rating_prompt() signature changed to zero-argument
623 " 🟣 build_rating_prompt() rewritten with strict 2–10 scale, 5-tier band table, and anti-inflation directive
625 7:48p 🔵 Photo Critique GPT Official Culling Prompt and Batch Workflow Documented
### May 24, 2026
626 10:48a 🔵 Photo-Pipeline Already Has Photo Rating/Cull Feature
627 " 🔵 Photo Rating Feature: All 16 Unit Tests Pass on Main Branch
628 10:49a 🔵 Rating/Cull Feature Fully Documented in README and Clean on Main
629 " 🟣 aggregate_ratings() Added for Multi-Pass Score Stabilization
630 " 🟣 --rate-passes N CLI Flag Added for Multi-Pass Score Averaging
631 " 🟣 TDD Tests Added for aggregate_ratings() and Updated make_rating_entry() Assertions
632 10:50a 🟣 Multi-Pass Rating Feature Complete: 18 Tests Pass, All Files Updated
633 " 🔵 Multi-Pass Rating Changes Unstaged — Ready to Commit
### Sep 5, 2026
9362 6:01p 🔵 User Investigating Photo-Pipeline Skill Portability
9363 " 🔵 photo-pipeline Is Not a Registered Global Skill
9364 " 🔵 cmbpix-publish Is a Project-Scoped Skill Wrapping photo-pipeline
S2166 User declined to create a global photo-pipeline skill — keeping current setup as-is (Sep 5 at 6:03 PM)
**Investigated**: Full audit of skill registrations for photo-pipeline across ~/.claude/skills/, mvd-agent-skills/claude/, and project-scoped .claude/skills/ directories.

**Learned**: photo-pipeline is intentionally repo-local: a standalone Python CLI accessed by opening Claude Code or Codex inside the repo directory. cmbpix-publish is the only skill wrapping it, and it remains scoped to the cmbpix.com-new project. No global skill registration exists or will be created.

**Completed**: Investigation complete. User made an explicit decision: no changes. photo-pipeline stays as a repo-local CLI; cmbpix-publish stays project-scoped to cmbpix.com-new. Session closed without any file modifications.

**Next Steps**: No active work. Session concluded after user declined the offer to create a global skill.


Access 1449k tokens of past work via get_observations([IDs]) or mem-search skill.
</claude-mem-context>