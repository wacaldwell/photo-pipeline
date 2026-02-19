#!/usr/bin/env python3
"""
Photo pipeline: Gemini vision -> SEO rename -> optional resize -> WP upload -> draft post.

Cross-platform (macOS + Linux). Requires:
  - A .env file (copy from env.example)
  - WordPress with REST API enabled
  - WP Application Password for authentication

Images are copied as-is by default (no resize). Use --resize to enable
macOS sips resizing, or rely on a downstream optimizer (e.g. Lambda).

Usage:
    python3 photo-pipeline.py /path/to/album [--title "My Album"] [--dry-run]
    python3 photo-pipeline.py /path/to/album --resize --wp-url http://10.0.0.1:8087
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}


def load_env(env_path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file."""
    env = {}
    if not env_path.exists():
        return env
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def find_images(album_dir: Path) -> list[Path]:
    """Find all supported image files in the album directory."""
    images = []
    for f in sorted(album_dir.iterdir()):
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS:
            images.append(f)
    return images


def gemini_analyze_image(image_path: Path, api_key: str) -> dict:
    """Send an image to Gemini Pro Vision and get SEO metadata back."""
    image_bytes = image_path.read_bytes()
    b64_image = base64.b64encode(image_bytes).decode("utf-8")

    mime_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }
    mime_type = mime_map.get(image_path.suffix.lower(), "image/jpeg")

    prompt = (
        "Analyze this photograph and return a JSON object with exactly these keys:\n"
        '- "seo_filename": a lowercase, hyphenated, SEO-friendly filename '
        "(no extension, max 60 chars, descriptive of the image content)\n"
        '- "alt_text": concise alt text for accessibility (max 125 chars)\n'
        '- "caption": a short, engaging caption suitable for a photo blog '
        "(1-2 sentences, max 200 chars)\n"
        '- "tags": array of 3-7 relevant SEO keyword tags (lowercase)\n'
        '- "description": a brief 1-sentence description of what the image shows\n'
        "\n"
        "Return ONLY valid JSON, no markdown fences, no extra text."
    )

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": b64_image,
                        }
                    },
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 512,
        },
    }

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={api_key}"
    )

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    max_retries = 4
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code == 429 and attempt < max_retries - 1:
                wait = 2 ** (attempt + 1) + 1  # 3s, 5s, 9s, ...
                print(f"  Rate limited (429), waiting {wait}s (attempt {attempt + 1}/{max_retries}) ...", file=sys.stderr)
                time.sleep(wait)
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                continue
            print(f"  ERROR: Gemini API returned {e.code}: {body[:300]}", file=sys.stderr)
            raise

    text = result["candidates"][0]["content"]["parts"][0]["text"]
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()

    return json.loads(text)


def sips_resize(src: Path, dst: Path, max_width: int, quality: int) -> None:
    """Resize an image using macOS sips, preserving aspect ratio."""
    shutil.copy2(src, dst)

    result = subprocess.run(
        ["sips", "-g", "pixelWidth", str(dst)],
        capture_output=True,
        text=True,
    )
    width_match = re.search(r"pixelWidth:\s*(\d+)", result.stdout)
    if width_match and int(width_match.group(1)) > max_width:
        subprocess.run(
            ["sips", "--resampleWidth", str(max_width), str(dst)],
            capture_output=True,
            check=True,
        )

    if dst.suffix.lower() in (".jpg", ".jpeg"):
        subprocess.run(
            [
                "sips",
                "-s", "formatOptions", str(quality),
                str(dst),
            ],
            capture_output=True,
            check=True,
        )


def wp_upload_media(
    image_path: Path,
    alt_text: str,
    caption: str,
    description: str,
    wp_url: str,
    wp_user: str,
    wp_password: str,
) -> dict:
    """Upload an image to WordPress via REST API, return media object."""
    upload_url = f"{wp_url}/wp-json/wp/v2/media"

    image_bytes = image_path.read_bytes()
    mime_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }
    content_type = mime_map.get(image_path.suffix.lower(), "image/jpeg")

    auth_str = base64.b64encode(f"{wp_user}:{wp_password}".encode()).decode()

    req = urllib.request.Request(
        upload_url,
        data=image_bytes,
        headers={
            "Content-Type": content_type,
            "Content-Disposition": f'attachment; filename="{image_path.name}"',
            "Authorization": f"Basic {auth_str}",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=60) as resp:
        media = json.loads(resp.read().decode("utf-8"))

    media_id = media["id"]
    update_url = f"{wp_url}/wp-json/wp/v2/media/{media_id}"
    update_payload = json.dumps({
        "alt_text": alt_text,
        "caption": caption,
        "description": description,
    }).encode("utf-8")

    update_req = urllib.request.Request(
        update_url,
        data=update_payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {auth_str}",
        },
        method="POST",
    )
    with urllib.request.urlopen(update_req, timeout=30) as resp:
        media = json.loads(resp.read().decode("utf-8"))

    return media


