"""
One-time helper: run this ON YOUR OWN COMPUTER to connect your Oura account.

It opens Oura's login page in your browser, you click "Allow", and it prints
a refresh token. Paste that token into GitHub as the OURA_REFRESH_TOKEN secret.

Usage (in Terminal, from the folder this file is in):
    python3 get_token.py
"""

import http.server
import json
import secrets
import urllib.parse
import urllib.request
import webbrowser

REDIRECT_URI = "http://localhost:8080/callback"
AUTHORIZE_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"


def main():
    client_id = input("Paste your Oura Client ID: ").strip()
    client_secret = input("Paste your Oura Client Secret: ").strip()
    state = secrets.token_urlsafe(16)

    login_url = AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": "workout",
        "state": state,
    })

    # Tiny web server that waits for Oura to send us back to localhost:8080
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result["code"] = query.get("code", [None])[0]
            result["state"] = query.get("state", [None])[0]
            result["error"] = query.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>All set. You can close this tab and go back to Terminal.</h2>")

        def log_message(self, *args):
            pass  # keep Terminal quiet

    print("\nOpening Oura in your browser. Log in and click Allow...")
    webbrowser.open(login_url)
    print(f"(If nothing opened, copy this link into your browser:\n{login_url}\n)")

    with http.server.HTTPServer(("localhost", 8080), Handler) as server:
        server.handle_request()

    if result.get("error") or not result.get("code"):
        raise SystemExit(f"Oura didn't give permission: {result.get('error')}")
    if result.get("state") != state:
        raise SystemExit("Security check failed (state mismatch). Try again.")

    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=body)) as resp:
        tokens = json.load(resp)

    print("\nSuccess! Copy the line below into GitHub as the secret OURA_REFRESH_TOKEN:\n")
    print(tokens["refresh_token"])
    print("\nKeep it private, like a password.")


if __name__ == "__main__":
    main()
