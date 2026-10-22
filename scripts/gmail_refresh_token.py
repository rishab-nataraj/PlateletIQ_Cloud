"""
Get a Gmail API refresh token, once, so the API can send email as you.

1. Google Cloud Console (the same project as Google sign-in):
   APIs & Services → Library → enable **Gmail API**.
2. APIs & Services → OAuth consent screen → add your Gmail address as a test
   user (or publish the app). Add the scope ``.../auth/gmail.send``.
3. APIs & Services → Credentials → Create credentials → OAuth client ID →
   application type **Desktop app**. Copy its client ID and secret.
4. Run, on your own computer:

       python scripts/gmail_refresh_token.py <client-id> <client-secret>

   A browser opens; sign in with the Gmail account that should send alerts
   and allow "Send email on your behalf". The script prints:

       GMAIL_SENDER=you@gmail.com
       GMAIL_CLIENT_ID=...
       GMAIL_CLIENT_SECRET=...
       GMAIL_REFRESH_TOKEN=...

   Put those four in the API's environment (Render / Doppler).

Note: while the consent screen is in "Testing", Google expires refresh tokens
after 7 days — re-run this before a demo, or set the app to "In production"
(an unverified app still works for your own account).

Standard library only, so it runs anywhere Python does.
"""

import base64
import http.server
import json
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

PORT = 8765
REDIRECT = f"http://127.0.0.1:{PORT}/"
SCOPES = "https://www.googleapis.com/auth/gmail.send openid email"


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 1
    client_id, client_secret = sys.argv[1], sys.argv[2]
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [""])[0] == state:
                result["code"] = query.get("code", [""])[0]
                result["error"] = query.get("error", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h3>PlateletIQ: you can close this tab and return to the terminal.</h3>")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()

    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })
    print("Opening your browser. If it does not open, visit:\n\n" + url + "\n")
    webbrowser.open(url)

    while "code" not in result and "error" not in result:
        threading.Event().wait(0.3)
    server.server_close()
    if result.get("error") or not result.get("code"):
        print(f"Authorisation failed: {result.get('error') or 'no code returned'}")
        return 1

    body = urllib.parse.urlencode({
        "code": result["code"],
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT,
        "grant_type": "authorization_code",
    }).encode()
    with urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data=body)) as res:
        tokens = json.loads(res.read())

    if "refresh_token" not in tokens:
        print("Google returned no refresh token. Remove the app's access at "
              "https://myaccount.google.com/permissions and run this again.")
        return 1

    email = ""
    if tokens.get("id_token"):
        # The ID token's middle segment is base64url JSON; we only read the address.
        payload = tokens["id_token"].split(".")[1]
        email = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("email", "")

    print("\nAdd these to the API environment:\n")
    print(f"GMAIL_SENDER={email or '<the Gmail address you just used>'}")
    print(f"GMAIL_CLIENT_ID={client_id}")
    print(f"GMAIL_CLIENT_SECRET={client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={tokens['refresh_token']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
