import io

from conftest import PDF_BYTES, make_docx, sub_id_from, upload


def _files(app, sid, kind=None):
    with app.app_context():
        from app.db import query
        sql = "SELECT * FROM files WHERE submission_id = ?"
        args = [sid]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        return [dict(r) for r in query(sql, args)]


def test_admin_sees_queue_and_downloads_original(app, alice, admin):
    sid = sub_id_from(upload(alice, title="Thesis chapter"))
    page = admin.get("/admin/").get_data(as_text=True)
    assert "Thesis chapter" in page and "Alice Doe" in page and "1 open" in page
    fid = _files(app, sid, "original")[0]["id"]
    r = admin.get(f"/files/{fid}")
    assert r.status_code == 200 and r.data == PDF_BYTES


def test_admin_detail_shows_docx_text_preview(alice, admin):
    sid = sub_id_from(upload(alice, content=make_docx("Opening line of my essay."), filename="essay.docx"))
    page = admin.get(f"/admin/submissions/{sid}").get_data(as_text=True)
    assert "Opening line of my essay." in page
    assert "Second paragraph &amp; more." in page


def test_queue_search_by_reference_and_email(alice, bob, admin):
    a = sub_id_from(upload(alice, title="Alpha"))
    upload(bob, title="Beta")
    page = admin.get(f"/admin/?tab=all&q=CA-{a:05d}").get_data(as_text=True)
    assert "Alpha" in page and "Beta" not in page
    page = admin.get("/admin/?tab=all&q=bob@").get_data(as_text=True)
    assert "Beta" in page and "Alpha" not in page


def test_claim_sets_in_review_and_notifies_customer(app, alice, admin, users):
    sid = sub_id_from(upload(alice))
    admin.post(f"/admin/submissions/{sid}/claim")
    page = alice.get(f"/submissions/{sid}").get_data(as_text=True)
    assert "In review" in page
    with app.app_context():
        from app.db import query
        row = query("SELECT status, assigned_to FROM submissions WHERE id = ?", (sid,), one=True)
        assert row["status"] == "in_review" and row["assigned_to"] == users["admin"]
        assert query("SELECT COUNT(*) AS n FROM notifications WHERE user_id = ?", (users["alice"],), one=True)["n"] == 1


def test_draft_is_hidden_until_sent_then_customer_gets_results(app, alice, admin):
    sid = sub_id_from(upload(alice, title="Cover letter"))
    outbox = app.config["MAIL_OUTBOX"]

    admin.post(f"/admin/submissions/{sid}/results", {
        "action": "save",
        "feedback": "Draft note: tighten the opening.",
        "result_files": (io.BytesIO(PDF_BYTES), "cover-letter-marked.pdf"),
    }, content_type="multipart/form-data")

    page = alice.get(f"/submissions/{sid}").get_data(as_text=True)
    assert "tighten the opening" not in page and "cover-letter-marked.pdf" not in page
    result = _files(app, sid, "result")[0]
    assert alice.get(f"/files/{result['id']}").status_code == 404
    to_alice = lambda: [m for m in outbox if m["to"] == "alice@example.com"]  # noqa: E731
    assert to_alice() == []
    assert {m["to"] for m in outbox} == {"admin@example.com", "admin2@example.com"}  # upload alerts

    admin.post(f"/admin/submissions/{sid}/results", {"action": "send", "feedback": "Lead with your strongest project."})

    page = alice.get(f"/submissions/{sid}").get_data(as_text=True)
    assert "Your feedback" in page and "Lead with your strongest project." in page
    assert "cover-letter-marked.pdf" in page and "Completed" in page
    r = alice.get(f"/files/{result['id']}")
    assert r.status_code == 200 and r.data == PDF_BYTES
    assert len(to_alice()) == 1
    mail = to_alice()[0]
    assert mail["to"] == "alice@example.com" and "feedback is ready" in mail["subject"].lower()
    assert f"https://example.test/submissions/{sid}" in mail["body"]


