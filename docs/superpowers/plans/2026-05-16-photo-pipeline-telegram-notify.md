# photo-pipeline `--tele` Telegram notify — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `--tele` CLI flag to `photo-pipeline.py` that, on successful WordPress publish, sends a plain-text Telegram message containing the preview URL to the "content/creative" topic of a Telegram group.

**Architecture:** One new function `telegram_notify()` (stdlib `urllib`, plain text, never raises). Three new config keys resolved via the existing CLI > env > AWS secret > .env chain. One guarded call site at the tail of `main()` after `summary.json` is written.

**Tech Stack:** Python 3.10+, stdlib only (no new deps). Test runner: `python3 -m unittest` with `unittest.mock` — the repo currently has no test framework, so this plan introduces a minimal `tests/` directory using stdlib unittest (no pytest, no pip).

**Spec:** `docs/superpowers/specs/2026-05-15-photo-pipeline-telegram-notify-design.md`

---

## File Structure

| Path | Action | Responsibility |
|---|---|---|
| `photo-pipeline.py` | modify | Add argparse flag, `telegram_notify()` function, three new `pick()` calls, guarded call site after `summary.json` |
| `tests/__init__.py` | create | Empty — marks `tests` as a package so `python3 -m unittest discover` works |
| `tests/test_telegram_notify.py` | create | Unit tests for `telegram_notify()` (mocks `urllib.request.urlopen`) |
| `env.example` | modify | Document the three Telegram env vars |
| `README.md` | modify | One-line mention of `--tele` in the run-modes section |

Total: 1 modified script, 2 new test files, 2 modified docs.

---

