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

from flask import current_app, render_template, url_for

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


def send_email(to_addr: str, subject: str, body: str, submission_id=None, html=None) -> str:
    """Send plain text, plus an HTML version when given. Returns "sent", "failed" or "not_configured"."""
    cfg = current_app.config
    outbox = cfg.get("MAIL_OUTBOX")
    if outbox is not None:  # tests
        outbox.append({"to": to_addr, "subject": subject, "body": body, "html": html, "reply_to": _reply_to()})
        log_message("email", to_addr, subject, body, "sent", submission_id=submission_id)
        return "sent"
    if cfg.get("RESEND_API_KEY"):
        error = _send_resend(to_addr, subject, body, html)
    elif cfg.get("SMTP_HOST"):
        error = _send_smtp(to_addr, subject, body, html)
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


def _send_resend(to_addr, subject, body, html=None):
    """Send through Resend's API. Returns None when accepted, else a reason for Admin > Messages.

    Retries network errors, 429 and 5xx with back-off. The Idempotency-Key is the same on every try,
    so a retry after a timeout can't send the email twice."""
    from .billing import user_agent  # Resend sits behind Cloudflare, which blocks "Python-urllib"
    cfg = current_app.config
    payload = {"from": cfg["EMAIL_FROM"], "to": [to_addr], "subject": subject, "text": body}
    if html:
        payload["html"] = html
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


def _send_smtp(to_addr, subject, body, html=None):
    cfg = current_app.config
    msg = EmailMessage()
    msg["From"] = cfg["EMAIL_FROM"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    if _reply_to():
        msg["Reply-To"] = _reply_to()
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
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


def notify(user_ids, message, submission_id=None, email_subject=None, link_endpoint=None, email=None):
    """Create an in-app notification for each user; email them too if a subject is given.

    `email` shapes the email (see compose_email): heading, intro, the document, a quote, the button.
    Without it the in-app message becomes the heading. Returns each email's outcome
    ("sent", "failed", "not_configured")."""
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
    if link_endpoint:
        args = {"sub_id": submission_id} if submission_id else {}
        link = current_app.config["BASE_URL"].rstrip("/") + url_for(link_endpoint, **args)
    placeholders = ",".join("?" * len(user_ids))
    outcomes = []
    for row in query(f"SELECT email, name FROM users WHERE id IN ({placeholders}) AND is_active = 1", user_ids):
        text, html = compose_email(email_subject, row["name"], {"heading": message, **(email or {})}, link)
        outcomes.append(send_email(row["email"], email_subject, text, submission_id, html=html))
    return outcomes


def _first_name(name):
    return (name or "").strip().split(" ")[0] or "there"


def compose_email(subject, name, content, link=""):
    """(plain text, HTML) for one email. content keys, all optional except heading:
    kicker, heading, intro (list of paragraphs), document, details [(label, value)], quote, quote_by,
    button, preheader, footer ("customer" or "reviewer")."""
    cfg = current_app.config
    app_name = cfg["APP_NAME"]
    home = cfg["BASE_URL"].rstrip("/")
    intro = [f"Hi {_first_name(name)},", *content.get("intro", [])]
    quote = (content.get("quote") or "").strip()
    if len(quote) > 900:
        quote = quote[:900].rsplit(" ", 1)[0] + "…"
    footer = (f"You get these because you review documents on {app_name}." if content.get("footer") == "reviewer"
              else f"You're getting this because you asked {app_name} to review a document.")
    ctx = {
        "subject": subject, "app_name": app_name, "home_url": home,
        "kicker": content.get("kicker", ""), "heading": content["heading"], "intro": intro,
        "document": content.get("document", ""), "details": content.get("details", []),
        "quote": quote, "quote_by": content.get("quote_by", ""),
        "button": content.get("button") or f"Open {app_name}", "button_url": link,
        "preheader": content.get("preheader") or (content.get("intro") or [content["heading"]])[0],
        "signoff": f"— {app_name}", "footer": footer,
    }
    html = render_template("email/message.html", **ctx)

    lines = [intro[0], "", content["heading"], ""]
    lines += [p for para in intro[1:] for p in (para, "")]
    if ctx["document"]:
        lines.append(f"Document: {ctx['document']}")
        lines += [f"{label}: {value}" for label, value in ctx["details"]]
        lines.append("")
    if quote:
        if ctx["quote_by"]:
            lines.append(f"{ctx['quote_by']}:")
        lines += ["> " + q for q in quote.splitlines() or [""]]
        lines.append("")
    if link:
        lines += [f"{ctx['button']}: {link}", ""]
    lines += [ctx["signoff"], "", footer]
    return "\n".join(lines) + "\n", html


def admin_ids():
    return [r["id"] for r in query("SELECT id FROM users WHERE role = 'admin' AND is_active = 1")]


def notify_admins(message, submission_id, assigned_to=None, email=None, content=None):
    """Tell the assigned reviewer, or every admin when nobody is assigned. `content` shapes the email."""
    targets = [assigned_to] if assigned_to else admin_ids()
    subject = None
    if email is None:
        email = current_app.config.get("ADMIN_EMAIL_ALERTS", False)
    if email:
        subject = f"[{current_app.config['APP_NAME']}] {message}"
    notify(targets, message, submission_id, email_subject=subject, link_endpoint="admin.submission",
           email={"button": "Open in admin", "footer": "reviewer", **(content or {})})