def test_send_requires_feedback_or_file(app, alice, admin):
    sid = sub_id_from(upload(alice))
    r = admin.post(f"/admin/submissions/{sid}/results", {"action": "send", "feedback": ""})
    assert r.status_code == 302
    with app.app_context():
        from app.db import query
        row = query("SELECT status, feedback_sent_at FROM submissions WHERE id = ?", (sid,), one=True)
        assert row["status"] == "submitted" and row["feedback_sent_at"] is None


def test_completed_status_only_via_send_results(app, alice, admin):
    sid = sub_id_from(upload(alice))
    admin.post(f"/admin/submissions/{sid}/status", {"status": "completed"})
    with app.app_context():
        from app.db import query
        assert query("SELECT status FROM submissions WHERE id = ?", (sid,), one=True)["status"] == "submitted"


def test_question_flow_needs_info_then_customer_reply(app, alice, admin, users):
    sid = sub_id_from(upload(alice))
    admin.post(f"/admin/submissions/{sid}/claim")
    admin.post(f"/admin/submissions/{sid}/message", {
        "body": "Which citation style does your tutor want?", "visibility": "customer", "request_info": "1"})
    page = alice.get(f"/submissions/{sid}").get_data(as_text=True)
    assert "Needs your input" in page and "Which citation style" in page and "Your reviewer" in page
    assert "Your reviewer has a question" in alice.get("/dashboard").get_data(as_text=True)
    mail = next(m for m in app.config["MAIL_OUTBOX"] if "has a question about" in m["subject"])
    assert "Which citation style" in mail["body"] and "Which citation style" in mail["html"]  # the question itself

    alice.post(f"/submissions/{sid}/reply", {"body": "Harvard, please."})
    with app.app_context():
        from app.db import query
        assert query("SELECT status FROM submissions WHERE id = ?", (sid,), one=True)["status"] == "in_review"
        note = query("SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC", (users["admin"],), one=True)
        assert "replied" in note["message"]


def test_internal_notes_never_reach_customer(alice, admin):
    sid = sub_id_from(upload(alice))
    admin.post(f"/admin/submissions/{sid}/message", {"body": "Looks AI-heavy, check sources", "visibility": "internal"})
    assert "Looks AI-heavy" in admin.get(f"/admin/submissions/{sid}").get_data(as_text=True)
    assert "Looks AI-heavy" not in alice.get(f"/submissions/{sid}").get_data(as_text=True)


def test_assign_to_other_admin_and_reject_non_admin(app, alice, admin, users):
    sid = sub_id_from(upload(alice))
    admin.post(f"/admin/submissions/{sid}/assign", {"assignee": str(users["admin2"])})
    r = admin.post(f"/admin/submissions/{sid}/assign", {"assignee": str(users["alice"])})
    assert r.status_code == 400
    with app.app_context():
        from app.db import query
        assert query("SELECT assigned_to FROM submissions WHERE id = ?", (sid,), one=True)["assigned_to"] == users["admin2"]


def test_remove_result_file_but_never_original(app, alice, admin):
    sid = sub_id_from(upload(alice))
    admin.post(f"/admin/submissions/{sid}/results", {
        "action": "save", "feedback": "",
        "result_files": (io.BytesIO(PDF_BYTES), "marked.pdf")}, content_type="multipart/form-data")
    result = _files(app, sid, "result")[0]
    original = _files(app, sid, "original")[0]
    assert admin.post(f"/admin/files/{original['id']}/delete").status_code == 400
    admin.post(f"/admin/files/{result['id']}/delete")
    assert _files(app, sid, "result") == []


def test_users_list_and_detail(alice, admin, users):
    upload(alice, title="Paper one")
    page = admin.get("/admin/users").get_data(as_text=True)
    assert "Alice Doe" in page and "bob@example.com" in page
    page = admin.get(f"/admin/users/{users['alice']}").get_data(as_text=True)
    assert "Paper one" in page and "alice@example.com" in page


def test_notifications_page_and_mark_all_read(alice, admin):
    sid = sub_id_from(upload(alice))
    admin.post(f"/admin/submissions/{sid}/claim")
    page = alice.get("/notifications").get_data(as_text=True)
    assert "A reviewer has started" in page and "Mark all as read" in page
    alice.post("/notifications/read-all")
    assert "Mark all as read" not in alice.get("/notifications").get_data(as_text=True)
