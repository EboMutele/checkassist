import io
import re

from conftest import PDF_BYTES, make_docx, sub_id_from, upload


def test_upload_creates_submission_and_alerts_admins(app, alice):
    r = upload(alice)
    assert r.status_code == 302
    sid = sub_id_from(r)
    page = alice.get(f"/submissions/{sid}").get_data(as_text=True)
    assert "My essay" in page and "essay.pdf" in page and "Submitted" in page
    with app.app_context():
        from app.db import query
        notes = query("SELECT user_id, message FROM notifications")
        assert len(notes) == 2  # both admins, nobody assigned yet
        assert all("New document from Alice Doe" in n["message"] for n in notes)
        f = query("SELECT * FROM files", one=True)
        assert f["size"] == len(PDF_BYTES) and len(f["sha256"]) == 64


def test_rejects_wrong_extension_fake_pdf_empty_and_binary_txt(alice):
    cases = [
        (b"MZ\x90\x00", "virus.exe", "isn&#39;t supported"),
        (b"not really a pdf", "fake.pdf", "doesn&#39;t look like a real .pdf"),
        (b"", "empty.pdf", "Choose a file"),  # werkzeug drops empty uploads' content; either message is fine
        (b"abc\x00def", "notes.txt", "binary data"),
    ]
    for content, name, expected in cases:
        r = upload(alice, content=content, filename=name)
        body = r.get_data(as_text=True)
        assert r.status_code == 400
        assert expected in body or "file is empty" in body, (name, body[:300])


def test_requires_title_and_rights_confirmation(alice):
    r = upload(alice, title="")
    assert r.status_code == 400 and "Give your document a title" in r.get_data(as_text=True)
    r = upload(alice, confirm_rights="")
    assert r.status_code == 400 and "right to share" in r.get_data(as_text=True)


def test_oversize_upload_returns_413(alice):
    big = b"%PDF-" + b"0" * (1024 * 1024 + 10)
    r = upload(alice, content=big)
    assert r.status_code == 413
    assert "File too large" in r.get_data(as_text=True)


def test_other_users_cannot_see_or_download(app, alice, bob):
    sid = sub_id_from(upload(alice))
    assert bob.get(f"/submissions/{sid}").status_code == 404
    with app.app_context():
        from app.db import query
        fid = query("SELECT id FROM files", one=True)["id"]
    assert bob.get(f"/files/{fid}").status_code == 404
    r = alice.get(f"/files/{fid}")
    assert r.status_code == 200 and r.data == PDF_BYTES
    assert "attachment" in r.headers["Content-Disposition"] and "essay.pdf" in r.headers["Content-Disposition"]
    assert r.headers["Cache-Control"] == "private, no-store"


def test_inline_txt_preview_is_sandboxed(app, alice):
    sid = sub_id_from(upload(alice, content=b"<script>alert(1)</script> plain text", filename="notes.txt"))
    with app.app_context():
        from app.db import query
        fid = query("SELECT id FROM files WHERE submission_id = ?", (sid,), one=True)["id"]
    r = alice.get(f"/files/{fid}?inline=1")
    assert r.headers["Content-Type"].startswith("text/plain")
    assert r.headers["Content-Security-Policy"].startswith("sandbox")


def test_filename_path_parts_are_stripped(app, alice):
    upload(alice, filename="../../etc/passwd.pdf")
    with app.app_context():
        from app.db import query
        assert query("SELECT original_name FROM files", one=True)["original_name"] == "passwd.pdf"


def test_dashboard_lists_own_documents_only(alice, bob):
    upload(alice, title="Alice paper")
    upload(bob, title="Bob paper")
    page = alice.get("/dashboard").get_data(as_text=True)
    assert "Alice paper" in page and "Bob paper" not in page


def test_docx_upload_accepted(alice):
    r = upload(alice, content=make_docx(), filename="essay.docx")
    assert r.status_code == 302
