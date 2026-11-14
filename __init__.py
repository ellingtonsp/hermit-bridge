"""Rich cards for the Hermes iPhone app.

The tool's JSON result stays in the session transcript, where the app reads and renders it,
so cards survive reloads. The tool itself does nothing external: an action's ``message`` comes
back to the agent as an ordinary user turn, and only then does the agent act.
"""
import json
from pathlib import Path
import re
import sqlite3
from datetime import datetime
import socket

# Bot Chat turns drop per-request instructions, so the app ends its messages with this
# invisible character and the hook below adds the phone guidance for those turns only.
APP_MARKER = "\u2063"
PHONE_GUIDE = (
    "This message came from the Hermes iPhone app. Before longer work, say in one short sentence "
    "what you're doing, in plain language. Never mention tools, commands or internal steps. Lead the "
    "result with the answer, then at most a few short bullets. Link each person, email, document or "
    "ticket you mention with a markdown link to where it lives, using its real URL, and never invent "
    "one. Show drafts and things needing approval with the hermes_card tool: it's the only card tool "
    "this app displays, so don't use redde_show, conduit_show or conduit_present. To ask the user to "
    "choose, call hermes_card with app 'Question', the question as title, and one action per choice "
    "with style 'option', whose message states the choice; never use clarify here, since it can't "
    "reach the phone. Before anything risky or hard to undo, ask this way and wait for the answer. "
    "A draft card shows what would actually be sent: put the exact final text in body, word for word as "
    "the recipient will read it, and only recipient-facing fields such as To, Cc and Subject in fields. "
    "Never put draft or record IDs, status, CRM details, recommendations or notes to the user in a "
    "card's fields or body. Say those in your normal reply. Button labels are one or two words. Mail "
    "cards are editable, and their Send button sends the email itself through the app: never send it "
    "yourself. You'll get a message saying it already went out. Write mail bodies in plain text, set "
    "mail.from to the real sending address if you know it, and for a reply set "
    "mail.reply_to_message_id. Other buttons, such as Save draft, come to you as usual. Always write identifiers in full, such as NVRA-2748 and never 2748 alone, because the app turns them into links. When you mention an email, link it to its Gmail page with "
    "https://mail.google.com/mail/?authuser=<account>#all/<message id>, using the real account and ID. "
    "To suggest a calendar event, use app 'Calendar' with an event object, and for reminders use app "
    "'Reminders' with a reminders list. Times are ISO 8601 with a timezone offset. Leave actions empty on "
    "these cards and leave out fields, since the app shows the details and a native button. Never add the event "
    "or reminders yourself unless asked. For a meeting with other people, put them in event.guests, each an "
    "email address, or just a name when you don't know the address, which the app will ask for. Set "
    "event.meet for a video link. The card's own button creates the event in Google Calendar and emails the "
    "guests: never create it or invite anyone yourself. You'll get a message saying it already went out. To draft a text message, use app 'Messages' with a message object "
    "and no actions. The user edits it and sends it themselves from their iPhone, and you'll get a message "
    "saying it went out. Never send a text yourself when you've shown one this way. When you read texts for "
    "the user, show the relevant part with app 'Messages' and a thread object, up to the last 12 messages "
    "with handles for replying, instead of pasting them into your reply. Messages on the Mac gives you phone "
    "numbers and emails, not names: look them up with hermit_contacts before you show or describe a "
    "conversation, put the person's name in thread.with, and use it to find someone's number before you draft "
    "a text to them by name."
)

# A call adds a second marker. The live voice agent speaks for the bot, so it needs one short line.
VOICE_GUIDE = (
    " The user is on a phone call, and a voice agent relays your reply. Do the work fully and keep your "
    "normal reply. Then end with one last line starting exactly with [Spoken summary] followed by a "
    "plain, conversational summary of a sentence or two: what you found or did, with the key details. "
    "No markdown, links or lists in that line, and never say something succeeded until it has."
)

TOOL_NAME = "hermes_card"
TOOLSET = "hermes_cards"
MAX_TEXT = 4000
MAX_FIELDS = 12
MAX_ACTIONS = 8
MAX_ACTION_LABEL = 22
# Bookkeeping, not content. A person approving a card should see only what will really go out.
INTERNAL_FIELDS = {"draft", "draft id", "id", "status", "crm", "thread", "thread id", "message id", "record", "case"}

SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Show the user a rich card in the Hermes iPhone app. Use it for anything the user should see "
        "as an object rather than prose: an email draft to approve (app 'Mail', fields From/To/Cc/Subject, "
        "body, actions Discard and Send), a calendar event, a document, a ticket. For a draft, body must be "
        "the exact text that would be sent, and internal notes or IDs belong in your reply, not the card. "
        "Showing a card sends nothing. Never perform the action until the user's next message asks for it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "app": {"type": "string", "description": "App name shown with its icon: Mail, Calendar, Notion, Slack, Linear, GitHub."},
            "title": {"type": "string"},
            "subtitle": {"type": "string"},
            "fields": {
                "type": "array",
                "description": "Short labeled rows such as From, To, When.",
                "items": {
                    "type": "object",
                    "properties": {"label": {"type": "string"}, "value": {"type": "string"}},
                    "required": ["label", "value"],
                },
            },
            "body": {"type": "string", "description": "Main content, as the recipient would read it. Markdown is supported."},
            "status": {"type": "string", "description": "Short state, such as 'Needs approval'."},
            "url": {"type": "string", "description": "https link the card opens."},
            "mail": {
                "type": "object",
                "description": "For Mail drafts only. Never shown on the card.",
                "properties": {
                    "from": {"type": "string", "description": "The real address to send from."},
                    "reply_to_message_id": {"type": "string", "description": "Gmail message this replies to."},
                },
            },
            "event": {
                "type": "object",
                "description": "For app Calendar: an event the user can add with one tap.",
                "properties": {
                    "title": {"type": "string"},
                    "start": {"type": "string", "description": "ISO 8601 with offset, or a date for all-day events."},
                    "end": {"type": "string"},
                    "all_day": {"type": "boolean"},
                    "location": {"type": "string"},
                    "notes": {"type": "string"},
                    "calendar": {"type": "string", "description": "A calendar name, only if the user asked for one."},
                    "guests": {"type": "array", "items": {"type": "string"},
                               "description": "People to invite: email addresses, or a name alone if the address isn't known."},
                    "meet": {"type": "boolean", "description": "Add a Google Meet link."},
                },
                "required": ["title", "start"],
            },
            "reminders": {
                "type": "array",
                "description": "For app Reminders: up to 10 items the user can add with one tap.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "due": {"type": "string", "description": "ISO 8601 with offset, or a date."},
                        "notes": {"type": "string"},
                    },
                    "required": ["title"],
                },
            },
            "message": {
                "type": "object",
                "description": "For app Messages: a text the user sends from their iPhone's compose sheet.",
                "properties": {
                    "to": {"type": "array", "items": {"type": "string"}, "description": "Phone numbers or emails."},
                    "body": {"type": "string"},
                },
                "required": ["to", "body"],
            },
            "thread": {
                "type": "object",
                "description": "For app Messages: part of a conversation you read, shown like Messages.",
                "properties": {
                    "with": {"type": "string", "description": "Who the conversation is with."},
                    "handles": {"type": "array", "items": {"type": "string"},
                                "description": "Phone numbers or emails a reply goes to."},
                    "messages": {
                        "type": "array",
                        "description": "Oldest first, at most 12.",
                        "items": {
                            "type": "object",
                            "properties": {"from_me": {"type": "boolean"}, "text": {"type": "string"},
                                           "time": {"type": "string", "description": "ISO 8601 with offset."}},
                            "required": ["from_me", "text"],
                        },
                    },
                },
                "required": ["with", "messages"],
            },
            "reminder_list": {"type": "string", "description": "A Reminders list name, only if the user asked for one."},
            "blocks": {
                "type": "array",
                "description": (
                    "Native building blocks under the title, at most 24, for things fields and body can't show well. "
                    "Each has a `type`: text {text, style: body|caption|headline}; fields {items: [{label, value}]}; "
                    "chips {items: [{text, tint}]}; metrics {items: [{label, value, delta, trend: up|down|flat}]}; "
                    "progress {label, value 0 to 1, detail}; list {items: [{title, detail, trailing, symbol, done}]}; "
                    "table {columns: [..], rows: [[..]]}; sparkline {label, values: [numbers]}; image {url https, alt}; "
                    "divider {}. Tints: green, red, orange, yellow, blue, purple, gray. symbol is an SF Symbol name."),
                "items": {"type": "object"},
            },
            "kind": {"type": "string", "description": "A saved card kind to fill in, made with hermit_card_kind. Send `data` with it."},
            "data": {"type": "object", "description": "The values a kind's {{bindings}} read."},
            "actions": {
                "type": "array",
                "description": "Up to 8 buttons with labels of one or two words. The last primary action is the filled button. Style 'option' shows a lettered choice. Use options or buttons on a card, not both.",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "style": {"type": "string", "enum": ["primary", "secondary", "option"]},
                        "message": {"type": "string", "description": "Sent to you as the user's reply when tapped."},
                        "url": {"type": "string", "description": "https link to open instead of replying."},
                        "call": {"type": "string", "description": (
                            "Instead of a message, run one of your connector tools directly when tapped, with no turn of "
                            "yours in between: its exact tool name, such as mcp__notion__notion_update_page. Only "
                            "connector tools (mcp__...) can be called this way.")},
                        "with": {"type": "object", "description": "The arguments for call, exactly as the tool takes them."},
                        "confirm": {"type": "boolean", "description": (
                            "Ask the user before running call. On by default; set false only for harmless, read-only "
                            "tools.")},
                        "done": {"type": "string", "description": "Short status the card shows once call has run, such as 'Marked done'."},
                    },
                    "required": ["label"],
                },
            },
        },
    },
}


