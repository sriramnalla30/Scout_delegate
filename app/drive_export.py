"""
Save traces as files in a folder of your Google Drive (meant for testing from your laptop).

Turn it on in .env (DRIVE_TRACES=true) after logging in once:
    python scripts/google_drive_login.py

What you get: a Drive folder (DRIVE_FOLDER_NAME, "place-discover-traces" by default) with one file
per browser session, `session-<id>.json`, holding every span of that session: name, timings, status,
the prompt version, and the AI input and output. Paste the session id shown in the top-right corner
of the app into the Drive search box to find the file.

How it logs in: OAuth. The login script saves a "refresh token" in google_token.json (git-ignored).
The app trades it for a short-lived access token whenever it needs one. It asks only for the
`drive.file` permission, so it can see ONLY the files and folders it created itself.
While your Google Cloud app is in "Testing", Google expires the refresh token after 7 days:
run the login script again if the log says the saved login is no longer accepted.

It uses plain urllib on purpose (not `requests`): calls made with `requests` are traced, so uploading
a trace would create a new span, which would trigger another upload, and so on forever.
"""
import glob
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from app.config import settings

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CLIENT_FILE_PATTERNS = ("google_oauth_client.json", "client_secret*.json")  # downloaded from Google Cloud
TOKEN_FILE = ROOT / "google_token.json"                                      # written by the login script

SCOPE = "https://www.googleapis.com/auth/drive.file"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_URL = "https://www.googleapis.com/drive/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"

MAX_SESSIONS_IN_MEMORY = 100   # the oldest sessions are forgotten after this many
WARN_EVERY_SECONDS = 300       # do not repeat the same warning more often than this


class DriveError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class DriveNotConfigured(DriveError):
    """The client file or the saved login is missing."""


class NeedsLogin(DriveError):
    """Google no longer accepts the saved login."""


def load_credentials() -> tuple[str, str, str]:
    """Return (client_id, client_secret, refresh_token) from the files in the project folder."""
    client_file = next((Path(p) for pattern in CLIENT_FILE_PATTERNS for p in sorted(glob.glob(str(ROOT / pattern)))), None)
    if not client_file:
        raise DriveNotConfigured("Google client file not found (google_oauth_client.json or client_secret_*.json in the project folder)")
    client = json.loads(client_file.read_text(encoding="utf-8"))
    client = client.get("installed") or client.get("web") or {}
    if not client.get("client_id") or not client.get("client_secret"):
        raise DriveNotConfigured(f"{client_file.name} has no client_id / client_secret (create a 'Desktop app' OAuth client)")
    if not TOKEN_FILE.exists():
        raise DriveNotConfigured("not logged in to Google yet: run  python scripts/google_drive_login.py")
    token = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    if not token.get("refresh_token"):
        raise DriveNotConfigured("google_token.json has no refresh token: run  python scripts/google_drive_login.py")
    return client["client_id"], client["client_secret"], token["refresh_token"]


