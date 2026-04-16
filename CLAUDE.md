# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

The pipeline runs from the user's Mac (or any host with AWS SSO). It uploads over HTTPS to the target WordPress REST API — no VM required. There is no CI/CD; the old `deploy-to-vm.yml` workflow was deleted on 2026-04-16 (never worked after Feb 19, no self-hosted runner registered).

## Orchestration

For cmbpix.com, the pipeline is driven by the `cmbpix-publish` skill in `~/code/websites/cmbpix.com-new/.claude/skills/cmbpix-publish/`. Read that skill's SKILL.md for the current orchestration contract (target selection, draft review, status flip, Cloudflare purge). The old OpenClaw/Cheryl/Malory/SMB-incoming flow is retired for cmbpix.

## Skill parity (OpenClaw agents)

For OpenClaw-agent use, this repo is mirrored by the `media/photo-pipeline` skill in `crawdad-skills` (`~/code/openclaw/skills/media/photo-pipeline/`). The skill does **not** ship a copy of `photo-pipeline.py` — its `install.sh` clones/pulls *this* repo into `~/tools/photo-pipeline/`, so the skill and repo cannot drift. When you change the pipeline:

1. Commit here.
2. Push to `main` on GitHub (`wacaldwell/photo-pipeline`).
3. On the agent host, re-run `./install.sh` from the skill dir (fast-forwards the clone).

Only touch the skill repo if you're changing the **invocation contract or agent docs** (SKILL.md, install.sh) — not the pipeline code itself.
