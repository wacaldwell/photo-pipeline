# photo-pipeline: `--tele` Telegram notification on publish

Status: approved 2026-05-15
Owner: Alex Caldwell

## Goal

When a photo-pipeline run successfully creates a WordPress draft (any of the three creation modes — Modula gallery, generic CPT, or standard post), optionally send a Telegram message containing the post's preview URL to the "content/creative" topic in a specific Telegram group.

## Non-goals

- Multiple Telegram destinations or per-run topic selection (single hardcoded topic for now).
- Edit-status mirroring, scheduled re-pings, or retries beyond a single attempt.
- Notifications for `--dry-run` mode.
- Backfill notifications for previously-published galleries.

## CLI surface

Add one boolean flag to `photo-pipeline.py`:

```
--tele    Send a Telegram notification to the content/creative topic
          on successful upload. No-op with --dry-run.
```

No other flags. Future per-topic routing can be added later without breaking this contract.

## Config

Credentials are resolved using the existing chain: **CLI flag (none here) > env var > AWS secret > .env > default**.

Keys added to the `wordpress-mcp/photo-pipeline` AWS secret:

| Secret key | Env var | Type | Purpose |
|---|---|---|---|
| `telegram_bot_token` | `TELEGRAM_BOT_TOKEN` | string | Telegram bot API token |
| `telegram_chat_id` | `TELEGRAM_CHAT_ID` | int (negative for supergroups) | Group chat ID |
| `telegram_content_creative_thread_id` | `TELEGRAM_CONTENT_CREATIVE_THREAD_ID` | int | `message_thread_id` for the content/creative topic |

If `--tele` is set and any of the three values is missing/empty, print a warning and skip the notification (do not fail the run).

### One-time thread ID discovery (for the operator, not in code)

1. Open Telegram desktop/web, navigate to the content/creative topic.
2. Right-click any message → "Copy Message Link".
3. URL is `https://t.me/c/<internal>/<thread_id>/<message_id>` — the middle number is `telegram_content_creative_thread_id`.
4. For `telegram_chat_id`, prepend `-100` to the internal id, **or** call `https://api.telegram.org/bot<TOKEN>/getUpdates` after posting in the topic and read `result[].message.chat.id` and `result[].message.message_thread_id`.

## Pipeline integration

A new function:

```python
def telegram_notify(
    token: str,
    chat_id: int | str,
    thread_id: int,
    title: str,
    preview_url: str,
    image_count: int,
    timeout: float = 10.0,
) -> None
```

Behavior:
- POSTs to `https://api.telegram.org/bot<token>/sendMessage` via `urllib.request` (no new deps).
- JSON body:
  ```json
  {
    "chat_id": <chat_id>,
    "message_thread_id": <thread_id>,
    "text": "📸 New gallery: <title> (<N> photos)\n👁 <preview_url>",
    "disable_web_page_preview": false
  }
  ```
  - **Plain text only.** No `parse_mode`. URLs auto-link in Telegram clients; titles with `()`, `-`, `.` etc. don't need escaping.
- On non-200 response or transport error: log a warning to stderr (`WARN: Telegram notify failed: <reason>`) and return. **Never raises.**
- 10s timeout. Single attempt — no retry.

Call site: end of `main()`, after `summary.json` is written, guarded by:

```python
if args.tele and not args.dry_run and post_id:
    tg_token = ...   # resolved via existing config chain
    tg_chat  = ...
    tg_thread = ...
    if tg_token and tg_chat and tg_thread:
        telegram_notify(tg_token, tg_chat, tg_thread,
                        title=final_title,
                        preview_url=preview_link,
                        image_count=len(uploaded_images))
    else:
        print("WARN: --tele set but Telegram creds missing; skipping notify.", file=sys.stderr)
```

Notification is best-effort: the WP post has already been created when this runs, so the pipeline exit code reflects upload success, not notification success.

## Message format

Exactly two lines, plain text:

```
📸 New gallery: Bagels (12 photos)
👁 https://cmbpix.com/?p=12345&preview=true
```

- Title comes from the resolved `--title` (or directory-derived fallback) — same value already shown in `summary.json`.
- Image count is `len(uploaded_images)` after the upload loop.
- Preview URL is the same `preview_url` already written to `summary.json`.

## Failure modes

| Scenario | Behavior |
|---|---|
| `--tele` not passed | Skip Telegram entirely. |
| `--dry-run` | Skip Telegram entirely. |
| WP upload fails before notify call | Skip Telegram — pipeline already exited non-zero. |
| `--tele` passed but creds missing | Print warning, exit 0 (upload succeeded). |
| Telegram API 4xx/5xx | Print warning with status + body snippet, exit 0. |
| Telegram timeout / DNS error | Print warning, exit 0. |

## Testing

- Manual smoke test: run a small album with `--tele --target cmbpix_prod --status draft`, confirm message lands in content/creative topic.
- Negative test: temporarily blank `telegram_bot_token` in the secret, run with `--tele`, confirm pipeline succeeds and prints the warning.
- Verify `--dry-run --tele` does not send a message.

## Out of scope (deferred)

- Multiple topics or per-target routing.
- Including thumbnails (Telegram `sendPhoto`).
- Notification on status flip (draft → publish) — that belongs in `cmbpix-publish`, not in `photo-pipeline.py`.
- Markdown / inline buttons.

## Files touched

- `photo-pipeline.py` — add flag, config keys, `telegram_notify()`, call site.
- `env.example` — add the three env var examples.
- `README.md` — one-line mention of `--tele` in the flag list.
- `CLAUDE.md` — no change (the agent contract is unchanged; `--tele` is operator-only).
- AWS secret `wordpress-mcp/photo-pipeline` — add three keys (operator action, not code).
