"""The blog: the safe-HTML boundary, publishing, scheduling, addresses, images, and search."""
import io
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from app import richtext
from app.db import execute, query
from test_seo import parse

MEDIA = "/blog/media/" + "a" * 32 + ".webp"


# ---------- the sanitizer ----------

@pytest.mark.parametrize("dirty", [
    '<script>alert(1)</script>',
    '<img src=x onerror=alert(1)>',
    '<a href="javascript:alert(1)">x</a>',
    '<a href=" JaVaScRiPt:alert(1)">x</a>',
    '<a href="&#106;avascript:alert(1)">x</a>',
    '<a href="data:text/html;base64,PHNjcmlwdD4=">x</a>',
    '<a href="//evil.example/">x</a>',
    '<p style="background:url(javascript:alert(1))" onclick="alert(1)">x</p>',
    '<iframe src="https://evil.example"></iframe>',
    '<svg onload=alert(1)><script>alert(1)</script></svg>',
    '<math><mi xlink:href="javascript:alert(1)">x</mi></math>',
    '<img src="https://tracker.example/pixel.gif">',
    '<object data="x.swf"></object><embed src="x">',
    '<form action="https://evil.example"><input name=a><button>go</button></form>',
    '<p>ok<style>p{display:none}</style></p>',
    '<<script>script>alert(1)<</script>/script>',
    '<a href="https://ok.example" onmouseover="alert(1)">x</a>',
])
def test_sanitizer_removes_anything_executable(dirty):
    out = richtext.clean(dirty)
    low = out.lower()
    for bad in ("<script", "javascript:", "onerror", "onclick", "onload", "onmouseover", "style=", "<iframe",
                "<svg", "<math", "<object", "<embed", "<form", "<input", "tracker.example", "data:", "//evil"):
        assert bad not in low, (dirty, out)
    # whatever is left only uses the allowed tags and attributes
    for tag, attrs in re.findall(r"<([a-z0-9]+)([^>]*)>", out):
        assert tag in {"p", "h2", "h3", "strong", "em", "s", "code", "a", "br", "blockquote", "ul", "ol", "li",
                       "pre", "hr", "figure", "img", "figcaption"}, out
        for name in re.findall(r'\s([a-z-]+)=', attrs):
            assert name in {"href", "src", "alt", "loading", "id"}, out


def test_sanitizer_keeps_real_formatting():
    html = ('<h1>Big</h1><p>Some <b>bold</b>, <i>italic</i>, <del>gone</del>, <code>x</code> and '
            '<a href="https://example.com/a?b=1&c=2">a link</a> plus <a href="/pricing">ours</a>.</p>'
            '<blockquote><p>Quote</p></blockquote><ul><li>One<ul><li>Nested</li></ul></li></ul><ol><li>First</li></ol>'
            f'<pre>line 1\n  line 2</pre><hr><figure><img src="{MEDIA}" alt=""><figcaption>Cap</figcaption></figure>')
    out = richtext.clean(html)
    assert '<h2 id="big">Big</h2>' in out
    assert "<strong>bold</strong>" in out and "<em>italic</em>" in out and "<s>gone</s>" in out
    assert '<a href="https://example.com/a?b=1&amp;c=2">a link</a>' in out and '<a href="/pricing">ours</a>' in out
    assert "<blockquote><p>Quote</p></blockquote>" in out
    assert "<ul><li>One<ul><li>Nested</li></ul></li></ul>" in out and "<ol><li>First</li></ol>" in out
    assert "<pre><code>line 1\n  line 2</code></pre>" in out and "<hr>" in out
    assert f'<figure><img src="{MEDIA}" alt="Cap" loading="lazy"><figcaption>Cap</figcaption></figure>' in out


def test_sanitizer_tidies_pastes_from_word_and_docs():
    word = ('<p class=MsoNormal style="margin:0"><span lang=EN-GB style="font-size:12pt">Pasted&nbsp;from'
            '&nbsp;Word</span><o:p></o:p></p><!-- [if gte mso 9]>junk<![endif] -->')
    assert richtext.clean(word) == "<p>Pasted from Word</p>"
    chrome = "<div>Line one</div><div>Line <span style='color:red'>two</span></div><div><br></div>"
    assert richtext.clean(chrome) == "<p>Line one</p>\n<p>Line two</p>"
    assert richtext.clean("<p><br></p><p>   </p><b></b>") == ""


