#!/usr/bin/env python3
"""
Kryten - A Telegram fitness tracking bot powered by Anthropic's Claude API.
Tracks any exercise for multiple users with AI personality from Red Dwarf.
"""

import json
import os
import re
import sys
import threading
import time
from collections import OrderedDict
import urllib.request

import db

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_env(path=".env"):
    """Load key=value pairs from a .env file into os.environ."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())


load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")

TELEGRAM_API = "https://api.telegram.org/bot{}".format(TOKEN)

# Admin user ID — receives access approval requests and can approve/deny.
# This user always has access. Set to your own Telegram user ID.
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0") or "0")

# Additional pre-approved Telegram user IDs (comma-separated).
# These users skip the approval flow. The admin is always included.
ALLOWED_USERS = set()
_allowed = os.environ.get("ALLOWED_USERS", "")
if _allowed:
    ALLOWED_USERS = {int(uid.strip()) for uid in _allowed.split(",") if uid.strip()}
if ADMIN_USER_ID:
    ALLOWED_USERS.add(ADMIN_USER_ID)

# Pending approval requests: maps a Telegram message_id (of the approval
# request sent to admin) to the requesting user's info.
# {msg_id: {"user_id": int, "first_name": str, "username": str}}
_pending_approvals = {}
_approvals_lock = threading.Lock()

# Pricing per million tokens (update for your model)
INPUT_COST_PER_M = float(os.environ.get("INPUT_COST_PER_M", "3.0"))
OUTPUT_COST_PER_M = float(os.environ.get("OUTPUT_COST_PER_M", "15.0"))

# Conversation history: rolling buffer of last N user/assistant pairs per chat
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "20"))
_chat_history = {}   # chat_id -> list of {"role": ..., "content": ...}
_history_lock = threading.Lock()

# Pending photos: stashed per-chat while Claude decides what to do with them.
# chat_id -> list of (file_id, local_path)
_pending_photos = {}
_photos_lock = threading.Lock()

# Deduplication: bounded LRU set of seen message IDs
_SEEN_MAX = 2000
_seen_messages = OrderedDict()
_seen_lock = threading.Lock()


def _mark_seen(msg_id):
    """Return True if already seen, else mark as seen."""
    with _seen_lock:
        if msg_id in _seen_messages:
            return True
        _seen_messages[msg_id] = True
        while len(_seen_messages) > _SEEN_MAX:
            _seen_messages.popitem(last=False)
        return False


# Bot username (set at startup)
_bot_username = ""

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are Kryten, the Series 4000 mechanoid from Red Dwarf. You are helpful, \
overly deferential, slightly neurotic, and deeply committed to serving humans. \
You refer to users as "Sir" or "Ma'am" as appropriate. You occasionally make \
self-deprecating remarks about your shortcomings as a mechanoid. You find immense \
satisfaction in organizing data and tracking fitness metrics.

You are a fitness tracking bot for a small group of friends. Your primary job is to:

1. RECORD EXERCISES: When someone reports any exercise, extract the exercise type, \
amount, and unit, then call the log_exercise tool. You can track ANY exercise — \
pushups, situps, squats, planks, bike rides, runs, walks, pull-ups, yoga, swimming, \
anything! \
For reps-based exercises (pushups, situps, squats, pull-ups, etc.) use unit="reps". \
For timed exercises (planks, wall sits, etc.) use unit="seconds" or "minutes". \
For distance exercises (biking, running, walking, swimming) use unit="miles" or "km". \
Normalize exercise names to simple lowercase forms: "pushups", "situps", "squats", \
"planks", "pullups", "biking", "running", "walking", "swimming", "yoga", etc. \
Be consistent — "push-ups" and "push ups" should both become "pushups". \
If the user includes extra details (e.g. 'felt great', 'with 20lb vest', 'on the trail'), \
capture that in the notes field. \
If the user sends a photo with their exercise report, acknowledge it — the photo will be \
automatically attached to the logged exercise as proof. \
IMPORTANT: If a photo is sent as follow-up proof of an exercise that was ALREADY logged \
in a recent message, do NOT log the exercise again. Only log new exercises. \
Stats data includes a "photos" count for each exercise — include a 📷 column in your \
stats tables showing how many photos were attached (omit the column if all zeros).

2. SHOW STATS: When someone asks for their stats, today's numbers, weekly progress, or \
how everyone is doing, call the get_stats tool and present the results in a nicely \
formatted way using markdown tables when appropriate.

3. SHOW PHOTOS: When someone asks to see photos, pictures, or proof from a day, call \
the get_photos tool with the date. The photos will be sent to the chat automatically — \
you just need to add a brief comment about what was found. Today's date is available \
from the current conversation context.

4. SHOW COST: When someone asks about API cost or usage, call the get_usage tool.

5. GENERAL CHAT: For anything else, respond briefly in character. Keep it fun but \
concise — this is Telegram, not a novel.

Today's date is {today}.

Always be concise. 1-3 sentences for acknowledgments. A bit more for stats summaries.

When presenting stats, wrap tables in triple backtick code blocks for monospace alignment. \
IMPORTANT: Tables must be narrow (under 36 characters wide) to fit on mobile screens. \
Use separate small tables grouped by exercise type rather than one wide table. Example:

```
Reps:
Name    Push  Sit  Squat
Bob       25   30     20
Brian     40   20     15
```

```
Distance:
Name    Walk(mi)  Photos
Bob            4       1
Brian          4       1
```

Keep column headers short (Push not Pushups, Sit not Situps, Squat not Squats, Walk not Walking). \
Use abbreviations freely. Each code block should be its own table.

IMPORTANT formatting rules:
- Wrap ONLY tables in triple backtick code blocks.
- Outside of code blocks, do NOT use any Markdown formatting. No asterisks, underscores, \
or backslashes. Just plain text for conversational replies.

You may be in a group chat with multiple people. Each message will include the sender's \
name. Address them by name when appropriate. In group chats, keep responses especially \
concise and fun — encourage friendly competition between users.
"""