def wp_create_draft_post(
    title: str,
    media_items: list[dict],
    tags: list[str],
    wp_url: str,
    wp_user: str,
    wp_password: str,
) -> dict:
    """Create a draft blog post with a gallery block referencing uploaded media."""
    image_ids = [str(m["id"]) for m in media_items]
    ids_str = ",".join(image_ids)

    gallery_block = f'<!-- wp:gallery {{"linkTo":"none","columns":3}} -->\n'
    gallery_block += '<figure class="wp-block-gallery has-nested-images columns-3 is-cropped">\n'
    for m in media_items:
        src = m["source_url"]
        alt = m.get("alt_text", "")
        cap = m.get("caption", {}).get("rendered", "").strip()
        cap_text = re.sub(r"<[^>]+>", "", cap)
        gallery_block += (
            f'<!-- wp:image {{"id":{m["id"]},"sizeSlug":"large","linkDestination":"none"}} -->\n'
            f'<figure class="wp-block-image size-large">'
            f'<img src="{src}" alt="{alt}" class="wp-image-{m["id"]}"/>'
        )
        if cap_text:
            gallery_block += f"<figcaption class=\"wp-element-caption\">{cap_text}</figcaption>"
        gallery_block += "</figure>\n<!-- /wp:image -->\n"
    gallery_block += "</figure>\n<!-- /wp:gallery -->"

    auth_str = base64.b64encode(f"{wp_user}:{wp_password}".encode()).decode()

    all_tags = set()
    for t in tags:
        all_tags.update(t if isinstance(t, list) else [t])

    tag_ids = []
    for tag_name in sorted(all_tags):
        search_url = f"{wp_url}/wp-json/wp/v2/tags?search={urllib.request.quote(tag_name)}"
        search_req = urllib.request.Request(
            search_url,
            headers={"Authorization": f"Basic {auth_str}"},
        )
        with urllib.request.urlopen(search_req, timeout=15) as resp:
            existing = json.loads(resp.read().decode("utf-8"))

        if existing:
            tag_ids.append(existing[0]["id"])
        else:
            create_url = f"{wp_url}/wp-json/wp/v2/tags"
            create_req = urllib.request.Request(
                create_url,
                data=json.dumps({"name": tag_name}).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Basic {auth_str}",
                },
                method="POST",
            )
            with urllib.request.urlopen(create_req, timeout=15) as resp:
                new_tag = json.loads(resp.read().decode("utf-8"))
            tag_ids.append(new_tag["id"])

    post_payload = {
        "title": title,
        "content": gallery_block,
        "status": "draft",
        "tags": tag_ids,
    }
    if media_items:
        post_payload["featured_media"] = media_items[0]["id"]

    post_url = f"{wp_url}/wp-json/wp/v2/posts"
    post_req = urllib.request.Request(
        post_url,
        data=json.dumps(post_payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {auth_str}",
        },
        method="POST",
    )

    with urllib.request.urlopen(post_req, timeout=30) as resp:
        post = json.loads(resp.read().decode("utf-8"))

    return post


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Photo pipeline: analyze, rename, resize, upload, and create WP draft post."
    )
    parser.add_argument("album", type=Path, help="Path to album directory containing images")
    parser.add_argument("--title", type=str, default=None, help="Blog post title (default: derived from album dir name)")
    parser.add_argument("--dry-run", action="store_true", help="Analyze and rename only, skip upload")
    parser.add_argument("--resize", action="store_true", help="Enable sips resize (macOS only). Off by default; images are copied as-is.")
    parser.add_argument("--max-width", type=int, default=None, help="Max image width in pixels (default: from .env or 1920)")
    parser.add_argument("--quality", type=int, default=None, help="JPEG quality 1-100 (default: from .env or 85)")
    parser.add_argument("--delay", type=float, default=2.0, help="Seconds to wait between Gemini API calls (default: 2.0)")
    parser.add_argument("--wp-url", type=str, default=None, help="WordPress URL (overrides .env WP_URL)")
    parser.add_argument("--wp-user", type=str, default=None, help="WordPress username (overrides .env WP_USER)")
    parser.add_argument("--wp-password", type=str, default=None, help="WordPress Application Password (overrides .env WP_APP_PASSWORD)")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    env = load_env(script_dir / ".env")

    gemini_key = env.get("GEMINI_API_KEY", "")
    wp_url = args.wp_url or env.get("WP_URL", "http://mvd-clawbase:8087")
    wp_user = args.wp_user or env.get("WP_USER", "admin")
    wp_password = args.wp_password or env.get("WP_APP_PASSWORD", "")
    max_width = args.max_width or int(env.get("MAX_WIDTH", "1920"))
    quality = args.quality or int(env.get("JPEG_QUALITY", "85"))

    if not gemini_key or gemini_key == "your-gemini-api-key-here":
        print("ERROR: Set GEMINI_API_KEY in .env", file=sys.stderr)
        sys.exit(1)
    if not args.dry_run and (not wp_password or wp_password == "your-application-password-here"):
        print("ERROR: Set WP_APP_PASSWORD in .env (or use --dry-run)", file=sys.stderr)
        sys.exit(1)

    album = args.album.resolve()
    if not album.is_dir():
        print(f"ERROR: Album directory not found: {album}", file=sys.stderr)
        sys.exit(1)

    images = find_images(album)
    if not images:
        print(f"ERROR: No supported images found in {album}", file=sys.stderr)
        sys.exit(1)

    post_title = args.title or album.name.replace("-", " ").replace("_", " ").title()
    print(f"Album:  {album}")
    print(f"Title:  {post_title}")
    print(f"Images: {len(images)}")
    print(f"Mode:   {'DRY RUN' if args.dry_run else 'LIVE'}")
    print()

    work_dir = Path(tempfile.mkdtemp(prefix="photo-pipeline-"))
    print(f"Working directory: {work_dir}")
    print()

    analysis_results = []
    all_tags = []

    for i, img in enumerate(images, 1):
        if i > 1 and args.delay > 0:
            time.sleep(args.delay)
        print(f"[{i}/{len(images)}] Analyzing {img.name} ...")
        try:
            metadata = gemini_analyze_image(img, gemini_key)
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            metadata = {
                "seo_filename": img.stem.lower().replace(" ", "-"),
                "alt_text": img.stem,
                "caption": "",
                "tags": [],
                "description": "",
            }

        seo_name = re.sub(r"[^a-z0-9-]", "", metadata["seo_filename"])[:60]
        if not seo_name:
            seo_name = f"image-{i:03d}"
        new_name = f"{seo_name}{img.suffix.lower()}"

        dst = work_dir / new_name
        counter = 2
        while dst.exists():
            dst = work_dir / f"{seo_name}-{counter}{img.suffix.lower()}"
            counter += 1

        print(f"  -> {dst.name}")
        print(f"     alt: {metadata.get('alt_text', '')[:80]}")
        print(f"     cap: {metadata.get('caption', '')[:80]}")
        print(f"     tags: {metadata.get('tags', [])}")

        if args.resize:
            sips_resize(img, dst, max_width, quality)
            dst_size = dst.stat().st_size
            orig_size = img.stat().st_size
            savings = ((orig_size - dst_size) / orig_size * 100) if orig_size > 0 else 0
            print(f"     size: {orig_size:,} -> {dst_size:,} ({savings:+.0f}%)")
        else:
            shutil.copy2(img, dst)
            print(f"     size: {img.stat().st_size:,} (copied as-is)")
        print()

        analysis_results.append({
            "original": str(img),
            "prepared": str(dst),
            "filename": dst.name,
            "metadata": metadata,
        })
        all_tags.append(metadata.get("tags", []))

    manifest_path = work_dir / "manifest.json"
    manifest_path.write_text(json.dumps(analysis_results, indent=2))
    print(f"Manifest written: {manifest_path}")

    if args.dry_run:
        print()
        print("DRY RUN complete. Files prepared in:")
        print(f"  {work_dir}")
        print()
        print("To upload, run again without --dry-run.")
        return

    print()
    print("Uploading to WordPress ...")
    media_items = []
    for i, item in enumerate(analysis_results, 1):
        prepared = Path(item["prepared"])
        meta = item["metadata"]
        print(f"  [{i}/{len(analysis_results)}] Uploading {prepared.name} ...")
        try:
            media = wp_upload_media(
                image_path=prepared,
                alt_text=meta.get("alt_text", ""),
                caption=meta.get("caption", ""),
                description=meta.get("description", ""),
                wp_url=wp_url,
                wp_user=wp_user,
                wp_password=wp_password,
            )
            media_items.append(media)
            print(f"       media_id={media['id']}  url={media['source_url']}")
        except Exception as e:
            print(f"       UPLOAD FAILED: {e}", file=sys.stderr)

    if not media_items:
        print("ERROR: No images were uploaded successfully.", file=sys.stderr)
        sys.exit(1)

    print()
    print("Creating draft post ...")
    try:
        post = wp_create_draft_post(
            title=post_title,
            media_items=media_items,
            tags=all_tags,
            wp_url=wp_url,
            wp_user=wp_user,
            wp_password=wp_password,
        )
        post_id = post["id"]
        edit_link = f"{wp_url}/wp-admin/post.php?post={post_id}&action=edit"
        preview_link = post.get("link", f"{wp_url}/?p={post_id}")

        print(f"  Post created: id={post_id}")
        print(f"  Edit:    {edit_link}")
        print(f"  Preview: {preview_link}")
    except Exception as e:
        print(f"  POST CREATION FAILED: {e}", file=sys.stderr)
        sys.exit(1)

    summary = {
        "post_id": post_id,
        "post_title": post_title,
        "edit_url": edit_link,
        "preview_url": preview_link,
        "images_uploaded": len(media_items),
        "images_analyzed": len(analysis_results),
        "working_directory": str(work_dir),
    }
    summary_path = work_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))

    print()
    print("Done.")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