def test_headings_get_unique_ids_for_the_table_of_contents():
    out = richtext.clean("<h2>Intro</h2><h2>Intro</h2><h2>What &amp; why?</h2><h3>Detail</h3>")
    assert richtext.headings(out) == [("intro", "Intro"), ("intro-2", "Intro"), ("what-why", "What & why?")]


def test_sanitizing_twice_changes_nothing():
    once = richtext.clean('<h2>A</h2><p>b <a href="/x">c</a></p><ul><li>d</li></ul>'
                          f'<figure><img src="{MEDIA}" alt="e"></figure>')
    assert richtext.clean(once) == once


# ---------- helpers ----------

def _png(size=(1600, 900), color=(200, 30, 40), fmt="PNG", exif=False):
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    kw = {}
    if exif:
        ex = Image.Exif()
        ex[0x010F] = "SecretCam"  # Make
        kw["exif"] = ex
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def _upload(admin, data, name="photo.png"):
    return admin.post("/admin/blog/media", {"image": (io.BytesIO(data), name)}, content_type="multipart/form-data")


def _new(admin):
    resp = admin.post("/admin/blog/new")
    assert resp.status_code == 302
    return int(re.search(r"/admin/blog/(\d+)/edit", resp.headers["Location"]).group(1))


def _save(admin, post_id, action="save", **fields):
    data = {"action": action, "title": "", "subtitle": "", "body": "", "excerpt": "", "slug": "", "cover": "",
            "cover_alt": ""}
    data.update(fields)
    return admin.post(f"/admin/blog/{post_id}", data)


def _post(app, post_id):
    with app.app_context():
        return query("SELECT * FROM posts WHERE id = ?", (post_id,), one=True)


BODY = "<h2>First</h2><p>" + "word " * 500 + "</p><h2>Second</h2><p>More.</p><h2>Third</h2><p>End.</p>"


def _publish(admin, app, title="How to cite a website", **kw):
    pid = _new(admin)
    fields = {"title": title, "body": BODY}
    fields.update(kw)
    resp = _save(admin, pid, "publish", **fields)
    assert resp.status_code == 302
    return pid


# ---------- admin only ----------

def test_only_admins_can_write(client, alice, admin):
    assert client.get("/admin/blog").status_code == 302
    assert alice.get("/admin/blog").status_code == 403
    assert alice.post("/admin/blog/new").status_code == 403
    assert _upload(alice, _png()).status_code == 403
    assert admin.get("/admin/blog").status_code == 200


def test_writes_need_the_csrf_token(app, admin):
    pid = _new(admin)
    resp = admin.c.post(f"/admin/blog/{pid}", data={"action": "save", "title": "x"})
    assert resp.status_code in (400, 403)


# ---------- drafts, publishing, scheduling ----------

def test_drafts_autosave_and_stay_private(app, client, admin):
    pid = _new(admin)
    resp = _save(admin, pid, "autosave", title="My First Post!", body="<p>Hello <script>x</script>there</p>")
    assert resp.status_code == 200 and resp.json["ok"]
    assert resp.json["slug"] == "my-first-post"
    row = _post(app, pid)
    assert row["status"] == "draft" and row["body"] == "<p>Hello there</p>" and row["slug"] == "my-first-post"
    assert client.get("/blog/my-first-post").status_code == 404
    assert "my-first-post" not in client.get("/sitemap.xml").get_data(as_text=True)
    preview = admin.get(f"/admin/blog/{pid}/preview")
    assert preview.status_code == 200 and "This is a draft" in preview.get_data(as_text=True)
    assert preview.headers["X-Robots-Tag"] == "noindex"


def test_publishing_needs_a_title_and_a_body(app, admin):
    pid = _new(admin)
    _save(admin, pid, "publish", title="", body="<p>text</p>")
    assert _post(app, pid)["status"] == "draft"
    _save(admin, pid, "publish", title="Title", body="<p> </p>")
    assert _post(app, pid)["status"] == "draft"


