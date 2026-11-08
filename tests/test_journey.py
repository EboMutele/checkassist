"""The customer journey end to end, and exactly who hears about each step and how.

This is the living version of docs/JOURNEY.md: if a step's notifications change, this fails.
"""
import re
from pathlib import Path

import pytest

from conftest import Session, sub_id_from, upload
from app.db import query


def _inbox(app, user_id):
    with app.app_context():
        return [r["message"] for r in query("SELECT message FROM notifications WHERE user_id = ? ORDER BY id", (user_id,))]


def _status(app, sid):
    with app.app_context():
        return query("SELECT status FROM submissions WHERE id = ?", (sid,), one=True)["status"]


def _log(app):
    with app.app_context():
        return [(r["channel"], r["recipient"], r["status"]) for r in query("SELECT * FROM message_log ORDER BY id")]


def test_full_journey_and_who_is_told_what(app, users, alice, admin):
    mail, texts = app.config["MAIL_OUTBOX"], app.config["SMS_OUTBOX"]
    reviewers = (users["admin"], users["admin2"])

    # 1. Upload: status Submitted, a check is spent, every reviewer is alerted (in-app + email)
    sid = sub_id_from(upload(alice, title="Climate essay"))
    assert _status(app, sid) == "submitted"
    for r in reviewers:
        assert _inbox(app, r)[-1] == "New document from Alice Doe: Climate essay"
    assert sorted(m["to"] for m in mail) == ["admin2@example.com", "admin@example.com"]
    assert _inbox(app, users["alice"]) == []  # the customer watches the timer on their page instead

    # 2. A reviewer claims it: In review; the customer gets an in-app note only
    mail.clear()
    admin.post(f"/admin/submissions/{sid}/claim")
    assert _status(app, sid) == "in_review"
    assert _inbox(app, users["alice"])[-1] == "A reviewer has started on “Climate essay”."
    assert mail == [] and texts == []

    # 3. The reviewer asks a question: Needs your input (the timer pauses); in-app + email
    admin.post(f"/admin/submissions/{sid}/message", {"body": "Which citation style?", "request_info": "1"})
    assert _status(app, sid) == "needs_info"
    assert _inbox(app, users["alice"])[-1] == "Your reviewer has a question about “Climate essay”."
    assert [m["to"] for m in mail] == ["alice@example.com"] and "/submissions/" in mail[-1]["body"]

    # 4. The customer replies: back to In review; only the assigned reviewer is alerted
    mail.clear()
    alice.post(f"/submissions/{sid}/reply", {"body": "APA 7 please"})
    assert _status(app, sid) == "in_review"
    assert _inbox(app, users["admin"])[-1] == "Alice Doe replied on Climate essay"
    assert _inbox(app, users["admin2"])[-1] != "Alice Doe replied on Climate essay"
    assert [m["to"] for m in mail] == ["admin@example.com"]

    # 5. Results sent: Completed; in-app + email + text
    mail.clear()
    page = admin.post(f"/admin/submissions/{sid}/results", {"action": "send", "feedback": "Strong argument."},
                      follow_redirects=True).get_data(as_text=True)
    assert _status(app, sid) == "completed"
    assert _inbox(app, users["alice"])[-1] == "Your feedback for “Climate essay” is ready."
    assert [m["to"] for m in mail] == ["alice@example.com"]
    assert [t["to"] for t in texts] == ["+254712345678"]
    assert "The customer was emailed and texted." in page
    assert "Strong argument." in alice.get(f"/submissions/{sid}").get_data(as_text=True)

    # Every email and text is in the message log
    assert ("sms", "+254712345678", "sent") in _log(app)
    assert ("email", "alice@example.com", "sent") in _log(app)


@pytest.fixture
def unconfigured(app):
    """A fresh install: no SMTP and no SMS provider."""
    app.config.update(MAIL_OUTBOX=None, SMS_OUTBOX=None, SMTP_HOST="", SMS_PROVIDER="")
    return app