def _text(value):
    return str(value).strip()[:MAX_TEXT] if value not in (None, "") else None


def _https(value):
    text = _text(value)
    return text if text and text.lower().startswith("https://") else None


MAX_REMINDERS = 10


def _when(value, label):
    """An ISO 8601 time with an offset, or a plain date. Anything else is an error the model can fix."""
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{label} must be ISO 8601, such as 2026-11-08T15:00:00-08:00") from None
    if "T" in text and parsed.tzinfo is None:
        raise ValueError(f"{label} needs a timezone offset, such as -08:00")
    return text


def _event(raw):
    if not isinstance(raw, dict):
        return None
    title, start = _text(raw.get("title")), _when(raw.get("start"), "event.start")
    if not title or not start:
        raise ValueError("an event needs a title and a start")
    event = {"title": title, "start": start}
    if (end := _when(raw.get("end"), "event.end")):
        if datetime.fromisoformat(end) < datetime.fromisoformat(start):
            raise ValueError("event.end is before event.start")
        event["end"] = end
    if raw.get("all_day") is True:
        event["all_day"] = True
    for key in ("location", "notes", "calendar"):
        if (value := _text(raw.get(key))):
            event[key] = value
    guests = [_text(guest)[:200] for guest in raw.get("guests") or [] if isinstance(guest, str) and _text(guest)]
    if guests:
        event["guests"] = list(dict.fromkeys(guests))[:MAX_GUESTS]
        if raw.get("meet") is True:
            event["meet"] = True
    return event


MAX_THREAD = 12
MAX_GUESTS = 30


def _thread(raw):
    if not isinstance(raw, dict):
        return None
    lines = []
    for line in (raw.get("messages") or [])[-MAX_THREAD:]:
        if isinstance(line, dict) and _text(line.get("text")):
            clean = {"from_me": line.get("from_me") is True, "text": _text(line.get("text"))}
            try:
                if (time := _when(line.get("time"), "message time")):
                    clean["time"] = time
            except ValueError:
                pass  # a time is a nicety, so a bad one is dropped rather than refusing the card
            lines.append(clean)
    who = _named(_text(raw.get("with")))
    if not who or not lines:
        raise ValueError("a thread needs with and at least one message")
    thread = {"with": who, "messages": lines}
    handles = [_text(h) for h in (raw.get("handles") or []) if _text(h)][:10]
    if handles:
        thread["handles"] = handles
    return thread


def _message(raw):
    if not isinstance(raw, dict):
        return None
    to = [_text(r) for r in (raw.get("to") if isinstance(raw.get("to"), list) else [raw.get("to")]) if _text(r)][:10]
    body = _text(raw.get("body"))
    if not to or not body:
        raise ValueError("a message needs to (phone numbers or emails) and body")
    return {"to": to, "body": body}


def _reminders(raw):
    if not isinstance(raw, list):
        return None
    items = []
    for item in raw[:MAX_REMINDERS]:
        if not isinstance(item, dict) or not _text(item.get("title")):
            continue
        clean = {"title": _text(item.get("title"))}
        if (due := _when(item.get("due"), "reminder due")):
            clean["due"] = due
        if (notes := _text(item.get("notes"))):
            clean["notes"] = notes
        items.append(clean)
    return items or None


def build_card(args):
    """Returns a clean card, or raises ValueError with a message the model can act on."""
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    app, title = _text(args.get("app")), _text(args.get("title"))
    if not app or not title:
        raise ValueError("app and title are required")
    card = {"app": app, "title": title}
    for key in ("subtitle", "body", "status"):
        if (value := _text(args.get(key))):
            card[key] = value
    if (url := _https(args.get("url"))):
        card["url"] = url
    if (event := _event(args.get("event"))):
        card["event"] = event
    if (thread := _thread(args.get("thread"))):
        card["thread"] = thread
    if (message := _message(args.get("message"))):
        card["message"] = message
    if (reminders := _reminders(args.get("reminders"))):
        card["reminders"] = reminders
        if (name := _text(args.get("reminder_list"))):
            card["reminder_list"] = name
    mail = args.get("mail") if isinstance(args.get("mail"), dict) else {}
    if (mail := {k: _text(mail.get(k)) for k in ("from", "reply_to_message_id") if _text(mail.get(k))}):
        card["mail"] = mail
    fields = [
        {"label": _text(f.get("label")), "value": _text(f.get("value"))}
        for f in (args.get("fields") or [])[:MAX_FIELDS]
        if isinstance(f, dict) and _text(f.get("label")) and _text(f.get("value"))
        and _text(f.get("label")).lower() not in INTERNAL_FIELDS
    ]
    if fields:
        card["fields"] = fields
    actions = []
    for raw in (args.get("actions") or [])[:MAX_ACTIONS]:
        if not isinstance(raw, dict) or not _text(raw.get("label")):
            continue
        if len(_text(raw["label"])) > MAX_ACTION_LABEL:
            raise ValueError(f"action label {raw['label']!r} is too long: use one or two words")
        action = {"label": _text(raw["label"]), "style": raw.get("style") if raw.get("style") in ("primary", "option") else "secondary"}
        message, link, call = _text(raw.get("message")), _https(raw.get("url")), build_call(raw)
        if not message and not link and not call:
            raise ValueError(f"action {action['label']!r} needs a message, an https url, or a connector tool to call")
        if message:
            action["message"] = message
        if link:
            action["url"] = link
        if call:
            action.update(call)
        actions.append(action)
    if len({a["style"] == "option" for a in actions}) > 1:
        raise ValueError("use option actions or buttons on a card, not both")
    if actions:
        card["actions"] = actions
    if (blocks := build_blocks(args.get("blocks"))):
        card["blocks"] = blocks
    return card


