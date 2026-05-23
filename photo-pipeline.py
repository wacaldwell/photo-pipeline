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
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}

# Override the default urllib User-Agent — Cloudflare's bot-fight mode blocks
# "Python-urllib/*" (error code 1010) on sites that have it enabled, which
# breaks every WP REST call. A normal-looking UA is enough to pass.
_opener = urllib.request.build_opener()
_opener.addheaders = [("User-Agent", "cmbpix-photo-pipeline/1.0 (+https://github.com/anthropics)")]
urllib.request.install_opener(_opener)


def fetch_aws_secret(secret_id: str, region: str = "us-east-1") -> dict:
    """Fetch a JSON secret from AWS Secrets Manager via the aws CLI.

    Returns the parsed JSON as a dict. Shells out rather than importing boto3
    to keep the pipeline single-file and stdlib-only.
    """
    try:
        result = subprocess.run(
            [
                "aws", "secretsmanager", "get-secret-value",
                "--secret-id", secret_id,
                "--region", region,
                "--query", "SecretString",
                "--output", "text",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
    except FileNotFoundError:
        print("ERROR: aws CLI not found. Install it or drop --secret.", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: aws secretsmanager returned {e.returncode}: {e.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


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


def telegram_notify(
    token: str,
    chat_id: int | str,
    thread_id: int,
    title: str,
    edit_url: str,
    image_count: int,
    timeout: float = 10.0,
) -> None:
    """Best-effort Telegram notification. Never raises.

    Posts a plain-text sendMessage to the given chat + topic. On any HTTP or
    transport error, prints a warning to stderr and returns. The pipeline's
    exit code reflects WP upload success, not notification success.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    text = f"📸 New gallery: {title} ({image_count} photos)\n✏️ {edit_url}"
    body = json.dumps({
        "chat_id": chat_id,
        "message_thread_id": thread_id,
        "text": text,
        "disable_web_page_preview": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            pass
    except urllib.error.HTTPError as e:
        snippet = e.read()[:200].decode("utf-8", errors="replace") if e.fp else ""
        print(
            f"WARN: Telegram notify failed: HTTP {e.code} {e.reason} {snippet}",
            file=sys.stderr,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"WARN: Telegram notify failed: {e}", file=sys.stderr)


def gemini_generate_json(prompt: str, api_key: str, max_output_tokens: int = 2048) -> dict:
    """Send a text-only prompt to Gemini and parse the JSON response."""
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": max_output_tokens,
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
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    text = result["candidates"][0]["content"]["parts"][0]["text"].strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()
    return json.loads(text)


def resolve_last30days_python() -> str | None:
    """Return a Python 3.12+ interpreter for last30days, if available."""
    candidates = [
        "python3.13",
        "python3.12",
        "python3",
        "python",
    ]
    for name in candidates:
        path = shutil.which(name)
        if not path:
            continue
        try:
            result = subprocess.run(
                [path, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
        except (subprocess.SubprocessError, OSError):
            continue
        version = (result.stdout or "").strip()
        if version in {"3.12", "3.13", "3.14"}:
            return path
    return None


def resolve_last30days_command(explicit_script: str | None, env: dict[str, str]) -> list[str] | None:
    """Resolve a usable last30days command, or None when unavailable."""
    configured = (
        explicit_script
        or os.environ.get("LAST30DAYS_SCRIPT")
        or env.get("LAST30DAYS_SCRIPT")
    )
    if configured:
        path = Path(configured).expanduser()
        if path.suffix == ".py":
            py = resolve_last30days_python()
            return [py, str(path)] if py else None
        return [str(path)]

    for binary_name in ("last30days", "last30"):
        path_cmd = shutil.which(binary_name)
        if path_cmd:
            return [path_cmd]

    candidates = [
        Path.home() / ".openclaw" / "skills" / "last30days" / "scripts" / "last30days.py",
        Path.home() / ".codex" / "skills" / "last30days" / "scripts" / "last30days.py",
        Path.home() / ".claude" / "plugins" / "cache" / "last30days-skill" / "last30days",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            nested = sorted(candidate.glob("*/skills/last30days/scripts/last30days.py"))
            flat = sorted(candidate.glob("*/SKILL.md"))
            if nested:
                py = resolve_last30days_python()
                return [py, str(nested[-1])] if py else None
            if flat:
                script = flat[-1].parent / "scripts" / "last30days.py"
                if script.exists():
                    py = resolve_last30days_python()
                    return [py, str(script)] if py else None
        if candidate.exists():
            py = resolve_last30days_python()
            return [py, str(candidate)] if py else None

    return None


def derive_research_topic(title: str, analysis_results: list[dict]) -> str:
    """Build a research query from the gallery title and top Gemini tags."""
    tag_counts: dict[str, int] = {}
    for item in analysis_results:
        for tag in item.get("metadata", {}).get("tags", []):
            norm = str(tag).strip().lower()
            if norm:
                tag_counts[norm] = tag_counts.get(norm, 0) + 1

    top_tags = [tag for tag, _count in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]
    if top_tags:
        return f"{title} photography trends {' '.join(top_tags)}"
    return f"{title} photography trends"


def run_social_research(
    topic: str,
    command: list[str] | None,
    days: int,
    sources: str,
    mode: str,
    timeout: int,
) -> dict:
    """Run last30days research and return a structured result."""
    if not command:
        return {
            "enabled": False,
            "topic": topic,
            "status": "unavailable",
            "reason": "last30days command not found",
        }

    cmd = list(command) + [topic, "--emit", "compact", f"--search={sources}", f"--days={days}"]
    if mode == "quick":
        cmd.append("--quick")
    elif mode == "deep":
        cmd.append("--deep")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return {
            "enabled": True,
            "topic": topic,
            "status": "unavailable",
            "reason": "last30days executable missing",
            "command": cmd,
        }
    except subprocess.TimeoutExpired:
        return {
            "enabled": True,
            "topic": topic,
            "status": "timeout",
            "reason": f"research exceeded {timeout}s timeout",
            "command": cmd,
        }

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    return {
        "enabled": True,
        "topic": topic,
        "status": "ok" if result.returncode == 0 else "failed",
        "command": cmd,
        "returncode": result.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


def build_editorial_prompt(
    title: str,
    analysis_results: list[dict],
    research_result: dict | None,
) -> str:
    """Assemble the editorial-generation prompt from gallery metadata."""
    images = []
    for item in analysis_results[:12]:
        meta = item.get("metadata", {})
        images.append({
            "filename": item.get("filename"),
            "alt_text": meta.get("alt_text", ""),
            "caption": meta.get("caption", ""),
            "tags": meta.get("tags", []),
            "description": meta.get("description", ""),
        })

    research_text = ""
    if research_result and research_result.get("status") == "ok":
        research_text = research_result.get("stdout", "")
    elif research_result and research_result.get("reason"):
        research_text = f"Research unavailable: {research_result['reason']}"

    payload = {
        "title": title,
        "images": images,
        "research_topic": research_result.get("topic") if research_result else "",
        "research_output": research_text[:12000],
    }
    return (
        "You are writing editorial copy for a photo gallery post.\n"
        "Use the gallery title, image metadata, and recent social/community research to create copy that fits the gallery without inventing facts.\n"
        "Return JSON with exactly these keys:\n"
        '- "intro_html": 2 short HTML paragraphs using only <p>, with a strong opening and no markdown\n'
        '- "outro_html": 1 short HTML paragraph using only <p>\n'
        '- "social_caption": 1 punchy caption for a social post, max 280 characters\n'
        '- "hashtags": array of 3-8 lowercase hashtags without # prefixes\n'
        '- "angle": 1 sentence summarizing the post angle derived from the research\n'
        "Keep the tone contemporary and specific to the photos.\n"
        "If the research is thin or unavailable, rely on the image metadata and do not mention the missing research.\n"
        "Input JSON:\n"
        f"{json.dumps(payload, ensure_ascii=True)}"
    )


def generate_gallery_editorial(
    title: str,
    analysis_results: list[dict],
    research_result: dict | None,
    api_key: str,
) -> dict:
    """Generate editorial and social copy for the gallery."""
    prompt = build_editorial_prompt(title, analysis_results, research_result)
    return gemini_generate_json(prompt, api_key, max_output_tokens=1536)


def build_post_content(intro_html: str, gallery_block: str, outro_html: str) -> str:
    """Compose the final post body with optional intro/outro around the gallery."""
    parts = [part.strip() for part in (intro_html, gallery_block, outro_html) if part and part.strip()]
    return "\n\n".join(parts)


def find_images(album_dir: Path) -> list[Path]:
    """Find all supported image files in the album directory."""
    images = []
    for f in sorted(album_dir.iterdir()):
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS:
            images.append(f)
    return images


def _image_inline_part(image_path: Path) -> dict:
    """Build a Gemini inline_data part (base64 image + mime type) for a file."""
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
    return {"inline_data": {"mime_type": mime_type, "data": b64_image}}


def _gemini_vision_request(payload: dict, api_key: str,
                           model: str = "gemini-2.0-flash",
                           timeout: int = 60) -> dict:
    """POST a generateContent payload with 429 backoff. Return the parsed response JSON."""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    data = json.dumps(payload).encode("utf-8")
    max_retries = 7
    for attempt in range(max_retries):
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code == 429 and attempt < max_retries - 1:
                # Prefer server's Retry-After; otherwise exponential backoff capped
                # at 60s. Jitter avoids herd-retry on concurrent runs.
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if retry_after and retry_after.strip().isdigit():
                    wait = min(60.0, float(retry_after.strip()))
                else:
                    wait = min(60.0, 2 ** (attempt + 1) + 1)
                wait += random.uniform(0, 1.5)
                print(f"  Rate limited (429), waiting {wait:.1f}s (attempt {attempt + 1}/{max_retries}) ...", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"  ERROR: Gemini API returned {e.code}: {body[:300]}", file=sys.stderr)
            raise
    raise RuntimeError("Gemini request exhausted retries")


def _gemini_response_json(result: dict) -> dict:
    """Extract the model's text from a Gemini response and parse it as JSON, tolerating ``` fences."""
    text = result["candidates"][0]["content"]["parts"][0]["text"].strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()
    return json.loads(text)


# PPA "12 Elements of a Merit Image" — the rubric the "Photo Critique" GPT uses.
# (json_key, Display Label, one-line definition)
RATING_ELEMENTS = [
    ("impact",              "Impact",              "the sense one gets upon viewing; emotional or intellectual response"),
    ("technical_excellence","Technical Excellence","print/image quality: exposure, sharpness, noise, artifacts, retouching"),
    ("creativity",          "Creativity",          "original, fresh, imaginative expression of the subject"),
    ("composition",         "Composition",         "arrangement of elements; balance, flow, use of the frame"),
    ("presentation",        "Presentation",        "how the image is finished/framed; nothing distracts from it"),
    ("color_harmony",       "Color Harmony",       "colors work together to support the image (or effective monochrome)"),
    ("interest_areas",      "Interest Areas",      "a clear center of interest; the eye is led there, not distracted"),
    ("light",               "Light",               "quality and use of lighting to shape the subject and mood"),
    ("technique",           "Technique",           "skillful use of tools/approach (DOF, motion, lens choice, etc.)"),
    ("the_story",           "The Story",           "the image tells a story or evokes a narrative/feeling"),
    ("style",               "Style",               "a distinctive, intentional visual approach"),
    ("subject_matter",      "Subject Matter",      "appropriateness and appeal of the chosen subject"),
]
RATING_KEYS = [key for key, _label, _defn in RATING_ELEMENTS]

# Real "Photo Critique" outputs (operator-provided), used as text few-shot anchors
# to pin Gemini's numeric scale and narrative voice. overall = mean(12), ~+0.1.
RATING_FEW_SHOT = [
    {
        "desc": "Wakeboarder mid-action behind a boat: wake, spray, diagonal tow rope, wide lake, big sky, shot in strong midday sun (shadowed face).",
        "scores": {"impact": 8, "technical_excellence": 7, "creativity": 7, "composition": 7,
                   "presentation": 7, "color_harmony": 8, "interest_areas": 7, "light": 6,
                   "technique": 8, "the_story": 8, "style": 7, "subject_matter": 8},
        "overall": 7.3,
    },
    {
        "desc": "Golden Gate Bridge through soft out-of-focus foreground foliage; longer-lens compression stacking bridge, hills, water, sky; tower slightly cropped at top.",
        "scores": {"impact": 8, "technical_excellence": 7, "creativity": 8, "composition": 7,
                   "presentation": 7, "color_harmony": 8, "interest_areas": 7, "light": 7,
                   "technique": 8, "the_story": 8, "style": 8, "subject_matter": 9},
        "overall": 7.8,
    },
    {
        "desc": "Child playing in a backyard water spray with a colorful pinwheel; suspended droplets, warm golden light, shallow depth of field, genuine expression.",
        "scores": {"impact": 8, "technical_excellence": 8, "creativity": 8, "composition": 7,
                   "presentation": 8, "color_harmony": 8, "interest_areas": 8, "light": 7,
                   "technique": 8, "the_story": 9, "style": 8, "subject_matter": 9},
        "overall": 8.1,
    },
    {
        "desc": "Tight winter portrait of a child with sharp expressive eyes, blue coat, olive hat, snow crystals, soft high-key snowy background; crop tight above the hat.",
        "scores": {"impact": 9, "technical_excellence": 8, "creativity": 8, "composition": 7,
                   "presentation": 8, "color_harmony": 9, "interest_areas": 8, "light": 8,
                   "technique": 8, "the_story": 8, "style": 8, "subject_matter": 9},
        "overall": 8.3,
    },
]


def build_rating_prompt(few_shot: list[dict]) -> str:
    """Build the Gemini rating prompt: PPA rubric + JSON schema + text few-shot anchors."""
    lines = [
        "You are an expert photography judge using the PPA \"12 Elements of a Merit "
        "Image\". Score the photograph on each element from 1 to 10 (integers).",
        "",
        "Elements:",
    ]
    for _key, label, defn in RATING_ELEMENTS:
        lines.append(f"- {label}: {defn}")
    lines += [
        "",
        "Return ONLY a JSON object (no markdown fences, no extra text) with exactly:",
        '{"scores": {' + ", ".join(f'"{k}": <1-10>' for k in RATING_KEYS) + "},",
        '  "narrative": {"impression": "...", "strongest": "...", "refine": "...",'
        ' "improvement": "...", "titles": ["...", "..."]}}',
        "",
        "Calibration examples (subject -> scores -> overall = mean of the twelve):",
    ]
    for ex in few_shot:
        score_str = ", ".join(f"{k}:{ex['scores'][k]}" for k in RATING_KEYS)
        lines.append(f"- {ex['desc']}")
        lines.append(f"    scores: {score_str}  (overall {ex['overall']})")
    lines += [
        "",
        "Judge the provided image on the same scale and in the same voice.",
    ]
    return "\n".join(lines)


def gemini_rate_image(image_path: Path, api_key: str,
                      few_shot: list[dict] | None = None) -> dict:
    """Rate one photo against the PPA 12 Elements. Returns {"scores": {...}, "narrative": {...}}."""
    if few_shot is None:
        few_shot = RATING_FEW_SHOT
    prompt = build_rating_prompt(few_shot)
    payload = {
        "contents": [
            {"parts": [{"text": prompt}, _image_inline_part(image_path)]}
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 1500,
        },
    }
    result = _gemini_vision_request(payload, api_key)
    data = _gemini_response_json(result)
    raw_scores = data.get("scores", {})
    scores = {k: int(raw_scores[k]) for k in RATING_KEYS}  # KeyError/ValueError -> caller fail-open
    narrative = data.get("narrative", {})
    return {"scores": scores, "narrative": narrative}


def compute_overall(scores: dict) -> float:
    """Overall rating = mean of the 12 element scores, rounded to 1 decimal."""
    vals = [scores[k] for k in RATING_KEYS]
    return round(sum(vals) / len(vals), 1)


def make_rating_entry(original: Path, rating: dict | None,
                      min_rating: float | None) -> dict:
    """Build a ratings.json entry with a kept/culled/unrated decision.

    Fail-open: a None rating (API failure) is 'unrated' and kept for manual review.
    No cutoff (rate-only mode) keeps everything.
    """
    if rating is None:
        return {"original": str(original), "scores": None, "overall": None,
                "decision": "unrated", "narrative": None}
    overall = compute_overall(rating["scores"])
    if min_rating is None:
        decision = "kept"
    else:
        decision = "kept" if overall >= min_rating else "culled"
    return {"original": str(original), "scores": rating["scores"],
            "overall": overall, "decision": decision,
            "narrative": rating.get("narrative")}


def resolve_rating_flags(rate: bool, min_rating: float | None) -> tuple[bool, float | None]:
    """--min-rating implies --rate. Returns (rating_enabled, cutoff)."""
    enabled = bool(rate or min_rating is not None)
    return enabled, min_rating


def gemini_analyze_image(image_path: Path, api_key: str) -> dict:
    """Send an image to Gemini and get SEO metadata back."""
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
            {"parts": [{"text": prompt}, _image_inline_part(image_path)]}
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 512,
        },
    }
    result = _gemini_vision_request(payload, api_key)
    return _gemini_response_json(result)


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


# ---------------------------------------------------------------------------
# Image validation (pre-Gemini quality gate)
# ---------------------------------------------------------------------------
# Probes each image for dimensions / format / colorspace via (in order):
#   1. ImageMagick `identify` (most portable, most detailed)
#   2. macOS `sips`
#   3. Minimal JPEG/PNG header reader from Python stdlib (last resort — gives
#      dimensions only, no colorspace)
#
# Rejected images skip both Gemini analysis and WordPress upload; warn-level
# images continue but are flagged in manifest.json.


def _probe_identify(path: Path) -> dict:
    """Use ImageMagick `identify`. Returns {} on any failure."""
    try:
        result = subprocess.run(
            ["identify", "-format", "%w %h %m %[colorspace]", str(path)],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    parts = result.stdout.strip().split(None, 3)
    if len(parts) < 3:
        return {}
    try:
        return {
            "width": int(parts[0]),
            "height": int(parts[1]),
            "format": parts[2].lower(),
            "colorspace": parts[3].lower() if len(parts) > 3 else "unknown",
        }
    except ValueError:
        return {}


def _probe_sips(path: Path) -> dict:
    """Use macOS `sips`. Returns {} on any failure."""
    try:
        result = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight",
             "-g", "format", "-g", "space", str(path)],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    w = re.search(r"pixelWidth:\s*(\d+)", result.stdout)
    h = re.search(r"pixelHeight:\s*(\d+)", result.stdout)
    f = re.search(r"format:\s*(\S+)", result.stdout)
    s = re.search(r"space:\s*(\S+)", result.stdout)
    if not (w and h):
        return {}
    return {
        "width": int(w.group(1)),
        "height": int(h.group(1)),
        "format": f.group(1).lower() if f else path.suffix.lstrip(".").lower(),
        "colorspace": s.group(1).lower() if s else "unknown",
    }


def _probe_stdlib(path: Path) -> dict:
    """Minimal JPEG/PNG header reader. Dimensions only; colorspace unknown."""
    try:
        with path.open("rb") as f:
            head = f.read(24)
            if len(head) < 24:
                return {}
            # PNG: 8-byte magic, then IHDR (width@16, height@20)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                w, h = struct.unpack(">II", head[16:24])
                return {"width": w, "height": h, "format": "png", "colorspace": "unknown"}
            # JPEG: scan segments for SOF0/1/2/3 markers
            if head[:2] == b"\xff\xd8":
                f.seek(2)
                while True:
                    b = f.read(1)
                    while b and b != b"\xff":
                        b = f.read(1)
                    while b == b"\xff":
                        b = f.read(1)
                    if not b:
                        break
                    marker = b[0]
                    if marker in (0xC0, 0xC1, 0xC2, 0xC3):  # SOF markers
                        f.read(3)  # length (2) + precision (1)
                        h, w = struct.unpack(">HH", f.read(4))
                        return {"width": w, "height": h, "format": "jpeg", "colorspace": "unknown"}
                    length_bytes = f.read(2)
                    if len(length_bytes) < 2:
                        break
                    length = struct.unpack(">H", length_bytes)[0]
                    f.read(length - 2)
    except (OSError, struct.error):
        pass
    return {}


def probe_image(path: Path) -> dict:
    """Return {width, height, format, colorspace} via first working backend."""
    for probe in (_probe_identify, _probe_sips, _probe_stdlib):
        result = probe(path)
        if result.get("width") and result.get("height"):
            return result
    return {}


def validate_image(
    path: Path,
    min_width_warn: int,
    min_width_hard: int,
    max_size_mb_warn: float,
    max_size_mb_hard: float,
    aspect_min: float,
    aspect_max: float,
    require_srgb: bool,
    strict: bool,
) -> dict:
    """Validate an image against policy. Returns a dict with:
      status: "ok" | "warn" | "reject"
      issues: list of "[level] message" strings
      file_size, width, height, aspect, format, colorspace (best-effort)
    """
    issues: list[str] = []
    worst = "ok"

    def note(level: str, msg: str) -> None:
        nonlocal worst
        issues.append(f"[{level}] {msg}")
        effective = "reject" if (level == "warn" and strict) else level
        rank = {"ok": 0, "warn": 1, "reject": 2}
        if rank[effective] > rank[worst]:
            worst = effective

    try:
        size_bytes = path.stat().st_size
    except OSError as e:
        return {"status": "reject", "issues": [f"[reject] stat failed: {e}"],
                "file_size": 0, "width": None, "height": None,
                "aspect": None, "format": None, "colorspace": None}

    size_mb = size_bytes / 1_048_576
    if size_bytes == 0:
        note("reject", "zero-byte file")
    elif size_mb >= max_size_mb_hard:
        note("reject", f"file size {size_mb:.1f} MB >= {max_size_mb_hard} MB (hard limit)")
    elif size_mb >= max_size_mb_warn:
        note("warn", f"file size {size_mb:.1f} MB >= {max_size_mb_warn} MB (consider resizing)")

    probe = probe_image(path)
    w = probe.get("width")
    h = probe.get("height")
    if not w or not h:
        note("reject", "could not read image dimensions (corrupted or unsupported)")
        return {"status": "reject", "issues": issues, "file_size": size_bytes,
                "width": None, "height": None, "aspect": None,
                "format": None, "colorspace": None}

    aspect = w / h
    if w < min_width_hard:
        note("reject", f"width {w}px < {min_width_hard}px (hard min)")
    elif w < min_width_warn:
        note("warn", f"width {w}px < {min_width_warn}px (soft min)")

    if aspect < aspect_min or aspect > aspect_max:
        note("reject", f"aspect ratio {aspect:.2f} outside [{aspect_min}, {aspect_max}]")

    if require_srgb:
        cs = (probe.get("colorspace") or "").lower()
        if "srgb" not in cs and cs not in ("rgb", "unknown"):
            note("warn", f"colorspace '{cs}' is not sRGB")

    return {
        "status": worst,
        "issues": issues,
        "file_size": size_bytes,
        "width": w,
        "height": h,
        "aspect": round(aspect, 3),
        "format": probe.get("format"),
        "colorspace": probe.get("colorspace"),
    }


def parse_aspect_range(s: str) -> tuple[float, float]:
    """Parse 'LOW:HIGH' to (low, high). Used by --aspect-range."""
    try:
        lo, hi = s.split(":", 1)
        low = float(lo)
        high = float(hi)
        if low <= 0 or high <= low:
            raise ValueError
        return low, high
    except (ValueError, AttributeError):
        raise argparse.ArgumentTypeError(
            f"Invalid --aspect-range '{s}'. Expected 'LOW:HIGH' with 0 < LOW < HIGH (e.g. '0.4:2.5')."
        )


def wp_auth_header(wp_user: str, wp_password: str) -> str:
    """Build a Basic auth header value."""
    return "Basic " + base64.b64encode(f"{wp_user}:{wp_password}".encode()).decode()


def wp_rest_request(
    method: str,
    url: str,
    auth: str,
    body: dict | None = None,
    timeout: int = 30,
) -> dict:
    """Generic WP REST JSON request. Returns parsed response dict (or {} on empty body)."""
    headers = {"Authorization": auth}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def wp_upload_media(
    image_path: Path,
    alt_text: str,
    caption: str,
    description: str,
    wp_url: str,
    wp_user: str,
    wp_password: str,
    attach_to: int | None = None,
) -> dict:
    """Upload an image to WordPress via REST API, return media object.

    If attach_to is provided, the media is attached to that post (post_parent set)
    via the follow-up update call.
    """
    upload_url = f"{wp_url}/wp-json/wp/v2/media"

    image_bytes = image_path.read_bytes()
    mime_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }
    content_type = mime_map.get(image_path.suffix.lower(), "image/jpeg")

    auth = wp_auth_header(wp_user, wp_password)

    req = urllib.request.Request(
        upload_url,
        data=image_bytes,
        headers={
            "Content-Type": content_type,
            "Content-Disposition": f'attachment; filename="{image_path.name}"',
            "Authorization": auth,
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=60) as resp:
        media = json.loads(resp.read().decode("utf-8"))

    media_id = media["id"]
    update_body = {
        "alt_text": alt_text,
        "caption": caption,
        "description": description,
    }
    if attach_to:
        update_body["post"] = attach_to

    media = wp_rest_request(
        "POST",
        f"{wp_url}/wp-json/wp/v2/media/{media_id}",
        auth,
        body=update_body,
        timeout=30,
    )

    return media


def wp_get_term_by_slug(
    taxonomy: str,
    slug: str,
    wp_url: str,
    auth: str,
) -> dict | None:
    """Look up a taxonomy term by slug. Returns term dict or None if not found."""
    search_url = (
        f"{wp_url}/wp-json/wp/v2/{taxonomy}"
        f"?slug={urllib.request.quote(slug)}"
    )
    req = urllib.request.Request(search_url, headers={"Authorization": auth})
    with urllib.request.urlopen(req, timeout=15) as resp:
        results = json.loads(resp.read().decode("utf-8"))
    return results[0] if results else None


def wp_create_term(
    taxonomy: str,
    slug: str,
    wp_url: str,
    auth: str,
) -> dict:
    """Create a taxonomy term. Name is derived from slug (title-cased)."""
    name = slug.replace("-", " ").replace("_", " ").title()
    return wp_rest_request(
        "POST",
        f"{wp_url}/wp-json/wp/v2/{taxonomy}",
        auth,
        body={"name": name, "slug": slug},
    )


def wp_create_cpt_post(
    cpt: str,
    title: str,
    status: str,
    wp_url: str,
    auth: str,
    content: str = "",
    menu_order: int | None = None,
) -> dict:
    """Create a post in a CPT. Returns post dict."""
    body = {"title": title, "status": status, "content": content}
    if menu_order is not None:
        body["menu_order"] = menu_order
    return wp_rest_request(
        "POST",
        f"{wp_url}/wp-json/wp/v2/{cpt}",
        auth,
        body=body,
    )


def wp_update_cpt_post(
    cpt: str,
    post_id: int,
    fields: dict,
    wp_url: str,
    auth: str,
) -> dict:
    """PATCH-style update via REST POST (WP REST accepts POST for updates)."""
    return wp_rest_request(
        "POST",
        f"{wp_url}/wp-json/wp/v2/{cpt}/{post_id}",
        auth,
        body=fields,
    )


# ---------------------------------------------------------------------------
# Modula gallery creation
# ---------------------------------------------------------------------------
# Modula stores its gallery content in two post-meta keys exposed via REST:
#   - modulaSettings  (serialized array of display options)
#   - modulaImages    (serialized list of image objects)
# The plugin registers both as REST fields on the `modula-gallery` CPT, so we
# can create a fully-populated gallery in one POST.

MODULA_DEFAULT_SETTINGS = {
    "type": "creative-gallery",
    "lightbox": "fancybox",
    "gutter": 10,
    "height": 800,
    "captionColor": "rgba(255,255,255,1)",
    "enableTwitter": 0,
    "enableFacebook": 0,
}


def build_modula_images(media_items: list[dict], analysis_results: list[dict]) -> list[dict]:
    """Build the modulaImages payload from uploaded media + Gemini metadata.

    media_items is the REST response list from /wp/v2/media. analysis_results
    is the pipeline's per-image analysis (filename, alt_text, caption, ...).
    They must be aligned index-by-index.
    """
    images = []
    for i, media in enumerate(media_items):
        meta = analysis_results[i]["metadata"] if i < len(analysis_results) else {}
        alt = meta.get("alt_text") or media.get("alt_text", "") or ""
        title = meta.get("seo_filename") or media.get("title", {}).get("rendered", "") or ""
        description = meta.get("caption") or ""
        images.append({
            "id": media["id"],
            "alt": alt,
            "title": title,
            "description": description,
            "link": "",
            "target": "",
            "halign": "center",
            "valign": "middle",
            "width": 2,
            "height": 2,
            "togglelightbox": "",
            "hide_title": "",
        })
    return images


def wp_create_modula_gallery(
    title: str,
    status: str,
    modula_images: list[dict],
    wp_url: str,
    auth: str,
    settings_overrides: dict | None = None,
    menu_order: int | None = None,
) -> dict:
    """Create a Modula gallery in one REST call. Returns the post dict."""
    settings = dict(MODULA_DEFAULT_SETTINGS)
    if settings_overrides:
        settings.update(settings_overrides)

    body = {
        "title": title,
        "status": status,
        "modulaSettings": settings,
        "modulaImages": modula_images,
    }
    if menu_order is not None:
        body["menu_order"] = menu_order

    return wp_rest_request(
        "POST",
        f"{wp_url}/wp-json/wp/v2/modula-gallery",
        auth,
        body=body,
    )


def wp_create_draft_post(
    title: str,
    media_items: list[dict],
    tags: list[str],
    wp_url: str,
    wp_user: str,
    wp_password: str,
    intro_html: str = "",
    outro_html: str = "",
    status: str = "draft",
) -> dict:
    """Create a draft blog post with a gallery block referencing uploaded media."""
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
    content = build_post_content(intro_html, gallery_block, outro_html)

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
        "content": content,
        "status": status,
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
    parser.add_argument("--cpt", type=str, default=None,
        help="Target a custom post type (e.g. 'gallery') instead of standard posts. "
             "Images are attached to the post as children; no inline gallery block is written.")
    parser.add_argument("--cpt-taxonomy", type=str, default=None,
        help="Taxonomy slug used with --category (e.g. 'gallery_category').")
    parser.add_argument("--category", type=str, default=None,
        help="Taxonomy term slug to assign. Requires --cpt-taxonomy. Must exist "
             "in WP unless --create-category is also passed.")
    parser.add_argument("--create-category", action="store_true",
        help="Opt-in: create the --category term if it doesn't exist. "
             "SEO best practice is NOT to auto-create terms; prefer curating them in wp-admin.")
    parser.add_argument("--featured", action="store_true",
        help="Set _cmbpix_featured=1 on the created post (cmbpix gallery CPT).")
    parser.add_argument("--menu-order", type=int, default=None,
        help="menu_order for the new post (affects featured-grid ordering).")
    parser.add_argument("--status", type=str, default="draft",
        choices=["draft", "publish", "pending", "private"],
        help="Post status for the created post (default: draft).")
    parser.add_argument("--secret", type=str, default=None,
        help="AWS Secrets Manager secret id holding pipeline credentials. "
             "Expected JSON: {gemini_api_key, <target>: {url, user, app_password}, ...}")
    parser.add_argument("--target", type=str, default=None,
        help="Profile key inside the secret to use for WP credentials "
             "(e.g. 'cmbpix_local', 'cmbpix_prod').")
    parser.add_argument("--aws-region", type=str, default="us-east-1",
        help="AWS region for --secret (default: us-east-1).")
    parser.add_argument("--min-width", type=int, default=1200,
        help="Soft-warn min image width in pixels (default: 1200).")
    parser.add_argument("--min-width-hard", type=int, default=800,
        help="Hard-reject min image width in pixels (default: 800).")
    parser.add_argument("--max-file-size-mb", type=float, default=15.0,
        help="Hard-reject file size >= this (MB). Default: 15.")
    parser.add_argument("--max-file-size-warn-mb", type=float, default=5.0,
        help="Soft-warn file size >= this (MB). Default: 5.")
    parser.add_argument("--aspect-range", type=str, default="0.4:2.5",
        help="Accepted aspect ratio range LOW:HIGH (default: 0.4:2.5). "
             "Rejects images outside this range.")
    parser.add_argument("--require-srgb", action="store_true",
        help="Warn if image colorspace is not sRGB (off by default; brittle "
             "across probe backends).")
    parser.add_argument("--strict", action="store_true",
        help="Promote all validation warnings to hard rejects.")
    parser.add_argument("--no-validate", action="store_true",
        help="Skip the validation pass entirely (not recommended).")
    parser.add_argument("--tele", action="store_true",
        help="On successful WP publish, send a Telegram message with the "
             "wp-admin edit URL to the content/creative topic. Requires "
             "telegram_bot_token, telegram_chat_id, and "
             "telegram_content_creative_thread_id in the AWS secret (or the "
             "corresponding TELEGRAM_* env vars). No-op with --dry-run.")
    parser.add_argument("--rate", action="store_true",
        help="Rate each image 1-10 against the PPA 12 Elements before SEO analysis; "
             "writes ratings.json. Culls nothing on its own.")
    parser.add_argument("--min-rating", type=float, default=None,
        help="Cull cutoff on the overall (mean) rating; implies --rate. "
             "Images scoring below this are excluded from upload (source files untouched).")
    parser.add_argument("--rate-delay", type=float, default=None,
        help="Seconds between rating API calls (default: same as --delay).")
    parser.add_argument("--social-research", action="store_true",
        help="Run last30days social/community research for the gallery topic "
             "and generate matching editorial copy for the WP post body.")
    parser.add_argument("--social-topic", type=str, default=None,
        help="Explicit last30days research topic. Default: derived from title "
             "and Gemini tags.")
    parser.add_argument("--social-sources", type=str, default="reddit",
        help="Comma-separated last30days sources (default: reddit).")
    parser.add_argument("--social-days", type=int, default=30,
        help="Lookback window for last30days research (default: 30).")
    parser.add_argument("--social-mode", type=str, default="quick",
        choices=["quick", "deep"],
        help="Research depth for last30days (default: quick).")
    parser.add_argument("--social-timeout", type=int, default=240,
        help="Timeout in seconds for the last30days research step (default: 240).")
    parser.add_argument("--last30days-script", type=str, default=None,
        help="Path to the last30days executable or script. Overrides "
             "LAST30DAYS_SCRIPT and auto-detection.")
    args = parser.parse_args()

    # Defensive default: cmbpix_* targets always mean a Modula gallery. The
    # cmbpix theme is Modula-oriented and standard WP posts render poorly
    # against it. Agents occasionally forget the flag — auto-apply it unless
    # the caller explicitly picked a CPT.
    if args.target and args.target.startswith("cmbpix_") and args.cpt is None:
        args.cpt = "modula-gallery"
        print(
            "Note: --target cmbpix_* → defaulting --cpt modula-gallery.",
            file=sys.stderr,
        )

    script_dir = Path(__file__).resolve().parent
    env = load_env(script_dir / ".env")

    # Resolution order for each value: CLI arg > env var > AWS secret > .env file > default.
    secret = fetch_aws_secret(args.secret, args.aws_region) if args.secret else {}
    target_profile = secret.get(args.target, {}) if args.target else {}

    def pick(cli_val, env_key, secret_key, profile_key, default=""):
        if cli_val:
            return cli_val
        if os.environ.get(env_key):
            return os.environ[env_key]
        if secret_key and secret.get(secret_key):
            return secret[secret_key]
        if profile_key and target_profile.get(profile_key):
            return target_profile[profile_key]
        return env.get(env_key, default)

    gemini_key  = pick(None,              "GEMINI_API_KEY",  "gemini_api_key", None)
    wp_url      = pick(args.wp_url,       "WP_URL",          None,             "url",          "http://mvd-clawbase:8087")
    wp_user     = pick(args.wp_user,      "WP_USER",         None,             "user",         "admin")
    wp_password = pick(args.wp_password,  "WP_APP_PASSWORD", None,             "app_password", "")
    tg_token   = pick(None, "TELEGRAM_BOT_TOKEN",                     "telegram_bot_token",                     None)
    tg_chat    = pick(None, "TELEGRAM_CHAT_ID",                       "telegram_chat_id",                       None)
    tg_thread  = pick(None, "TELEGRAM_CONTENT_CREATIVE_THREAD_ID",    "telegram_content_creative_thread_id",    None)
    max_width = args.max_width or int(env.get("MAX_WIDTH", "1920"))
    quality = args.quality or int(env.get("JPEG_QUALITY", "85"))
    last30days_command = resolve_last30days_command(args.last30days_script, env)

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

    # -----------------------------------------------------------------------
    # Validation pass — runs BEFORE Gemini so rejected images don't burn
    # credits. Warn-level images continue; reject-level are dropped from the
    # processing set.
    # -----------------------------------------------------------------------
    aspect_min, aspect_max = parse_aspect_range(args.aspect_range)
    validation_map: dict[Path, dict] = {}

    if args.no_validate:
        for img in images:
            validation_map[img] = {"status": "ok", "issues": ["validation skipped (--no-validate)"]}
    else:
        print(f"Validating {len(images)} image(s) ...")
        n_ok = n_warn = n_reject = 0
        for img in images:
            v = validate_image(
                img,
                min_width_warn=args.min_width,
                min_width_hard=args.min_width_hard,
                max_size_mb_warn=args.max_file_size_warn_mb,
                max_size_mb_hard=args.max_file_size_mb,
                aspect_min=aspect_min,
                aspect_max=aspect_max,
                require_srgb=args.require_srgb,
                strict=args.strict,
            )
            validation_map[img] = v
            if v["status"] == "ok":
                n_ok += 1
            elif v["status"] == "warn":
                n_warn += 1
                print(f"  WARN   {img.name}: {'; '.join(v['issues'])}", file=sys.stderr)
            else:
                n_reject += 1
                print(f"  REJECT {img.name}: {'; '.join(v['issues'])}", file=sys.stderr)
        print(f"Validation: {n_ok} ok, {n_warn} warn, {n_reject} rejected")
        print()

        images = [img for img in images if validation_map[img]["status"] != "reject"]
        if not images:
            print("ERROR: all images rejected by validation. Nothing to process.", file=sys.stderr)
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
            "validation": validation_map.get(img, {"status": "ok", "issues": []}),
        })
        all_tags.append(metadata.get("tags", []))

    manifest_path = work_dir / "manifest.json"
    manifest_path.write_text(json.dumps(analysis_results, indent=2))
    print(f"Manifest written: {manifest_path}")

    research_result = None
    editorial = None
    editorial_path = None
    research_path = None
    research_topic = args.social_topic or derive_research_topic(post_title, analysis_results)

    if args.social_research:
        print()
        print(f"Research topic: {research_topic}")
        research_result = run_social_research(
            topic=research_topic,
            command=last30days_command,
            days=args.social_days,
            sources=args.social_sources,
            mode=args.social_mode,
            timeout=args.social_timeout,
        )
        research_path = work_dir / "social_research.json"
        research_path.write_text(json.dumps(research_result, indent=2))
        print(f"Social research written: {research_path}")

        if research_result["status"] == "ok":
            print("Generating editorial copy from research + gallery metadata ...")
        else:
            reason = research_result.get("reason") or research_result.get("stderr") or "unknown error"
            print(f"WARN: social research unavailable: {reason}", file=sys.stderr)
            print("Generating editorial copy from gallery metadata only ...")

        try:
            editorial = generate_gallery_editorial(
                title=post_title,
                analysis_results=analysis_results,
                research_result=research_result,
                api_key=gemini_key,
            )
            editorial_path = work_dir / "post_draft.json"
            editorial_path.write_text(json.dumps(editorial, indent=2))
            print(f"Editorial draft written: {editorial_path}")
        except Exception as e:
            print(f"WARN: editorial generation failed: {e}", file=sys.stderr)
            editorial = None

    if args.dry_run:
        print()
        print("DRY RUN complete. Files prepared in:")
        print(f"  {work_dir}")
        print()
        print("To upload, run again without --dry-run.")
        return

    auth = wp_auth_header(wp_user, wp_password)

    # -----------------------------------------------------------------------
    # Pre-flight: validate --category / --cpt-taxonomy combo before doing work
    # -----------------------------------------------------------------------
    term_id = None
    if args.category:
        # Default taxonomy for modula-gallery is gallery_category. For other
        # CPTs the user must supply --cpt-taxonomy explicitly.
        taxonomy = args.cpt_taxonomy
        if not taxonomy and args.cpt == "modula-gallery":
            taxonomy = "gallery_category"
        if not taxonomy:
            print("ERROR: --category requires --cpt-taxonomy.", file=sys.stderr)
            sys.exit(1)
        term = wp_get_term_by_slug(taxonomy, args.category, wp_url, auth)
        if term:
            term_id = term["id"]
            print(f"Category: {args.category} (term id {term_id})")
        elif args.create_category:
            new_term = wp_create_term(taxonomy, args.category, wp_url, auth)
            term_id = new_term["id"]
            print(f"Category: {args.category} (CREATED, term id {term_id})")
        else:
            print(
                f"ERROR: Term '{args.category}' not found in taxonomy "
                f"'{taxonomy}'. Create it in wp-admin, or pass "
                f"--create-category to opt in to auto-creation.",
                file=sys.stderr,
            )
            sys.exit(1)

    # -----------------------------------------------------------------------
    # CPT path: decide whether to pre-create or create-after-upload.
    #
    # For modula-gallery we upload media first and then create the gallery in
    # one REST call with the full modulaImages payload (Modula references
    # attachments by ID only — no post_parent relationship needed).
    #
    # For any other CPT we use the legacy flow: create a draft post first,
    # then upload media with attach_to=post_id.
    # -----------------------------------------------------------------------
    is_modula = args.cpt == "modula-gallery"
    post_id = None
    if args.cpt and not is_modula:
        print()
        print(f"Creating {args.cpt} post (draft) ...")
        try:
            post = wp_create_cpt_post(
                cpt=args.cpt,
                title=post_title,
                status="draft",  # always draft during upload; flip to final at end
                wp_url=wp_url,
                auth=auth,
                content="",
                menu_order=args.menu_order,
            )
            post_id = post["id"]
            print(f"  {args.cpt} id={post_id}")
        except Exception as e:
            print(f"  POST CREATION FAILED: {e}", file=sys.stderr)
            sys.exit(1)

    print()
    print("Uploading to WordPress ...")
    media_items = []
    # Parallel list of the analysis_results that correspond to each successful
    # upload — same length as media_items, same order. We can't index
    # analysis_results by position because failed uploads shift the alignment
    # and cause Modula to pair images with the wrong metadata.
    analysis_for_media: list[dict] = []
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
                attach_to=post_id,  # None for modula-gallery; set for legacy CPT
            )
            media_items.append(media)
            analysis_for_media.append(item)
            print(f"       media_id={media['id']}  url={media['source_url']}")
        except Exception as e:
            print(f"       UPLOAD FAILED: {e}", file=sys.stderr)

    if not media_items:
        print("ERROR: No images were uploaded successfully.", file=sys.stderr)
        sys.exit(1)

    # -----------------------------------------------------------------------
    # Create / finalize the gallery post
    # -----------------------------------------------------------------------
    if is_modula:
        print()
        print("Creating modula-gallery (draft) ...")
        modula_images = build_modula_images(media_items, analysis_for_media)
        try:
            post = wp_create_modula_gallery(
                title=post_title,
                status="draft",
                modula_images=modula_images,
                wp_url=wp_url,
                auth=auth,
                menu_order=args.menu_order,
            )
            post_id = post["id"]
            print(f"  modula-gallery id={post_id}")
        except Exception as e:
            print(f"  MODULA GALLERY CREATION FAILED: {e}", file=sys.stderr)
            sys.exit(1)

        print()
        print("Finalizing modula-gallery ...")
        update_fields = {
            "featured_media": media_items[0]["id"],
            "status": args.status,
        }
        if editorial:
            update_fields["content"] = build_post_content(
                editorial.get("intro_html", ""),
                "",
                editorial.get("outro_html", ""),
            )
        if args.featured:
            update_fields["meta"] = {"_cmbpix_featured": True}
        if term_id is not None:
            # Default taxonomy for modula-gallery is gallery_category; allow
            # override via --cpt-taxonomy for unusual setups.
            taxonomy = args.cpt_taxonomy or "gallery_category"
            update_fields[taxonomy] = [term_id]

        try:
            post = wp_update_cpt_post("modula-gallery", post_id, update_fields, wp_url, auth)
        except Exception as e:
            print(f"  MODULA GALLERY FINALIZE FAILED: {e}", file=sys.stderr)
            sys.exit(1)

        edit_link = f"{wp_url}/wp-admin/post.php?post={post_id}&action=edit"
        preview_link = post.get("link", f"{wp_url}/?p={post_id}")
        print(f"  Edit:    {edit_link}")
        print(f"  Preview: {preview_link}")

    elif args.cpt:
        print()
        print(f"Finalizing {args.cpt} post ...")
        update_fields = {
            "featured_media": media_items[0]["id"],
            "status": args.status,
        }
        if editorial:
            update_fields["content"] = build_post_content(
                editorial.get("intro_html", ""),
                "",
                editorial.get("outro_html", ""),
            )
        if args.featured:
            update_fields["meta"] = {"_cmbpix_featured": True}
        if term_id is not None:
            update_fields[args.cpt_taxonomy] = [term_id]

        try:
            post = wp_update_cpt_post(args.cpt, post_id, update_fields, wp_url, auth)
        except Exception as e:
            print(f"  POST FINALIZE FAILED: {e}", file=sys.stderr)
            sys.exit(1)

        edit_link = f"{wp_url}/wp-admin/post.php?post={post_id}&action=edit"
        preview_link = post.get("link", f"{wp_url}/?p={post_id}")
        print(f"  Edit:    {edit_link}")
        print(f"  Preview: {preview_link}")
    else:
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
                intro_html=(editorial or {}).get("intro_html", ""),
                outro_html=(editorial or {}).get("outro_html", ""),
                status=args.status,
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
        "social_research_enabled": bool(args.social_research),
        "social_research_topic": research_topic if args.social_research else None,
        "social_research_status": research_result.get("status") if research_result else None,
        "social_research_path": str(research_path) if research_path else None,
        "editorial_draft_path": str(editorial_path) if editorial_path else None,
        "social_caption": (editorial or {}).get("social_caption"),
        "hashtags": (editorial or {}).get("hashtags"),
    }
    summary_path = work_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))

    if args.tele and not args.dry_run:
        if tg_token and tg_chat and tg_thread:
            try:
                chat_id_val = int(tg_chat)
            except (TypeError, ValueError):
                chat_id_val = tg_chat
            try:
                thread_id_val = int(tg_thread)
            except (TypeError, ValueError):
                thread_id_val = tg_thread
            telegram_notify(
                token=tg_token,
                chat_id=chat_id_val,
                thread_id=thread_id_val,
                title=post_title,
                edit_url=edit_link,
                image_count=len(media_items),
            )
        else:
            print(
                "WARN: --tele set but Telegram creds missing "
                "(need telegram_bot_token, telegram_chat_id, "
                "telegram_content_creative_thread_id); skipping notify.",
                file=sys.stderr,
            )

    print()
    print("Done.")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
