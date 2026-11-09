#!/usr/bin/env python3
"""Connects Hermit to the Hermes on this Mac. Run through bridge/install.sh.

--check only reports. Otherwise each change is listed and confirmed first. Nothing leaves this Mac, and
the pairing code it prints is short-lived and holds no keys.
"""
import argparse
import base64
import json
import os
import pathlib
import plistlib
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).parent))

HOME = pathlib.Path.home() / ".hermes"
REPO = pathlib.Path(__file__).resolve().parent.parent
LABEL = "ai.hermes.app-bridge"
HTTPS_PORT = 8444
BRIDGE_PORT = 8650
API_PORT = "8642"
BREW_FOLDERS = ("/opt/homebrew/bin", "/usr/local/bin")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def find_tool(name: str, *extra: str) -> str | None:
    if found := shutil.which(name):
        return found
    for folder in (*BREW_FOLDERS, *extra):
        if os.access(path := os.path.join(folder, name), os.X_OK):
            return path
    return None


def tailscale_binary() -> str | None:
    return find_tool("tailscale") or find_tool("Tailscale", "/Applications/Tailscale.app/Contents/MacOS")


def profiles(home: pathlib.Path) -> dict[str, pathlib.Path]:
    found = {"default": home}
    folder = home / "profiles"
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            if (path / "config.yaml").exists() or (path / ".env").exists():
                found[path.name] = path
    return found


def api_key(home: pathlib.Path) -> str | None:
    env = home / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("API_SERVER_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"") or None
    return None


def missing_keys(home: pathlib.Path) -> list[str]:
    return [name for name, path in profiles(home).items() if not api_key(path)]


def add_api_key(home: pathlib.Path, is_default: bool, key: str) -> None:
    """Adds what the API server needs and leaves every other line of the file alone."""
    env = home / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    lines = [line for line in lines if not line.startswith("API_SERVER_KEY=")]
    if is_default:
        have = {line.split("=", 1)[0] for line in lines if "=" in line}
        defaults = {"API_SERVER_ENABLED": "true", "API_SERVER_HOST": "127.0.0.1", "API_SERVER_PORT": API_PORT}
        lines += [f"{name}={value}" for name, value in defaults.items() if name not in have]
    lines.append(f"API_SERVER_KEY={key}")
    env.touch(mode=0o600)
    env.write_text("\n".join(lines) + "\n")
    env.chmod(0o600)


def tailnet_host(status: dict) -> str | None:
    if status.get("BackendState") != "Running":
        return None
    return ((status.get("Self") or {}).get("DNSName") or "").rstrip(".") or None


def launch_agent(python: str, bridge: str, log: str) -> bytes:
    return plistlib.dumps({"Label": LABEL, "ProgramArguments": [python, bridge], "RunAtLoad": True,
                           "KeepAlive": True, "StandardOutPath": log, "StandardErrorPath": log})


def pairing_code(home: pathlib.Path, url: str) -> str:
    bots = [{"profile": name, "key": key} for name, path in profiles(home).items() if (key := api_key(path))]
    return base64.b64encode(json.dumps({"url": url, "bots": bots}).encode()).decode()


def current_host() -> str | None:
    binary = tailscale_binary()
    if not binary:
        return None
    result = subprocess.run([binary, "status", "--json"], capture_output=True, text=True)
    try:
        return tailnet_host(json.loads(result.stdout))
    except ValueError:
        return None


def platform_problem(platform: str) -> str | None:
    """The bridge runs as a macOS launch agent, so anything else has to stop before changes are made."""
    if platform == "darwin":
        return None
    return "  MISSING  Mac: Hermit's setup only works with Hermes running on a Mac for now. Nothing was changed."


def checks(home: pathlib.Path, public_url: str | None = None) -> list[Check]:
    import hermes_app_bridge as bridge  # Here so a missing Python package is reported, not a crash.
    found = bridge.hermes_compatibility()
    version = found.get("version") or "unknown version"
    host = current_host()
    reach = (Check("Your address", True, public_url) if public_url
             else Check("Tailscale", bool(host), host or "Install Tailscale and sign in, or pass --url with your own HTTPS address."))
    return [
        Check("Hermes", found["compatible"], version if found["compatible"] else found["problem"]),
        reach,
        Check("Node", bool(find_tool("node", *nvm_folders())), "Draws bot faces", required=False),
        Check("rsvg-convert", bool(find_tool("rsvg-convert")), "Draws bot faces: brew install librsvg", required=False),
        Check("gog", bool(find_tool("gog")), "Sends email from cards", required=False),
    ]


def nvm_folders() -> list[str]:
    return [str(path) for path in sorted(pathlib.Path.home().glob(".nvm/versions/node/*/bin"))]


def confirm(question: str, assume_yes: bool) -> bool:
    return assume_yes or input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def run(*command: str) -> None:
    subprocess.run(command, check=True)


def install_plugin(home: pathlib.Path, hermes: str) -> None:
    # Installed through `hermes plugins install`, this folder already is the plugin.
    packaged = (REPO / "plugin.yaml").exists()
    plugin = "hermit" if packaged else "hermes-cards"
    target = REPO if packaged else home / "plugins/hermes-cards"
    if not packaged:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(REPO / "plugin/hermes-cards", target, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "test_*"))
    for name, path in profiles(home).items():
        if name != "default":
            link = path / f"plugins/{plugin}"
            link.parent.mkdir(parents=True, exist_ok=True)
            if not link.exists():
                link.symlink_to(target)
        run(hermes, "-p", name, "plugins", "enable", plugin)
        run(hermes, "-p", name, "tools", "enable", "--platform", "api_server", "hermes_cards")