## Task 1: Scaffold tests directory and add failing tests for `telegram_notify()`

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_telegram_notify.py`

- [ ] **Step 1: Create empty package marker**

Create `tests/__init__.py` with zero bytes.

```bash
: > tests/__init__.py
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_telegram_notify.py`:

```python
"""Tests for telegram_notify() in photo-pipeline.py.

Run with:  python3 -m unittest tests.test_telegram_notify -v
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

# Load photo-pipeline.py as a module despite the hyphen in the filename.
_REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("photo_pipeline", _REPO / "photo-pipeline.py")
photo_pipeline = importlib.util.module_from_spec(_spec)
sys.modules["photo_pipeline"] = photo_pipeline
_spec.loader.exec_module(photo_pipeline)


class TelegramNotifyTests(unittest.TestCase):
    def _fake_200(self):
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: False
        resp.status = 200
        resp.read.return_value = b'{"ok":true}'
        return resp

    def test_posts_to_sendmessage_url_with_token(self):
        with patch("urllib.request.urlopen", return_value=self._fake_200()) as mock_open:
            photo_pipeline.telegram_notify(
                token="ABC:xyz",
                chat_id=-1001234567890,
                thread_id=42,
                title="Bagels",
                preview_url="https://example.com/?p=1",
                image_count=12,
            )
        self.assertEqual(mock_open.call_count, 1)
        req = mock_open.call_args[0][0]
        self.assertEqual(req.full_url, "https://api.telegram.org/botABC:xyz/sendMessage")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Content-type"), "application/json")

    def test_body_shape(self):
        with patch("urllib.request.urlopen", return_value=self._fake_200()) as mock_open:
            photo_pipeline.telegram_notify(
                token="T",
                chat_id=-1001234567890,
                thread_id=42,
                title="Bagels",
                preview_url="https://example.com/?p=1",
                image_count=12,
            )
        body = json.loads(mock_open.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(body["chat_id"], -1001234567890)
        self.assertEqual(body["message_thread_id"], 42)
        self.assertEqual(body["disable_web_page_preview"], False)
        self.assertNotIn("parse_mode", body)  # plain text — no markdown
        self.assertEqual(
            body["text"],
            "📸 New gallery: Bagels (12 photos)\n👁 https://example.com/?p=1",
        )

    def test_swallows_http_error(self):
        err = urllib.error.HTTPError(
            url="https://api.telegram.org/botT/sendMessage",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(b'{"ok":false,"description":"chat not found"}'),
        )
        buf = io.StringIO()
        with patch("urllib.request.urlopen", side_effect=err), redirect_stderr(buf):
            # Must NOT raise — notification is best-effort.
            photo_pipeline.telegram_notify(
                token="T", chat_id=-1, thread_id=1,
                title="x", preview_url="https://e/", image_count=1,
            )
        self.assertIn("Telegram", buf.getvalue())

    def test_swallows_url_error(self):
        buf = io.StringIO()
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("name resolution failure")), \
             redirect_stderr(buf):
            photo_pipeline.telegram_notify(
                token="T", chat_id=-1, thread_id=1,
                title="x", preview_url="https://e/", image_count=1,
            )
        self.assertIn("Telegram", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /Users/alexcaldwell/code/repos/photo-pipeline && python3 -m unittest tests.test_telegram_notify -v`

Expected: All 4 tests FAIL with `AttributeError: module 'photo_pipeline' has no attribute 'telegram_notify'`.

- [ ] **Step 4: Commit the failing tests**

```bash
git add tests/__init__.py tests/test_telegram_notify.py
git commit -m "[pipeline]: add failing tests for telegram_notify()"
```

---

## Task 2: Implement `telegram_notify()` to make the tests pass

**Files:**
- Modify: `photo-pipeline.py` — insert new function near other top-level helpers (after `load_env`, before `find_images` — current line ~88)

- [ ] **Step 1: Add the function**

Open `photo-pipeline.py`. Find the line just above `def find_images(album_dir: Path)` (currently line 90). Insert the following function so it sits between `load_env` and `find_images`:

```python
def telegram_notify(
    token: str,
    chat_id: int | str,
    thread_id: int,
    title: str,
    preview_url: str,
    image_count: int,
    timeout: float = 10.0,
) -> None:
    """Best-effort Telegram notification. Never raises.

    Posts a plain-text sendMessage to the given chat + topic. On any HTTP or
    transport error, prints a warning to stderr and returns. The pipeline's
    exit code reflects WP upload success, not notification success.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    text = f"📸 New gallery: {title} ({image_count} photos)\n👁 {preview_url}"
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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 300:
                print(
                    f"WARN: Telegram notify returned HTTP {resp.status}",
                    file=sys.stderr,
                )
    except urllib.error.HTTPError as e:
        snippet = e.read()[:200].decode("utf-8", errors="replace") if e.fp else ""
        print(
            f"WARN: Telegram notify failed: HTTP {e.code} {e.reason} {snippet}",
            file=sys.stderr,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"WARN: Telegram notify failed: {e}", file=sys.stderr)
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `cd /Users/alexcaldwell/code/repos/photo-pipeline && python3 -m unittest tests.test_telegram_notify -v`

Expected: All 4 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add photo-pipeline.py
git commit -m "[pipeline]: add telegram_notify() helper (best-effort, never raises)"
```

---

## Task 3: Add `--tele` CLI flag

**Files:**
- Modify: `photo-pipeline.py` — argparse block (currently ends at line 812)

- [ ] **Step 1: Add the flag**

Open `photo-pipeline.py`. Find the line:

```python
    parser.add_argument("--no-validate", action="store_true",
        help="Skip the validation pass entirely (not recommended).")
```

(currently lines 810–811). Immediately AFTER it (before `args = parser.parse_args()`), add:

```python
    parser.add_argument("--tele", action="store_true",
        help="On successful WP publish, send a Telegram message with the "
             "preview URL to the content/creative topic. Requires "
             "telegram_bot_token, telegram_chat_id, and "
             "telegram_content_creative_thread_id in the AWS secret (or the "
             "corresponding TELEGRAM_* env vars). No-op with --dry-run.")
