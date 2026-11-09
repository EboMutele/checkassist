"""Instant alerts for the people who run the business: an email and a text the moment a customer places
an order or a payment comes in.

Who gets them is set on Admin > Messages and kept in the database, not in code or .env: the repository
is public (addresses in it get harvested), and the list changes when staff do.

Each alert is composed during the request, then sent on a background thread, so a slow email or SMS
gateway never holds up a customer's upload or Paystack's webhook. Every attempt is recorded on
Admin > Messages like any other email or text.
"""
import logging
import re
import threading
import unicodedata
from decimal import Decimal

from flask import current_app, url_for

from . import phone as phone_numbers
from . import pricing
from .db import get_db, query, utcnow
from .helpers import ref
from .notify import compose_email, send_email
from .sms import send_sms

log = logging.getLogger(__name__)

EMAILS_KEY, PHONES_KEY = "alert_emails", "alert_phones"
MAX_RECIPIENTS = 5
SMS_LIMIT = 160  # one GSM-7 text; see gsm7()
_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
CHANNELS = {"card": "card", "mobile_money": "mobile money", "bank": "bank", "bank_transfer": "bank transfer",
            "ussd": "USSD", "qr": "QR", "apple_pay": "Apple Pay", "eft": "EFT"}


# ---- who gets them ------------------------------------------------------------------------------

def get_setting(key, default=""):
    row = query("SELECT value FROM settings WHERE key = ?", (key,), one=True)
    return row["value"] if row else default


def set_setting(key, value):
    """Caller commits."""
    get_db().execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, utcnow()))


def recipients():
    """(emails, phone numbers) that get alerts."""
    return ([e for e in get_setting(EMAILS_KEY).split("\n") if e],
            [p for p in get_setting(PHONES_KEY).split("\n") if p])


def save_recipients(emails, phones):
    set_setting(EMAILS_KEY, "\n".join(emails))
    set_setting(PHONES_KEY, "\n".join(phones))
    get_db().commit()


def parse_emails(raw):
    """(addresses, error) from what was typed: one per line, or separated by commas or spaces."""
    found = []
    for item in re.split(r"[\s,;]+", raw or ""):
        item = item.strip().strip("<>").lower()
        if not item:
            continue
        if len(item) > 254 or not _EMAIL.match(item):
            return None, f"“{item[:60]}” isn't an email address."
        found.append(item)
    found = list(dict.fromkeys(found))
    if len(found) > MAX_RECIPIENTS:
        return None, f"Add up to {MAX_RECIPIENTS} email addresses."
    return found, None


def parse_phones(raw, default_country="KE"):
    """(numbers in +E.164, error). One per line or separated by commas; spaces inside a number are fine,
    and a local number like 0712 345 678 is read as Kenyan."""
    found = []
    for item in re.split(r"[,;\n]+", raw or ""):
        item = item.strip()
        if not item:
            continue
        e164, _, error = phone_numbers.parse(item, default_country)
        if error:
            return None, f"{item[:30]}: {error}"
        found.append(e164)
    found = list(dict.fromkeys(found))
    if len(found) > MAX_RECIPIENTS:
        return None, f"Add up to {MAX_RECIPIENTS} phone numbers."
    return found, None


# ---- texts that cost one SMS ---------------------------------------------------------------------

_GSM7 = set("@£$¥èéùìòÇ\nØøÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
_PLAIN = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                        "…": "...", " ": " ", "→": ">", "·": "-"})


def gsm7(text):
    """Keep a text inside the GSM-7 alphabet. A single curly quote, accent outside it or emoji turns the whole
    message into UCS-2: 70 characters a part instead of 160, so the same alert costs two or three texts."""
    out = []
    for ch in (text or "").translate(_PLAIN):
        if ch in _GSM7:
            out.append(ch)
        else:  # "Zoë" -> "Zoe", "Ngũgĩ" -> "Ngugi"; anything else is dropped
            out.extend(c for c in unicodedata.normalize("NFKD", ch) if c in _GSM7)
    return re.sub(r"  +", " ", "".join(out))


