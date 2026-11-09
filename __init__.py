"""Rich cards for the Hermes iPhone app.

The tool's JSON result stays in the session transcript, where the app reads and renders it,
so cards survive reloads. The tool itself does nothing external: an action's ``message`` comes
back to the agent as an ordinary user turn, and only then does the agent act.
"""
import json
from pathlib import Path
import re
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
    "or reminders yourself unless asked. To draft a text message, use app 'Messages' with a message object "
    "and no actions. The user edits it and sends it themselves from their iPhone, and you'll get a message "
    "saying it went out. Never send a text yourself when you've shown one this way. When you read texts for "
    "the user, show the relevant part with app 'Messages' and a thread object, up to the last 12 messages "
    "with handles for replying, instead of pasting them into your reply."
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
                    },
                    "required": ["label"],
                },
            },
        },
        "required": ["app", "title"],
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
    return event


MAX_THREAD = 12


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
    who = _text(raw.get("with"))
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
        message, link = _text(raw.get("message")), _https(raw.get("url"))
        if not message and not link:
            raise ValueError(f"action {action['label']!r} needs a message or an https url")
        if message:
            action["message"] = message
        if link:
            action["url"] = link
        actions.append(action)
    if len({a["style"] == "option" for a in actions}) > 1:
        raise ValueError("use option actions or buttons on a card, not both")
    if actions:
        card["actions"] = actions
    return card


def handle(args, **_kwargs):
    try:
        return json.dumps({"card": build_card(args), "shown": True,
                           "note": "The user can see this card. Don't repeat what's on it. Reply in one short "
                                   "sentence at most, or only with what isn't on the card."})
    except ValueError as exc:
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
    if tool_name == TOOL_NAME:
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


def on_pre_llm_call(user_message=None, session_id=None, **_kwargs):
    if isinstance(user_message, str) and user_message.rstrip(" \n").endswith(APP_MARKER):
        # Tells the bridge to watch this turn, so an approval can reach the phone even if the app closes.
        _emit(type="app_turn", session=session_id)
        on_call = user_message.rstrip(" \n").endswith(APP_MARKER * 2)
        return {"context": PHONE_GUIDE + (VOICE_GUIDE if on_call else "")}
    return None


def _setup_arguments(parser):
    parser.add_argument("action", choices=["setup"])
    parser.add_argument("--check", action="store_true", help="report what's ready and change nothing")
    parser.add_argument("--yes", action="store_true", help="don't ask before each change")
    parser.add_argument("--url", help="your own https:// address, instead of Tailscale")
    parser.add_argument("--no-restart", action="store_true", help="leave restarting the gateway to you")


def _run_setup(args):
    import pathlib, subprocess, sys
    home = pathlib.Path.home() / ".hermes"
    python = home / "hermes-agent/venv/bin/python3"
    command = [str(python if python.exists() else sys.executable), str(pathlib.Path(__file__).parent / "bridge/hermit_setup.py")]
    command += [flag for flag, on in (("--check", args.check), ("--yes", args.yes), ("--no-restart", args.no_restart)) if on]
    command += ["--url", args.url] if args.url else []
    return subprocess.call(command)


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