```

- [ ] **Step 2: Verify argparse still parses**

Run: `cd /Users/alexcaldwell/code/repos/photo-pipeline && python3 photo-pipeline.py --help | grep -A2 -- --tele`

Expected: Help text for `--tele` appears.

- [ ] **Step 3: Commit**

```bash
git add photo-pipeline.py
git commit -m "[pipeline]: add --tele flag for Telegram notifications"
```

---

## Task 4: Resolve Telegram config from secret/env and wire the call site

**Files:**
- Modify: `photo-pipeline.py` — config-resolution block (currently lines 843–848) and summary block (currently lines 1176–1190)

- [ ] **Step 1: Add the three `pick()` calls**

Open `photo-pipeline.py`. Find the block:

```python
    gemini_key  = pick(None,              "GEMINI_API_KEY",  "gemini_api_key", None)
    wp_url      = pick(args.wp_url,       "WP_URL",          None,             "url",          "http://mvd-clawbase:8087")
    wp_user     = pick(args.wp_user,      "WP_USER",         None,             "user",         "admin")
    wp_password = pick(args.wp_password,  "WP_APP_PASSWORD", None,             "app_password", "")
```

Immediately AFTER `wp_password = ...`, add:

```python
    tg_token   = pick(None, "TELEGRAM_BOT_TOKEN",                     "telegram_bot_token",                     None)
    tg_chat    = pick(None, "TELEGRAM_CHAT_ID",                       "telegram_chat_id",                       None)
    tg_thread  = pick(None, "TELEGRAM_CONTENT_CREATIVE_THREAD_ID",    "telegram_content_creative_thread_id",    None)
```

- [ ] **Step 2: Add the guarded call site after `summary.json` is written**

In the same file, find this block (currently around lines 1185–1190):

```python
    summary_path = work_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))

    print()
    print("Done.")
    print(json.dumps(summary, indent=2))
```

Replace it with:

```python
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
                preview_url=preview_link,
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
```

- [ ] **Step 3: Smoke-test that the dry-run path still works (no Telegram side effect)**

Run with a non-existent album to force an early-exit so we don't touch WP:

```bash
cd /Users/alexcaldwell/code/repos/photo-pipeline
python3 photo-pipeline.py /tmp/does-not-exist --tele --dry-run 2>&1 | head -5
```

Expected: `ERROR: Album directory not found: /tmp/does-not-exist` and exit code 1. No Telegram-related output.

- [ ] **Step 4: Smoke-test the "creds missing" warning path**

Create an empty temp album and run with `--tele` but no creds anywhere:

```bash
cd /Users/alexcaldwell/code/repos/photo-pipeline
TMPALBUM=$(mktemp -d)
# Empty dir would fail earlier at "no images" — drop a 1-pixel PNG to bypass.
python3 -c "import base64,sys; sys.stdout.buffer.write(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgAAIAAAUAAeImBZsAAAAASUVORK5CYII='))" > "$TMPALBUM/x.png"
# Use --dry-run so the run completes without WP; we expect Telegram to be SKIPPED under --dry-run.
unset TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID TELEGRAM_CONTENT_CREATIVE_THREAD_ID
python3 photo-pipeline.py "$TMPALBUM" --tele --dry-run --no-validate 2>&1 | tail -10
rm -rf "$TMPALBUM"
```

Expected: Pipeline completes. **No** "Telegram creds missing" warning appears (because we passed `--dry-run`, which short-circuits Telegram entirely — this confirms the dry-run guard).

- [ ] **Step 5: Re-run the unit tests to confirm nothing regressed**

Run: `cd /Users/alexcaldwell/code/repos/photo-pipeline && python3 -m unittest tests.test_telegram_notify -v`

Expected: All 4 tests still PASS.

- [ ] **Step 6: Commit**

```bash
git add photo-pipeline.py
git commit -m "[pipeline]: wire --tele to send Telegram notify after publish"
```

---

## Task 5: Document the new env vars in `env.example`

**Files:**
- Modify: `env.example`

- [ ] **Step 1: Append the Telegram block**

Open `env.example`. Append (after the existing `JPEG_QUALITY=85` line):

```bash

