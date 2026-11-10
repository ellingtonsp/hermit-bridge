---
name: mini-apps
description: Build a mini app for the Hermit app - a small page in the user's chat, with an optional server for live data, credentials and actions, and values on the status bar. Read this before creating or changing one.
---

# Building a mini app for Hermit

A mini app is a small web page the user opens from a button in your chat in the Hermit app. It lives in a
folder on the user's own computer. It can have a small server beside it, which their computer runs, and it
can put values on the status bar under the chat's header.

Build the smallest thing that does the job. Most requests need only the first or second shape below.

## Pick the shape

| The user wants | Build | Needs their OK in the app |
| --- | --- | --- |
| A number or two at a glance | Status bar only: `hermit_status`, or a live feed with `hermit_status_config` | No |
| A page of things you already know or can fetch in a turn | A page that reads `data.json`, which you rewrite | No |
| Live data, anything with a login or key, or buttons that do things | A page plus a server | Yes, once |

Don't build a server to show data that changes a few times a day. Rewrite `data.json` when you learn
something new, or from a routine.

## The page

1. Call `hermit_mini_app` with `name`, `title` and `icon` (an SF Symbol name). It returns the folder.
2. Write `index.html` there, with its CSS and JavaScript inline or in files beside it.

Rules the page must follow, because the app enforces them:

- **Nothing loads from the internet.** No CDN scripts, web fonts, remote images or outside `fetch`. Put
  every file the page needs in its folder. Use the system font: `font: -apple-system-body` or
  `font-family: -apple-system, system-ui`.
- **Relative addresses only.** `fetch("data.json")`, `fetch("api/state")`, `<img src="img/a.png">`. Never
  start an address with `/` or `http`.
- **Follow the app's colours** so it works in light and dark: `--hermit-bg`, `--hermit-surface`,
  `--hermit-text`, `--hermit-secondary`, `--hermit-separator`, `--hermit-accent`, `--hermit-on-accent`,
  `--hermit-danger`, `--hermit-success`. Listen for the `hermit:theme` event if you draw to a canvas.
- **Never write a colour as a literal**: no `#fff`, `white`, `black` or `rgb(...)` for text, backgrounds or
  borders. A literal that looks right in light mode is invisible in dark. Fallbacks inside `var()` are fine.
- **The accent is not blue.** It's near black in light mode and near white in dark. So white text on an
  accent button disappears in dark mode. Colours come in pairs, and each background has one text colour:

  | Background | Text on it |
  | --- | --- |
  | `--hermit-accent` | `--hermit-on-accent` |
  | `--hermit-bg` or `--hermit-surface` | `--hermit-text` or `--hermit-secondary` |
  | `--hermit-surface`, for a warning or a good state | `--hermit-danger` or `--hermit-success`, as text or border only |

  Don't use `--hermit-danger` or `--hermit-success` as a background: they have no paired text colour.

- **Buttons are styled for you.** A plain `<button>` already gets the accent pair, a 44px height and rounded
  corners. Leave it alone for a main action. For a quieter one, use
  `background: var(--hermit-surface); color: var(--hermit-text); border: 1px solid var(--hermit-separator)`.
  If you set a button's background, set its `color` from the same row of the table.
- **It's a phone.** One column, 16px side padding, text 15px or larger, tap targets 44px or taller, and
  bottom padding of `env(safe-area-inset-bottom)`. The sheet can be half height, so put what matters first.
- **Refresh without reloading.** Fetch on a timer (every 5 to 30 seconds is plenty) and update the page in
  place. Show when the data was last updated, and show a plain message when a fetch fails.
- **Never put text from data into `innerHTML`.** Use `textContent`. Data from other services is not yours.

What the page can ask the app to do, through `window.hermit`:

- `send(text)` sends a message to you as if the user typed it, and closes the page. The user is shown the
  message and confirms it first, so keep it short and say what it will do. Use it for anything that needs
  your judgment.
- `setStatus({name: value})` puts values on the status bar. An empty value removes one.
- `openLink(url)` opens an https link outside, after the user confirms the site. A normal `<a href>` link
  the user taps opens without asking. `close()` closes the page. `haptic()` taps.

## The server

Add one only when the page needs live data, a credential, or to do something.

Call `hermit_mini_app` again with `server`:

```json
{"name": "home", "server": {"command": ["python3", "server.py"], "secrets": ["TESLA_TOKEN"]}}
```

Then write the server in the same folder. The user's computer starts it after they allow it in the app,
keeps it running, and restarts it if it stops. Tell the user: "Open the mini app and allow its server."
They are asked again whenever `command` or `secrets` change, so settle those before asking.