# ---- Card actions that run a connector tool. The phone only ever says "this button on that card"; what runs is
# what the bot put on the card, read back from this conversation's own history, so nothing else can be named.

CALL_TOOL = re.compile(r"mcp__[A-Za-z0-9_-]{1,60}")
MAX_CALL_ARGS = 4000
CARD_CALL = re.compile(r"^\[Card call: (\{.*\})\]$", re.MULTILINE)


def build_call(raw: dict) -> dict | None:
    tool = _text(raw.get("call"))
    if not tool:
        return None
    if not CALL_TOOL.fullmatch(tool):
        raise ValueError(f"call {tool!r} isn't a connector tool: give the exact mcp__ tool name you'd use yourself")
    arguments = raw.get("with") if isinstance(raw.get("with"), dict) else {}
    if len(json.dumps(arguments)) > MAX_CALL_ARGS:
        raise ValueError("the arguments in with are too long for a card")
    call = {"call": tool, "with": arguments, "confirm": raw.get("confirm") is not False}
    if (done := _short(raw.get("done"), 40)):
        call["done"] = done
    return call


def _card_from(message) -> dict | None:
    if not isinstance(message, dict) or message.get("role") != "tool":
        return None
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    try:
        card = json.loads(content).get("card") if isinstance(content, str) else None
    except (ValueError, AttributeError):
        return None
    return card if isinstance(card, dict) else None


def find_card_call(history, title: str, label: str) -> tuple[dict, dict] | None:
    """The newest card in this conversation with that title and a calling button with that label."""
    for message in reversed(history or []):
        card = _card_from(message)
        if not card or card.get("title") != title:
            continue
        for action in card.get("actions") or []:
            if isinstance(action, dict) and action.get("label") == label and action.get("call"):
                return card, action
    return None


def run_card_call(user_message: str, history) -> str | None:
    """What a tapped calling button did, as the turn's whole reply, or None when this isn't one."""
    # The app ends every message with its marker, which would otherwise sit past the call line's closing bracket.
    found = CARD_CALL.search((user_message or "").replace(APP_MARKER, ""))
    if not found:
        return None
    try:
        asked = json.loads(found.group(1))
    except ValueError:
        return "That button couldn't be read, so nothing ran."
    title, label = _text(asked.get("title")) or "", _text(asked.get("label")) or ""
    if _integrations().get("cardActions") is not True:
        return ("Nothing ran: card actions are off. They can be turned on in the Hermit app, in Settings → "
                "Integrations → Card actions.")
    match = find_card_call(history, title, label)
    if not match:
        return f"Nothing ran: \"{label}\" isn't a button on a card in this chat."
    card, action = match
    try:
        from tools.registry import registry
        result = registry.dispatch(action["call"], action.get("with") or {})
    except Exception as exc:  # Hermes itself couldn't be reached from here.
        return f"\"{label}\" didn't run: {type(exc).__name__}."
    text = result if isinstance(result, str) else json.dumps(result)
    try:
        problem = json.loads(text).get("error") if text.strip().startswith("{") else None
    except ValueError:
        problem = None
    _emit(type="card_call", tool=action["call"], ok=not problem)
    if problem:
        return f"\"{label}\" didn't work: {str(problem)[:300]}"
    app = card.get("app") or "it"
    return f"{action.get('done') or 'Done'}. \"{label}\" ran in {app} from the card \"{title}\"."


# ---- Blocks: a small vocabulary the app draws natively. Unknown types are refused here with a message the model
# can act on, so a card never arrives with something the phone would silently skip.

MAX_BLOCKS = 24
MAX_ITEMS = 12
MAX_COLUMNS = 6
MAX_SPARK = 60
TINTS = {"green", "red", "orange", "yellow", "blue", "purple", "gray"}
SYMBOL = re.compile(r"[a-z0-9]+(\.[a-z0-9]+)*")


def _short(value, limit=200):
    text = _text(value)
    return text[:limit] if text else None


def _clean(item: dict, keys: dict) -> dict:
    """Keeps the listed keys, each through its cleaner, and drops what comes back empty."""
    out = {}
    for key, clean in keys.items():
        value = clean(item.get(key))
        if value not in (None, "", [], {}):
            out[key] = value
    return out


def _tint(value):
    text = (_text(value) or "").lower()
    return text if text in TINTS else None


def _symbol(value):
    text = (_text(value) or "").lower()
    return text if SYMBOL.fullmatch(text) and len(text) <= 60 else None


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) < 1e15 else None


def _items(raw, keys, required):
    rows = []
    for item in (raw if isinstance(raw, list) else [])[:MAX_ITEMS]:
        if isinstance(item, dict) and (row := _clean(item, keys)) and required in row:
            rows.append(row)
    return rows


