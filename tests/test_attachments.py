"""Screenshots and files on conversation messages, and screenshots among reviewed files."""
import html
import io
import re

from PIL import Image

from tests.conftest import sub_id_from, upload


def png(size=(40, 30), color=(200, 30, 40)):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


def jpeg_with_location_and_rotation():
    """A phone photo: GPS position in its EXIF, and stored sideways with 'rotate 90°' in the orientation tag."""
    img = Image.new("RGB", (60, 20), (10, 120, 200))
    exif = Image.Exif()
    exif[0x0112] = 6  # orientation: rotate 90° clockwise to display
    exif[0x8825] = {1: "N", 2: (1.0, 17.0, 0.0), 3: "E", 4: (36.0, 49.0, 0.0)}  # GPS: Nairobi
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif)
    return out.getvalue()


def reply(session, sub_id, body="", files=()):
    data = {"body": body, "attachments": [(io.BytesIO(content), name) for name, content in files]}
    return session.post(f"/submissions/{sub_id}/reply", data, content_type="multipart/form-data")


def admin_message(session, sub_id, body="", files=(), internal=False):
    data = {"body": body, "visibility": "internal" if internal else "customer",
            "attachments": [(io.BytesIO(content), name) for name, content in files]}
    return session.post(f"/admin/submissions/{sub_id}/message", data, content_type="multipart/form-data")


def attachment_ids(html):
    return [int(n) for n in dict.fromkeys(re.findall(r"/attachments/(\d+)", html))]


def test_customer_sends_a_screenshot_with_or_without_text(alice):
    sub_id = sub_id_from(upload(alice))
    assert reply(alice, sub_id, "Here's the rubric.", [("rubric.png", png())]).status_code == 302
    assert reply(alice, sub_id, "", [("only-a-screenshot.png", png())]).status_code == 302  # no text needed
    page = alice.get(f"/submissions/{sub_id}").get_data(as_text=True)
    ids = attachment_ids(page)
    assert len(ids) == 2 and 'alt="Screenshot: rubric.png"' in page and 'width="40" height="30"' in page
    r = alice.get(f"/attachments/{ids[0]}")
    assert r.status_code == 200 and r.mimetype == "image/png" and r.data.startswith(b"\x89PNG")
    assert "sandbox" in r.headers["Content-Security-Policy"] and r.headers["Cache-Control"] == "private, no-store"


def test_reviewer_sees_it_and_is_told_how_many_were_attached(app, alice, admin):
    sub_id = sub_id_from(upload(alice))
    reply(alice, sub_id, "See attached", [("a.png", png()), ("b.png", png())])
    page = admin.get(f"/admin/submissions/{sub_id}").get_data(as_text=True)
    assert len(attachment_ids(page)) == 2
    assert admin.get(f"/attachments/{attachment_ids(page)[0]}").status_code == 200
    with app.app_context():
        from app.db import query
        assert query("SELECT message FROM notifications ORDER BY id DESC LIMIT 1", one=True)["message"].endswith("(2 attached)")


def test_empty_message_and_too_many_files_are_refused(alice):
    sub_id = sub_id_from(upload(alice))
    r = reply(alice, sub_id, "")
    assert "Write a message or attach a screenshot" in alice.get(r.headers["Location"]).get_data(as_text=True)
    r = reply(alice, sub_id, "lots", [(f"{i}.png", png()) for i in range(6)])
    assert "Attach up to 5 files" in alice.get(r.headers["Location"]).get_data(as_text=True)
    assert attachment_ids(alice.get(f"/submissions/{sub_id}").get_data(as_text=True)) == []


def test_fake_and_unsupported_files_are_refused(alice):
    sub_id = sub_id_from(upload(alice))
    for name, content, message in [
        ("screenshot.png", b"<script>alert(1)</script>", "isn't an image we can open"),
        ("tool.exe", b"MZ\x90\x00", "Attach a screenshot (PNG, JPG or WebP)"),
        ("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "Attach a screenshot"),
    ]:
        r = reply(alice, sub_id, "x", [(name, content)])
        assert message in html.unescape(alice.get(r.headers["Location"]).get_data(as_text=True)), name
    assert attachment_ids(alice.get(f"/submissions/{sub_id}").get_data(as_text=True)) == []


def test_huge_images_are_refused_before_decoding(alice):
    sub_id = sub_id_from(upload(alice))
    big = io.BytesIO()
    Image.new("1", (8000, 6000)).save(big, "PNG")  # 48 megapixels, tiny file
    r = reply(alice, sub_id, "x", [("huge.png", big.getvalue())])
    assert "too large an image (8000×6000)" in alice.get(r.headers["Location"]).get_data(as_text=True)


def test_photos_lose_their_location_and_are_turned_the_right_way_up(alice):
    sub_id = sub_id_from(upload(alice))
    reply(alice, sub_id, "photo of my notes", [("IMG_0042.jpeg", jpeg_with_location_and_rotation())])
    page = alice.get(f"/submissions/{sub_id}").get_data(as_text=True)
    stored = Image.open(io.BytesIO(alice.get(f"/attachments/{attachment_ids(page)[0]}").data))
    assert stored.format == "JPEG" and stored.size == (20, 60)  # rotated upright
    assert not stored.getexif()  # no GPS, no orientation tag, nothing
    assert "IMG_0042.jpg" in page


def test_internal_note_attachments_stay_internal(alice, admin):
    sub_id = sub_id_from(upload(alice))
    admin_message(admin, sub_id, "Similarity report, don't share", [("report.png", png())], internal=True)
    note_id = attachment_ids(admin.get(f"/admin/submissions/{sub_id}").get_data(as_text=True))[0]
    assert alice.get(f"/attachments/{note_id}").status_code == 404
    assert f"/attachments/{note_id}" not in alice.get(f"/submissions/{sub_id}").get_data(as_text=True)
    admin_message(admin, sub_id, "", [("marked-up.pdf", b"%PDF-1.4 reviewed")])
    shared = [i for i in attachment_ids(admin.get(f"/admin/submissions/{sub_id}").get_data(as_text=True)) if i != note_id]
    r = alice.get(f"/attachments/{shared[0]}")
    assert r.status_code == 200 and r.headers["Content-Disposition"].startswith("attachment;")  # documents download


def test_other_customers_cannot_open_an_attachment(alice, bob):
    sub_id = sub_id_from(upload(alice))
    reply(alice, sub_id, "mine", [("mine.png", png())])
    att = attachment_ids(alice.get(f"/submissions/{sub_id}").get_data(as_text=True))[0]
    assert bob.get(f"/attachments/{att}").status_code == 404


def test_reviewed_files_can_include_screenshots(alice, admin):
    sub_id = sub_id_from(upload(alice))
    r = admin.post(f"/admin/submissions/{sub_id}/results", {
        "feedback": "See the highlighted page.", "action": "send",
        "result_files": [(io.BytesIO(png((300, 200))), "page-3-marked.png")]}, content_type="multipart/form-data")
    assert r.status_code == 302
    page = alice.get(f"/submissions/{sub_id}").get_data(as_text=True)
    file_id = re.search(r'href="/files/(\d+)\?inline=1"', page).group(1)  # an "Open" link, not just Download
    r = alice.get(f"/files/{file_id}?inline=1")
    assert r.status_code == 200 and r.mimetype == "image/png" and "sandbox" in r.headers["Content-Security-Policy"]
