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

## Discord workflow (OpenClaw)

The pipeline integrates with OpenClaw's agent system for hands-off
album processing:

1. Drop an album folder into the site's `incoming/` directory on the
   SMB share (e.g. `smb://openclaw/<site>/incoming/album-name/`)
2. In Discord: `@builderbot process album album-name on cmbpix`
3. **Malory** delegates to **Cheryl** (photo agent)
4. Cheryl runs the pipeline against
   `/srv/wordpress-media/<site>/incoming/<album>/`
5. Draft URL and stats are posted back to Discord

## Deployment

On the VM, the script lives at `~/tools/photo-pipeline/`. The GitHub
Actions workflow rsyncs on push to `main`. The `.env` file is managed
manually on the VM and excluded from deploy.

## Cost

Gemini vision analysis costs approximately $0.01-0.03 per image.
A 20-photo album costs roughly $0.20-0.60.
