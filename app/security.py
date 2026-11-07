"""CSRF protection, security headers, upload validation and login throttling."""
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

from flask import abort, request, session

# Extension -> accepted leading bytes (None = text check instead).
ALLOWED_TYPES = {
    "pdf": (b"%PDF-",),
    "docx": (b"PK\x03\x04",),
    "odt": (b"PK\x03\x04",),
    "doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    "rtf": (b"{\\rtf",),
    "txt": None,
}
ALLOWED_LABEL = "PDF, Word (.doc, .docx), ODT, RTF or TXT"
INLINE_PREVIEW_TYPES = {"pdf": "application/pdf", "txt": "text/plain; charset=utf-8"}


def csrf_token() -> str:
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def validate_upload(file_storage):
    """Return (ext, None) when the upload is acceptable, else (None, error message)."""
    if file_storage is None or not file_storage.filename:
        return None, "Choose a file to upload."
    name = file_storage.filename
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in ALLOWED_TYPES:
        return None, f"That file type isn't supported. Upload a {ALLOWED_LABEL} file."

    head = file_storage.stream.read(2048)
    file_storage.stream.seek(0)
    if not head:
        return None, "That file is empty."
    signatures = ALLOWED_TYPES[ext]
    if signatures is None:
        if b"\x00" in head:
            return None, "That .txt file contains binary data. Upload a plain-text file."
    elif not any(head.startswith(sig) for sig in signatures):
        return None, f"That file doesn't look like a real .{ext} file. Re-save it and try again."
    return ext, None


class LoginThrottle:
    """In-memory limit on failed logins per (IP, email). Resets on success."""

    def __init__(self, max_failures=8, window_seconds=900):
        self.max_failures = max_failures
        self.window = window_seconds
        self._failures = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key, now):
        q = self._failures[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, key) -> bool:
        with self._lock:
            return len(self._prune(key, time.monotonic())) >= self.max_failures

    def fail(self, key):
        with self._lock:
            self._prune(key, time.monotonic()).append(time.monotonic())

    def reset(self, key):
        with self._lock:
            self._failures.pop(key, None)


login_throttle = LoginThrottle()

CSP = (
    "default-src 'self'; img-src 'self' data:; "
    "style-src 'self'; font-src 'self'; "
    "script-src 'self'; form-action 'self' https://checkout.paystack.com https://paystack.com;"
    " frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
)


def init_app(app):
    @app.before_request
    def check_csrf():
        if not app.config.get("CSRF_ENABLED", True):
            return
        if request.endpoint in app.config.get("CSRF_EXEMPT_ENDPOINTS", ()):
            return  # e.g. Paystack's webhook, verified by its HMAC signature instead
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
            expected = session.get("_csrf") or ""
            if not expected or not hmac.compare_digest(sent, expected):
                abort(400, description="Your session expired. Reload the page and try again.")

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Content-Security-Policy", CSP)
        if app.config.get("SESSION_COOKIE_SECURE"):
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    app.jinja_env.globals["csrf_token"] = csrf_token