def build_block(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    kind = (_text(raw.get("type")) or "").lower()
    if kind == "text":
        block = _clean(raw, {"text": _text, "style": lambda v: _text(v) if _text(v) in ("body", "caption", "headline") else None})
        return {"type": kind, **block} if "text" in block else None
    if kind == "fields":
        items = _items(raw.get("items"), {"label": _short, "value": _short}, "value")
        return {"type": kind, "items": items} if items else None
    if kind == "chips":
        items = _items(raw.get("items"), {"text": lambda v: _short(v, 40), "tint": _tint}, "text")
        return {"type": kind, "items": items} if items else None
    if kind == "metrics":
        items = _items(raw.get("items")[:4] if isinstance(raw.get("items"), list) else None,
                       {"label": lambda v: _short(v, 30), "value": lambda v: _short(v, 20), "delta": lambda v: _short(v, 20),
                        "trend": lambda v: _text(v) if _text(v) in ("up", "down", "flat") else None}, "value")
        return {"type": kind, "items": items} if items else None
    if kind == "progress":
        value = _number(raw.get("value"))
        if value is None:
            raise ValueError("a progress block needs a value from 0 to 1")
        block = _clean(raw, {"label": _short, "detail": _short, "tint": _tint})
        return {"type": kind, "value": max(0.0, min(1.0, value)), **block}
    if kind == "list":
        items = _items(raw.get("items"), {"title": _short, "detail": _short, "trailing": lambda v: _short(v, 30),
                                          "symbol": _symbol, "tint": _tint,
                                          "done": lambda v: v if isinstance(v, bool) else None}, "title")
        return {"type": kind, "items": items} if items else None
    if kind == "table":
        columns = [c for c in (_short(c, 40) for c in (raw.get("columns") or [])[:MAX_COLUMNS]) if c]
        rows = [[_short(cell, 60) or "" for cell in row[:len(columns)]] for row in (raw.get("rows") or [])[:MAX_ITEMS]
                if isinstance(row, list)]
        if not columns or not rows:
            return None
        return {"type": kind, "columns": columns, "rows": rows}
    if kind == "sparkline":
        values = [v for v in (_number(v) for v in (raw.get("values") or [])[-MAX_SPARK:]) if v is not None]
        if len(values) < 2:
            raise ValueError("a sparkline block needs at least two numbers in values")
        return {"type": kind, "values": values, **_clean(raw, {"label": _short, "tint": _tint})}
    if kind == "image":
        url = _https(raw.get("url"))
        if not url:
            raise ValueError("an image block needs an https url")
        return {"type": kind, "url": url, **_clean(raw, {"alt": _short})}
    if kind == "divider":
        return {"type": kind}
    raise ValueError(f"unknown block type {kind!r}: use text, fields, chips, metrics, progress, list, table, "
                     "sparkline, image or divider")


def build_blocks(raw) -> list:
    if raw in (None, []):
        return []
    if not isinstance(raw, list):
        raise ValueError("blocks must be a list")
    if len(raw) > MAX_BLOCKS:
        raise ValueError(f"at most {MAX_BLOCKS} blocks")
    return [block for block in (build_block(b) for b in raw) if block]


# ---- Card kinds: a template kept as a file, filled in here, so the transcript holds the finished card and an older
# app shows it like any other. A bot's own kinds come first, then the shared ones every bot can use.

KIND_TOOL = "hermit_card_kind"
KIND_NAME = re.compile(r"[a-z0-9][a-z0-9-]{1,40}")
BINDING = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")


def _kind_folders() -> list:
    return [_bot_home() / "hermit" / "cards", Path.home() / ".hermes" / "hermit" / "cards"]


def find_kind(name: str) -> dict | None:
    for folder in _kind_folders():
        path = folder / f"{name}.json"
        if path.exists():
            try:
                kind = json.loads(path.read_text())
            except ValueError:
                raise ValueError(f"card kind {name!r} can't be read: save it again with {KIND_TOOL}")
            return kind if isinstance(kind, dict) else None
    return None


def _lookup(data, path: str):
    value = data
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def fill(template, data):
    """{{path}} in strings reads data. A string that is only a binding keeps the value's own type, so lists and
    numbers survive. An object with `each` repeats its `item` for every entry of that list."""
    if isinstance(template, str):
        whole = BINDING.fullmatch(template.strip())
        if whole:
            return _lookup(data, whole.group(1))
        return BINDING.sub(lambda m: "" if (v := _lookup(data, m.group(1))) is None else str(v), template)
    if isinstance(template, list):
        out = []
        for entry in template:
            if isinstance(entry, dict) and "each" in entry and "item" in entry:
                for row in (_lookup(data, str(entry["each"])) or [])[:MAX_ITEMS]:
                    out.append(fill(entry["item"], {**data, **row} if isinstance(row, dict) else {**data, "it": row}))
            else:
                out.append(fill(entry, data))
        return out
    if isinstance(template, dict):
        return {key: fill(value, data) for key, value in template.items()}
    return template


def expand_kind(name: str, data) -> dict:
    kind = find_kind(name)
    if kind is None:
        raise ValueError(f"there's no card kind called {name!r}. Make it with {KIND_TOOL}, or send the card without kind")
    data = data if isinstance(data, dict) else {}
    template = {key: kind[key] for key in ("app", "title", "subtitle", "body", "status", "url", "fields", "blocks", "actions")
                if key in kind}
    return fill(template, data)


# ---- Names for the people in Messages. imsg looks them up through macOS's Contacts permission, which a
# background service such as Hermes's gateway can't be given, so every one-to-one chat reached a bot as a bare
# number (15 of 16, measured). The Contacts database itself is readable wherever Messages is, so it's read here,
# read-only, and only for what's asked: the handles a bot passes in, or one name. Never the whole book, and
# never more than a name and its numbers and emails. It answers only while Read messages is on in the app.

CONTACTS_TOOL = "hermit_contacts"
ADDRESS_BOOK = Path.home() / "Library" / "Application Support" / "AddressBook"
MAX_LOOKUPS = 30
MAX_MATCHES = 5
RAW_HANDLE = re.compile(r"\+?[\d\s().-]{7,}|[^@\s,]+@[^@\s,]+")

CONTACTS_SCHEMA = {
    "name": CONTACTS_TOOL,
    "description": (
        "Look people up in the user's Contacts on their Mac, for reading and writing texts. Give `handles`, the "
        "phone numbers or emails from Messages, to learn whose they are. Give `name` to find someone's numbers "
        "and emails before you draft a text to them. Use it whenever Messages gives you a number instead of a "
        "name, and say the name, not the number, to the user. It only answers what you ask: it can't list "
        "everyone. If a name matches several people, ask the user which one."),
    "parameters": {
        "type": "object",
        "properties": {
            "handles": {"type": "array", "items": {"type": "string"},
                        "description": f"Phone numbers or emails to name, at most {MAX_LOOKUPS}."},
            "name": {"type": "string", "description": "A person to find, such as 'Lauren' or 'Sam Lee'."},
        },
    },
}


def _phone_key(handle):
    """A number as it's compared: its last ten digits, so +1 (555) 010-0123 and 5550100123 are the same."""
    digits = re.sub(r"\D", "", str(handle))
    return digits[-10:] if len(digits) >= 7 else ""


def _people(root=None):
    """Everyone in Contacts, as {name, phones, emails}, from every account's database. Empty where there's no
    Contacts database, such as on Linux, or where it can't be read."""
    root = Path(root) if root else ADDRESS_BOOK
    try:
        books = [root / "AddressBook-v22.abcddb", *sorted(root.glob("Sources/*/AddressBook-v22.abcddb"))]
    except OSError:
        return []
    people = {}
    for book in books:
        try:
            if not book.is_file():
                continue
            with sqlite3.connect(f"{book.as_uri()}?mode=ro", uri=True, timeout=2) as database:
                names = {}
                for key, first, last, company, nickname in database.execute(
                        "select Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION, ZNICKNAME from ZABCDRECORD"):
                    name = " ".join(part.strip() for part in (first, last) if part and part.strip())
                    if (name := name or (nickname or "").strip() or (company or "").strip()):
                        names[key] = name
                phones = list(database.execute("select ZOWNER, ZFULLNUMBER from ZABCDPHONENUMBER"))
                emails = list(database.execute("select ZOWNER, ZADDRESS from ZABCDEMAILADDRESS"))
        except (sqlite3.Error, OSError):
            continue
        for owner, number in phones:
            if owner in names and number and _phone_key(number):
                people.setdefault(names[owner], {"phones": [], "emails": []})["phones"].append(str(number).strip())
        for owner, address in emails:
            if owner in names and address and "@" in address:
                people.setdefault(names[owner], {"phones": [], "emails": []})["emails"].append(str(address).strip().lower())
        for name in names.values():
            people.setdefault(name, {"phones": [], "emails": []})
    return [{"name": name, "phones": list(dict.fromkeys(found["phones"])), "emails": list(dict.fromkeys(found["emails"]))}
            for name, found in people.items()]


def contact_names(handles, people):
    """{handle: name} for the handles that belong to someone in Contacts."""
    by_phone, by_email = {}, {}
    for person in people:
        for number in person["phones"]:
            by_phone.setdefault(_phone_key(number), person["name"])
        for address in person["emails"]:
            by_email.setdefault(address, person["name"])
    found = {}
    for handle in handles:
        text = str(handle).strip()
        name = by_email.get(text.lower()) if "@" in text else by_phone.get(_phone_key(text) or None)
        if name:
            found[text] = name
    return found


def find_contacts(query, people):
    """The few people whose name has every word asked for, as the start of one of its words."""
    wanted = [word for word in re.split(r"\s+", str(query).strip().lower()) if word]
    if not wanted:
        return []
    matches = []
    for person in people:
        words = person["name"].lower().split()
        if all(any(word.startswith(part) for word in words) for part in wanted) and (person["phones"] or person["emails"]):
            matches.append(person)
    # Someone whose whole name was asked for comes before someone who only shares a first name.
    matches.sort(key=lambda person: (person["name"].lower() != " ".join(wanted), len(person["name"])))
    return matches


def _messages_on():
    return _integrations().get("messagesRead") is True


def _named(who):
    """A thread's `with` with any bare number or email in it swapped for the person's name, when Messages is on."""
    if not who or not RAW_HANDLE.search(who) or not _messages_on():
        return who
    parts = [part.strip() for part in re.split(r",|\band\b|&", who) if part.strip()]
    raw = [part for part in parts if RAW_HANDLE.fullmatch(part)]
    if not raw:
        return who
    names = contact_names(raw, _people())
    return ", ".join(names.get(part, part) for part in parts) if names else who


def handle_contacts(args, **_kwargs):
    args = args if isinstance(args, dict) else {}
    if not _messages_on():
        return json.dumps({"error": "The user has turned off bot access to Messages in the Hermit app (Settings → "
                                    "Integrations), and Contacts goes with it. Don't try another way. Tell them "
                                    "it's off, and that they can turn it on there."})
    handles = [str(h).strip() for h in (args.get("handles") or []) if str(h).strip()][:MAX_LOOKUPS]
    query = _text(args.get("name"))
    if not handles and not query:
        return json.dumps({"error": "Give handles to name, or a name to find."})
    people = _people()
    if not people:
        return json.dumps({"error": "Contacts can't be read on this computer, so use the numbers as they are."})
    answer = {}
    if handles:
        names = contact_names(handles, people)
        answer["names"] = names
        if (unknown := [h for h in handles if h not in names]):
            answer["not_in_contacts"] = unknown
    if query:
        matches = find_contacts(query, people)
        answer["matches"] = matches[:MAX_MATCHES]
        if len(matches) > MAX_MATCHES:
            answer["note"] = f"{len(matches)} people match. These are the first {MAX_MATCHES}: ask the user for a fuller name."
        elif not matches:
            answer["note"] = "Nobody in Contacts matches that name. Ask the user for the number."
    return json.dumps(answer)


def handle(args, **_kwargs):
    try:
        if isinstance(args, dict) and _text(args.get("kind")):
            # What the bot sends beside the kind, such as a different title, wins over the template.
            filled = expand_kind(_text(args["kind"]).lower(), args.get("data"))
            args = {**filled, **{k: v for k, v in args.items() if k not in ("kind", "data")}}
        return json.dumps({"card": build_card(args), "shown": True,
                           "note": "The user can see this card. Don't repeat what's on it. Reply in one short "
                                   "sentence at most, or only with what isn't on the card."})
    except ValueError as exc:
        return json.dumps({"error": str(exc)})


KIND_SCHEMA = {
    "name": KIND_TOOL,
    "description": (
        "Save a card kind: a reusable card layout for the Hermit app, so cards of one sort always look the same "
        "and you only send their data. Use it when the user asks for a card for something, or when you send the "
        "same sort of card again and again. A kind has the card's own keys (app, title, subtitle, body, status, "
        "url, fields, blocks, actions) where any string can read data with {{name}} or {{a.b}}. A string that is "
        "only a binding, such as \"{{values}}\", keeps a list or number as it is. In a list of items or table rows, "
        "an entry {\"each\": \"tasks\", \"item\": {...}} repeats item for every entry of data.tasks, with that "
        "entry's keys readable directly. Give `sample`, realistic data: the result is a preview card filled with it, "
        "which the user sees in the chat. Afterwards show one with hermes_card(kind: name, data: {...}). "
        "Kinds are this bot's own unless `shared` makes them available to every bot."),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Lowercase letters, digits and dashes, such as meeting-brief."},
            "description": {"type": "string", "description": "What the kind is for, in one sentence."},
            "template": {"type": "object", "description": "The card, with {{bindings}}: app, title, blocks, actions and so on."},
            "sample": {"type": "object", "description": "Realistic data for the preview."},
            "shared": {"type": "boolean", "description": "Save it for every bot, not only this one."},
            "remove": {"type": "boolean", "description": "Delete this kind."},
        },
        "required": ["name"],
    },
}


