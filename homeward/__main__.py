"""homeward CLI.

  run                  poll + apply forever (what the background service runs)
  once [--dry]         a single poll/apply round
  scan                 poll only, then show the ranked queue (sends nothing)
  status               counts and the latest activity
  health               one-line check for monitoring; details only when something needs attention
  draft KEY            show the message the model would send for one listing (sends nothing)
  login                open the bot's Chrome window to log in to WG-Gesucht once
  retry                re-queue failed listings
  service install|uninstall|status   background service (launchd, survives reboots)
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from .core import DATA, ROOT, Store, km_to_work, listing_from_row, load_config

LABEL = "com.homeward.agent"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def show_rows(rows, limit=25):
    for r in rows[:limit]:
        print(f"{r['status']:8} {r['km'] or '':>5} km  {r['summary'][:70]:70}  {r['url']}  {r['reason'][:50]}")


def service(action: str) -> None:
    uid = os.getuid()
    if action == "install":
        python = ROOT / ".venv/bin/python"
        PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/caffeinate</string><string>-is</string>
    <string>{python}</string><string>-m</string><string>homeward</string><string>run</string>
  </array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>{DATA}/service.out.log</string>
  <key>StandardErrorPath</key><string>{DATA}/service.err.log</string>
</dict></plist>
""")
        DATA.mkdir(exist_ok=True)
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
        time.sleep(2)  # let the old instance exit before re-registering
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)], check=True)
        print(f"started; logs: tail -f {DATA}/homeward.log")
    elif action == "uninstall":
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
        PLIST.unlink(missing_ok=True)
        print("stopped and removed")
    else:
        subprocess.run(["launchctl", "print", f"gui/{uid}/{LABEL}"])


def health() -> None:
    """Cheap monitor: heartbeat age, today's numbers, and only the log lines since the last check that matter."""
    from datetime import datetime
    store = Store()
    beat = DATA / "heartbeat"
    age = (datetime.now() - datetime.fromisoformat(beat.read_text())).total_seconds() / 60 if beat.exists() else 1e9
    counts = {r["status"]: r["n"] for r in store.db.execute("select status, count(*) n from listings group by 1")}
    sent = sum(store.sent_today(s) for s in ("fff", "wgg"))
    log, mark = DATA / "homeward.log", DATA / "health.offset"
    lines = log.read_text().splitlines() if log.exists() else []
    start = int(mark.read_text()) if mark.exists() else max(0, len(lines) - 50)
    mark.write_text(str(len(lines)))
    events = [l for l in lines[start:] if any(k in l for k in ("REPLY", "ACCEPTED", "FAILED", "error", "failed", "waiting", "not logged in"))]
    status = "OK" if age < 6 and not any("error" in e or "FAILED" in e or "failed" in e for e in events) else "ATTENTION"
    print(f"{status} | heartbeat {age:.0f} min ago | sent today {sent} | applied {counts.get('applied', 0)} | "
          f"replied {counts.get('replied', 0)} | queued {counts.get('queued', 0)} | failed {counts.get('failed', 0)}"
          f"{' | PAUSED' if (DATA / 'paused').exists() else ''}")
    for e in events[-15:]:
        print("  " + e[:220])


def main() -> None:
    args = sys.argv[1:] or ["status"]
    cmd = args[0]
    if cmd in ("run", "once", "scan"):
        from .agent import Agent
        agent = Agent(dry="--dry" in args)
        if cmd == "run":
            agent.run()
        elif cmd == "once":
            agent.tick()
        else:
            agent.poll()
            store = agent.store
            print("NEW:"); show_rows(store.queue("fff", False) + store.queue("wgg", False))
            print("\nBACKLOG (next first):")
            show_rows(sorted(store.queue("fff", True) + store.queue("wgg", True), key=lambda r: r["score"]), 40)
    elif cmd == "status":
        store = Store()
        for row in store.db.execute("select source, status, count(*) n from listings group by 1,2 order by 1,2"):
            print(f"{row['source']} {row['status']:8} {row['n']}")
        print("\nlatest activity:")
        show_rows(store.rows("status in ('applied','draft','failed','rejected')"), 15)
    elif cmd == "health":
        health()
    elif cmd == "draft":
        from . import llm
        cfg, store = load_config(), Store()
        row = store.rows("key=?", (args[1],))[0]
        l = listing_from_row(row)
        if l.source == "fff":
            from .phone import Phone
            ph = Phone(cfg["phone"].get("serial", ""))
            ph.open(l)
            l.text = f"Host: {ph.host()}\n{ph.read()}"
        print(l.text[:1500], "\n---")
        print(llm.assess(l, cfg, km_to_work(l, cfg)))
    elif cmd == "login":
        from .wgg import login
        login()
    elif cmd == "retry":
        store = Store()
        n = store.db.execute("update listings set status='queued' where status='failed'").rowcount
        store.db.commit()
        print(f"re-queued {n}")
    elif cmd == "service":
        service(args[1] if len(args) > 1 else "status")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
