"""In-app notifications plus optional email (SMTP). Integrations hook in here later."""
import logging
import smtplib
from email.message import EmailMessage

from flask import current_app, url_for

from .db import get_db, query, utcnow

log = logging.getLogger(__name__)


def send_email(to_addr: str, subject: str, body: str):
    cfg = current_app.config
    outbox = cfg.get("MAIL_OUTBOX")
    if outbox is not None:  # tests
        outbox.append({"to": to_addr, "subject": subject, "body": body})
        return
    if not cfg.get("SMTP_HOST"):
        log.info("Email not configured; skipped '%s' to %s", subject, to_addr)
        return
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
    except Exception:  # never fail the request because email failed
        log.exception("Failed to send email to %s", to_addr)


def notify(user_ids, message, submission_id=None, email_subject=None, link_endpoint=None):
    """Create an in-app notification for each user; email them too if a subject is given."""
    user_ids = [uid for uid in dict.fromkeys(user_ids) if uid]
    if not user_ids:
        return
    conn = get_db()
    now = utcnow()
    conn.executemany(
        "INSERT INTO notifications (user_id, submission_id, message, created_at) VALUES (?, ?, ?, ?)",
        [(uid, submission_id, message, now) for uid in user_ids],
    )
    conn.commit()
    if not email_subject:
        return
    link = ""
    if link_endpoint and submission_id:
        link = current_app.config["BASE_URL"].rstrip("/") + url_for(link_endpoint, sub_id=submission_id)
    placeholders = ",".join("?" * len(user_ids))
    for row in query(f"SELECT email, name FROM users WHERE id IN ({placeholders}) AND is_active = 1", user_ids):
        body = f"Hi {row['name']},\n\n{message}\n"
        if link:
            body += f"\nOpen it here: {link}\n"
        body += f"\n— {current_app.config['APP_NAME']}"
        send_email(row["email"], email_subject, body)


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
