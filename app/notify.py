"""In-app notifications plus optional email (Resend's API, or any SMTP service).

Every email and text is recorded in message_log with what happened to it (sent, failed, or not set
up), so admins can see on the Messages page exactly what customers were told.
"""
import json
import logging
import smtplib
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from flask import current_app, url_for

from .db import get_db, query, utcnow

log = logging.getLogger(__name__)


KEEP_LOG_DAYS = 90


def log_message(channel, recipient, subject, body, status, error="", submission_id=None):
    """Record a delivery attempt. Never raises: losing a log line mustn't break a request."""
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO message_log (channel, recipient, subject, body, status, error, submission_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (channel, recipient, subject[:300], body[:5000], status, (error or "")[:300], submission_id, utcnow()))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_LOG_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute("DELETE FROM message_log WHERE created_at < ?", (cutoff,))
        conn.commit()
    except Exception:
        log.exception("Couldn't record a %s to %s", channel, recipient)


RESEND_URL = "https://api.resend.com/emails"
RESEND_WAITS = (0.5, 1.5)  # seconds before the 2nd and 3rd tries


def _reply_to():
    cfg = current_app.config
    return cfg.get("EMAIL_REPLY_TO") or cfg.get("SUPPORT_EMAIL") or ""


def send_email(to_addr: str, subject: str, body: str, submission_id=None) -> str:
    """Returns "sent", "failed" or "not_configured"."""
    cfg = current_app.config
    outbox = cfg.get("MAIL_OUTBOX")
    if outbox is not None:  # tests
        outbox.append({"to": to_addr, "subject": subject, "body": body, "reply_to": _reply_to()})
        log_message("email", to_addr, subject, body, "sent", submission_id=submission_id)
        return "sent"
    if cfg.get("RESEND_API_KEY"):
        error = _send_resend(to_addr, subject, body)
    elif cfg.get("SMTP_HOST"):
        error = _send_smtp(to_addr, subject, body)
    else:
        log.info("Email not configured; skipped '%s' to %s", subject, to_addr)
        log_message("email", to_addr, subject, body, "not_configured",
                    "Email isn't set up (RESEND_API_KEY or SMTP_HOST)", submission_id)
        return "not_configured"
    if error:
        log_message("email", to_addr, subject, body, "failed", error, submission_id)
        return "failed"
    log_message("email", to_addr, subject, body, "sent", submission_id=submission_id)
    return "sent"


def _resend_post(url, headers, body):
    """The one place that talks HTTP to Resend (tests replace it). Returns (status, parsed JSON or {})."""
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            status, text = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read().decode("utf-8", "replace")
    try:
        return status, json.loads(text or "{}")
    except ValueError:
        return status, {"message": text[:200]}


def _send_resend(to_addr, subject, body):
    """Send through Resend's API. Returns None when accepted, else a reason for Admin > Messages.

    Retries network errors, 429 and 5xx with back-off. The Idempotency-Key is the same on every try,
    so a retry after a timeout can't send the email twice."""
    from .billing import user_agent  # Resend sits behind Cloudflare, which blocks "Python-urllib"
    cfg = current_app.config
    payload = {"from": cfg["EMAIL_FROM"], "to": [to_addr], "subject": subject, "text": body}
    if _reply_to():
        payload["reply_to"] = _reply_to()
    headers = {"Authorization": f"Bearer {cfg['RESEND_API_KEY']}", "Content-Type": "application/json",
               "Accept": "application/json", "User-Agent": user_agent(), "Idempotency-Key": str(uuid.uuid4())}
    data = json.dumps(payload).encode()
    error = "No answer from Resend"
    for attempt, wait in enumerate((0, *RESEND_WAITS)):
        if wait:
            time.sleep(wait)
        try:
            status, answer = _resend_post(RESEND_URL, headers, data)
        except OSError as e:  # timeouts, DNS, refused connections
            error = f"Couldn't reach Resend: {type(e).__name__}: {e}"
            continue
        if 200 <= status < 300:
            log.info("Resend accepted '%s' to %s (id %s)", subject, to_addr, answer.get("id"))
            return None
        error = f"Resend HTTP {status}: {answer.get('message') or answer.get('name') or 'no reason given'}"
        if status != 429 and status < 500:
            break  # our request is wrong (bad key, unverified domain…): retrying won't help
    log.warning("Email '%s' to %s failed: %s", subject, to_addr, error)
    return error


def _send_smtp(to_addr, subject, body):
    cfg = current_app.config
    msg = EmailMessage()
    msg["From"] = cfg["EMAIL_FROM"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    if _reply_to():
        msg["Reply-To"] = _reply_to()
    msg.set_content(body)
    try:
        with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=10) as smtp:
            if cfg.get("SMTP_TLS"):
                smtp.starttls()
            if cfg.get("SMTP_USER"):
                smtp.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            smtp.send_message(msg)
    except Exception as e:  # never fail the request because email failed
        log.exception("Failed to send email to %s", to_addr)
        return f"{type(e).__name__}: {e}"
    return None


def notify(user_ids, message, submission_id=None, email_subject=None, link_endpoint=None):
    """Create an in-app notification for each user; email them too if a subject is given.
    Returns each email's outcome ("sent", "failed", "not_configured")."""
    user_ids = [uid for uid in dict.fromkeys(user_ids) if uid]
    if not user_ids:
        return []
    conn = get_db()
    now = utcnow()
    conn.executemany(
        "INSERT INTO notifications (user_id, submission_id, message, created_at) VALUES (?, ?, ?, ?)",
        [(uid, submission_id, message, now) for uid in user_ids],
    )
    conn.commit()
    if not email_subject:
        return []
    link = ""
    if link_endpoint and submission_id:
        link = current_app.config["BASE_URL"].rstrip("/") + url_for(link_endpoint, sub_id=submission_id)
    placeholders = ",".join("?" * len(user_ids))
    outcomes = []
    for row in query(f"SELECT email, name FROM users WHERE id IN ({placeholders}) AND is_active = 1", user_ids):
        body = f"Hi {row['name']},\n\n{message}\n"
        if link:
            body += f"\nOpen it here: {link}\n"
        body += f"\n— {current_app.config['APP_NAME']}"
        outcomes.append(send_email(row["email"], email_subject, body, submission_id))
    return outcomes


def admin_ids():
    return [r["id"] for r in query("SELECT id FROM users WHERE role = 'admin' AND is_active = 1")]


def notify_admins(message, submission_id, assigned_to=None, email=None):
    """Tell the assigned reviewer, or every admin when nobody is assigned."""
    targets = [assigned_to] if assigned_to else admin_ids()
    subject = None
    if email is None:
        email = current_app.config.get("ADMIN_EMAIL_ALERTS", False)
    if email:
        subject = f"[{current_app.config['APP_NAME']}] {message}"
    notify(targets, message, submission_id, email_subject=subject, link_endpoint="admin.submission")
