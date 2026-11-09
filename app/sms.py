"""Text messages to customers.

SMS_PROVIDER picks the gateway:
  africastalking  AT_USERNAME, AT_API_KEY, optional AT_SENDER_ID (AT_USERNAME=sandbox uses the sandbox)
  twilio          TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_FROM or TWILIO_MESSAGING_SERVICE_SID
  (empty)         texts are skipped; the reason is recorded on the submission timeline
No third-party packages: both gateways are plain HTTPS form posts.
"""
import base64
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple, Optional

from flask import current_app

log = logging.getLogger(__name__)

_SEPARATORS = re.compile(r"[\s\-().]")
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
AT_SUCCESS_CODES = {100, 101, 102}  # Processed, Sent, Queued


class SmsResult(NamedTuple):
    ok: bool
    message_id: Optional[str] = None
    error: Optional[str] = None


def normalize_phone(raw, default_country_code=""):
    """Return an E.164 number like +254712345678, or None if it can't be one.

    Accepts +254 712 345 678, 00254712345678, and, when a default country code is set,
    local numbers such as 0712 345 678 or 254712345678.
    """
    # "+44 (0)7700 900123": the bracketed 0 is the domestic trunk prefix and must go.
    s = _SEPARATORS.sub("", (raw or "").replace("(0)", ""))
    cc = (default_country_code or "").lstrip("+")
    if cc and s.startswith(f"+{cc}0"):
        s = f"+{cc}" + s[len(cc) + 2:]  # "+254 0712…" -> "+254712…"
    if s.startswith("00"):
        s = "+" + s[2:]
    elif not s.startswith("+"):
        if cc and s.startswith("0"):
            s = "+" + cc + s[1:]
        elif cc and s.startswith(cc) and s.isdigit():
            s = "+" + s
        else:
            return None
    return s if _E164.match(s) else None


def mask_phone(phone):
    if not phone or len(phone) < 8:
        return phone or ""
    return f"{phone[:4]}•••{phone[-3:]}"


def user_agent():
    from .billing import user_agent as named  # same reason as for Paystack: Cloudflare blocks "Python-urllib"
    return named()


def _post(url, fields, headers):
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(fields).encode(), method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": user_agent(), **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"message": body[:200]}


def _send_africastalking(cfg, to, message):
    username = cfg["AT_USERNAME"]
    host = "https://api.sandbox.africastalking.com" if username == "sandbox" else "https://api.africastalking.com"
    fields = {"username": username, "to": to, "message": message}
    if cfg.get("AT_SENDER_ID"):
        fields["from"] = cfg["AT_SENDER_ID"]
    status, data = _post(f"{host}/version1/messaging", fields, {"apiKey": cfg["AT_API_KEY"]})
    sms = data.get("SMSMessageData") or {}
    recipients = sms.get("Recipients") or []
    if recipients and recipients[0].get("statusCode") in AT_SUCCESS_CODES:
        return SmsResult(True, recipients[0].get("messageId"))
    reason = (recipients[0].get("status") if recipients else None) or sms.get("Message") or f"HTTP {status}"
    return SmsResult(False, error=str(reason)[:200])


def _send_twilio(cfg, to, message):
    sid, token = cfg["TWILIO_ACCOUNT_SID"], cfg["TWILIO_AUTH_TOKEN"]
    fields = {"To": to, "Body": message}
    if cfg.get("TWILIO_MESSAGING_SERVICE_SID"):
        fields["MessagingServiceSid"] = cfg["TWILIO_MESSAGING_SERVICE_SID"]
    else:
        fields["From"] = cfg["TWILIO_FROM"]
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    status, data = _post(
        f"https://api.twilio.com/2010-04-01/Accounts/{urllib.parse.quote(sid)}/Messages.json",
        fields, {"Authorization": f"Basic {auth}"},
    )
    if 200 <= status < 300 and data.get("sid") and data.get("status") not in ("failed", "undelivered"):
        return SmsResult(True, data["sid"])
    return SmsResult(False, error=str(data.get("message") or data.get("error_message") or f"HTTP {status}")[:200])


PROVIDERS = {"africastalking": _send_africastalking, "twilio": _send_twilio}


def send_sms(to, message, submission_id=None) -> SmsResult:
    """Send one text and record the outcome (Admin > Messages). Never raises."""
    result = _deliver(to, message)
    from .notify import log_message
    status = "sent" if result.ok else ("not_configured" if result.error and "switched off" in result.error else "failed")
    log_message("sms", to, "", message, status, "" if result.ok else result.error, submission_id)
    return result


def _deliver(to, message) -> SmsResult:
    cfg = current_app.config
    outbox = cfg.get("SMS_OUTBOX")
    if outbox is not None:  # tests
        outbox.append({"to": to, "message": message})
        return SmsResult(True, f"test-{len(outbox)}")
    provider = (cfg.get("SMS_PROVIDER") or "").lower()
    if not provider:
        return SmsResult(False, error="Texts are switched off (SMS_PROVIDER is not set)")
    send = PROVIDERS.get(provider)
    if send is None:
        return SmsResult(False, error=f"Unknown SMS_PROVIDER '{provider}'")
    try:
        result = send(cfg, to, message)
    except Exception as e:  # network errors, timeouts, bad config
        log.exception("SMS to %s failed", mask_phone(to))
        return SmsResult(False, error=f"{type(e).__name__}: {e}"[:200])
    if not result.ok:
        log.warning("SMS to %s not sent: %s", mask_phone(to), result.error)
    return result


def feedback_ready_text(app_name, ref, link, updated=False):
    """Short, GSM-7 only (one SMS segment), and no document title on the lock screen."""
    what = "updated feedback" if updated else "feedback"
    return f"{app_name}: your {what} for {ref} is ready. Read it here: {link}"
