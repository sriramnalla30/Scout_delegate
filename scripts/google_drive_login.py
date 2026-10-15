"""
Log in to Google once, so the app can save traces in a folder of your Google Drive.

    python scripts/google_drive_login.py

What happens:
  1. Your browser opens a Google page. Choose your account and click Allow.
     (Google may say "Google hasn't verified this app": click Advanced, then continue.
      Your Gmail address must be listed under "Test users" in the Google Cloud console.)
  2. This script receives the answer on a tiny local web server (127.0.0.1) and trades it
     for a "refresh token", which is saved in google_token.json (git-ignored).
  3. It creates the Drive folder and uploads a small connection-test file to prove it works.

Then set DRIVE_TRACES=true in .env and start the app.
"""
import base64
import hashlib
import json
import secrets
import sys
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import drive_export  # noqa: E402  (reads .env through app.config)
from app.drive_export import DriveClient, DriveError, post_form  # noqa: E402

LOGIN_TIMEOUT_SECONDS = 300


class _AnswerHandler(BaseHTTPRequestHandler):
    """Receives Google's redirect: http://127.0.0.1:<port>/?code=...&state=..."""

    answer: dict = {}

    def do_GET(self):
        query = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
        if "code" not in query and "error" not in query:
            self.send_response(404)
            self.end_headers()
            return
        type(self).answer = query
        ok = "code" in query
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            ("<h2>Done. You can close this tab and go back to the terminal.</h2>" if ok
             else f"<h2>Login failed: {query.get('error')}</h2><p>Go back to the terminal.</p>").encode()
        )

    def log_message(self, *args):  # keep the terminal quiet
        pass


def login(client_id: str, client_secret: str, open_browser=webbrowser.open, timeout: int = LOGIN_TIMEOUT_SECONDS) -> dict:
    """Run the browser login and return Google's token answer (it contains the refresh token)."""
    _AnswerHandler.answer = {}
    server = HTTPServer(("127.0.0.1", 0), _AnswerHandler)
    server.timeout = 1
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}"

    # PKCE: a one-time secret that proves the same program that started the login finishes it
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)

    url = drive_export.AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": drive_export.SCOPE, "access_type": "offline", "prompt": "consent",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
    })
    print("Opening your browser. If nothing opens, copy this address into it:\n" + url + "\n")
    open_browser(url)

    deadline = time.monotonic() + timeout
    while not _AnswerHandler.answer and time.monotonic() < deadline:
        server.handle_request()
    server.server_close()

    answer = _AnswerHandler.answer
    if not answer:
        raise SystemExit(f"No answer from Google within {timeout} seconds. Run the script again.")
    if "error" in answer:
        raise SystemExit(f"Google refused the login: {answer['error']}. "
                         "If it says access_denied, add your Gmail under Test users in the Google Cloud console.")
    if answer.get("state") != state:
        raise SystemExit("The answer did not match this login attempt (state mismatch). Run the script again.")

    return post_form(drive_export.TOKEN_URL, {
        "code": answer["code"], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier,
    })


def main() -> None:
    try:
        client_id, client_secret, _ = _client_only()
    except DriveError as e:
        raise SystemExit(f"{e}")

    token = login(client_id, client_secret)
    if not token.get("refresh_token"):
        raise SystemExit("Google did not return a refresh token. Remove this app at "
                         "https://myaccount.google.com/permissions and run the script again.")
    if drive_export.SCOPE not in token.get("scope", ""):
        raise SystemExit(f"Google did not grant the Drive permission (granted: {token.get('scope')}). Run the script again and allow it.")

    drive_export.TOKEN_FILE.write_text(json.dumps({
        "refresh_token": token["refresh_token"],
        "scope": token["scope"],
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, indent=2), encoding="utf-8")
    print(f"Logged in. Saved {drive_export.TOKEN_FILE.name} (keep it private; it is git-ignored).")

    # Prove it works: create the folder and put one small file in it
    from app.config import settings
    client = DriveClient(client_id, client_secret, token["refresh_token"])
    folder_id = client.ensure_folder(settings.drive_folder_name)
    client.create_file("connection-test.json", json.dumps(
        {"message": "Place Discover can write to this folder.", "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}, indent=2
    ).encode(), folder_id)
    print(f"Test file written to the Drive folder '{settings.drive_folder_name}'. Open Google Drive to see it.")
    print("Next: set DRIVE_TRACES=true in .env and start the app.")


def _client_only() -> tuple[str, str, str]:
    """Client id and secret (the saved login does not exist yet on the first run)."""
    import glob
    for pattern in drive_export.CLIENT_FILE_PATTERNS:
        for path in sorted(glob.glob(str(ROOT / pattern))):
            info = json.loads(Path(path).read_text(encoding="utf-8"))
            info = info.get("installed") or info.get("web") or {}
            if info.get("client_id") and info.get("client_secret"):
                return info["client_id"], info["client_secret"], ""
    raise drive_export.DriveNotConfigured(
        "Google client file not found: save the downloaded JSON in the project folder "
        "(google_oauth_client.json or client_secret_*.json)")


if __name__ == "__main__":
    main()
