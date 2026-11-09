import io
import json
import re
import sqlite3
import urllib.parse

import pytest

from conftest import Session, sub_id_from, upload

from app import create_app, sms
from app.sms import feedback_ready_text, normalize_phone


# ---- phone numbers --------------------------------------------------------------

@pytest.mark.parametrize("raw, cc, expected", [
    ("+254 712 345 678", "", "+254712345678"),
    ("+254-712-345-678", "", "+254712345678"),
    ("00254712345678", "", "+254712345678"),
    ("0712 345 678", "254", "+254712345678"),
    ("254712345678", "254", "+254712345678"),
    ("+44 (0)7700 900123", "", "+447700900123"),  # bracketed trunk 0 dropped
    ("+254 0712 345 678", "254", "+254712345678"),  # stray trunk 0 after the home code dropped
    ("+447700900123", "254", "+447700900123"),  # explicit country code wins
    ("0712 345 678", "", None),                 # local format needs DEFAULT_COUNTRY_CODE
    ("12345", "", None),
    ("+0123456789", "", None),
    ("", "254", None),
    ("not a number", "254", None),
])
def test_normalize_phone(raw, cc, expected):
    assert normalize_phone(raw, cc) == expected


def test_feedback_text_fits_one_gsm7_segment():
    text = feedback_ready_text("NerdAccess", "CA-00012", "https://checkassist.example.com/submissions/12")
    assert len(text) <= 160 and text.isascii()
    assert "CA-00012" in text and "https://checkassist.example.com/submissions/12" in text


# ---- signup and account -----------------------------------------------------------

def _register(app, **overrides):
    s = Session(app)
    data = {"name": "Nia", "email": "nia@example.com", "phone": "+254 712 000 111",
            "password": "longenough", "confirm": "longenough"}
    data.update(overrides)
    return s, s.post("/register", data)


def test_signup_requires_a_valid_mobile_number(app):
    _, r = _register(app, phone="")
    assert r.status_code == 200 and "Enter your mobile number" in r.get_data(as_text=True)
    _, r = _register(app, phone="0712 000 111")  # no DEFAULT_COUNTRY_CODE in tests
    assert "starting with + and your country code" in r.get_data(as_text=True)
    s, r = _register(app)
    assert r.status_code == 302
    with app.app_context():
        from app.db import query
        assert query("SELECT phone FROM users WHERE email = 'nia@example.com'", one=True)["phone"] == "+254712000111"


def test_local_numbers_accepted_with_default_country_code(tmp_path):
    app = create_app({"TESTING": True, "SECRET_KEY": "t", "DATABASE": str(tmp_path / "d.sqlite3"),
                      "UPLOAD_DIR": str(tmp_path / "u"), "DEFAULT_COUNTRY_CODE": "254"})
    page = app.test_client().get("/register").get_data(as_text=True)
    assert re.search(r'value="KE"[^>]* selected>', page) and 'placeholder="712 123456"' in page
    _, r = _register(app, phone="0712 000 111")
    assert r.status_code == 302
    with app.app_context():
        from app.db import query
        assert query("SELECT phone FROM users", one=True)["phone"] == "+254712000111"


def test_customer_without_number_is_prompted_and_can_add_one(app, bob):
    assert "Add your mobile number" in bob.get("/dashboard").get_data(as_text=True)
    r = bob.post("/account", {"name": "Bob Roe", "phone": "nope"})
    assert r.status_code == 400
    r = bob.post("/account", {"name": "Bob Roe", "phone": "+254 700 111 222"})
    assert r.status_code == 302
    assert "Add your mobile number" not in bob.get("/dashboard").get_data(as_text=True)
    page = bob.get("/account").get_data(as_text=True)
    assert re.search(r'value="KE"[^>]* selected>', page) and 'value="700111222"' in page