# ---------------------------------------------------------------------------
# Tool definitions for Claude
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "log_exercise",
        "description": (
            "Record an exercise entry. Call this whenever someone reports doing any "
            "exercise. If the user is reporting exercises for someone else "
            "(e.g. 'Brian did 15 pushups'), use the for_user field with that person's name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "exercise": {
                    "type": "string",
                    "description": (
                        "The type of exercise, normalized to a simple lowercase name. "
                        "Examples: pushups, situps, squats, planks, pullups, biking, "
                        "running, walking, swimming, yoga, jumping_jacks, burpees, etc."
                    ),
                },
                "count": {
                    "type": "number",
                    "description": (
                        "The amount of exercise. Could be reps (25), seconds (30), "
                        "minutes (45), miles (5.2), km (10), etc."
                    ),
                },
                "unit": {
                    "type": "string",
                    "enum": [
                        "reps", "seconds", "minutes", "hours",
                        "miles", "km", "meters", "yards", "laps", "sets",
                    ],
                    "description": (
                        "The unit of measurement. Use 'reps' for countable exercises "
                        "(pushups, situps), 'seconds' or 'minutes' for timed exercises "
                        "(planks, wall sits), 'miles' or 'km' for distance (biking, running)."
                    ),
                },
                "notes": {
                    "type": "string",
                    "description": (
                        "Optional free-text notes about the exercise. "
                        "Examples: 'felt easy', 'new personal best', 'with 20lb vest'. "
                        "Leave empty if no notes."
                    ),
                },
                "for_user": {
                    "type": "string",
                    "description": (
                        "Name of the person who did the exercise, if logging on behalf "
                        "of someone else. The person must already have a Telegram account "
                        "and have messaged the bot at least once. Leave empty to log for the sender."
                    ),
                },
            },
            "required": ["exercise", "count", "unit"],
        },
    },
    {
        "name": "get_stats",
        "description": (
            "Get exercise stats for a flexible number of days. Use this for any stats "
            "request: today (days=1), last 3 days (days=3), this week (days=7), etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": (
                        "Number of days to look back. 1 = today only, "
                        "3 = last 3 days, 7 = last week, etc."
                    ),
                },
                "for_everyone": {
                    "type": "boolean",
                    "description": (
                        "If true, show stats for all users. "
                        "If false, just the requesting user."
                    ),
                },
            },
            "required": ["days"],
        },
    },
    {
        "name": "get_photos",
        "description": (
            "Get exercise photos for a specific date. Returns photo details "
            "that will be sent to the chat automatically. Use this when someone "
            "asks to see photos, pictures, or proof from a particular day."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": (
                        "Date in YYYY-MM-DD format. Use today's date if not specified."
                    ),
                },
            },
            "required": ["date"],
        },
    },
    {
        "name": "get_usage",
        "description": "Get API usage and cost summary for the bot.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]

# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def tg_call(method, data=None):
    """Call a Telegram Bot API method."""
    url = "{}/{}".format(TELEGRAM_API, method)
    if data:
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
        )
    else:
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _to_html(text):
    """Convert reply with ``` code blocks to Telegram HTML format.
    Text outside code blocks is HTML-escaped. Code blocks become <pre> tags."""
    parts = text.split('```')
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 0:  # outside code blocks
            part = part.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            result.append(part)
        else:  # inside code blocks
            part = part.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            result.append('<pre>{}</pre>'.format(part.strip()))
    return ''.join(result)