def test_published_post_is_public_with_article_metadata(app, client, admin):
    app.config["ALLOW_INDEXING"] = True
    pid = _publish(admin, app, subtitle="A quick guide", excerpt="Cite websites properly in APA, MLA and Harvard.")
    resp = client.get("/blog/how-to-cite-a-website")
    assert resp.status_code == 200 and "X-Robots-Tag" not in resp.headers
    h = parse(resp)
    assert h.title == "How to cite a website – NerdAccess"
    assert h.meta["description"] == "Cite websites properly in APA, MLA and Harvard."
    assert h.links["canonical"] == ["https://example.test/blog/how-to-cite-a-website"]
    assert h.meta["og:type"] == "article" and "article:published_time" in h.meta
    post = next(n for n in h.jsonld[0]["@graph"] if n["@type"] == "BlogPosting")
    assert post["headline"] == "How to cite a website" and post["author"]["name"] == "Rita Reviewer"
    assert post["wordCount"] == _post(app, pid)["words"] == 505
    html = resp.get_data(as_text=True)
    assert "3 min read" in html and "In this article" in html and 'href="#second"' in html
    assert "A quick guide" in html
    # listed on the blog, in the sitemap and in the feed; "Blog" appears in the nav
    assert "How to cite a website" in client.get("/blog").get_data(as_text=True)
    assert "/blog/how-to-cite-a-website" in client.get("/sitemap.xml").get_data(as_text=True)
    rss = ET.fromstring(client.get("/blog/feed.xml").data)
    assert [i.findtext("title") for i in rss.iter("item")] == ["How to cite a website"]
    assert 'href="/blog"' in client.get("/pricing").get_data(as_text=True)


def test_empty_blog_is_hidden(app, client):
    app.config["ALLOW_INDEXING"] = True
    resp = client.get("/blog")
    assert resp.status_code == 200 and resp.headers["X-Robots-Tag"] == "noindex"
    assert "/blog" not in client.get("/sitemap.xml").get_data(as_text=True)
    assert 'href="/blog"' not in client.get("/pricing").get_data(as_text=True)


def test_future_publish_time_schedules_the_post(app, client, admin):
    later = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pid = _publish(admin, app, publish_at=later)
    row = _post(app, pid)
    assert row["status"] == "published" and row["published_at"] == later
    assert client.get("/blog/how-to-cite-a-website").status_code == 404
    assert "Scheduled" in admin.get("/admin/blog").get_data(as_text=True)
    with app.app_context():
        execute("UPDATE posts SET published_at = '2020-01-01T00:00:00Z' WHERE id = ?", (pid,))
    assert client.get("/blog/how-to-cite-a-website").status_code == 200


def test_unpublish_and_delete(app, client, admin):
    pid = _publish(admin, app)
    _save(admin, pid, "unpublish", title="How to cite a website", body=BODY)
    assert client.get("/blog/how-to-cite-a-website").status_code == 404
    admin.post(f"/admin/blog/{pid}/delete")
    assert _post(app, pid) is None


def test_published_posts_dont_autosave(app, admin):
    pid = _publish(admin, app)
    resp = _save(admin, pid, "autosave", title="Half-typed", body="<p>x</p>")
    assert resp.status_code == 409
    assert _post(app, pid)["title"] == "How to cite a website"


def test_changing_the_address_keeps_old_links_working(app, client, admin):
    pid = _publish(admin, app)
    _save(admin, pid, "update", title="How to cite a website", body=BODY, slug="cite-a-website")
    assert _post(app, pid)["slug"] == "cite-a-website"
    old = client.get("/blog/how-to-cite-a-website")
    assert old.status_code == 301 and old.headers["Location"].endswith("/blog/cite-a-website")
    # a new post can take over the old address; then it's that post, not a redirect
    pid2 = _publish(admin, app, title="How to cite a website")
    assert _post(app, pid2)["slug"] == "how-to-cite-a-website"
    assert client.get("/blog/how-to-cite-a-website").status_code == 200


def test_published_post_cannot_take_another_posts_address(app, admin):
    _publish(admin, app, title="Alpha")
    pid = _publish(admin, app, title="Beta")
    _save(admin, pid, "update", title="Beta", body=BODY, slug="alpha")
    assert _post(app, pid)["slug"] == "beta"


def test_drafts_with_the_same_title_get_different_addresses(app, admin):
    a, b = _new(admin), _new(admin)
    _save(admin, a, "autosave", title="Same", body="<p>x</p>")
    _save(admin, b, "autosave", title="Same", body="<p>y</p>")
    assert {_post(app, a)["slug"], _post(app, b)["slug"]} == {"same", "same-2"}


