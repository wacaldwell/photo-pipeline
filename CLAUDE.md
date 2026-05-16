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
  --status draft \
  --tele

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
  --status draft \
  --tele
```

For `cmbpix_*` targets, the CLI automatically defaults to `--cpt modula-gallery` if no CPT is supplied.

### Credentials for agents

Use the dedicated IAM profile `hermes-photo-pipeline`, **never** the human admin profile (`clownshow` / SSO). The agent profile has exactly one permission: `secretsmanager:GetSecretValue` on `wordpress-mcp/photo-pipeline*`. No IAM access, no other secrets, no console login. Blast radius is one read-only secret.

Provisioning + rotation of this IAM user is managed in [`clownshow-infra`](https://github.com/wacaldwell/clownshow-infra) → `iam/hermes-photo-pipeline/` (Terraform). If the profile doesn't exist on the agent host, follow that module's README to provision and harvest credentials into `~/.aws/credentials`.

If `AWS_PROFILE=hermes-photo-pipeline` is unavailable for any reason, fall back to env vars resolved by the pipeline directly: `GEMINI_API_KEY`, `WP_URL`, `WP_USER`, `WP_APP_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_CONTENT_CREATIVE_THREAD_ID`. Drop `--secret` and `--target` from the command when going this route.

Agent behavior:
- Treat the album path as the only required user-provided input.
- Derive `--title` from the directory name unless the user gives a better title.
- Prefer `--status draft` so the user can review before publishing.
- Add `--category <slug>` only when the user provides a known curated category.
- Add `--featured` only when the user asks for a featured gallery.
- **Pass `--tele` by default.** Sends a plain-text Telegram message with the wp-admin edit URL to the user's content/creative topic on successful publish. Credentials are already in the AWS secret (`telegram_bot_token`, `telegram_chat_id`, `telegram_content_creative_thread_id`). No-op under `--dry-run`. Notification failures only warn — they never fail the pipeline, so passing `--tele` is safe even if the bot is down.
- Report the final `post_id`, `post_title`, `edit_url`, `preview_url`, `images_uploaded`, and `images_analyzed` from `summary.json`.
- On failure, report the command, exit status, and the relevant stderr/stdout lines. Do not retry with altered publishing flags unless the user asks.

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
9. **Notification (optional)** — When `--tele` is passed and the run succeeded, `telegram_notify()` posts a plain-text message containing the wp-admin edit URL to the configured Telegram topic. Best-effort: any HTTP/transport failure prints a warning to stderr and the pipeline still exits 0. Skipped under `--dry-run`.

All HTTP via `urllib` (no `requests`). **Global UA override** installed at module import: `cmbpix-photo-pipeline/1.0`. This is required because Cloudflare bot-fight mode blocks `Python-urllib/*`. If you add new HTTP code, use the installed opener or set the same UA explicitly.

## Modula specifics

`MODULA_DEFAULT_SETTINGS` hardcodes `creative-gallery` type, FancyBox lightbox, 10px gutter, 800px height, white captions. Override via `settings_overrides` param to `wp_create_modula_gallery()` if you add a CLI flag for it.

`build_modula_images()` takes the media REST responses + per-image Gemini metadata and produces Modula's image-object list. Image `id` must be a WP attachment ID. Filter tags (Modula Pro `filters` field) are not wired via CLI — add there if needed; the Pro extension's `modula_gallery_image_attributes` hook will accept/whitelist them only when Pro is licensed.

## Deployment

The pipeline runs from any host that has AWS credentials for the `clownshow` profile with read access to the `wordpress-mcp/photo-pipeline` secret. It uploads over HTTPS to the target WordPress REST API. There is no CI/CD; the old `deploy-to-vm.yml` workflow was deleted on 2026-04-16 (never worked after Feb 19, no self-hosted runner registered).

Deployment target:
- **Mac** (user-driven, interactive): AWS SSO profile `clownshow`, Modula gallery via `cmbpix-publish` skill.
- **Mac** (agent-driven, e.g. Hermes): dedicated IAM user `hermes-photo-pipeline` (long-lived access key in `~/.aws/credentials`, scoped to one secret). Same CLI; pass `--tele` so the user is notified when a draft is ready. IAM provisioned via `clownshow-infra` Terraform — see the "Credentials for agents" subsection above.

## Orchestration

The live cmbpix orchestrator is the **Mac** `cmbpix-publish` skill in the cmbpix theme repo (`~/code/websites/cmbpix.com-new/.claude/skills/cmbpix-publish/`). It handles target selection, draft review, status flip, and Cloudflare purge.

The old OpenClaw VM / Malory / Cheryl / incoming SMB flow is retired for cmbpix. Do not use it as the publishing path.

## Agent wrapper parity

For agent use, wrappers such as Hermes skills or the `media/photo-pipeline` skill in `crawdad-skills` may clone or pull this repo. Wrappers should call this repo's CLI and must not ship a forked copy of `photo-pipeline.py`, so the tool and wrapper contract cannot drift. When you change the pipeline:

1. Commit here.
2. Push to `main` on GitHub (`wacaldwell/photo-pipeline`).
3. On agent hosts, update the clone or rerun the wrapper's install step so it fast-forwards to the new version.

Only touch the skill repo if you're changing the **invocation contract or agent docs** (SKILL.md, install.sh) — not the pipeline code itself.