def test_admin_can_leave_phone_empty_and_sees_customer_numbers(admin, users):
    assert admin.post("/account", {"name": "Rita Reviewer", "phone": ""}).status_code == 302
    page = admin.get(f"/admin/users/{users['alice']}").get_data(as_text=True)
    assert 'href="tel:+254712345678"' in page
    assert "Alice Doe" in admin.get("/admin/users?q=712345678").get_data(as_text=True)
    assert "Alice Doe" in admin.get("/admin/users?q=712 345 678").get_data(as_text=True)  # spaces ignored
    assert "Alice Doe" not in admin.get("/admin/users?q=799999").get_data(as_text=True)


# ---- text when results are sent --------------------------------------------------

def test_sending_results_texts_the_customer(app, alice, admin):
    sid = sub_id_from(upload(alice, title="Cover letter"))
    r = admin.post(f"/admin/submissions/{sid}/results", {"action": "send", "feedback": "Lead with your project."})
    texts = app.config["SMS_OUTBOX"]
    assert len(texts) == 1
    assert texts[0]["to"] == "+254712345678"
    assert f"CA-{sid:05d}" in texts[0]["message"] and f"https://example.test/submissions/{sid}" in texts[0]["message"]
    assert "Cover letter" not in texts[0]["message"]  # no document title on a lock screen
    flash = admin.get(f"/admin/submissions/{sid}").get_data(as_text=True)
    assert "Text sent to +254" in flash
    assert "Text sent" not in alice.get(f"/submissions/{sid}").get_data(as_text=True)  # internal only
    assert r.status_code == 302


def test_drafts_and_status_changes_do_not_text(app, alice, admin):
    sid = sub_id_from(upload(alice))
    admin.post(f"/admin/submissions/{sid}/claim")
    admin.post(f"/admin/submissions/{sid}/results", {"action": "save", "feedback": "Draft"})
    assert app.config["SMS_OUTBOX"] == []


def test_resend_texts_updated_feedback(app, alice, admin):
    sid = sub_id_from(upload(alice))
    admin.post(f"/admin/submissions/{sid}/results", {"action": "send", "feedback": "One"})
    admin.post(f"/admin/submissions/{sid}/results", {"action": "send", "feedback": "Two"})
    texts = app.config["SMS_OUTBOX"]
    assert len(texts) == 2 and "updated feedback" in texts[1]["message"]


def test_no_number_means_no_text_and_reviewer_is_told(app, bob, admin):
    sid = sub_id_from(upload(bob))
    r = admin.post(f"/admin/submissions/{sid}/results", {"action": "send", "feedback": "Done"}, follow_redirects=True)
    page = r.get_data(as_text=True)
    assert app.config["SMS_OUTBOX"] == []
    assert "The customer was emailed. No text went out" in page and "no mobile number saved" in page


# ---- gateways (HTTP calls faked) -------------------------------------------------

class FakeResponse:
    def __init__(self, status, payload):
        self.status, self._body = status, json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def captured(monkeypatch):
    calls = []

    def install(status, payload):
        def fake_urlopen(req, timeout=None):
            calls.append({"url": req.full_url, "headers": {k.lower(): v for k, v in req.header_items()},
                          "fields": dict(urllib.parse.parse_qsl(req.data.decode())), "timeout": timeout})
            return FakeResponse(status, payload)
        monkeypatch.setattr(sms.urllib.request, "urlopen", fake_urlopen)
        return calls
    return install


def _gateway_app(tmp_path, **cfg):
    return create_app({"TESTING": True, "SECRET_KEY": "t", "DATABASE": str(tmp_path / "d.sqlite3"),
                       "UPLOAD_DIR": str(tmp_path / "u"), **cfg})


def test_africastalking_request_and_success(tmp_path, captured):
    calls = captured(201, {"SMSMessageData": {"Message": "Sent to 1/1", "Recipients": [
        {"statusCode": 101, "number": "+254712345678", "status": "Success", "messageId": "ATXid_1"}]}})
    app = _gateway_app(tmp_path, SMS_PROVIDER="africastalking", AT_USERNAME="myapp", AT_API_KEY="k123", AT_SENDER_ID="CHECKASSIST")
    with app.app_context():
        result = sms.send_sms("+254712345678", "hello")
    assert result.ok and result.message_id == "ATXid_1"
    call = calls[0]
    assert call["url"] == "https://api.africastalking.com/version1/messaging"
    assert call["headers"]["apikey"] == "k123" and call["headers"]["accept"] == "application/json"
    assert call["fields"] == {"username": "myapp", "to": "+254712345678", "message": "hello", "from": "CHECKASSIST"}
    assert call["timeout"] == 10