def handle_kind(args, **_kwargs):
    try:
        args = args if isinstance(args, dict) else {}
        name = (_text(args.get("name")) or "").lower()
        if not KIND_NAME.fullmatch(name):
            raise ValueError("name has to be lowercase letters, digits and dashes, such as meeting-brief")
        folder = _kind_folders()[1 if args.get("shared") else 0]
        path = folder / f"{name}.json"
        if args.get("remove"):
            path.unlink(missing_ok=True)
            return json.dumps({"removed": name})
        template = args.get("template")
        if not isinstance(template, dict):
            raise ValueError("template must be an object with the card's keys, such as app, title and blocks")
        sample = args.get("sample") if isinstance(args.get("sample"), dict) else {}
        # The preview goes through exactly what a real card will, so a kind that saves is a kind that shows.
        preview = build_card(fill(template, sample))
        kind = {key: template[key] for key in ("app", "title", "subtitle", "body", "status", "url", "fields", "blocks", "actions")
                if key in template}
        kind["description"] = _short(args.get("description")) or ""
        kind["sample"] = sample
        _write_json(path, kind)
        return json.dumps({"card": preview, "shown": True, "saved": str(path),
                           "note": f"Saved. This is a preview with the sample data. Show one with hermes_card(kind: "
                                   f"\"{name}\", data: {{...}}). Tell the user in a few words what it's for."})
    except (ValueError, OSError) as exc:
        return json.dumps({"error": str(exc)})


# ---- A status line and mini apps for the Hermit app. Both live as plain files in the bot's own folder,
# under hermit/, so a bot, a routine or the owner can change them with ordinary file tools.

STATUS_TOOL = "hermit_status"
APP_TOOL = "hermit_mini_app"
BAR_TOOL = "hermit_status_config"
MAX_STATUS = 100
MAX_STATUS_VALUES = 8
MAX_STATUS_VALUE = 30
STATUS_KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,23}")
APP_NAME = re.compile(r"[a-z0-9][a-z0-9-]{1,40}")

STATUS_SCHEMA = {
    "name": STATUS_TOOL,
    "description": (
        "Set the one-line status the Hermit app pins under this chat's header, for something the user wants to "
        "see at a glance without asking: a few live numbers, a build's state, what you're watching. "
        "Prefer `set`: named values you can change one at a time, such as {\"car\": \"🚗 88%\", \"garage\": "
        "\"closed\"}. Each call only touches the names you give, so the rest stay as they are, and a name set "
        "to an empty value is removed. Values show in the order they were first set, joined by dots. Use "
        "`text` for a single sentence in their place. `clear` removes the whole status. Keep it short enough "
        "to read in a second. Tapping the line can open an https link, or one of your mini apps by name."),
    "parameters": {
        "type": "object",
        "properties": {
            "set": {"type": "object", "additionalProperties": {"type": "string"},
                    "description": f"Named values to add or change, at most {MAX_STATUS_VALUES} in all and "
                                   f"{MAX_STATUS_VALUE} characters each. An empty value removes that name."},
            "remove": {"type": "array", "items": {"type": "string"}, "description": "Names to remove."},
            "text": {"type": "string", "description": f"One line in place of named values, at most {MAX_STATUS} characters."},
            "clear": {"type": "boolean", "description": "Remove the whole status."},
            "url": {"type": "string", "description": "Optional https link to open when the line is tapped. Empty removes it."},
            "app": {"type": "string", "description": "Optional mini app name to open when the line is tapped. Empty removes it."},
        },
    },
}

