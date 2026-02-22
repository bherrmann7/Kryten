# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Kryten is a Telegram fitness-tracking bot powered by Claude AI. It demonstrates a "microbot" pattern: one LLM + a constrained set of tools (4 total) + one chat interface. The bot's personality is Kryten from Red Dwarf — helpful, deferential, and neurotic.

**No external dependencies.** Pure Python stdlib only (3.8+). No build step, no package manager.

## Running the Bot

```bash
python3 bot.py
```

Requires a `.env` file with (see `.env.example`):
- `TELEGRAM_BOT_TOKEN` — from @BotFather
- `ANTHROPIC_API_KEY` — from console.anthropic.com
- `ADMIN_USER_ID` — your Telegram user ID

## macOS System Service

```bash
# Install and start at boot (runs without login)
sudo cp com.kryten.bot.plist /Library/LaunchDaemons/
sudo chown root:wheel /Library/LaunchDaemons/com.kryten.bot.plist
sudo launchctl load /Library/LaunchDaemons/com.kryten.bot.plist

# Restart
sudo launchctl kickstart -k system/com.kryten.bot
```

Logs go to `data/bot.log`.

## Architecture

Two source files:

- **`bot.py`** — Everything: Telegram long-polling, access control, Claude API client, tool dispatch, conversation history, message threading
- **`db.py`** — SQLite layer; `init_db()` creates schema and applies migrations automatically

Data lives in `data/` (gitignored): `kryten.db`, `bot.log`, `photos/`.

### Message Flow

```
Telegram → poll_loop() → handle_message()
  → access control check (zero-token canned responses for unknown/denied users)
  → zero-token shortcuts (help, usage, photos)
  → Claude API (max 5 tool-use rounds)
  → tool dispatch → db.py
  → response back to Telegram
```

### The 4 Tools

Claude can only call these — nothing else:
1. `log_exercise` — insert a row into `exercises`
2. `get_stats` — query aggregated stats for N days
3. `get_photos` — fetch photos for a date (sends them to chat)
4. `get_usage` — return API cost summary from `api_usage`

### Access Control

New users get a canned intro; admin is notified. Admin replies YES/approve to allow or anything else to deny. Pending/denied users get zero-token rejections. Pre-approved users can be set via `ALLOWED_USERS` env var.

### Threading & Safety

Each incoming message is handled in its own thread. Locks protect: chat history dict, photos dict, approvals set, and the message dedup LRU cache.

## Key Implementation Details

- **Timezone:** All dates use US Eastern (`zoneinfo.ZoneInfo("America/New_York")`)
- **Conversation history:** Last `MAX_HISTORY` (default 20) message pairs per chat, stored in memory (not persisted across restarts)
- **Message dedup:** Bounded `OrderedDict` as LRU cache (1000 entries) prevents reprocessing Telegram updates
- **Telegram API:** Uses `urllib.request` directly — no `python-telegram-bot` library
- **Anthropic API:** Uses `urllib.request` directly — no `anthropic` SDK
- **HTML formatting:** Bot auto-detects code blocks in Claude's response and sends as `parse_mode=HTML`
