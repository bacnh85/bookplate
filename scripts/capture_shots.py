#!/usr/bin/env python3
"""Regenerate the README screenshots (docs/images/*.png) from a running server.

Launches headless Chrome over CDP, signs in with the bootstrap admin, and
captures: Home, Library (context menu open), Book Store, and the book-detail
dialog (from a live z-lib search — skipped when the search returns nothing).

Usage: .venv/bin/python scripts/capture_shots.py [--base http://127.0.0.1:8480]
Admin password: BOOKPLATE_ADMIN_PASS env, or data/initial_admin_password.
"""
import argparse
import base64
import json
import pathlib
import subprocess
import time
import urllib.request

from websockets.sync.client import connect

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SHOTS = [  # (filename, js to reach the state, settle seconds, extra js or None)
    ("home.png", "", 2.5, None),
    ("library.png", "document.querySelector(\".side-link[data-view='library'][data-filter='all']\").click()", 1.5,
     "document.querySelector('#grid .tile .tile-more').click()"),
    ("book-store.png", "document.querySelector('#nav-store').click()", 2.0, None),
]


def admin_token(base):
    pw = pathlib.Path(ROOT / "data/initial_admin_password")
    import os
    password = os.environ.get("BOOKPLATE_ADMIN_PASS") or pw.read_text().strip()
    req = urllib.request.Request(
        f"{base}/api/auth/login", method="POST",
        data=json.dumps({"username": "admin", "password": password}).encode(),
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req))["token"]


class CDP:
    def __init__(self, url):
        self.ws = connect(url, max_size=None)
        self.n = 0

    def cmd(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv(timeout=60))
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expr, await_promise=False):
        r = self.cmd("Runtime.evaluate", expression=expr, returnByValue=True,
                     awaitPromise=await_promise)
        if r.get("exceptionDetails"):
            raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description", "js error"))
        return r.get("result", {}).get("value")

    def shot(self, path):
        data = self.cmd("Page.captureScreenshot", format="png")["data"]
        pathlib.Path(path).write_bytes(base64.b64decode(data))
        print(f"  {path}")


def wait_for(cdp, expr, timeout, ok=lambda v: v):
    """Poll a js expression until ok(value) is truthy."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if ok(cdp.js(expr)):
                return True
        except RuntimeError:
            pass
        time.sleep(0.5)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8480")
    args = ap.parse_args()
    base, out = args.base.rstrip("/"), ROOT / "docs/images"
    out.mkdir(exist_ok=True)
    import os
    password = os.environ.get("BOOKPLATE_ADMIN_PASS") or (
        ROOT / "data/initial_admin_password").read_text().strip()
    token = admin_token(base)

    prof = f"/tmp/bookplate-shots-profile-{time.time_ns()}"  # fresh: stale profiles break image fetches
    subprocess.run(["pkill", "-f", "remote-debugging-port=9223"], capture_output=True)
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", "--remote-debugging-port=9223",
         f"--user-data-dir={prof}", "--hide-scrollbars", "--window-size=1280,960",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = None
        for _ in range(30):
            try:
                targets = json.load(urllib.request.urlopen("http://127.0.0.1:9223/json"))
                ws_url = next((t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page"), None)
                if ws_url:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not ws_url:
            raise RuntimeError("chrome devtools endpoint never came up")

        with connect(ws_url, max_size=None) as raw:
            cdp = CDP.__new__(CDP)
            cdp.ws, cdp.n = raw, 0
            cdp.cmd("Page.enable")
            cdp.cmd("Runtime.enable")
            print(f"capturing from {base}")
            cdp.cmd("Page.navigate", url=f"{base}/")
            time.sleep(1.5)
            # log in through the real form: the session cookie it sets is what
            # later authenticates <img> cover requests (a localStorage token
            # alone does not ride along on image fetches)
            cdp.js("document.querySelector('#auth-user').value = 'admin'")
            cdp.js(f"document.querySelector('#auth-pass').value = {json.dumps(password)}")
            cdp.js("document.querySelector('#auth-submit').click()")
            assert wait_for(cdp, "!!document.querySelector('#home-stats .stat')", 15), "home never rendered"
            time.sleep(2.0)
            # headless Chrome can defer lazy images past the screenshot —
            # force the covers eager so tiles render with real art
            cdp.js("document.querySelectorAll('.cover img[loading=lazy]')"
                   ".forEach(i => { i.loading = 'eager'; });")
            time.sleep(1.5)
            for name, nav_js, settle, extra in SHOTS:
                if nav_js:
                    cdp.js(nav_js)
                time.sleep(settle)
                if extra:
                    cdp.js(extra)
                    time.sleep(0.5)
                cdp.shot(out / name)

            # Ask AI: a real store-search turn → queue action card
            cdp.js("document.querySelector('#ai-btn').click()")
            time.sleep(0.5)
            cdp.js("document.querySelector('#ai-input').value = "
                   "'Search Z-Library for \\u201cthe pragmatic programmer\\u201d "
                   "and propose one edition to queue.'")
            cdp.js("document.querySelector('#ai-send').click()")
            if wait_for(cdp, "document.querySelectorAll('.ai-action').length > 0", 75):
                time.sleep(1.0)
                cdp.shot(out / "ai-chat.png")   # replaced only on a healthy turn
            else:
                print("  ai-chat.png SKIPPED — no action card; previous shot kept")
            cdp.js("document.querySelector('#ai-close').click()")

            # Settings → API: token created, shown-once box visible
            cdp.js("document.querySelector('#tab-settings').click()")
            cdp.js("document.querySelector('.admin-tab[data-tab=\"api\"]').click()")
            time.sleep(1.0)
            cdp.js("document.querySelector('#token-label').value = 'Claude Desktop'")
            cdp.js("document.querySelector('#token-form .btn-primary').click()")
            if wait_for(cdp, "!!document.querySelector('#token-shown code')", 10):
                time.sleep(0.5)
                # redact: never publish a live token in a committed screenshot
                cdp.js("document.querySelector('#token-shown code').textContent = "
                       "'bp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx  (copied)'")
            cdp.shot(out / "api-tokens.png")
            # cleanup: revoke the screenshot token so no live credential lingers
            token = admin_token(base)
            req = urllib.request.Request(f"{base}/api/tokens", headers={
                "Authorization": f"Bearer {token}"})
            for t in json.load(urllib.request.urlopen(req)):
                if t["label"] == "Claude Desktop":
                    urllib.request.urlopen(urllib.request.Request(
                        f"{base}/api/tokens/{t['id']}", method="DELETE",
                        headers={"Authorization": f"Bearer {token}"}))

            # book detail: live z-lib search -> first result row (skipped when none)
            cdp.js("document.querySelector('#nav-store').click()")
            time.sleep(1.0)
            cdp.js("const i = document.querySelector('#zlib-search'); i.value = 'python'; i.dispatchEvent(new Event('input'))")
            if wait_for(cdp, "document.querySelectorAll('.result-row').length", 30,
                        lambda n: n > 0):
                cdp.js("document.querySelector('.result-row').click()")
                time.sleep(3.0)
                cdp.shot(out / "book-detail.png")
            else:
                print("  book-detail.png skipped (z-lib search returned nothing)")
    finally:
        chrome.terminate()


if __name__ == "__main__":
    main()