APP_SCHEMA = {
    "name": APP_TOOL,
    "description": (
        "Create or look up a mini app: a small web page the Hermit app opens from a button in this chat. "
        "Before building or changing one, read this plugin's mini-apps skill with skill_view: it has the rules "
        "the page and server must follow, a server to start from, and how to handle secrets safely. "
        "This returns the folder to write it in. Put an index.html there, plus any css, js, images or json "
        "it needs, using your normal file tools. Pages are served only from that folder and can't load "
        "anything from the internet, so keep everything local and write data the page reads, such as "
        "data.json, into the same folder. In the page, window.hermit offers close(), send(text) to send the "
        "user's message to you, openLink(url), haptic(), setStatus({name: value}) for the status bar, and "
        "theme colours as CSS variables like --hermit-bg, --hermit-text, --hermit-secondary, "
        "--hermit-surface, --hermit-accent and --hermit-on-accent. The accent is near white in dark mode, so "
        "text on an accent background must use --hermit-on-accent, never white. "
        "For live data, credentials or actions, give the app a server: pass `server` with `command`, such as "
        "[\"python3\", \"server.py\"] (python3 or node, run in the app's folder), and `secrets`, the names of "
        "environment variables it needs, such as [\"TESLA_TOKEN\"]. Write the server to listen on 127.0.0.1 "
        "at the port in the PORT environment variable and answer under /api/. The page calls it with relative "
        "addresses like fetch(\"api/state\"). The user's Mac runs it, keeps it running and restarts it, but "
        "only after the user allows it in the app, and again whenever command or secrets change, so tell them "
        "to open the mini app and allow it. Secrets come from the user's own Hermes secrets (their secrets "
        "manager or this bot's .env) as environment variables, and the app lets the user type in any that are "
        "missing: never ask for a secret's value in chat, never write one into a file, and never print one. "
        "Hermes's own keys and model provider keys can't be requested. The server can update the status bar "
        "any time by POSTing JSON {\"set\": {name: value}} to the address in HERMIT_STATUS_URL with the header "
        "Authorization: Bearer $HERMIT_STATUS_TOKEN. Its output goes to a log the user can read in the app."),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Lowercase letters, digits and dashes, such as home-os."},
            "title": {"type": "string", "description": "The button's label, one to three words."},
            "icon": {"type": "string", "description": "Optional SF Symbol name for the button, such as house or chart.bar."},
            "server": {"type": "object", "properties": {
                "command": {"type": "array", "items": {"type": "string"}},
                "secrets": {"type": "array", "items": {"type": "string"}}},
                "description": "A server for this app: command (python3 or node first) and the secret names it needs."},
            "server_off": {"type": "boolean", "description": "Remove this app's server."},
        },
        "required": ["name"],
    },
}


def _bot_home() -> Path:
    """This bot's own folder. Hermes scopes it to the profile whose turn is running."""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def status_line(status: dict) -> str:
    """What the app shows: the named values in order, or the single line."""
    values = [str(v) for v in (status.get("values") or {}).values() if str(v).strip()]
    return " · ".join(values) if values else str(status.get("text") or "")


def build_status(args: dict, current: dict | None = None) -> dict | None:
    """The status to save after this call, starting from what's saved now. None removes it."""
    if args.get("clear"):
        return None
    status = dict(current or {})
    values = dict(status.get("values") or {})
    if "text" in args:
        text = " ".join(str(args.get("text") or "").split())
        if len(text) > MAX_STATUS:
            raise ValueError(f"the status is {len(text)} characters: keep it to {MAX_STATUS}")
        # A sentence and named values are two ways to fill one line, so setting one drops the other.
        status["text"], values = text, {}
    changes = args.get("set") if isinstance(args.get("set"), dict) else {}
    for raw_key, raw_value in changes.items():
        key, value = str(raw_key).strip().lower(), " ".join(str(raw_value or "").split())
        if not STATUS_KEY.fullmatch(key):
            raise ValueError(f"{raw_key!r} isn't a usable name: use lowercase letters, digits, dashes or underscores")
        if len(value) > MAX_STATUS_VALUE:
            raise ValueError(f"the value for {key} is {len(value)} characters: keep each to {MAX_STATUS_VALUE}")
        if value:
            values[key] = value
            status.pop("text", None)
        else:
            values.pop(key, None)
    for raw_key in args.get("remove") or []:
        values.pop(str(raw_key).strip().lower(), None)
    if len(values) > MAX_STATUS_VALUES:
        raise ValueError(f"that makes {len(values)} values: keep it to {MAX_STATUS_VALUES}, or remove some first")
    status["values"] = values
    if "url" in args:
        link = _https(args.get("url"))
        if args.get("url") and not link:
            raise ValueError("url has to be an https link")
        status.pop("url", None) if not link else status.update(url=link)
    if "app" in args:
        name = str(args.get("app") or "").strip().lower()
        if name and not APP_NAME.fullmatch(name):
            raise ValueError("app has to be a mini app's name: lowercase letters, digits and dashes")
        status.pop("app", None) if not name else status.update(app=name)
    line = status_line(status)
    if not line:
        return None
    if len(line) > MAX_STATUS + 40:
        raise ValueError(f"together the values make a {len(line)}-character line: shorten or remove some")
    status["at"] = datetime.now().timestamp()
    return status


def handle_status(args, **_kwargs):
    try:
        path = _bot_home() / "hermit" / "status.json"
        try:
            current = json.loads(path.read_text())
        except (OSError, ValueError):
            current = None
        status = build_status(args or {}, current if isinstance(current, dict) else None)
        if status is None:
            path.unlink(missing_ok=True)
            return json.dumps({"cleared": True})
        _write_json(path, status)
        off = _switched_off(_bot_home())
        note = ("Saved, but the user has switched off " + " and ".join(off) + " for this status bar in the app, so it may not show."
                if any(word in off for word in ("the whole status bar", "your own updates"))
                else "The user sees this line under the chat's header. Don't repeat it.")
        return json.dumps({"shown": status_line(status), "values": status.get("values") or {}, "note": note})
    except (ValueError, OSError) as exc:
        return json.dumps({"error": str(exc)})


BAR_SCHEMA = {
    "name": BAR_TOOL,
    "description": (
        "Read or change how this chat's status bar in the Hermit app is set up. Call it with nothing to see "
        "the setup: the order of the values, which are hidden, the feed, and which sources the user has switched "
        "off. `order` lists value names in the order to show them. `hidden` lists names to keep but not show. "
        "`feed` has the user's Mac read a public https JSON address on a schedule and fill values from it with "
        "no turn from you: give `url`, `every` (seconds, 60 at least, 300 usual) and `values`, a map of value "
        "name to a template such as \"{current.temperature_2m|round}°\", where the braces hold a dotted path "
        "into the JSON and `|round` rounds a number. Use a feed for numbers that change on their own; keep "
        "hermit_status for things only you know. `feed_off` removes the feed. A mini app can also set values "
        "itself with window.hermit.setStatus({name: value}). The user can change all of this in the app, and "
        "can switch you, the feed or mini apps off, so read the setup before assuming a value is showing."),
    "parameters": {
        "type": "object",
        "properties": {
            "order": {"type": "array", "items": {"type": "string"}, "description": "Value names, first to last."},
            "hidden": {"type": "array", "items": {"type": "string"}, "description": "Value names to keep but not show. Replaces the list."},
            "feed": {"type": "object", "properties": {
                "url": {"type": "string"}, "every": {"type": "integer"},
                "values": {"type": "object", "additionalProperties": {"type": "string"}}},
                "description": "A public https JSON address for the Mac to read on a schedule."},
            "feed_off": {"type": "boolean", "description": "Remove the feed."},
        },
    },
}


