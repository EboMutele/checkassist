"""Email through Resend's API (DigitalOcean blocks the SMTP ports), with SMTP as the fallback."""
import json

import pytest

from app import notify
from app.db import query


@pytest.fixture
def resend_app(app, monkeypatch):
    app.config.update(MAIL_OUTBOX=None, RESEND_API_KEY="re_test_key", SMTP_HOST="",
                      EMAIL_FROM="NerdAccess <notifications@mail.nerdaccess.store>",
                      EMAIL_REPLY_TO="", SUPPORT_EMAIL="support@nerdaccess.store", BASE_URL="https://nerdaccess.store")
    monkeypatch.setattr(notify, "RESEND_WAITS", (0, 0))
    return app


def fake_resend(monkeypatch, answers):
    """Replace the HTTP call with scripted answers; returns the list of requests made."""
    calls = []

    def post(url, headers, body):
        calls.append({"url": url, "headers": headers, "payload": json.loads(body)})
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(notify, "_resend_post", post)
    return calls


def test_sends_through_resend_with_reply_to_and_a_named_user_agent(resend_app, monkeypatch):
    calls = fake_resend(monkeypatch, [(200, {"id": "email_123"})])
    with resend_app.test_request_context():
        assert notify.send_email("amina@example.com", "Your feedback is ready", "Hello") == "sent"
        logged = query("SELECT status, error FROM message_log ORDER BY id DESC LIMIT 1", one=True)
    (call,) = calls
    assert call["url"] == "https://api.resend.com/emails"
    assert call["payload"] == {"from": "NerdAccess <notifications@mail.nerdaccess.store>", "to": ["amina@example.com"],
                               "subject": "Your feedback is ready", "text": "Hello",
                               "reply_to": "support@nerdaccess.store"}
    h = call["headers"]
    assert h["Authorization"] == "Bearer re_test_key" and h["User-Agent"].startswith("NerdAccess/1.0")
    assert len(h["Idempotency-Key"]) == 36
    assert (logged["status"], logged["error"]) == ("sent", "")


def test_retries_server_errors_and_timeouts_with_the_same_idempotency_key(resend_app, monkeypatch):
    calls = fake_resend(monkeypatch, [TimeoutError("timed out"), (503, {"message": "busy"}), (200, {"id": "e"})])
    with resend_app.test_request_context():
        assert notify.send_email("a@example.com", "s", "b") == "sent"
    assert len(calls) == 3
    assert len({c["headers"]["Idempotency-Key"] for c in calls}) == 1  # a retry can't send it twice


def test_a_wrong_key_or_unverified_domain_fails_once_with_resends_reason(resend_app, monkeypatch):
    calls = fake_resend(monkeypatch, [(403, {"name": "validation_error",
                                             "message": "The mail.nerdaccess.store domain is not verified."})])
    with resend_app.test_request_context():
        assert notify.send_email("a@example.com", "s", "b") == "failed"
        logged = query("SELECT status, error FROM message_log ORDER BY id DESC LIMIT 1", one=True)
    assert len(calls) == 1  # a 4xx won't get better by retrying
    assert logged["error"] == "Resend HTTP 403: The mail.nerdaccess.store domain is not verified."


def test_gives_up_after_three_tries(resend_app, monkeypatch):
    calls = fake_resend(monkeypatch, [(429, {"message": "rate limited"})] * 3)
    with resend_app.test_request_context():
        assert notify.send_email("a@example.com", "s", "b") == "failed"
    assert len(calls) == 3


def test_no_reply_to_without_a_support_address(resend_app, monkeypatch):
    resend_app.config["SUPPORT_EMAIL"] = ""
    calls = fake_resend(monkeypatch, [(200, {"id": "e"})])
    with resend_app.test_request_context():
        notify.send_email("a@example.com", "s", "b")
    assert "reply_to" not in calls[0]["payload"]


def test_without_a_key_or_smtp_email_is_not_configured(resend_app):
    resend_app.config["RESEND_API_KEY"] = ""
    with resend_app.test_request_context():
        assert notify.send_email("a@example.com", "s", "b") == "not_configured"


def test_messages_page_counts_resend_as_set_up(resend_app, admin):
    page = admin.get("/admin/messages").get_data(as_text=True)
    assert "Email isn't set up" not in page