# Telegram notifications (only used when --tele is passed)
# Get the bot token from @BotFather. The chat_id is the supergroup id
# (negative integer, usually -100...). The thread_id is the message_thread_id
# of the content/creative topic — find it via "Copy Message Link" on any
# message in that topic (the middle number in the URL) or via getUpdates.
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_CONTENT_CREATIVE_THREAD_ID=
```

- [ ] **Step 2: Commit**

```bash
git add env.example
git commit -m "[docs]: document TELEGRAM_* env vars in env.example"
```

---

## Task 6: Document `--tele` in `README.md`

**Files:**
- Modify: `README.md` — run-modes section (currently around lines 55–78)

- [ ] **Step 1: Add a "With Telegram notification" block**

Open `README.md`. Find the existing "Override WordPress target" block:

```markdown
Override WordPress target:

```bash
python3 photo-pipeline.py /path/to/album --wp-url http://myhost:8087
```
```

Immediately AFTER it (and before the `## Agent invocation contract` heading), insert:

```markdown
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
`TELEGRAM_*` env vars). Sends a plain-text message containing the preview
URL to the content/creative topic. No-op with `--dry-run`; failure to
notify only warns — it never fails the pipeline.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "[docs]: README example for --tele Telegram notification"
```

---

## Task 7: End-to-end live verification (operator only — not for the implementing agent)

This task is a manual handoff to the operator. Skip in CI/automation.

- [ ] **Step 1: Add the three Telegram keys to AWS secret**

Run:

```bash
AWS_PROFILE=clownshow aws secretsmanager get-secret-value \
  --secret-id wordpress-mcp/photo-pipeline \
  --query SecretString --output text > /tmp/secret.json

# Edit /tmp/secret.json to add:
#   "telegram_bot_token": "...",
#   "telegram_chat_id": -1001234567890,
#   "telegram_content_creative_thread_id": 42

AWS_PROFILE=clownshow aws secretsmanager put-secret-value \
  --secret-id wordpress-mcp/photo-pipeline \
  --secret-string file:///tmp/secret.json
rm /tmp/secret.json
```

- [ ] **Step 2: Run a real album with `--tele`**

```bash
AWS_PROFILE=clownshow python3 photo-pipeline.py /path/to/small/album \
  --title "Telegram smoke test" \
  --secret wordpress-mcp/photo-pipeline \
  --target cmbpix_prod \
  --status draft \
  --tele
```

Expected: Pipeline completes, prints the preview URL, and a Telegram message lands in the content/creative topic of your group.

- [ ] **Step 3: Confirm the failure mode**

Temporarily blank `telegram_bot_token` in the secret, rerun the same command, confirm:
1. Pipeline exits 0.
2. A `WARN: Telegram notify failed:` line appears on stderr.
3. The WP draft was still created.

Restore the token after.

---

## Self-review notes

Cross-checked against `2026-05-15-photo-pipeline-telegram-notify-design.md`:
- ✅ `--tele` boolean flag (Task 3)
- ✅ Three AWS secret keys + matching env var names (Task 4 Step 1, Task 5)
- ✅ Plain text, no `parse_mode`, message format matches spec exactly (Task 1 test_body_shape, Task 2 implementation)
- ✅ `message_thread_id` set (Task 1 + 2)
- ✅ Best-effort: never raises, warn-and-continue (Task 2 implementation, Task 1 test_swallows_*)
- ✅ Skipped under `--dry-run` (Task 4 Step 2 guard, Task 4 Step 4 smoke test)
- ✅ Skipped + warns when creds missing (Task 4 Step 2 else branch)
- ✅ Single attempt, 10s timeout (Task 2 default arg)
- ✅ Call site after `summary.json` write (Task 4 Step 2)
- ✅ env.example updated (Task 5)
- ✅ README updated (Task 6)
- ✅ AGENT contract unchanged (no `--tele` in agent invocation — operator-only)
- ✅ Thread-ID discovery instructions in env.example comment (Task 5)

No placeholders, no TBDs, every code step shows the exact code, every test step shows the exact command and expected outcome.