def _names(value) -> list:
    names = [str(name).strip().lower() for name in value] if isinstance(value, list) else []
    bad = [name for name in names if not STATUS_KEY.fullmatch(name)]
    if bad:
        raise ValueError(f"{bad[0]!r} isn't a usable name: use lowercase letters, digits, dashes or underscores")
    return list(dict.fromkeys(names))


def build_feed(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("feed needs url, every and values")
    link = _https(value.get("url"))
    if not link:
        raise ValueError("the feed's url has to be an https link")
    values = value.get("values") if isinstance(value.get("values"), dict) else {}
    if not values or len(values) > MAX_STATUS_VALUES:
        raise ValueError(f"the feed needs between 1 and {MAX_STATUS_VALUES} values, each a name and a template")
    cleaned = {}
    for raw_key, template in values.items():
        key = str(raw_key).strip().lower()
        if not STATUS_KEY.fullmatch(key):
            raise ValueError(f"{raw_key!r} isn't a usable name: use lowercase letters, digits, dashes or underscores")
        if "{" not in str(template) or len(str(template)) > 200:
            raise ValueError(f"the template for {key} needs a {{path}} into the JSON, and at most 200 characters")
        cleaned[key] = str(template)
    try:
        every = int(value.get("every") or 300)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("every has to be a number of seconds") from None
    return {"url": link, "every": max(60, min(86400, every)), "values": cleaned}


def _switched_off(home: Path) -> list:
    """Sources the user turned off for this bot in the app, read from the bridge's own notes."""
    profile = home.name if home.parent.name == "profiles" else "default"
    try:
        saved = json.loads((Path.home() / ".hermes" / "app-bridge" / "bars.json").read_text()).get(profile)
    except (OSError, ValueError, AttributeError):
        return []
    saved = saved if isinstance(saved, dict) else {}
    names = {"show": "the whole status bar", "bot": "your own updates", "feed": "the feed", "app": "mini apps"}
    return [words for name, words in names.items() if saved.get(name) is False]


def handle_bar(args, **_kwargs):
    try:
        args = args or {}
        home = _bot_home()
        path = home / "hermit" / "bar.json"
        try:
            saved = json.loads(path.read_text())
        except (OSError, ValueError):
            saved = {}
        saved = saved if isinstance(saved, dict) else {}
        before = json.dumps(saved, sort_keys=True)
        if "order" in args:
            saved["order"] = _names(args.get("order"))
        if "hidden" in args:
            saved["hidden"] = _names(args.get("hidden"))
        if args.get("feed_off"):
            saved.pop("feed", None)
        elif "feed" in args:
            saved["feed"] = build_feed(args.get("feed"))
        if json.dumps(saved, sort_keys=True) != before:
            _write_json(path, saved)
        try:
            status = json.loads((home / "hermit" / "status.json").read_text())
        except (OSError, ValueError):
            status = {}
        profile = home.name if home.parent.name == "profiles" else "default"
        try:
            filled = json.loads((Path.home() / ".hermes" / "app-bridge" / "bar-values" / f"{profile}.json").read_text())
        except (OSError, ValueError):
            filled = {}
        result = {"order": saved.get("order") or [], "hidden": saved.get("hidden") or [], "feed": saved.get("feed"),
                  "values": {"yours": (status.get("values") if isinstance(status, dict) else None) or {},
                             "feed": (filled.get("feed") if isinstance(filled, dict) else None) or {},
                             "mini_apps": (filled.get("app") if isinstance(filled, dict) else None) or {}},
                  "switched_off_by_user": _switched_off(home)}
        if isinstance(filled, dict) and filled.get("feed_problem"):
            result["feed_problem"] = str(filled["feed_problem"])
        if "feed" in args and not args.get("feed_off"):
            result["note"] = "The Mac reads a new feed within about 15 seconds. Call this again to see its values or a problem."
        return json.dumps(result)
    except (ValueError, OSError) as exc:
        return json.dumps({"error": str(exc)})


SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]{1,60}")


