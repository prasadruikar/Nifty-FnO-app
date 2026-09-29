"""
upstox_auth.py  -  One-time daily login to get an Upstox access token.
======================================================================
RUN EACH MORNING:  python upstox_auth.py

Upstox access tokens expire daily (around 3:30 AM). So each trading day
you run this once: it opens the Upstox login in your browser, you log in
with your credentials + PIN, and it captures the access token and saves it
to `access_token.json`. The scanner then reads that token.

How it works (standard OAuth 2.0 authorization-code flow):
  1. We open Upstox's authorize URL in your browser.
  2. You log in. Upstox redirects to your REDIRECT_URI with a `code`.
  3. A tiny local web server catches that redirect and grabs the code.
  4. We exchange the code for an access token via Upstox's token endpoint.
  5. Token saved to access_token.json (valid for the day).
"""

import json, time, urllib.parse, webbrowser, threading, sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import requests
import config

AUTH_URL  = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
TOKEN_FILE = Path("access_token.json")

_captured = {"code": None}


class _CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence server logs
        pass

    def do_GET(self):
        # The redirect lands here with ?code=XXXX
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        code = params.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        if code:
            _captured["code"] = code
            self.wfile.write(b"<h2>Login captured. You can close this tab and return to the terminal.</h2>")
        else:
            self.wfile.write(b"<h2>No code found in redirect. Check your app's redirect URI.</h2>")


def _port_from_redirect(uri):
    p = urllib.parse.urlparse(uri)
    return p.port or 80, p.path or "/"


def get_access_token():
    if not config.API_KEY or config.API_KEY.startswith("PASTE"):
        sys.exit("\n  Fill in API_KEY / API_SECRET / REDIRECT_URI in config.py first.\n")

    port, _path = _port_from_redirect(config.REDIRECT_URI)

    # 1. Build the authorize URL and open it in the browser
    params = {
        "client_id": config.API_KEY,
        "redirect_uri": config.REDIRECT_URI,
        "response_type": "code",
    }
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    print("\n  Opening Upstox login in your browser...")
    print("  If it doesn't open, paste this URL manually:\n")
    print("   ", url, "\n")

    # 2. Start a local server to catch the redirect
    server = HTTPServer(("localhost", port), _CallbackHandler)
    t = threading.Thread(target=server.handle_request, daemon=True)  # handle ONE request
    t.start()

    time.sleep(1)
    webbrowser.open(url)

    # 3. Wait for the code (up to 3 minutes)
    print("  Waiting for you to log in...")
    for _ in range(180):
        if _captured["code"]:
            break
        time.sleep(1)
    server.server_close()

    if not _captured["code"]:
        sys.exit("\n  Timed out waiting for login. Try again.\n")

    print("  Got authorization code. Exchanging for access token...")

    # 4. Exchange code for access token
    resp = requests.post(TOKEN_URL, headers={
        "accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }, data={
        "code": _captured["code"],
        "client_id": config.API_KEY,
        "client_secret": config.API_SECRET,
        "redirect_uri": config.REDIRECT_URI,
        "grant_type": "authorization_code",
    })

    if resp.status_code != 200:
        sys.exit(f"\n  Token exchange failed: {resp.status_code}\n  {resp.text}\n")

    data = resp.json()
    token = data.get("access_token")
    if not token:
        sys.exit(f"\n  No access_token in response:\n  {data}\n")

    # 5. Save it
    TOKEN_FILE.write_text(json.dumps({
        "access_token": token,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, indent=2))
    print(f"\n  Success. Token saved to {TOKEN_FILE.resolve()}")
    print("  You can now run:  python orderflow_scanner.py\n")
    return token


def load_token():
    """Read the saved token. Returns None if missing."""
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text()).get("access_token")
    except Exception:
        return None


if __name__ == "__main__":
    get_access_token()
