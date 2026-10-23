# Homeward

A small background bot that looks for a room or studio in Munich close to work, and messages the
advertiser within minutes of a listing going online.

## How it works

Every minute or so it:

1. **Finds** new listings on Flatsforfriendz, WG-Gesucht, Kleinanzeigen, ImmoScout24 and immowelt.
2. **Filters** them with fixed rules from `config.toml`: distance to work, all-in rent, move-in date, minimum stay.
3. **Judges** each remaining listing with a model via the local Ollama app. The model reads the full ad,
   rejects misfits and scams, and writes a short personal message in the ad's language.
4. **Sends** the message. Flatsforfriendz goes through the Android app on a USB-connected phone, and the
   other four sites go through a separate Chrome window that the bot keeps open.
5. **Watches** the inboxes and notifies you about replies.

Closest listings go first. Everything is saved in `data/homeward.sqlite3`, and you can follow it on the
dashboard at http://127.0.0.1:8765, where you can also pause sending.

**Safety rules built into the code:**
- It never pays for or ticks an upsell, and never sends the same flat twice.
- It never solves a robot check itself. The check stays open for you to solve (see `captcha_wait_minutes`).
- A send it can't confirm is marked *failed*, never retried blindly.

## Setup

You need Python 3.12, [uv](https://docs.astral.sh/uv/), Google Chrome, [Ollama](https://ollama.com)
(signed in, for the `:cloud` models) and adb (Android platform-tools).

```bash
uv venv -p 3.12
uv pip install -e ".[dev]"
cp config.example.toml config.toml      # then fill in [me]; config.toml stays private (git-ignored)
```

- **Get adb.** On macOS run `brew install --cask android-platform-tools`. On Windows run
  `winget install Google.PlatformTools`.
- **Prepare the phone.** Turn on USB debugging and *Stay awake*, accept the "Allow USB debugging" prompt,
  and log in to the Flatsforfriendz app.
- **Log in to the sites once.** Run `.venv/bin/python -m homeward login` (on Windows:
  `.venv\Scripts\python -m homeward login`), log in to the sites in the Chrome window that opens,
  then leave it open.

## Daily use

| Command (`python -m homeward …`) | What it does |
|---|---|
| `scan` | search once and show what matches, ranked; sends nothing |
| `draft fff:<id>` | show the message it would send for one listing |
| `service install` / `uninstall` / `status` | run in the background, also after a reboot |
| `status` · `health` | what was applied, replied, failed |
| `retry` | re-queue failed listings |

Set `send = false` in `config.toml` to get drafts only. The logs are in `data/homeward.log`.

## Code map

| File | Role |
|---|---|
| `homeward/core.py` | config, the listing model, filter + ranking rules, SQLite store, notifications |
| `homeward/sources.py` | Flatsforfriendz + WG-Gesucht discovery over plain HTTP |
| `homeward/more.py` | Kleinanzeigen, ImmoScout24, immowelt: discovery, details, sending |
| `homeward/wgg.py` | the bot Chrome window and WG-Gesucht sending + inbox |
| `homeward/phone.py` | the Flatsforfriendz Android app over ADB |
| `homeward/llm.py` | the one Ollama call per listing (judge + write) |
| `homeward/agent.py` | the main loop that ties it together |
| `homeward/dashboard.py` | the local web page |

## Working on it

- Run `python -m pytest`. The tests run offline: no phone, logins or Ollama needed.
- GitHub Actions runs the same tests on Windows and macOS for every push and pull request.
- Bugs and ideas go in [GitHub Issues](../../issues). This repo is public, so keep names,
  addresses and phone numbers out of issues and logs you paste there.
