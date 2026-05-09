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
8. **Output** — Writes `summary.json` with `post_id`, `edit_url`, `preview_url`.

All HTTP via `urllib` (no `requests`). **Global UA override** installed at module import: `cmbpix-photo-pipeline/1.0`. This is required because Cloudflare bot-fight mode blocks `Python-urllib/*`. If you add new HTTP code, use the installed opener or set the same UA explicitly.

## Modula specifics

`MODULA_DEFAULT_SETTINGS` hardcodes `creative-gallery` type, FancyBox lightbox, 10px gutter, 800px height, white captions. Override via `settings_overrides` param to `wp_create_modula_gallery()` if you add a CLI flag for it.

`build_modula_images()` takes the media REST responses + per-image Gemini metadata and produces Modula's image-object list. Image `id` must be a WP attachment ID. Filter tags (Modula Pro `filters` field) are not wired via CLI — add there if needed; the Pro extension's `modula_gallery_image_attributes` hook will accept/whitelist them only when Pro is licensed.

## Deployment

The pipeline runs from any host that has AWS credentials for the `clownshow` profile with read access to the `wordpress-mcp/photo-pipeline` secret. It uploads over HTTPS to the target WordPress REST API. There is no CI/CD; the old `deploy-to-vm.yml` workflow was deleted on 2026-04-16 (never worked after Feb 19, no self-hosted runner registered).

Deployment target:
- **Mac** (user-driven, interactive): AWS SSO, Modula gallery via `cmbpix-publish` skill.

## Orchestration

The live cmbpix orchestrator is the **Mac** `cmbpix-publish` skill in the cmbpix theme repo (`~/code/websites/cmbpix.com-new/.Codex/skills/cmbpix-publish/`). It handles target selection, draft review, status flip, and Cloudflare purge.

The old OpenClaw VM / Malory / Cheryl / incoming SMB flow is retired for cmbpix. Do not use it as the publishing path.

## Skill parity

For agent use, this repo may be mirrored by the `media/photo-pipeline` skill in `crawdad-skills` (`~/code/openclaw/skills/media/photo-pipeline/`). The skill does **not** ship a copy of `photo-pipeline.py` — its `install.sh` clones/pulls *this* repo into `~/tools/photo-pipeline/`, so the skill and repo cannot drift. When you change the pipeline:

1. Commit here.
2. Push to `main` on GitHub (`wacaldwell/photo-pipeline`).
3. On the agent host, re-run `./install.sh` from the skill dir (fast-forwards the clone).

Only touch the skill repo if you're changing the **invocation contract or agent docs** (SKILL.md, install.sh) — not the pipeline code itself.


<claude-mem-context>
# Memory Context

# [photo-pipeline] recent context, 2026-05-09 12:17pm EDT

Legend: 🎯session 🔴bugfix 🟣feature 🔄refactor ✅change 🔵discovery ⚖️decision 🚨security_alert 🔐security_note
Format: ID TIME TYPE TITLE
Fetch details: get_observations([IDs]) | Search: mem-search skill

Stats: 4 obs (2,046t read) | 114,021t work | 98% savings

### May 9, 2026
161 12:14p 🔵 photo-pipeline: Gemini Vision → SEO Rename → WordPress Upload Pipeline
162 12:15p 🔵 photo-pipeline Orchestration Architecture: Two Live Paths, One Retired
163 " 🔵 photo-pipeline CLI Surface and Three Gallery Creation Modes
164 12:16p ✅ AGENTS.md and CLAUDE.md Updated to Retire OpenClaw VM Publishing Path

Access 114k tokens of past work via get_observations([IDs]) or mem-search skill.
</claude-mem-context>
