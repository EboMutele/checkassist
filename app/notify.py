"""In-app notifications plus optional email (SMTP). Integrations hook in here later.

Every email and text is recorded in message_log with what happened to it (sent, failed, or not set
up), so admins can see on the Messages page exactly what customers were told.
"""
import logging
import smtplib
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


def send_email(to_addr: str, subject: str, body: str, submission_id=None) -> str:
    """Returns "sent", "failed" or "not_configured"."""
    cfg = current_app.config
    outbox = cfg.get("MAIL_OUTBOX")
    if outbox is not None:  # tests
        outbox.append({"to": to_addr, "subject": subject, "body": body})
        log_message("email", to_addr, subject, body, "sent", submission_id=submission_id)
        return "sent"
    if not cfg.get("SMTP_HOST"):
        log.info("Email not configured; skipped '%s' to %s", subject, to_addr)
        log_message("email", to_addr, subject, body, "not_configured", "Email isn't set up (SMTP_HOST is empty)",
                    submission_id)
        return "not_configured"
    msg = EmailMessage()
    msg["From"] = cfg["SMTP_FROM"]
    msg["To"] = to_addr
    msg["Subject"] = subject
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
        log_message("email", to_addr, subject, body, "failed", f"{type(e).__name__}: {e}", submission_id)
        return "failed"
    log_message("email", to_addr, subject, body, "sent", submission_id=submission_id)
    return "sent"


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