def install_bridge(python: str, tailscale: str | None, api_port: str) -> None:
    (HOME / "logs").mkdir(exist_ok=True)
    agent = pathlib.Path.home() / f"Library/LaunchAgents/{LABEL}.plist"
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_bytes(launch_agent(python, str(REPO / "bridge/hermes_app_bridge.py"), str(HOME / "logs/app-bridge.log")))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    run("launchctl", "bootstrap", domain, str(agent))
    if not tailscale:
        return
    run(tailscale, "serve", "--bg", f"--https={HTTPS_PORT}", f"http://127.0.0.1:{api_port}")
    run(tailscale, "serve", "--bg", f"--https={HTTPS_PORT}", "--set-path=/app", f"http://127.0.0.1:{BRIDGE_PORT}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Connect Hermit to the Hermes on this Mac.")
    parser.add_argument("--check", action="store_true", help="report what's ready and change nothing")
    parser.add_argument("--yes", action="store_true", help="don't ask before each change")
    parser.add_argument("--url", help="your own https:// address, instead of Tailscale")
    parser.add_argument("--no-restart", action="store_true", help="leave restarting the Hermes gateway to you")
    options = parser.parse_args()
    public_url = options.url.rstrip("/") if options.url else None
    if public_url and not public_url.startswith("https://"):
        print("Your address has to start with https://.")
        return 1

    if problem := platform_problem(sys.platform):
        print(problem)
        return 1

    results = checks(HOME, public_url)
    for check in results:
        mark = "ok" if check.ok else "MISSING" if check.required else "optional"
        print(f"  {mark:9}{check.name}: {check.detail}")
    missing = missing_keys(HOME)
    print(f"  {'ok' if not missing else 'to add':9}API keys: {len(profiles(HOME)) - len(missing)} of {len(profiles(HOME))} profiles")
    if any(c.required and not c.ok for c in results):
        print("\nFix the items marked MISSING, then run this again.")
        return 1
    if options.check:
        return 0

    host = current_host()
    hermes = find_tool("hermes", str(pathlib.Path.home() / ".local/bin"), str(HOME / "bin"))
    if not hermes:
        print("Couldn't find the hermes command.")
        return 1
    if missing and confirm(f"Add an API key to {len(missing)} profile(s)?", options.yes):
        all_profiles = profiles(HOME)
        for name in missing:
            add_api_key(all_profiles[name], name == "default", secrets.token_urlsafe(32))
    if confirm("Install the hermes-cards plugin in every profile?", options.yes):
        install_plugin(HOME, hermes)
    python = str(HOME / "hermes-agent/venv/bin/python3")
    if confirm(f"Install the Hermit bridge, reachable at {public_url or host}?", options.yes):
        install_bridge(python if os.path.exists(python) else sys.executable, None if public_url else tailscale_binary(), API_PORT)
    if public_url:
        print(f"\nPoint {public_url} at this Mac: / to 127.0.0.1:{API_PORT}, /app to 127.0.0.1:{BRIDGE_PORT}, and")
        print("/p/<profile> to any standalone profile. Your API keys are the only protection on a public address.")
    import hermes_app_bridge as bridge
    url = public_url or f"https://{host}:{HTTPS_PORT}"
    code = bridge.new_pair_code(url)
    print(f"\nIn Hermit, enter\n  Address: {url}\n  Code: {code}\nThe code works once and expires in 10 minutes.")
    if not options.no_restart:
        # Last and detached: when a bot runs this, the restart ends its own turn.
        print("Restarting Hermes in a few seconds so everything loads.", flush=True)
        subprocess.Popen(["/bin/sh", "-c", f"sleep 8; '{hermes}' -p default gateway restart"], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