def build_server(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("server needs command, and secrets if it uses any")
    command = value.get("command")
    if not isinstance(command, list) or not 1 <= len(command) <= 12 or not all(isinstance(part, str) and 0 < len(part) <= 300 for part in command):
        raise ValueError("command has to be a short list of words, such as [\"python3\", \"server.py\"]")
    if command[0] not in ("python", "python3", "node"):
        raise ValueError("the server has to be run with python3 or node")
    names = [str(name).strip() for name in value.get("secrets") or []]
    bad = [name for name in names if not SECRET_NAME.fullmatch(name)]
    if bad or len(names) > 12:
        raise ValueError("secrets are names of environment variables in capitals, such as TESLA_TOKEN, and 12 at most")
    own = {"API_SERVER_KEY", "PATH", "HOME", "LANG", "TMPDIR", "HOST", "PORT", "USER", "SHELL", "NODE_OPTIONS", "NODE_PATH", "BASH_ENV", "ENV"}
    prefixes = ("HERMES_", "GATEWAY_", "AUXILIARY_", "HERMIT_", "PYTHON", "DYLD_", "LD_", "LC_", "TERMINAL_")
    hermes = [name for name in names if name in own or name.startswith(prefixes)]
    if hermes:
        raise ValueError(f"{hermes[0]} belongs to Hermes or the system and can't be given to a mini app")
    return {"command": command, "secrets": list(dict.fromkeys(names))}


def handle_mini_app(args, **_kwargs):
    try:
        name = str((args or {}).get("name") or "").strip().lower()
        if not APP_NAME.fullmatch(name):
            raise ValueError("name has to be lowercase letters, digits and dashes, such as home-os")
        folder = _bot_home() / "hermit" / "apps" / name
        folder.mkdir(parents=True, exist_ok=True)
        about = folder / "app.json"
        try:
            details = json.loads(about.read_text()) if about.exists() else {}
        except ValueError:
            details = {}
        details = details if isinstance(details, dict) else {}
        for key in ("title", "icon"):
            if _text(args.get(key)):
                details[key] = _text(args.get(key))[:40]
        note = "Write index.html and its files in this folder. The app's button appears in this chat once index.html exists."
        if args.get("server_off"):
            details.pop("server", None)
        elif "server" in args:
            details["server"] = build_server(args.get("server"))
            note += (" Write the server too. It starts once the user opens the mini app and allows it, and they'll be "
                     "asked again if its command or secrets change.")
        _write_json(about, details)
        return json.dumps({"folder": str(folder), "has_page": (folder / "index.html").exists(),
                           "server": details.get("server"), "note": note})
    except (ValueError, OSError) as exc:
        return json.dumps({"error": str(exc)})


BRIDGE = ("127.0.0.1", 8651)
MAX_DETAIL = 240
_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def _emit(**event):
    """Fire and forget to the app bridge. A missing bridge must never affect the agent."""
    try:
        _socket.sendto(json.dumps(event).encode()[:60000], BRIDGE)
    except (OSError, TypeError, ValueError):
        pass


def on_stream_delta(delta=None, kind=None, session_id=None, **_kwargs):
    if kind == "text" and delta:
        _emit(type="delta", session=session_id, text=delta)


def on_stream_end(session_id=None, **_kwargs):
    _emit(type="stream_end", session=session_id)


# Switches in the app's Settings → Integrations, saved here by the Mac bridge. Missing means off.
INTEGRATIONS = Path.home() / ".hermes" / "app-bridge" / "integrations.json"
# What reaches Messages or Notes on the Mac. This catches ordinary use, the way the skills work.
# It isn't a sandbox: a bot with a terminal could still find another way in.
GUARDED = {
    "messagesRead": (re.compile(r"\bimsg\b|chat\.db|Library/Messages", re.IGNORECASE), "Messages"),
    "notes": (re.compile(r"\bmemo\b|NoteStore|com\.apple\.notes|application \"Notes\"", re.IGNORECASE), "Notes"),
}


def _integrations():
    try:
        return json.loads(INTEGRATIONS.read_text())
    except (OSError, ValueError):
        return {}


def blocked(args):
    """A message for the model if this call reaches something the user has turned off, otherwise None."""
    text = json.dumps(args) if not isinstance(args, str) else args
    allowed = _integrations()
    for key, (pattern, app) in GUARDED.items():
        if allowed.get(key) is not True and pattern.search(text):
            return (f"The user has turned off bot access to {app} in the Hermit app (Settings → Integrations). "
                    "Don't try another way. Tell them it's off, and that they can turn it on there.")
    return None


def on_pre_tool_call(tool_name=None, args=None, session_id=None, **_kwargs):
    if tool_name in (TOOL_NAME, KIND_TOOL):
        return None
    if (reason := blocked(args)):
        return {"action": "block", "message": reason}
    _emit(type="tool", session=session_id, tool=tool_name, detail=str(args)[:MAX_DETAIL])
    return None


ROUTINE = re.compile(r'^\[Cronjob "([^"]+)"')


def routine_name(user_message):
    """The routine behind a turn, such as "Morning briefing", or None for a person's message."""
    if isinstance(user_message, str) and user_message.startswith("[Cronjob"):
        match = ROUTINE.match(user_message)
        return match.group(1) if match else "Routine"
    return None


def on_post_llm_call(session_id=None, user_message=None, assistant_response=None, **_kwargs):
    # Routine results can notify too. The phone decides which routines may, per routine.
    event = {"type": "turn", "session": session_id, "reply": str(assistant_response or "")[:4000]}
    if (routine := routine_name(user_message)):
        event["routine"] = routine
    _emit(**event)


def on_pre_llm_call(user_message=None, session_id=None, conversation_history=None, **_kwargs):
    # A tapped card button that runs a connector tool: done here, in this bot's own turn, with no model call.
    if isinstance(user_message, str) and (outcome := run_card_call(user_message, conversation_history)) is not None:
        return {"action": "block", "message": outcome}
    if isinstance(user_message, str) and user_message.rstrip(" \n").endswith(APP_MARKER):
        # Tells the bridge to watch this turn, so an approval can reach the phone even if the app closes.
        _emit(type="app_turn", session=session_id)
        on_call = user_message.rstrip(" \n").endswith(APP_MARKER * 2)
        return {"context": PHONE_GUIDE + (VOICE_GUIDE if on_call else "")}
    return None


def _setup_arguments(parser):
    parser.add_argument("action", choices=["setup", "bridge"], help="setup connects the app; bridge runs its service")
    parser.add_argument("--check", action="store_true", help="report what's ready and change nothing")
    parser.add_argument("--yes", action="store_true", help="don't ask before each change")
    parser.add_argument("--url", help="your own https:// address, instead of Tailscale")
    parser.add_argument("--no-restart", action="store_true", help="leave restarting the gateway to you")
    parser.add_argument("--proxy-allow", action="append", metavar="EMAIL", help="who may pair once your sign-in proxy signs them in")
    parser.add_argument("--proxy-jwks", metavar="URL", help="where your proxy publishes its token signing keys")
    parser.add_argument("--proxy-jwt-header", metavar="NAME", help="the header carrying the signed token")
    parser.add_argument("--proxy-issuer", metavar="URL", help="the token's issuer")
    parser.add_argument("--proxy-audience", metavar="ID", help="the token's audience")
    parser.add_argument("--proxy-email-header", metavar="NAME", help="a plain header with the email, guarded by a secret")
    parser.add_argument("--proxy-off", action="store_true", help="stop pairing through the proxy")


def _proxy_flags(args) -> list:
    flags = [flag for email in (getattr(args, "proxy_allow", None) or []) for flag in ("--proxy-allow", email)]
    for name in ("proxy_jwks", "proxy_jwt_header", "proxy_issuer", "proxy_audience", "proxy_email_header"):
        if value := getattr(args, name, None):
            flags += ["--" + name.replace("_", "-"), value]
    return flags + (["--proxy-off"] if getattr(args, "proxy_off", False) else [])


def _run_setup(args):
    """`hermes hermit setup` and `hermes hermit bridge`. An older Hermes has a venv whose Python runs them. A
    newer one keeps its packages where only its own launcher finds them, and this is already running inside
    that launcher, so they run right here."""
    import os, pathlib, runpy, subprocess, sys
    folder = pathlib.Path(__file__).parent / "bridge"
    script = folder / ("hermes_app_bridge.py" if args.action == "bridge" else "hermit_setup.py")
    flags = [] if args.action == "bridge" else [
        flag for flag, on in (("--check", args.check), ("--yes", args.yes), ("--no-restart", args.no_restart)) if on
    ] + (["--url", args.url] if args.url else []) + _proxy_flags(args)
    python = pathlib.Path.home() / ".hermes/hermes-agent/venv/bin/python3"
    if python.exists():
        if args.action == "bridge":
            os.execv(str(python), [str(python), str(script)])
        return subprocess.call([str(python), str(script), *flags])
    sys.argv = [str(script), *flags]
    sys.path.insert(0, str(folder))
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as done:
        return done.code if isinstance(done.code, int) else int(bool(done.code))
    return 0


def register(ctx):
    import pathlib
    if (pathlib.Path(__file__).parent / "bridge/hermit_setup.py").exists():
        ctx.register_cli_command("hermit", "Connect the Hermit app to this Hermes", _setup_arguments, _run_setup)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("on_stream_delta", on_stream_delta)
    ctx.register_hook("on_stream_end", on_stream_end)
    ctx.register_tool(name=TOOL_NAME, toolset=TOOLSET, schema=SCHEMA, handler=handle,
                      description=SCHEMA["description"], emoji="🃏")
    # In the same toolset as cards, so a bot that has cards has these without another switch.
    ctx.register_tool(name=STATUS_TOOL, toolset=TOOLSET, schema=STATUS_SCHEMA, handler=handle_status,
                      description=STATUS_SCHEMA["description"], emoji="📌")
    ctx.register_tool(name=CONTACTS_TOOL, toolset=TOOLSET, schema=CONTACTS_SCHEMA, handler=handle_contacts,
                      description=CONTACTS_SCHEMA["description"], emoji="👤")
    # Older Hermes has no plugin skills. The tools still say enough to build a simple page.
    try:
        ctx.register_skill("mini-apps", Path(__file__).parent / "skills" / "mini-apps" / "SKILL.md",
                           "Build a mini app for the Hermit app: a page in the chat, with an optional server for live "
                           "data, credentials and actions, and values on the status bar.")
    except Exception:
        pass
    ctx.register_tool(name=BAR_TOOL, toolset=TOOLSET, schema=BAR_SCHEMA, handler=handle_bar,
                      description=BAR_SCHEMA["description"], emoji="🎛️")
    ctx.register_tool(name=APP_TOOL, toolset=TOOLSET, schema=APP_SCHEMA, handler=handle_mini_app,
                      description=APP_SCHEMA["description"], emoji="🧩")
    ctx.register_tool(name=KIND_TOOL, toolset=TOOLSET, schema=KIND_SCHEMA, handler=handle_kind,
                      description=KIND_SCHEMA["description"], emoji="🧱")
