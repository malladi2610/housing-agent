# Homeward

A small background agent that finds rooms/studios near work in Munich and applies immediately.

```
every ~60 s:
  Flatsforfriendz  one HTTP GET -> all ~375 Munich offers incl. coordinates
  WG-Gesucht       newest-first search (rent pre-filtered) -> detail page for coordinates + text
  Kleinanzeigen    WG + flat searches over HTTP -> ad page (embedded JSON) for coordinates + text   (opt-in)
  ImmoScout24      result-list model + exposé, loaded in the bot Chrome; MieterPlus-only days skipped (opt-in)
  immowelt         search cards + exposé JSON in the bot Chrome; swaps / WBS-only skipped           (opt-in)
        |
  Python filters   <= 6 km straight-line to MTZ, <= €850, move-in <= 1 Jan, >= 6 months
                   flats: warm + €70 power/internet, or cold + €200 (marked as an estimate)
  ranking          km + price + lateness (lower = better); brand-new listings jump the queue
        |
  Ollama           kimi-k3:cloud (fallback glm-5.2:cloud) reads the full ad, rejects misfits,
                   writes a personal message in the ad's language from [me].about
        |
  send             Flatsforfriendz: Android app over ADB (uiautomator2), free option only
                   WG-Gesucht, Kleinanzeigen, ImmoScout24, immowelt: dedicated Chrome profile (Playwright)
```

Listings that existed before the first run are worked through closest-first, one every
`backlog_every_minutes` during the day. Everything is stored in `data/homeward.sqlite3`.

## Setup (macOS)

```bash
brew install --cask android-platform-tools   # adb
uv venv -p 3.12 && uv pip install -e .
cp config.example.toml config.toml           # fill in [me]
.venv/bin/python -m homeward login           # log in to WG-Gesucht once, close the window
```

Phone: Developer options → USB debugging ON and **Stay awake** ON, Auto Blocker OFF, accept the
"Allow USB debugging" prompt, be logged in to the Flatsforfriendz app. Ollama must be running and
signed in (`ollama signin`) for the `:cloud` models.

## Use

```bash
.venv/bin/python -m homeward scan             # dry: show what matches, ranked
.venv/bin/python -m homeward draft fff:<id>   # show the message it would send
.venv/bin/python -m homeward service install  # run in the background (launchd, survives reboot)
.venv/bin/python -m homeward status           # what was applied / rejected / failed
tail -f data/homeward.log
.venv/bin/python -m homeward service uninstall
```

Set `send = false` in `config.toml` to only get notifications with drafts.
The phone must stay connected and unlocked (it is only used for a ~30 s burst per application).