def test_without_email_or_texts_set_up_the_reviewer_is_told_the_truth(unconfigured, users, alice, admin):
    sid = sub_id_from(upload(alice))
    page = admin.post(f"/admin/submissions/{sid}/results", {"action": "send", "feedback": "Done"},
                      follow_redirects=True).get_data(as_text=True)
    assert "no email or text went out" in page and "emailed" not in page.split("Results sent.")[1][:120]
    detail = admin.get(f"/admin/submissions/{sid}").get_data(as_text=True)
    assert "No email sent: email isn&#39;t set up" in detail or "No email sent: email isn't set up" in detail
    log = _log(unconfigured)
    assert ("email", "alice@example.com", "not_configured") in log
    assert ("sms", "+254712345678", "not_configured") in log
    messages = admin.get("/admin/messages").get_data(as_text=True)
    assert "Email isn&#39;t set up" in messages or "Email isn't set up" in messages
    assert "Your feedback is ready" in messages and "Not set up" in messages


def test_failed_email_is_logged_and_never_breaks_the_request(unconfigured, users, alice, admin):
    unconfigured.config.update(SMTP_HOST="127.0.0.1", SMTP_PORT=1)  # nothing listens there
    sid = sub_id_from(upload(alice))
    r = admin.post(f"/admin/submissions/{sid}/results", {"action": "send", "feedback": "Done"})
    assert r.status_code == 302
    assert ("email", "alice@example.com", "failed") in _log(unconfigured)
    assert "Failed" in admin.get("/admin/messages?show=problems").get_data(as_text=True)


def test_messages_page_is_for_reviewers_only(client, alice):
    assert client.get("/admin/messages").status_code == 302
    assert alice.get("/admin/messages").status_code == 403


# ---- trying the app out ---------------------------------------------------------------------

def test_seed_demo_creates_accounts_with_checks(app):
    result = app.test_cli_runner().invoke(args=["seed-demo"])
    assert result.exit_code == 0, result.output
    assert "reviewer@demo.test" in result.output and "demo-pass-2026" in result.output
    s = Session(app)
    s.login("student@demo.test", "demo-pass-2026")
    assert "Checks <span class=\"count count-quiet\">5</span>" in s.get("/dashboard").get_data(as_text=True)
    again = app.test_cli_runner().invoke(args=["seed-demo"])
    assert "already there" in again.output
    with app.app_context():
        assert query("SELECT currency FROM users WHERE email = 'wanjiru@demo.test'", one=True)["currency"] == "KES"


def test_seed_demo_refuses_to_run_in_production(app):
    app.config["APP_ENV"] = "production"
    result = app.test_cli_runner().invoke(args=["seed-demo"])
    assert result.exit_code != 0 and "test copies only" in result.output


# ---- the install itself ---------------------------------------------------------------------

PACKAGES = {"flask": "flask", "werkzeug": "flask", "click": "flask", "markupsafe": "flask", "jinja2": "flask",
            "dotenv": "python-dotenv", "pypdf": "pypdf", "PIL": "pillow"}


def test_requirements_cover_every_third_party_import():
    """A missing line here once meant a fresh deploy couldn't start (Pillow, for the blog)."""
    import ast
    import sys
    root = Path(__file__).resolve().parent.parent
    declared = {re.split(r"[<>=!~\[ ]", line.strip(), maxsplit=1)[0].lower()
                for line in (root / "requirements.txt").read_text().splitlines() if line.strip() and not line.startswith("#")}
    used = set()
    for path in (root / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                used |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                used.add(node.module.split(".")[0])
    third_party = {m for m in used if m not in sys.stdlib_module_names}
    unknown = third_party - PACKAGES.keys()
    assert not unknown, f"Add these to PACKAGES and requirements.txt: {unknown}"
    missing = {PACKAGES[m] for m in third_party} - declared
    assert not missing, f"requirements.txt is missing: {missing}"
