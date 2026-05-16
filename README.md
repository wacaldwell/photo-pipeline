# Photo Pipeline

Analyze, rename, and publish photo albums as WordPress draft posts.

Uses Google Gemini vision to inspect each image, generate SEO-friendly
filenames, alt text, captions, and tags, then uploads to WordPress via
REST API and creates a gallery draft post.

## How it works

1. **Gemini Vision** analyzes each image and returns SEO metadata
   (filename, alt text, caption, tags)
2. Images are copied as-is (no resize by default). Use `--resize` for
   macOS `sips` resizing, or rely on a downstream optimizer (e.g. Lambda).
3. **WP REST API** uploads images and creates a draft gallery post
4. You review the draft and publish when ready

## Modula gallery support (`--cpt modula-gallery`)

When `--cpt modula-gallery` is passed, the pipeline creates a Modula Gallery
(not a standard post or a generic CPT). Flow:

1. Upload each image to `/wp/v2/media` — no `post_parent` linkage
   (Modula references images by WP attachment ID only).
2. POST `/wp/v2/modula-gallery` with `modulaSettings` and `modulaImages`
   built from the uploaded attachment IDs + per-image Gemini metadata.
   This creates a fully-populated gallery in one REST call.
3. PATCH the new gallery with `featured_media` (first image as cover),
   `meta._cmbpix_featured` (if `--featured`), `gallery_category` taxonomy
   term (if `--category`), `menu_order`, and final `status`.

Default Modula settings: `creative-gallery` grid, FancyBox lightbox,
10px gutter, 800px height. Override the helper in `wp_create_modula_gallery`
in code if you need different defaults.

This is the path cmbpix.com uses — see `.claude/skills/cmbpix-publish` in
the cmbpix theme repo for the orchestration skill.

## Prerequisites

- Python 3.10+ (stdlib only, no pip packages)
- WordPress instance with REST API enabled
- WP Application Password
- Google Gemini API key

## Setup

```bash
cp env.example .env
# Edit .env with your actual API key and WP app password
```

## Usage

Process an album:

```bash
python3 photo-pipeline.py /path/to/album --title "My Photo Post"
```

Dry run (analyze + rename only, no upload):

```bash
python3 photo-pipeline.py /path/to/album --dry-run
```

With macOS resize (optional):

```bash
python3 photo-pipeline.py /path/to/album --resize --max-width 1200 --quality 80
```

Override WordPress target:

```bash
python3 photo-pipeline.py /path/to/album --wp-url http://myhost:8087
```

With Telegram notification on publish:

```bash
AWS_PROFILE=clownshow python3 photo-pipeline.py /path/to/album \
  --title "Bagels" \
  --secret wordpress-mcp/photo-pipeline \
  --target cmbpix_prod \
  --status draft \
  --tele
```

Requires `telegram_bot_token`, `telegram_chat_id`, and
`telegram_content_creative_thread_id` in the AWS secret (or matching
`TELEGRAM_*` env vars). Sends a plain-text message containing the
wp-admin edit URL (draft preview URLs return 404 to anonymous viewers,
so the edit link is more useful in practice). No-op with `--dry-run`;
failure to notify only warns — it never fails the pipeline.

## Agent invocation contract

`photo-pipeline.py` is the canonical implementation. Agents, skills, and
local wrapper scripts should invoke this CLI directly instead of copying or
reimplementing the pipeline logic.

Minimum input:

- An album directory containing supported image files: jpg, jpeg, png, webp,
  tif, or tiff.
- Credentials supplied through CLI flags, environment variables, AWS Secrets
  Manager, or `.env`.

Recommended cmbpix production invocation (for agents):

```bash
AWS_PROFILE=hermes-photo-pipeline python3 photo-pipeline.py /path/to/album \
  --title "Album Title" \
  --secret wordpress-mcp/photo-pipeline \
  --target cmbpix_prod \
  --status draft \
  --tele
```

For `cmbpix_*` targets, the CLI automatically defaults to
`--cpt modula-gallery` if no CPT is supplied.

Credentials for agents: use the dedicated IAM profile
`hermes-photo-pipeline` (scoped to one `secretsmanager:GetSecretValue` on
this single secret), **not** the human SSO admin profile (`clownshow`).
Provisioning + rotation is managed in
[`clownshow-infra`](https://github.com/wacaldwell/clownshow-infra) →
`iam/hermes-photo-pipeline/` (Terraform). If the profile isn't on the
agent host, follow that module's README to provision it.

Agent behavior:

- Treat the album path as the only required user-provided input.
- Derive `--title` from the directory name unless the user gives a better
  title.
- Prefer `--status draft` so the user can review before publishing.
- Add `--category <slug>` only when the user provides a known curated category.
- Add `--featured` only when the user asks for a featured gallery.
- **Pass `--tele` by default.** Sends a plain-text Telegram message with
  the wp-admin edit URL to the user's content/creative topic on
  successful publish. No-op under `--dry-run`. Notification failures only
  warn — they never fail the pipeline.
- Report the final `post_id`, `post_title`, `edit_url`, `preview_url`,
  `images_uploaded`, and `images_analyzed` from `summary.json`.
- On failure, report the command, exit status, and the relevant stderr/stdout
  lines. Do not retry with altered publishing flags unless the user asks.

## Output

- Renamed images in a temp working directory
- `manifest.json` with per-image metadata from Gemini
- `summary.json` with post URL and stats
- A WordPress draft post with a gallery block and tags

## cmbpix.com workflow (primary)

The pipeline is driven directly from Claude Code via the `cmbpix-publish`
skill that lives in the cmbpix theme repo (`.claude/skills/cmbpix-publish/`).
The skill picks the `--target` (local dev on `tools`, or prod Lightsail),
pulls WP app password + Gemini key from AWS Secrets Manager
(`wordpress-mcp/photo-pipeline`), and runs this pipeline with
`--cpt modula-gallery`. Draft is created on the target site, skill hands
you the `edit_url`, you review and flip to publish.

No Cheryl/Malory/incoming/SMB flow. That earlier OpenClaw path has been
retired for cmbpix. If you're running the pipeline for cmbpix, use the
skill; for other sites, invoke the pipeline directly with the appropriate
`--secret` / `--target` / `--cpt` flags.

## Deployment

The pipeline runs from the user's Mac (or any host with AWS SSO access).
It does not need to be deployed to a VM — it uploads over HTTPS to the
target WordPress REST API. The `.env` file is optional (used if not
pulling config from AWS Secrets Manager).

## Cost

Gemini vision analysis costs approximately $0.01-0.03 per image.
A 20-photo album costs roughly $0.20-0.60.
