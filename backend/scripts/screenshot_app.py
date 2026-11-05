#!/usr/bin/env python
"""Screenshot the authenticated map view using Chrome DevTools Protocol.

Used for manual verification of the staff UI: logs in by injecting the JWT into
localStorage, reloads, and captures a PNG.

Usage:  python scripts/screenshot_app.py <jwt> [out.png] [url]
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
import urllib.request

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9333


def cdp_targets():
    return json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=10))


def main() -> int:
    token = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/app.png"
    url = sys.argv[3] if len(sys.argv) > 3 else "http://localhost:5173/#/map"

    proc = subprocess.Popen([
        CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
        "--enable-unsafe-swiftshader", "--use-angle=swiftshader",
        f"--remote-debugging-port={PORT}", "--window-size=1500,950", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(30):
            try:
                targets = cdp_targets()
                if targets:
                    break
            except Exception:
                time.sleep(0.5)
        page = next(t for t in cdp_targets() if t["type"] == "page")
        from websockets.sync.client import connect

        with connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            msg_id = 0

            def send(method, params=None):
                nonlocal msg_id
                msg_id += 1
                ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
                while True:
                    reply = json.loads(ws.recv())
                    if reply.get("id") == msg_id:
                        return reply

            send("Page.enable")
            send("Runtime.enable")
            send("DOM.enable")
            # Collect real console errors / uncaught exceptions from the page.
            send("Runtime.evaluate", {"expression": """
                window.__kedr_errors = [];
                addEventListener('error', (e) => window.__kedr_errors.push('error: ' + e.message));
                addEventListener('unhandledrejection', (e) => window.__kedr_errors.push('rejection: ' + e.reason));
                const ce = console.error.bind(console);
                console.error = (...a) => { window.__kedr_errors.push('console: ' + a.map(String).join(' ')); ce(...a); };
            """})
            send("Page.navigate", {"url": "http://localhost:5173/?boot=1"})
            time.sleep(3)
            send("Runtime.evaluate", {"expression": f"localStorage.setItem('kedr_token', {json.dumps(token)})"})
            # A fresh document is required: a hash-only navigation does not reload the
            # SPA. The cache-buster must go before the hash, or the router sees a bad route.
            base, _, frag = url.partition("#")
            fresh = f"{base}{'&' if '?' in base else '?'}fresh={int(time.time())}#{frag}"
            send("Page.navigate", {"url": fresh})
            time.sleep(14)
            errs = send("Runtime.evaluate", {
                "expression": "JSON.stringify(window.__kedr_errors || [])", "returnByValue": True})
            print("runtime errors:", errs.get("result", {}).get("result", {}).get("value"))
            upload = sys.argv[4] if len(sys.argv) > 4 else None
            if upload:
                doc = send("DOM.getDocument")
                node = send("DOM.querySelector", {"nodeId": doc["result"]["root"]["nodeId"],
                                                  "selector": "input[type=file]"})
                send("DOM.setFileInputFiles", {"files": [upload],
                                               "nodeId": node["result"]["nodeId"]})
                time.sleep(1)
                send("Runtime.evaluate", {"expression": """
                    const b = [...document.querySelectorAll('button')]
                        .find(x => /Отправить|Send|Подтвердить|Создать инцидент/.test(x.textContent));
                    if (b) b.click();
                """})
                time.sleep(20)

            shot = send("Page.captureScreenshot", {"format": "png"})
            with open(out, "wb") as fh:
                fh.write(base64.b64decode(shot["result"]["data"]))
            print("saved", out)
    finally:
        proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