def post_form(url: str, fields: dict) -> dict:
    """POST form fields (the way Google's token endpoint wants them) and return the JSON answer."""
    request = urllib.request.Request(
        url, data=urllib.parse.urlencode(fields).encode(), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise DriveError(f"Google answered HTTP {e.code}: {body[:300]}", status=e.code, body=body) from None


def _multipart(metadata: dict, content: bytes, mime_type: str) -> tuple[bytes, str]:
    """Body of a Drive 'multipart' upload: file info (JSON) + file content in one request."""
    boundary = "trace" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--".encode()
    return body, f"multipart/related; boundary={boundary}"


class DriveClient:
    """The few Google Drive calls we need, using only the standard library."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._expires_at = 0.0

    def _token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        try:
            data = post_form(TOKEN_URL, {
                "client_id": self._client_id, "client_secret": self._client_secret,
                "refresh_token": self._refresh_token, "grant_type": "refresh_token",
            })
        except DriveError as e:
            if "invalid_grant" in e.body:
                raise NeedsLogin(
                    "Google no longer accepts the saved login (it expires after 7 days while your Google Cloud app is in "
                    "Testing). Run again:  python scripts/google_drive_login.py"
                ) from None
            raise
        self._access_token = data["access_token"]
        self._expires_at = time.time() + int(data.get("expires_in", 3600))
        return self._access_token

    def _call(self, method: str, url: str, data: bytes | None = None, content_type: str | None = None, retry: bool = True) -> dict:
        headers = {"Authorization": f"Bearer {self._token()}"}
        if content_type:
            headers["Content-Type"] = content_type
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=headers), timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry:  # the access token was refused: get a fresh one and try once more
                self._access_token = None
                return self._call(method, url, data, content_type, retry=False)
            body = e.read().decode(errors="replace")
            raise DriveError(f"Google Drive answered HTTP {e.code}: {body[:300]}", status=e.code, body=body) from None

    def ensure_folder(self, name: str) -> str:
        """Id of the folder called `name` (made by this app), creating it if it does not exist."""
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        query = f"name = '{escaped}' and mimeType = '{FOLDER_MIME}' and trashed = false"
        found = self._call("GET", f"{DRIVE_URL}/files?" + urllib.parse.urlencode(
            {"q": query, "fields": "files(id,name)", "spaces": "drive", "pageSize": 1}
        )).get("files") or []
        if found:
            return found[0]["id"]
        metadata = json.dumps({"name": name, "mimeType": FOLDER_MIME}).encode()
        return self._call("POST", f"{DRIVE_URL}/files?fields=id", metadata, "application/json")["id"]

    def create_file(self, name: str, content: bytes, folder_id: str, mime_type: str = "application/json") -> str:
        body, content_type = _multipart({"name": name, "parents": [folder_id]}, content, mime_type)
        return self._call("POST", f"{UPLOAD_URL}/files?uploadType=multipart&fields=id", body, content_type)["id"]

    def update_file(self, file_id: str, content: bytes, mime_type: str = "application/json") -> None:
        self._call("PATCH", f"{UPLOAD_URL}/files/{file_id}?uploadType=media&fields=id", content, mime_type)


def _iso(nanoseconds: int | None) -> str | None:
    if nanoseconds is None:
        return None
    return datetime.fromtimestamp(nanoseconds / 1e9, tz=timezone.utc).isoformat(timespec="milliseconds")


def span_to_dict(span) -> dict:
    """One finished span as a plain, readable dictionary."""
    context = span.get_span_context()
    start, end = span.start_time, span.end_time
    return {
        "name": span.name,
        "trace_id": format(context.trace_id, "032x"),
        "span_id": format(context.span_id, "016x"),
        "parent_span_id": format(span.parent.span_id, "016x") if span.parent else None,
        "start_time": _iso(start),
        "end_time": _iso(end),
        "duration_ms": round((end - start) / 1e6, 1) if start and end else None,
        "status": span.status.status_code.name,
        "status_description": span.status.description,
        "kind": span.kind.name,
        "attributes": dict(span.attributes or {}),
        "events": [{"name": e.name, "time": _iso(e.timestamp), "attributes": dict(e.attributes or {})} for e in span.events],
    }


class DriveTraceExporter(SpanExporter):
    """
    Receives finished spans (in batches, in a background thread) and saves them to Drive:
    one file per session. Drive cannot append to a file, so each batch rewrites that session's file
    with all of its spans (which we keep in memory). If an upload fails, the spans stay in memory
    and are uploaded together with the next batch.
    """

    def __init__(self, client: DriveClient | None = None, folder_name: str | None = None):
        self._client = client or DriveClient(*load_credentials())
        self._folder_name = folder_name or settings.drive_folder_name
        self._folder_id: str | None = None
        self._sessions: OrderedDict[str, dict] = OrderedDict()  # session id -> {"file_id": str | None, "spans": [...]}
        self._last_warning: float | None = None

    def export(self, spans) -> SpanExportResult:
        touched = []
        for span in spans:
            record = span_to_dict(span)
            session_id = str(record["attributes"].get("session.id") or "no-session")
            session = self._sessions.setdefault(session_id, {"file_id": None, "spans": []})
            session["spans"].append(record)
            self._sessions.move_to_end(session_id)
            if session_id not in touched:
                touched.append(session_id)

        try:
            for session_id in touched:
                self._save(session_id)
        except Exception as e:
            self._warn(e)
            return SpanExportResult.FAILURE
        finally:
            while len(self._sessions) > MAX_SESSIONS_IN_MEMORY:
                self._sessions.popitem(last=False)
        return SpanExportResult.SUCCESS

    def _save(self, session_id: str) -> None:
        session = self._sessions[session_id]
        if self._folder_id is None:
            self._folder_id = self._client.ensure_folder(self._folder_name)

        spans = sorted(session["spans"], key=lambda record: record["start_time"] or "")
        content = json.dumps({
            "session_id": session_id,
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "span_count": len(spans),
            "spans": spans,
        }, indent=2, ensure_ascii=False, default=str).encode("utf-8")

        try:
            if session["file_id"]:
                try:
                    self._client.update_file(session["file_id"], content)
                    return
                except DriveError as e:
                    if e.status != 404:  # 404 = the file was deleted in Drive: make a new one
                        raise
            file_name = "session-" + re.sub(r"[^A-Za-z0-9_-]", "_", session_id) + ".json"
            session["file_id"] = self._client.create_file(file_name, content, self._folder_id)
        except DriveError as e:
            if e.status == 404:  # the folder was deleted in Drive: look it up / create it again next time
                self._folder_id = None
            raise

    def _warn(self, error: Exception) -> None:
        now = time.monotonic()
        if self._last_warning is None or now - self._last_warning > WARN_EVERY_SECONDS:
            self._last_warning = now
            logger.warning(f"Could not save traces to Google Drive (will retry with the next batch): {error}")

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