def fit_sms(before, flexible, after):
    """before + flexible + after in one text, shortening `flexible` (a title or name) if it must."""
    before, flexible, after = gsm7(before), " ".join(gsm7(flexible).split()), gsm7(after)
    room = SMS_LIMIT - len(before) - len(after)
    if len(flexible) > room:
        flexible = flexible[:max(room - 3, 0)].rstrip() + "..." if room > 6 else ""
    return (before + flexible + after)[:SMS_LIMIT]


# ---- sending -------------------------------------------------------------------------------------

def _link(endpoint, **values):
    base = current_app.config["BASE_URL"].rstrip("/")
    try:
        return base + url_for(endpoint, **values)
    except RuntimeError:  # outside a request (a CLI command)
        with current_app.test_request_context():
            return base + url_for(endpoint, **values)


def _deliver(emails, subject, text, html, phones, sms, submission_id):
    for to in phones:  # texts first: they're what gets noticed
        send_sms(to, sms, submission_id)
    for to in emails:
        send_email(to, subject, text, submission_id, html=html)


def _dispatch(emails, subject, text, html, phones, sms, submission_id=None):
    """Send in the background (inline under tests). Returns the thread, if one was started."""
    app = current_app._get_current_object()
    args = (emails, subject, text, html, phones, sms, submission_id)
    if app.testing and not app.config.get("ALERTS_IN_BACKGROUND"):
        _deliver(*args)
        return None

    def run():
        with app.app_context():
            try:
                _deliver(*args)
            except Exception:
                log.exception("Sending alert '%s' failed", subject)

    # Not a daemon: a redeploy waits for an alert that's already going out (each try has a timeout).
    thread = threading.Thread(target=run, name="alerts")
    thread.start()
    return thread


def _contact(name, email):
    return f"{name} · {email}" if email else name


def new_order(sub_id, customer, title, words, checks, instructions=""):
    """A customer just placed an order. Never raises: an alert must not break the upload."""
    try:
        emails, phones = recipients()
        cfg = current_app.config
        if cfg.get("ADMIN_EMAIL_ALERTS"):  # reviewers' own accounts already got the new-document email
            reviewers = {r["email"].lower() for r in query("SELECT email FROM users WHERE role = 'admin' AND is_active = 1")}
            emails = [e for e in emails if e not in reviewers]
        if not emails and not phones:
            return None
        app_name, minutes = cfg["APP_NAME"], cfg.get("REVIEW_MINUTES") or 20
        name = customer["name"]
        link = _link("admin.submission", sub_id=sub_id)
        subject = f"New order from {name}: {title}"
        content = {
            "kicker": "New order",
            "heading": f"{name} placed an order",
            "intro": [f"It's in the queue now. Feedback is due within {minutes} minutes."],
            "document": title,
            "details": [(label, value) for label, value in (
                ("Order", ref(sub_id)),
                ("Words", f"{words:,}" if words else ""),
                ("Checks used", str(checks) if checks else ""),
                ("Customer", _contact(name, customer["email"])),
                ("Phone", customer["phone"] or ""),
                ("Instructions", (instructions or "")[:200]),
            ) if value],
            "button": "Open the order",
            "preheader": f"{ref(sub_id)}" + (f", {words:,} words" if words else "") + f". Due within {minutes} minutes.",
            "footer": "alerts",
        }
        text, html = compose_email(subject, None, content, link)
        size = f", {words:,} words" if words else ""
        sms = fit_sms(f'{app_name}: New order from {name[:40]}: "', title, f'"{size}. {link}')
        return _dispatch(emails, subject, text, html, phones, sms, sub_id)
    except Exception:
        log.exception("Couldn't send the new-order alert for %s", ref(sub_id))
        return None


