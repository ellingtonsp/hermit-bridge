# Hermit Bridge

Connects the Hermit app to your own [Hermes Agent](https://github.com/NousResearch/hermes-agent), so you can manage your bots as a fleet with rich cards, live status and voice.

## Install

The easiest way is to tap **Copy Setup Prompt** in Hermit and send it to your bot. To do it yourself, on the Mac running Hermes:

```bash
hermes plugins install ellingtonsp/hermit-bridge --enable
hermes hermit setup
```

Setup prints an address and a code to enter in Hermit. The code works once and expires in 10 minutes.

- `hermes hermit setup --check` reports what's ready and changes nothing.
- `hermes hermit setup --url https://your.address` uses your own HTTPS address instead of Tailscale. Your API keys are then the only protection on that address.

## What it does

Setup adds an API key to any profile without one, enables this plugin in every profile, and runs a small local service on `127.0.0.1` through launchd. It's reachable only through Tailscale Serve or an address you set up. Requires macOS.

Hermit is an independent app and isn't affiliated with Nous Research.

## License

MIT