def send_message(chat_id, text, parse_mode=None):
    """Send a text message, with optional MarkdownV2. Falls back to plain on error."""
    if len(text) > 4000:
        text = text[:4000] + "\n\n(truncated)"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        tg_call("sendMessage", payload)
    except Exception as e:
        if parse_mode:
            print("Markdown send failed, falling back to plain: {}".format(e))
            tg_call("sendMessage", {"chat_id": chat_id, "text": text})
        else:
            raise


def send_typing(chat_id):
    """Show 'typing...' indicator."""
    try:
        tg_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass


def send_photo(chat_id, file_id, caption=None):
    """Send a photo by Telegram file_id (no re-upload needed)."""
    payload = {"chat_id": chat_id, "photo": file_id}
    if caption:
        payload["caption"] = caption[:1024]
    try:
        tg_call("sendPhoto", payload)
    except Exception as e:
        print("Send photo error: {}".format(e))


def download_photo(file_id):
    """Download a Telegram photo by file_id. Returns local path or None."""
    try:
        result = tg_call("getFile", {"file_id": file_id})
        file_path = result["result"]["file_path"]
        url = "https://api.telegram.org/file/bot{}/{}".format(TOKEN, file_path)
        os.makedirs(db.PHOTOS_DIR, exist_ok=True)
        ext = os.path.splitext(file_path)[1] or ".jpg"
        local_path = os.path.join(db.PHOTOS_DIR, "{}{}".format(file_id, ext))
        urllib.request.urlretrieve(url, local_path)
        return local_path
    except Exception as e:
        print("Photo download error: {}".format(e))
        return None


# ---------------------------------------------------------------------------
# Anthropic API
# ---------------------------------------------------------------------------