def payment(payment_id):
    """A payment was just recorded as paid. Never raises: the payment is already saved."""
    try:
        emails, phones = recipients()
        if not emails and not phones:
            return None
        from .billing import balance, plan_by_id
        p = query("SELECT p.*, u.name, u.email, u.phone FROM payments p LEFT JOIN users u ON u.id = p.user_id"
                  " WHERE p.id = ? AND p.status = 'paid'", (payment_id,), one=True)
        if p is None:
            return None
        app_name = current_app.config["APP_NAME"]
        major = Decimal(p["amount"]) / 100
        amount = (pricing.money(major, p["currency"]) if p["currency"] in pricing.CURRENCIES
                  else f"{p['currency']} {major:,.2f}")
        plan = plan_by_id(p["plan_id"], p["currency"]) or plan_by_id(p["plan_id"])
        if p["kind"] == "renewal":
            what = "a monthly plan renewal"
        elif plan is None:
            what = p["plan_id"]
        else:
            what = "1 check" if plan.checks == 1 else f"a {plan.checks}-check pack"
        name = p["name"] or "a deleted account"
        first = p["name"].split(" ")[0] if p["name"] else "The customer"
        channel = CHANNELS.get(p["channel"] or "", (p["channel"] or "").replace("_", " "))
        now_has = balance(p["user_id"]) if p["user_id"] else None
        link = _link("admin.payments")
        subject = f"Payment received: {amount} from {name}"
        content = {
            "kicker": "Payment received",
            "heading": f"{amount} from {name}",
            "intro": [f"{first} just paid for {what}."
                      + (f" They now have {now_has} {'check' if now_has == 1 else 'checks'}." if now_has is not None else "")],
            "document_label": "Payment",
            "document": f"{amount} for {what}",
            "details": [(label, value) for label, value in (
                ("Paid with", channel),
                ("Customer", _contact(name, p["email"])),
                ("Phone", p["phone"] or ""),
                ("Paystack reference", p["reference"]),
            ) if value],
            "button": "Open payments",
            "preheader": f"{name} paid {amount} for {what}.",
            "footer": "alerts",
        }
        text, html = compose_email(subject, None, content, link)
        how = f" ({channel})" if channel else ""
        sms = fit_sms(f"{app_name}: Payment {amount} from ", name, f" for {what}{how}. {link}")
        return _dispatch(emails, subject, text, html, phones, sms)
    except Exception:
        log.exception("Couldn't send the payment alert for payment %s", payment_id)
        return None


def send_test(sent_by):
    """Send a test to everyone on the list now, not in the background, so the page can say what happened.
    Returns [(recipient, outcome)] where outcome is "sent", "not set up" or the reason it failed."""
    emails, phones = recipients()
    app_name = current_app.config["APP_NAME"]
    subject = f"Test alert from {app_name}"
    text, html = compose_email(subject, None, {
        "kicker": "Test",
        "heading": "Alerts reach this address",
        "intro": [f"{sent_by} sent this test from Admin → Messages. From now on, every new order and every "
                  "payment is emailed here the moment it happens."],
        "button": "Open Messages",
        "footer": "alerts",
    }, _link("admin.messages"))
    sms = gsm7(f"{app_name}: test alert. New orders and payments will be texted to this number.")
    results = []
    for to in phones:
        r = send_sms(to, sms)
        results.append((to, "sent" if r.ok else ("not set up" if "switched off" in (r.error or "") else r.error)))
    for to in emails:
        outcome = send_email(to, subject, text, html=html)
        if outcome == "failed":
            row = query("SELECT error FROM message_log WHERE channel = 'email' AND recipient = ?"
                        " ORDER BY id DESC LIMIT 1", (to,), one=True)
            outcome = row["error"] if row else "failed"
        results.append((to, {"not_configured": "not set up"}.get(outcome, outcome)))
    return results