def test_africastalking_sandbox_and_rejection(tmp_path, captured):
    calls = captured(201, {"SMSMessageData": {"Message": "Sent to 0/1", "Recipients": [
        {"statusCode": 405, "number": "+254712345678", "status": "UserInBlacklist"}]}})
    app = _gateway_app(tmp_path, SMS_PROVIDER="africastalking", AT_USERNAME="sandbox", AT_API_KEY="k")
    with app.app_context():
        result = sms.send_sms("+254712345678", "hello")
    assert calls[0]["url"].startswith("https://api.sandbox.africastalking.com/")
    assert not result.ok and result.error == "UserInBlacklist"


def test_twilio_request_and_success(tmp_path, captured):
    calls = captured(201, {"sid": "SM123", "status": "queued"})
    app = _gateway_app(tmp_path, SMS_PROVIDER="twilio", TWILIO_ACCOUNT_SID="AC1", TWILIO_AUTH_TOKEN="tok", TWILIO_FROM="+15005550006")
    with app.app_context():
        result = sms.send_sms("+447700900123", "hello")
    assert result.ok and result.message_id == "SM123"
    call = calls[0]
    assert call["url"] == "https://api.twilio.com/2010-04-01/Accounts/AC1/Messages.json"
    assert call["headers"]["authorization"] == "Basic QUMxOnRvaw=="  # base64("AC1:tok")
    assert call["fields"] == {"To": "+447700900123", "Body": "hello", "From": "+15005550006"}


def test_twilio_messaging_service_used_instead_of_from(tmp_path, captured):
    calls = captured(201, {"sid": "SM9", "status": "accepted"})
    app = _gateway_app(tmp_path, SMS_PROVIDER="twilio", TWILIO_ACCOUNT_SID="AC1", TWILIO_AUTH_TOKEN="tok",
                       TWILIO_FROM="+15005550006", TWILIO_MESSAGING_SERVICE_SID="MG1")
    with app.app_context():
        sms.send_sms("+447700900123", "hello")
    assert calls[0]["fields"].get("MessagingServiceSid") == "MG1" and "From" not in calls[0]["fields"]


def test_network_error_never_raises(tmp_path, monkeypatch):
    def boom(req, timeout=None):
        raise OSError("connection refused")
    monkeypatch.setattr(sms.urllib.request, "urlopen", boom)
    app = _gateway_app(tmp_path, SMS_PROVIDER="twilio", TWILIO_ACCOUNT_SID="AC1", TWILIO_AUTH_TOKEN="t", TWILIO_FROM="+1500")
    with app.app_context():
        result = sms.send_sms("+447700900123", "hello")
    assert not result.ok and "connection refused" in result.error


def test_switched_off_reports_why(tmp_path):
    app = _gateway_app(tmp_path)
    with app.app_context():
        result = sms.send_sms("+447700900123", "hello")
    assert not result.ok and "SMS_PROVIDER" in result.error


# ---- upgrading an existing database --------------------------------------------

def test_existing_database_gets_phone_column(tmp_path):
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL,"
                 " password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', is_active INTEGER NOT NULL DEFAULT 1,"
                 " created_at TEXT NOT NULL, last_login_at TEXT)")
    conn.execute("INSERT INTO users (email, name, password_hash, created_at) VALUES ('old@example.com', 'Old', 'x', '2026-01-01T00:00:00Z')")
    conn.commit()
    conn.close()
    _gateway_app(tmp_path, DATABASE=str(db))
    cols = [r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(users)")]
    assert "phone" in cols
    assert sqlite3.connect(db).execute("SELECT email FROM users").fetchone()[0] == "old@example.com"
