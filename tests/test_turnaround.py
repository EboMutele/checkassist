from datetime import datetime, timedelta, timezone

from conftest import Session, sub_id_from, upload

from app import create_app
from app.helpers import timing


def _ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def test_timing_states():
    created = _ts(NOW - timedelta(minutes=8))
    t = timing(created, "in_review", None, 20, now=NOW)
    assert t["state"] == "due" and t["minutes_left"] == 12 and t["due_at"] == _ts(NOW + timedelta(minutes=12))

    late = timing(_ts(NOW - timedelta(minutes=27)), "submitted", None, 20, now=NOW)
    assert late["state"] == "overdue" and late["minutes_over"] == 7

    assert timing(created, "needs_info", None, 20, now=NOW)["state"] == "waiting"

    done = timing(_ts(NOW - timedelta(minutes=30)), "completed", _ts(NOW - timedelta(minutes=13)), 20, now=NOW)
    assert done["state"] == "done" and done["took"] == 17 and done["on_time"] is True

    slow = timing(_ts(NOW - timedelta(minutes=60)), "completed", _ts(NOW), 20, now=NOW)
    assert slow["took"] == 60 and slow["on_time"] is False

    assert timing(created, "in_review", None, 0, now=NOW)["state"] is None  # promise switched off


def _set_created(app, sid, minutes_ago):
    with app.app_context():
        from app.db import execute
        execute("UPDATE submissions SET created_at = ? WHERE id = ?",
                (_ts(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)), sid))


def test_homepage_promises_twenty_minutes(client):
    page = client.get("/").get_data(as_text=True)
    assert "in about 20 minutes" in page and "Notes back in about 20 minutes" in page


def test_promise_can_be_switched_off(tmp_path):
    app = create_app({"TESTING": True, "SECRET_KEY": "t", "DATABASE": str(tmp_path / "d.sqlite3"),
                      "UPLOAD_DIR": str(tmp_path / "u"), "REVIEW_MINUTES": 0})
    page = app.test_client().get("/").get_data(as_text=True)
    assert "20 minutes" not in page and "Notes back in" not in page


def test_customer_sees_expected_time_then_overdue_message(app, alice):
    sid = sub_id_from(upload(alice))
    page = alice.get(f"/submissions/{sid}").get_data(as_text=True)
    assert "Expected by" in page and "in about 20 minutes" in page and "data-autorefresh" in page
    _set_created(app, sid, 26)
    page = alice.get(f"/submissions/{sid}").get_data(as_text=True)
    assert "taking a little longer than usual" in page and "Expected by" not in page


def test_admin_queue_flags_overdue_and_counts_it(app, alice, admin):
    on_time = sub_id_from(upload(alice, title="Fresh one"))
    late = sub_id_from(upload(alice, title="Old one"))
    _set_created(app, late, 31)
    page = admin.get("/admin/").get_data(as_text=True)
    assert "<title>(1 overdue) Review queue" in page
    assert "11 min overdue" in page and "20 min left" in page
    assert page.index("Old one") < page.index("Fresh one")  # oldest first
    detail = admin.get(f"/admin/submissions/{late}").get_data(as_text=True)
    assert "11 min overdue" in detail
    assert on_time  # silence unused warning


def test_completed_shows_actual_time_and_on_time_rate(app, alice, admin):
    sid = sub_id_from(upload(alice, title="Quick one"))
    _set_created(app, sid, 17)
    admin.post(f"/admin/submissions/{sid}/results", {"action": "send", "feedback": "Good work."})
    page = alice.get(f"/submissions/{sid}").get_data(as_text=True)
    assert "17 minutes after you uploaded" in page
    assert "Reviewed in 17 minutes" in alice.get("/dashboard").get_data(as_text=True)
    queue = admin.get("/admin/?tab=completed").get_data(as_text=True)
    assert "Took 17 min" in queue
    assert "100% within 20 min" in admin.get("/admin/").get_data(as_text=True)


def test_resend_keeps_first_turnaround(app, alice, admin):
    sid = sub_id_from(upload(alice))
    _set_created(app, sid, 12)
    admin.post(f"/admin/submissions/{sid}/results", {"action": "send", "feedback": "First pass."})
    _set_created(app, sid, 50)  # pretend the upload was earlier; first send is now 38 min after it
    with app.app_context():
        from app.db import execute
        # move the first send back so it's 12 min after the (new) upload time
        execute("UPDATE events SET created_at = ? WHERE submission_id = ? AND type = 'results_sent'",
                (_ts(datetime.now(timezone.utc) - timedelta(minutes=38)), sid))
    admin.post(f"/admin/submissions/{sid}/results", {"action": "send", "feedback": "Updated pass."})
    page = alice.get(f"/submissions/{sid}").get_data(as_text=True)
    assert "12 minutes after you uploaded" in page and "Updated pass." in page


def test_needs_info_pauses_customer_countdown(alice, admin):
    sid = sub_id_from(upload(alice))
    admin.post(f"/admin/submissions/{sid}/message", {"body": "Which style?", "visibility": "customer", "request_info": "1"})
    page = alice.get(f"/submissions/{sid}").get_data(as_text=True)
    assert "Expected by" not in page and "Your reviewer has a question" in page
    assert "Waiting on customer" in admin.get("/admin/").get_data(as_text=True)