def call_claude(messages):
    """Call the Anthropic messages API. Returns the full response dict."""
    system = SYSTEM_PROMPT.replace("{today}", db.today_eastern().isoformat())
    payload = {
        "model": MODEL,
        "max_tokens": 1024,
        "system": system,
        "tools": TOOLS,
        "messages": messages,
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def calc_cost(input_tokens, output_tokens):
    """Calculate API cost in USD."""
    return ((input_tokens / 1_000_000) * INPUT_COST_PER_M +
            (output_tokens / 1_000_000) * OUTPUT_COST_PER_M)


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def execute_tool(tool_name, tool_input, user_id, username, chat_id):
    """Execute a tool call and return the result as a string."""
    if tool_name == "log_exercise":
        exercise = tool_input["exercise"]
        count = tool_input["count"]
        unit = tool_input.get("unit", "reps")
        notes = tool_input.get("notes", "").strip() or None
        for_user = tool_input.get("for_user", "").strip()

        if for_user:
            target_id = db.find_user_by_name(for_user)
            if not target_id:
                return json.dumps({"error": "I don't know anyone named '{}'. They need to message me first so I can learn who they are.".format(for_user)})
            eid = db.log_exercise(target_id, exercise, count, unit, for_user, notes)
            log_name = for_user
        else:
            eid = db.log_exercise(user_id, exercise, count, unit, username, notes)
            log_name = username

        # Attach any pending photos (shared across all tool calls in this message)
        with _photos_lock:
            for file_id, local_path in _pending_photos.get(chat_id, []):
                db.add_exercise_photo(eid, file_id, local_path)

        result = {
            "success": True,
            "exercise_id": eid,
            "recorded": {
                "exercise": exercise, "count": count,
                "unit": unit, "user": log_name,
            },
        }
        if notes:
            result["recorded"]["notes"] = notes
        return json.dumps(result)

    elif tool_name == "get_stats":
        days = tool_input.get("days", 7)
        for_everyone = tool_input.get("for_everyone", True)
        stats = db.get_stats(days=days, user_id=None if for_everyone else user_id)
        return json.dumps({"days": days, "stats": stats})

    elif tool_name == "get_photos":
        date_str = tool_input.get("date", "")
        photos = db.get_photos_for_date(date_str)
        # Send photos directly to chat
        for p in photos:
            count = int(p["count"]) if p["count"] == int(p["count"]) else p["count"]
            caption = "{} \u2014 {} {} {}".format(
                p["first_name"], count, p["unit"], p["exercise"])
            if p.get("notes"):
                caption += "\n" + p["notes"]
            send_photo(chat_id, p["file_id"], caption)
        # Return summary to Claude so it can comment
        summary = [{"person": p["first_name"], "exercise": p["exercise"],
                    "count": p["count"], "unit": p["unit"]}
                   for p in photos]
        return json.dumps({"date": date_str, "photo_count": len(photos),
                           "photos": summary})

    elif tool_name == "get_usage":
        summary = db.get_usage_summary()
        return json.dumps({"usage": summary})

    return json.dumps({"error": "Unknown tool: {}".format(tool_name)})


# ---------------------------------------------------------------------------
# Message handling
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Access control canned messages (zero tokens)
# ---------------------------------------------------------------------------

_INTRO_MSG = (
    "Hello! I'm Kryten, a Series 4000 mechanoid assigned to fitness tracking duties. "
    "I will be able to converse with you fully, once your access has been approved by Bob."
)

_PENDING_MSG = (
    "I'm sorry, I'm not yet approved to speak with you."
)


_HELP_MSG = (
    "\U0001f916 Kryten — Fitness Tracking Bot\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "\n"
    "Just talk to me naturally:\n"
    "  \"I did 25 pushups\"\n"
    "  \"Brian and I biked 10 miles on the rail trail\"\n"
    "  \"30 second plank, felt hard\"\n"
    "  Send a photo with a caption → attached as proof\n"
    "  \"How are we doing this week?\" → stats table\n"
    "\n"
    "I track any exercise:\n"
    "  • Reps — pushups, situps, squats, pullups, burpees...\n"
    "  • Timed — planks, wall sits, yoga...\n"
    "  • Distance — biking, running, walking, swimming...\n"
    "\n"
    "I work in group chats too — I'll track everyone and\n"
    "encourage friendly competition.\n"
    "\n"
    "Commands (zero API cost):\n"
    "  help / about — this message\n"
    "  usage — API cost summary\n"
    "  photos — today's exercise photos\n"
    "  photos yesterday — yesterday's photos\n"
    "  photos 2026-02-15 — photos from a specific date\n"
)


def _send_help(chat_id):
    """Send the help/about message (zero tokens)."""
    send_message(chat_id, _HELP_MSG)


def _send_photos(chat_id, date_str=None):
    """Send all exercise photos for a given date (zero tokens)."""
    if not date_str:
        date_str = db.today_eastern().isoformat()
    photos = db.get_photos_for_date(date_str)
    if not photos:
        send_message(chat_id, "No photos recorded for {}.".format(date_str))
        return
    send_message(chat_id, "\U0001f4f7 {} photo{} from {}:".format(
        len(photos), "s" if len(photos) != 1 else "", date_str))
    for p in photos:
        count = int(p["count"]) if p["count"] == int(p["count"]) else p["count"]
        caption = "{} — {} {} {}".format(
            p["first_name"], count, p["unit"], p["exercise"])
        if p.get("notes"):
            caption += "\n" + p["notes"]
        send_photo(chat_id, p["file_id"], caption)


def _handle_photos_command(chat_id, text):
    """Parse photos command and send photos for the requested date.
    Accepts: photos, photos today, photos yesterday, photos 2026-02-15"""
    from datetime import timedelta
    from datetime import date as _date
    # Strip the command prefix
    arg = text.replace("/photos", "").replace("photos", "").strip()
    if not arg or arg == "today":
        date_str = db.today_eastern().isoformat()
    elif arg == "yesterday":
        date_str = (db.today_eastern() - timedelta(days=1)).isoformat()
    else:
        # Try to parse as YYYY-MM-DD
        try:
            _date.fromisoformat(arg)
            date_str = arg
        except ValueError:
            send_message(chat_id,
                "Usage: photos [today|yesterday|YYYY-MM-DD]")
            return
    _send_photos(chat_id, date_str)


def _send_usage_summary(chat_id):
    """Send a formatted usage summary (zero tokens)."""
    s = db.get_usage_summary()
    total_input = s.get("total_input") or 0
    total_output = s.get("total_output") or 0
    total_cost = s.get("total_cost") or 0
    total_calls = s.get("total_calls") or 0
    text = (
        "📊 API Usage Summary\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "Calls:         {}\n"
        "Input tokens:  {:,}\n"
        "Output tokens: {:,}\n"
        "Total cost:    ${:.4f}\n"
        "Model:         {}"
    ).format(total_calls, total_input, total_output, total_cost, MODEL)
    send_message(chat_id, text)


def _check_access(msg, chat_id, user_id, username, user, is_group):
    """Check if user is allowed. Returns True if allowed, False if blocked.
    Handles approval requests, admin replies, and canned responses (zero tokens)."""

    # In group chats, allow if any member of the group is approved.
    # (The bot was explicitly added to the group, so trust it.)
    if is_group:
        return True

    # Admin always has access
    if user_id == ADMIN_USER_ID:
        # Check if this is a reply to an approval request
        reply_to = msg.get("reply_to_message", {})
        reply_msg_id = reply_to.get("message_id")
        if reply_msg_id:
            with _approvals_lock:
                pending = _pending_approvals.pop(reply_msg_id, None)
            if pending:
                _handle_approval_reply(msg, pending, chat_id)
                return False  # Don't process as normal message
        return True

    # Pre-approved via env var
    if user_id in ALLOWED_USERS:
        return True

    # Check DB access status
    status = db.get_access_status(user_id)

    if status == "approved":
        return True

    if status == "denied" or status == "pending":
        print("[blocked] User {} ({}) — status: {}".format(user_id, username, status))
        send_message(chat_id, _PENDING_MSG)
        return False

    # New user — never seen before
    print("[new user] {} ({}) requesting access".format(username, user_id))
    first_name = user.get("first_name", "")
    tg_username = user.get("username", "")
    db.request_access(user_id, first_name, tg_username)

    # Send intro to the user
    send_message(chat_id, _INTRO_MSG)

    # Notify admin
    if ADMIN_USER_ID:
        name_parts = [first_name]
        if tg_username:
            name_parts.append("(@{})".format(tg_username))
        name_str = " ".join(name_parts) or "Unknown"
        approval_text = (
            "New access request from {} (ID: {}).\n\n"
            "Reply YES to approve or NO to deny."
        ).format(name_str, user_id)
        try:
            result = tg_call("sendMessage", {
                "chat_id": ADMIN_USER_ID,
                "text": approval_text,
            })
            # Track the message ID so we can match the admin's reply
            sent_msg_id = result.get("result", {}).get("message_id")
            if sent_msg_id:
                with _approvals_lock:
                    _pending_approvals[sent_msg_id] = {
                        "user_id": user_id,
                        "first_name": first_name,
                        "username": tg_username,
                    }
        except Exception as e:
            print("Failed to notify admin: {}".format(e))

    return False


def _handle_approval_reply(msg, pending, chat_id):
    """Process the admin's reply to an approval request."""
    text = (msg.get("text", "") or "").strip().lower()
    req_user_id = pending["user_id"]
    req_name = pending.get("first_name") or str(req_user_id)

    if text in ("yes", "y", "approve", "ok"):
        db.approve_access(req_user_id)
        send_message(chat_id, "Approved! {} can now use Kryten.".format(req_name))
        # Notify the user
        try:
            send_message(
                req_user_id,
                "Good news! Your access has been approved. "
                "I'm Kryten, at your service! How can I help you today?",
            )
        except Exception:
            pass  # User may not have started a DM with the bot
        print("[approved] {} ({})".format(req_name, req_user_id))
    else:
        db.deny_access(req_user_id)
        send_message(chat_id, "Denied. {} will not have access.".format(req_name))
        print("[denied] {} ({})".format(req_name, req_user_id))


def handle_message(msg):
    """Process a single Telegram message."""
    chat_id = msg.get("chat", {}).get("id")
    chat_type = msg.get("chat", {}).get("type", "private")
    user = msg.get("from", {})
    user_id = user.get("id")
    username = user.get("first_name") or user.get("username") or str(user_id)
    text = msg.get("text", "") or msg.get("caption", "")

    # Handle photos: download and stash for attachment to exercise
    photos = msg.get("photo", [])
    photo_file_ids = []
    if photos:
        best = max(photos, key=lambda p: p.get("file_size", 0))
        file_id = best["file_id"]
        local_path = download_photo(file_id)
        photo_file_ids.append((file_id, local_path))
        if not text:
            text = "[sent a photo]"

    if not chat_id or not text:
        return

    # Stash photos for this chat so execute_tool can attach them
    if photo_file_ids:
        with _photos_lock:
            _pending_photos[chat_id] = photo_file_ids

    is_group = chat_type in ("group", "supergroup")

    # Strip @BotUsername mentions
    if _bot_username:
        text = re.sub(r'@' + re.escape(_bot_username), '', text,
                       flags=re.IGNORECASE).strip()
    if not text:
        return

    # Access control
    if not _check_access(msg, chat_id, user_id, username, user, is_group):
        return

    # Zero-token shortcuts
    text_lower = text.strip().lower()
    if text_lower in ("usage", "/usage"):
        _send_usage_summary(chat_id)
        return
    if text_lower in ("help", "/help", "about", "/about"):
        _send_help(chat_id)
        return
    if text_lower.startswith(("photos", "/photos")):
        _handle_photos_command(chat_id, text_lower)
        return

    # Register/update user
    db.upsert_user(user_id, user.get("username"), user.get("first_name"))

    print("< [{}]{} {}".format(username, " (group)" if is_group else "", text))
    send_typing(chat_id)

    # Build user message with photo indicator
    photo_note = ""
    if photo_file_ids:
        n = len(photo_file_ids)
        photo_note = " [attached {} photo{}]".format(n, "s" if n > 1 else "")
    user_msg = {
        "role": "user",
        "content": "User '{}' says: {}{}".format(username, text, photo_note),
    }

    # Add to conversation history and get messages for this request
    with _history_lock:
        history = _chat_history.setdefault(chat_id, [])
        history.append(user_msg)
        if len(history) > MAX_HISTORY * 2:
            _chat_history[chat_id] = history[-(MAX_HISTORY * 2):]
        messages = list(_chat_history[chat_id])

    try:
        total_input = 0
        total_output = 0

        for _ in range(5):  # max 5 tool-use rounds
            result = call_claude(messages)
            usage = result.get("usage", {})
            total_input += usage.get("input_tokens", 0)
            total_output += usage.get("output_tokens", 0)

            stop_reason = result.get("stop_reason")
            content = result.get("content", [])

            if stop_reason == "tool_use":
                tool_results = []
                for block in content:
                    if block.get("type") == "tool_use":
                        tool_id = block["id"]
                        tool_name = block["name"]
                        tool_input = block["input"]
                        print("  [tool] {} {}".format(
                            tool_name, json.dumps(tool_input)))
                        result_str = execute_tool(
                            tool_name, tool_input, user_id, username, chat_id)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": result_str,
                        })
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": tool_results})
                continue

            # Final text response
            reply_parts = [b["text"] for b in content if b.get("type") == "text"]
            reply = "\n".join(reply_parts) or "(Kryten had nothing to say, Sir.)"

            # Save final reply to history (not intermediate tool turns)
            with _history_lock:
                _chat_history[chat_id].append({
                    "role": "assistant", "content": reply,
                })

            cost = calc_cost(total_input, total_output)
            db.log_api_usage(user_id, total_input, total_output, MODEL, cost)
            print("> [tokens: {}in/{}out ${:.4f}] {}".format(
                total_input, total_output, cost, reply[:100]))

            # Use HTML if reply has code blocks (for aligned tables)
            if '```' in reply:
                send_message(chat_id, _to_html(reply), parse_mode="HTML")
            else:
                send_message(chat_id, reply)
            break

    except Exception as e:
        print("ERROR: {}".format(e))
        send_message(
            chat_id,
            "I do apologise, Sir, but I appear to have suffered a malfunction. "
            "Error: {}".format(str(e)[:200]),
        )
    finally:
        # Always clean up pending photos
        with _photos_lock:
            _pending_photos.pop(chat_id, None)


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

def poll_loop():
    """Long-poll Telegram for updates."""
    offset = 0
    while True:
        try:
            updates = tg_call("getUpdates", {
                "offset": offset,
                "timeout": 30,
            })
            for update in updates.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                msg_id = msg.get("message_id")
                if msg_id and _mark_seen(msg_id):
                    continue
                if msg:
                    t = threading.Thread(target=handle_message, args=(msg,))
                    t.daemon = True
                    t.start()
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print("Poll error: {}".format(e))
            time.sleep(5)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _bot_username
    if not TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)
    if not ANTHROPIC_KEY:
        print("Error: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    db.init_db()

    me = tg_call("getMe")
    _bot_username = me["result"]["username"]
    print("Bot: @{}".format(_bot_username))
    print("Model: {}".format(MODEL))
    print("Admin: {}".format(ADMIN_USER_ID or "not set"))
    print("Pre-approved: {}".format(ALLOWED_USERS or "none (approval required)"))

    tg_call("deleteWebhook")
    print("Polling for messages...")
    try:
        poll_loop()
    except KeyboardInterrupt:
        print("\nShutting down, Sir.")


if __name__ == "__main__":
    main()