def test_hand_edited_rows_are_cleaned_on_the_way_out(app, client, admin):
    pid = _publish(admin, app)
    with app.app_context():
        execute("UPDATE posts SET body = ? WHERE id = ?", ('<p onclick="x()">Hi<script>alert(1)</script></p>', pid))
    html = client.get("/blog/how-to-cite-a-website").get_data(as_text=True)
    assert "<script>alert" not in html and "onclick" not in html and "<p>Hi</p>" in html


# ---------- images ----------

def test_image_upload_is_reencoded_resized_and_stripped(app, admin):
    resp = _upload(admin, _png((3000, 1500), fmt="JPEG", exif=True), "photo.jpg")
    assert resp.status_code == 200 and resp.json["ok"]
    name = resp.json["name"]
    assert resp.json["width"] == 2000 and resp.json["height"] == 1000
    for path, size in ((f"/blog/media/{name}.webp", (2000, 1000)), (f"/blog/media/{name}-800.webp", (800, 400)),
                       (f"/blog/media/{name}-og.jpg", (1200, 630))):
        r = admin.get(path)
        assert r.status_code == 200 and "immutable" in r.headers["Cache-Control"]
        img = Image.open(io.BytesIO(r.data))
        assert img.size == size
        assert b"SecretCam" not in r.data


@pytest.mark.parametrize("data,name", [
    (b"%PDF-1.4 not an image", "x.png"),
    (b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'/>", "x.svg"),
    (b"", "empty.png"),
])
def test_non_images_are_refused(admin, data, name):
    resp = _upload(admin, data, name)
    assert resp.status_code == 400 and not resp.json["ok"]


def test_media_route_only_serves_our_image_files(app, client):
    from pathlib import Path
    folder = Path(app.config["UPLOAD_DIR"]) / "blog"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "notes.txt").write_text("private")
    (folder / "x.html").write_text("<script>alert(1)</script>")
    for name in ("notes.txt", "x.html", "..%2F..%2Fapp.py", "notahexname.webp"):
        assert client.get(f"/blog/media/{name}").status_code == 404, name


def test_cover_shows_on_the_post_and_in_link_previews(app, client, admin):
    name = _upload(admin, _png()).json["name"]
    _publish(admin, app, cover=name, cover_alt="A red book")
    resp = client.get("/blog/how-to-cite-a-website")
    h = parse(resp)
    assert h.meta["og:image"] == f"https://example.test/blog/media/{name}-og.jpg"
    assert f'src="/blog/media/{name}.webp"' in resp.get_data(as_text=True) and 'alt="A red book"' in resp.get_data(as_text=True)


def test_cover_must_be_an_uploaded_image(app, admin):
    pid = _publish(admin, app, cover="../../etc/passwd")
    assert _post(app, pid)["cover"] is None


def test_inline_images_must_be_ours(app, client, admin):
    name = _upload(admin, _png()).json["name"]
    body = (f'<p>a</p><figure><img src="/blog/media/{name}.webp"><figcaption>Ours</figcaption></figure>'
            '<figure><img src="https://elsewhere.example/x.png"></figure>')
    _publish(admin, app, body=body)
    html = client.get("/blog/how-to-cite-a-website").get_data(as_text=True)
    assert f'/blog/media/{name}.webp' in html and "elsewhere.example" not in html


# ---------- index ----------

def test_index_pages_and_newest_first(app, client, admin):
    for i in range(14):
        pid = _publish(admin, app, title=f"Post {i}")
        with app.app_context():
            execute("UPDATE posts SET published_at = ? WHERE id = ?", (f"2026-01-{i + 1:02d}T00:00:00Z", pid))
    first = client.get("/blog").get_data(as_text=True)
    assert first.index("Post 13") < first.index("Post 12") and "Post 0" not in first
    assert "Older posts" in first
    second = client.get("/blog?page=2")
    assert "Post 0" in second.get_data(as_text=True)
    assert parse(second).links["canonical"] == ["https://example.test/blog?page=2"]
    assert client.get("/blog?page=9").status_code == 404


def test_home_page_shows_latest_posts_only_when_there_are_some(app, client, admin):
    assert "From the blog" not in client.get("/").get_data(as_text=True)
    _publish(admin, app)
    home = client.get("/").get_data(as_text=True)
    assert "From the blog" in home and 'href="/blog/how-to-cite-a-website"' in home
