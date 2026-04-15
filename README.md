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
