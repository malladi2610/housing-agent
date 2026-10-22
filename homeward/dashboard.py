"""Tiny local dashboard (stdlib only), served by the agent at http://127.0.0.1:8765."""
from __future__ import annotations

import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import DATA, Store

PAUSE = DATA / "paused"
PORT = 8765

SECTIONS = [
    ("Replies", "status='replied'", "updated desc"),
    ("Applied", "status='applied'", "updated desc"),
    ("Up next", "status='queued'", "backlog, score"),
    ("Drafts / failed", "status in ('draft','failed')", "updated desc"),
    ("Rejected by the model", "status='rejected'", "updated desc"),
    ("Closed", "status='closed'", "updated desc"),
]

CSS = """
:root{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e4e4df;--accent:#2f6f4f;--warn:#a33}
@media (prefers-color-scheme: dark){:root{--bg:#161615;--fg:#ececea;--muted:#9a9a94;--card:#1f1f1d;--line:#33332f;--accent:#6fbf94;--warn:#e88}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:20px 16px 60px}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 8px}
.muted{color:var(--muted)}.stats{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;min-width:110px}
.stat b{display:block;font-size:22px}table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
td,th{padding:7px 10px;border-top:1px solid var(--line);text-align:left;vertical-align:top}th{border-top:0;color:var(--muted);font-weight:500;font-size:12px}
a{color:var(--accent)}details summary{cursor:pointer;color:var(--muted)}pre{white-space:pre-wrap;margin:6px 0 0;font:13px/1.45 inherit}
.src{font-size:11px;padding:1px 6px;border-radius:6px;border:1px solid var(--line)}.reply td{background:color-mix(in srgb,var(--accent) 10%,transparent)}
button{font:inherit;padding:6px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
.paused{color:var(--warn);font-weight:600}.log{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px;font:12px/1.5 ui-monospace,monospace;white-space:pre-wrap;overflow-x:auto}
.wrap{overflow-x:auto}
"""


def render() -> str:
    store = Store()
    counts = {r["status"]: r["n"] for r in store.db.execute("select status, count(*) n from listings group by 1")}
    sent_today = sum(store.sent_today(s) for s in ("fff", "wgg"))
    paused = PAUSE.exists()
    out = [f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<meta http-equiv=refresh content=30><title>Homeward</title><style>{CSS}</style></head><body><main>",
           "<h1>Homeward</h1><div class=muted>Flatsforfriendz + WG-Gesucht · ≤ 6 km from MTZ · refreshes every 30 s</div>",
           "<form method=post action=/toggle style='margin-top:12px'>",
           f"<span class={'paused' if paused else 'muted'}>{'Sending PAUSED' if paused else 'Sending is on'}</span> &nbsp;"
           f"<button>{'Resume sending' if paused else 'Pause sending'}</button></form><div class=stats>"]
    for label, n in [("sent today", sent_today), ("applied", counts.get("applied", 0)),
                     ("replies", counts.get("replied", 0)), ("queued", counts.get("queued", 0)),
                     ("rejected", counts.get("rejected", 0)), ("filtered out", counts.get("skipped", 0))]:
        out.append(f"<div class=stat><b>{n}</b><span class=muted>{label}</span></div>")
    out.append("</div>")
    for title, where, order in SECTIONS:
        rows = store.db.execute(f"select * from listings where {where} order by {order} limit 60").fetchall()
        if not rows:
            continue
        out.append(f"<h2>{title} <span class=muted>({len(rows)})</span></h2><div class=wrap><table>"
                   "<tr><th></th><th>km</th><th>listing</th><th>when</th><th>details</th></tr>")
        for r in rows:
            data = json.loads(r["data"])
            name = html.escape(data.get("title") or r["summary"])[:90]
            detail = html.escape(r["reason"] or "")
            if r["message"]:
                detail += f"<details><summary>message</summary><pre>{html.escape(r['message'])}</pre></details>"
            out.append(f"<tr class={'reply' if r['status'] == 'replied' else ''}>"
                       f"<td><span class=src>{'FFF' if r['source'] == 'fff' else 'WGG'}</span></td>"
                       f"<td>{r['km'] or ''}</td><td><a href='{html.escape(r['url'])}' target=_blank>{name}</a>"
                       f"<div class=muted>{html.escape(r['summary'])}</div></td>"
                       f"<td class=muted>{r['updated'][5:16].replace('T', ' ')}</td><td>{detail}</td></tr>")
        out.append("</table></div>")
    log = (DATA / "homeward.log")
    tail = log.read_text().splitlines()[-40:] if log.exists() else []
    out.append("<h2>Log</h2><div class=log>" + html.escape("\n".join(reversed(tail))) + "</div></main></body></html>")
    return "".join(out)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        origin = self.headers.get("Origin")
        if origin not in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            self.send_response(403)
            self.end_headers()
            return
        if self.path == "/toggle":
            PAUSE.unlink() if PAUSE.exists() else PAUSE.touch()
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, *args):
        pass


def serve() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
