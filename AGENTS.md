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

# [photo-pipeline] recent context, 2026-05-17 9:29am EDT

Legend: 🎯session 🔴bugfix 🟣feature 🔄refactor ✅change 🔵discovery ⚖️decision 🚨security_alert 🔐security_note
Format: ID TIME TYPE TITLE
Fetch details: get_observations([IDs]) | Search: mem-search skill

Stats: 50 obs (16,381t read) | 545,793t work | 97% savings

### May 15, 2026
S96 Spec approved; implementation plan written, committed, and user choosing execution approach (May 15 at 8:56 PM)
S93 Status check on `--tele` Telegram notification feature for photo-pipeline (May 15 at 8:56 PM)
### May 16, 2026
S97 Continue working on photo-pipeline feat/telegram-notify branch — complete implementation, review, and prepare for merge (May 16 at 7:37 AM)
305 9:31a 🟣 Telegram credential resolution wired into photo-pipeline main()
306 " 🟣 Guarded telegram_notify() call site added after WP publish summary write
307 " 🟣 Smoke test confirms --tele flag accepted by argparse without error
304 9:57a 🟣 Task 4 complete: telegram_notify() call site wired into main()
308 11:18a 🟣 All 4 telegram_notify unit tests pass after Task 4 wiring
309 " 🟣 Task 4 committed: --tele wiring merged to feat/telegram-notify at 87fe8f9
310 " 🟣 feat/telegram-notify branch commit history confirmed — 5 commits, Tasks 1-4 complete
311 " 🔵 Task 5 and 6 exact content confirmed from plan file
312 11:41a 🟣 photo-pipeline --tele flag: env.example TELEGRAM_* vars documented
313 12:46p ✅ photo-pipeline README.md: --tele example block inserted before Agent invocation contract
314 " 🔵 README.md HEAD lacks "Agent invocation contract" section — it's uncommitted pre-existing dirty content
315 1:06p ⚖️ README.md Task 6: targeted patch approach to isolate --tele block from pre-existing dirty content
316 1:07p 🔵 tele-only.patch has correct LF line endings — patch context references ## Output not ## Agent invocation contract
318 " ⚖️ README.md Task 6: second patch attempt with corrected hunk header @@ -76,7 +76,23 @@
319 " 🔵 README.md working tree uses em-dash (U+2014, 0xe2 0x80 0x94) in "never fails" line — patch has ASCII "--"
317 " 🔵 HEAD README.md line 79: ## Output follows --wp-url block with single blank line — patch context matches exactly
320 " ✅ README Updated with Telegram Notification Usage Example
321 1:08p ✅ README Telegram Block Injected via Git Index Manipulation
322 1:10p ✅ README Telegram Docs Committed to feat/telegram-notify Branch
323 " 🟣 Agent Invocation Contract Section Added to README
324 " 🔵 git add -p Cannot Split Adjacent Hunks — Used hash-object Workaround
325 1:11p ✅ feat/telegram-notify Branch Task 6 Verified Complete — Task 10 Started
326 " 🔵 feat/telegram-notify Branch Has 7 Commits — Full Implementation Visible
327 " 🔵 feat/telegram-notify Branch Total Scope: 203 Lines Across 5 Files
328 " 🔵 photo-pipeline Telegram Notify Spec and Implementation Plan Located
329 1:12p 🔵 telegram_notify() Implementation Drops resp.status Check — Relies on urllib HTTPError
330 1:18p 🟣 telegram_notify() Unit Tests All Pass — Feature Implementation Verified
331 " 🔵 Code Review: feat/telegram-notify READY TO MERGE — No Blocking Issues
S98 Complete Task 7: Add Telegram credentials to AWS secret and run live smoke test for feat/telegram-notify (May 16 at 1:20 PM)
332 5:03p 🔵 AWS clownshow Profile Confirmed Active — AdministratorAccess via SSO
S99 Add Telegram bot token to AWS secret for photo-pipeline --tele feature — blocked on invalid token (May 16 at 5:04 PM)
333 5:04p 🔵 Telegram Bot Token Failed Validation — 401 Unauthorized
S100 Complete Task 7 live smoke test: run photo-pipeline with --tele against cmbpix_prod and verify Telegram notification delivered (May 16 at 5:04 PM)
334 5:05p 🔵 Valid Telegram Bot Token Confirmed — @archermvdbot
335 " ✅ Telegram Credentials Written to wordpress-mcp/photo-pipeline AWS Secret
336 5:06p 🔵 mothers-day-26 Album Selected as Smoke Test Target — 11 Images
337 " 🔵 Bagels Album on NAS Selected as --tele Smoke Test Target — 7 Images
338 " 🟣 --tele Live Smoke Test Passed — Bagels Gallery Published to cmbpix_prod with Telegram Notification
339 " 🔵 Telegram Notification Sends Unusable Preview URL for Draft Modula Galleries
S101 Fix post-smoke-test bug: switch Telegram notification from preview_url to edit_url, then re-smoke-test and merge to main (May 16 at 5:06 PM)
S103 photo-pipeline feat/telegram-notify: complete branch — fix Telegram link to use wp-admin edit URL, smoke test, and ff-merge to main (May 16 at 5:10 PM)
340 5:11p 🔴 telegram_notify() Signature Updated to edit_url — Call Site Still Needs Fix
343 " 🔵 Task 12 Smoke Test: Telegram message delivered but edit URL link errors
341 " 🔴 Call Site Fixed: edit_url=edit_link Now Passed to telegram_notify()
342 " 🔵 Unit Tests Now Broken — All 4 Tests Still Pass preview_url Keyword
345 5:13p 🔵 Telegram Edit URL Link Errored on First Smoke Test
346 5:26p 🔴 Telegram Notify Switched from Preview URL to wp-admin Edit URL
347 " 🟣 Smoke Test v2 Confirms Edit URL Delivered Correctly in Telegram
348 " ⚖️ Selective README Staging via git hash-object to Isolate Unrelated Dirty Hunks
344 5:27p 🟣 Telegram edit URL fix committed to feat/telegram-notify (commit a322a90)
349 5:36p 🟣 feat/telegram-notify Fast-Forward Merged to main (Push Pending)
S102 photo-pipeline feat/telegram-notify branch: fix Telegram notification to use wp-admin edit URL, commit, smoke test, and merge to main (May 16 at 5:36 PM)
S104 photo-pipeline feat/telegram-notify: ship Telegram --tele notification feature — fix edit URL, smoke test, merge to main, push to GitHub, clean up branch (May 16 at 5:37 PM)
350 5:38p 🔵 Pre-existing Dirty Files in photo-pipeline Repo: Substantial Uncommitted Work
351 " ✅ Selective revert of claude-mem-context block in AGENTS.md
352 5:39p ✅ photo-pipeline docs: agent invocation contract added to AGENTS.md, CLAUDE.md, README.md
353 " 🔵 .omc/ directory not in photo-pipeline .gitignore

Access 546k tokens of past work via get_observations([IDs]) or mem-search skill.
</claude-mem-context>