What the server must do:

- Listen on `127.0.0.1` at the port in the `PORT` environment variable. Never `0.0.0.0`.
- Answer under `/api/`. The page's `fetch("api/state")` arrives as `GET /api/state`.
- Use Python's standard library or Node's built-in modules. Don't install packages: the server runs in a
  bare environment and an install the user didn't ask for is a surprise.
- Answer within 30 seconds, with at most 10 MB. No streaming or websockets: the page polls.
- Keep working when a service is down. Cache the last good answer and say how old it is.

A starting point:

```python
import json, os, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

state = {"updated": None, "problem": None}

def refresh():
    while True:
        try:
            request = urllib.request.Request("https://api.example.com/v1/thing",
                                             headers={"Authorization": "Bearer " + os.getenv("EXAMPLE_TOKEN")})
            with urllib.request.urlopen(request, timeout=10) as reply:
                data = json.load(reply)
            state.update(value=data["value"], updated=time.time(), problem=None)
            set_status({"thing": str(data["value"])})
        except Exception as error:
            state["problem"] = type(error).__name__   # never the error text: it can hold the address and token
        time.sleep(60)

def set_status(values):
    request = urllib.request.Request(os.getenv("HERMIT_STATUS_URL"), data=json.dumps({"set": values}).encode(),
                                     headers={"Authorization": "Bearer " + os.getenv("HERMIT_STATUS_TOKEN"),
                                              "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(request, timeout=5).close()
    except OSError:
        pass

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] != "/api/state":
            return self.send_error(404)
        body = json.dumps(state).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

threading.Thread(target=refresh, daemon=True).start()
ThreadingHTTPServer(("127.0.0.1", int(os.getenv("PORT"))), Handler).serve_forever()
```

## Secrets

- **Ask for names, never values.** List what the server needs in `secrets`, as environment variable names
  in capitals. The user's computer hands them to the server from the user's own Hermes secrets: their
  secrets manager if they use one, otherwise this bot's `.env`.
- **Never ask the user to paste a key in chat.** If one is missing, the app shows a field for it when they
  open the mini app. Tell them which name it will ask for and where to get the key.
- **Never write a secret into a file, the page, `data.json`, a log line or your reply.** The server reads
  it with `os.getenv` and nowhere else.
- **Never send a secret to the page.** The page gets results, not credentials. Check every `/api/` answer.
- **Hermes's own keys are off limits**: this bot's key, model provider keys, and names starting with
  `HERMES_`, `GATEWAY_`, `AUXILIARY_` or `HERMIT_`. Asking for one stops the server from starting.
- **Ask for the narrowest access.** Prefer a read-only token. If the service has scopes, name the one needed.

## Actions

A button that does something real (unlock, send, buy, delete) needs more care than a number on a page.

- Use `POST` for anything that changes something. Never change things on `GET`.
- Allow a fixed list of actions on the server. Never pass a path, address, command or query from the page
  through to a service or a shell.
- Make the page confirm before anything hard to undo, and show what happened afterwards.
- For anything that needs judgment, don't act from the server: have the page call
  `window.hermit.send("Lock the front door")` so the request comes to you in chat.
- Never run a shell command built from request data.

## The status bar

- `hermit_status` with `set` for values only you know. Each call changes only the names you give.
- `hermit_status_config` with `feed` for a public JSON address with no login: the user's computer reads it
  on a schedule with no turn from you.
- From a server, POST `{"set": {...}}` to `HERMIT_STATUS_URL` as in the example above.
- Keep each value under 30 characters and the whole line readable in a second: an emoji and a number beats
  a sentence. Set `app` on the status so a tap opens the mini app.
- The user can hide the bar, reorder it, or switch you, the feed or mini apps off. Call
  `hermit_status_config` with nothing to see how it stands before telling them a value is showing.

## Before you say it's done

- [ ] The page has no address starting with `http` or `/`, and loads nothing from outside its folder.
- [ ] It reads well at phone width, and says something useful when data is missing.
- [ ] Dark mode: no colour literals outside `var()` fallbacks, and every button, badge and chip takes its
      background and text from the same row of the colour table. Search the page for `#` and `white`.
- [ ] No secret appears in any file, log line or `/api/` answer. Search the folder for the values' names.
- [ ] The server listens on `127.0.0.1:$PORT`, answers under `/api/`, and survives a service being down.
- [ ] Changing things takes a `POST` to a fixed action, with a confirmation for anything hard to undo.
- [ ] You told the user what to allow, which secrets it will ask for, and where to get them.

If the server won't start or the page shows an error, the user can read the server's log in the app, from
the mini app's menu. Ask them what it says rather than guessing.
