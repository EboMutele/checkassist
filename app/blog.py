"""The blog: admins write and publish posts; everyone can read them.

Public:  /blog, /blog/<slug>, /blog/feed.xml, /blog/media/<file>
Admin:   /admin/blog (list), editor, preview, image uploads, publish/schedule/unpublish/delete

A post is live when it's published and its publish time has passed, so publishing with a future time
schedules it. Drafts and scheduled posts 404 publicly; admins see them through the preview.
"""
import io
import math
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)
from markupsafe import Markup
from PIL import Image, ImageOps, UnidentifiedImageError

from . import richtext, seo
from .auth import admin_required
from .db import get_db, query, utcnow

bp = Blueprint("blog", __name__, url_prefix="/blog")
admin_bp = Blueprint("blog_admin", __name__, url_prefix="/admin/blog")

PER_PAGE = 12
WORDS_PER_MINUTE = 230
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MEDIA_FILE = re.compile(r"^[0-9a-f]{32}(?:-800|-og)?\.(?:webp|jpg)$")
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_PIXELS = 50_000_000
LIMITS = {"title": 200, "subtitle": 300, "excerpt": 300, "cover_alt": 300, "slug": 80}


# ---------- helpers ----------

def media_dir():
    path = Path(current_app.config["UPLOAD_DIR"]) / "blog"
    path.mkdir(parents=True, exist_ok=True)
    return path


def media_url(name, variant=""):
    ext = "jpg" if variant == "og" else "webp"
    return f"/blog/media/{name}{'-' + variant if variant else ''}.{ext}"


def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:70].strip("-")


def unique_slug(base, post_id):
    base = base or f"post-{post_id}"
    slug, n = base, 1
    while query("SELECT 1 FROM posts WHERE slug = ? AND id != ?", (slug, post_id), one=True):
        n += 1
        slug = f"{base}-{n}"
    return slug


def read_minutes(words):
    return max(1, math.ceil((words or 0) / WORDS_PER_MINUTE))


def state(post, now=None):
    if post["status"] != "published":
        return "draft"
    return "scheduled" if (post["published_at"] or "") > (now or utcnow()) else "published"


def excerpt_for(post):
    if post["excerpt"]:
        return post["excerpt"]
    text = " ".join(richtext.plain_text(post["body"]).split("\n"))
    if len(text) <= 158:
        return text
    return text[:157].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


def _date_label(ts):
    if not ts:
        return ""
    dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    return f"{dt:%b} {dt.day}, {dt:%Y}"


def initials(name):
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    return ((parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")) if parts else "?").upper()


def view(post):
    """A post row plus what templates need."""
    d = dict(post)
    d["url"] = url_for("blog.post", slug=post["slug"])
    d["minutes"] = read_minutes(post["words"])
    d["date"] = _date_label(post["published_at"] or post["updated_at"])
    d["state"] = state(post)
    d["description"] = excerpt_for(post)
    d["author_name"] = post["author_name"] if "author_name" in post.keys() and post["author_name"] else f"{current_app.config['APP_NAME']} team"
    d["author_initials"] = initials(d["author_name"])
    if post["cover"]:
        d["cover_url"] = media_url(post["cover"])
        d["cover_small"] = media_url(post["cover"], "800")
        d["cover_og"] = media_url(post["cover"], "og")
    return d


POST_SQL = "SELECT posts.*, users.name AS author_name FROM posts LEFT JOIN users ON users.id = posts.author_id"


def live_posts(limit, offset=0, exclude=None):
    rows = query(f"{POST_SQL} WHERE status = 'published' AND published_at <= ? AND posts.id != ?"
                 " ORDER BY published_at DESC, posts.id DESC LIMIT ? OFFSET ?",
                 (utcnow(), exclude or 0, limit, offset))
    return [view(r) for r in rows]


def live_count():
    return query("SELECT COUNT(*) AS n FROM posts WHERE status = 'published' AND published_at <= ?",
                 (utcnow(),), one=True)["n"]


def sitemap_entries():
    rows = query("SELECT slug, updated_at FROM posts WHERE status = 'published' AND published_at <= ?"
                 " ORDER BY published_at DESC", (utcnow(),))
    if not rows:
        return []
    return [("/blog", rows[0]["updated_at"][:10])] + [(f"/blog/{r['slug']}", r["updated_at"][:10]) for r in rows]


def _structured(post, url):
    base = seo.base_url()
    data = {
        "@type": "BlogPosting", "headline": post["title"][:110], "description": post["description"],
        "url": url, "mainEntityOfPage": url,
        "datePublished": post["published_at"], "dateModified": post["updated_at"],
        "author": {"@type": "Person", "name": post["author_name"]},
        "publisher": {"@id": base + "/#org"},
        "wordCount": post["words"],
    }
    if post.get("cover_url"):
        data["image"] = [base + post["cover_og"], base + post["cover_url"]]
    return {"@context": "https://schema.org", "@graph": [seo._org(), data]}


# ---------- images ----------

class ImageError(ValueError):
    pass


def _fit(img, width):
    if img.width <= width:
        return img.copy()
    return img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)


def save_image(file_storage, user_id):
    """Re-encode an upload (drops metadata such as GPS), in three sizes. Returns (name, width, height)."""
    data = file_storage.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError(f"Images can be up to {MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt, (w, h) = probe.format, probe.size
            if fmt not in ("JPEG", "PNG", "WEBP", "GIF"):
                raise ImageError("Use a JPG, PNG, WebP or GIF image.")
            if w * h > MAX_PIXELS:
                raise ImageError("That image is too large. Resize it to under 50 megapixels.")
            probe.verify()
        img = Image.open(io.BytesIO(data))
        img.load()
    except ImageError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise ImageError("That file isn't an image we can read. Use a JPG, PNG, WebP or GIF.")
    img = ImageOps.exif_transpose(img)
    alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    img = img.convert("RGBA" if alpha else "RGB")
    name = secrets.token_hex(16)
    folder = media_dir()
    full = _fit(img, 2000)
    full.save(folder / f"{name}.webp", "WEBP", quality=82, method=4)
    _fit(img, 800).save(folder / f"{name}-800.webp", "WEBP", quality=80, method=4)
    flat = img.convert("RGB") if not alpha else Image.alpha_composite(
        Image.new("RGBA", img.size, (255, 255, 255, 255)), img).convert("RGB")
    ImageOps.fit(flat, (1200, 630), Image.LANCZOS).save(folder / f"{name}-og.jpg", "JPEG", quality=85,
                                                          optimize=True, progressive=True)
    db = get_db()
    db.execute("INSERT INTO blog_media (name, width, height, uploaded_by, created_at) VALUES (?, ?, ?, ?, ?)",
               (name, full.width, full.height, user_id, utcnow()))
    db.commit()
    return name, full.width, full.height


# ---------- public ----------

@bp.route("")
def index():
    page = max(1, request.args.get("page", 1, type=int))
    total = live_count()
    pages = max(1, math.ceil(total / PER_PAGE))
    if page > pages:
        abort(404)
    posts = live_posts(PER_PAGE, (page - 1) * PER_PAGE)
    path = "/blog" if page == 1 else f"/blog?page={page}"
    seo.describe(
        "Blog" if page == 1 else f"Blog, page {page}",
        f"Practical writing advice from the reviewers at {current_app.config['APP_NAME']}: essays, citations, dissertations, "
        "applications and getting your work ready to hand in.",
        path, indexable=total > 0, feed=True,
    )
    return render_template("blog/index.html", posts=posts, page=page, pages=pages, total=total)


@bp.route("/<slug>")
def post(slug):
    row = query(f"{POST_SQL} WHERE slug = ?", (slug,), one=True)
    if row is None:
        moved = query("SELECT posts.slug FROM post_slugs JOIN posts ON posts.id = post_slugs.post_id"
                      " WHERE post_slugs.slug = ?", (slug,), one=True)
        if moved:
            return redirect(url_for("blog.post", slug=moved["slug"]), code=301)
        abort(404)
    p = view(row)
    if p["state"] != "published":
        abort(404)
    return _render_post(p)


def _render_post(p, preview=False):
    url = seo.base_url() + p["url"]
    seo.describe(p["title"], p["description"], p["url"], image=seo.base_url() + p["cover_og"] if p.get("cover_og") else None,
                 og_type="article", structured=_structured(p, url), indexable=not preview,
                 published=p["published_at"], modified=p["updated_at"], feed=True)
    p["body_html"] = Markup(richtext.clean(p["body"]))  # cleaned again on the way out, in case a row was edited by hand
    toc = richtext.headings(p["body_html"])
    more = live_posts(3, exclude=p["id"])
    cover = query("SELECT width, height FROM blog_media WHERE name = ?", (p["cover"],), one=True) if p["cover"] else None
    return render_template("blog/post.html", post=p, toc=toc if len(toc) >= 3 else [], more=more, preview=preview,
                           cover=cover, share_url=url)


@bp.route("/feed.xml")
def feed():
    posts = live_posts(20)
    base = seo.base_url()
    items = []
    for p in posts:
        link = base + p["url"]
        pub = datetime.strptime(p["published_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        items.append(
            f"<item><title>{escape(p['title'])}</title><link>{escape(link)}</link>"
            f"<guid isPermaLink=\"true\">{escape(link)}</guid><pubDate>{pub:%a, %d %b %Y %H:%M:%S} +0000</pubDate>"
            f"<description>{escape(p['description'])}</description></item>")
    app_name = current_app.config["APP_NAME"]
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
            f"<title>{escape(app_name)} blog</title><link>{escape(base)}/blog</link>"
            f"<description>Writing advice from the reviewers at {escape(app_name)}</description>"
            f"<language>en</language>{''.join(items)}</channel></rss>\n")
    return current_app.response_class(body, mimetype="application/rss+xml")


@bp.route("/media/<name>")
def media(name):
    if not MEDIA_FILE.match(name):
        abort(404)
    resp = send_from_directory(media_dir(), name, max_age=31536000)
    resp.cache_control.immutable = True
    resp.cache_control.public = True
    return resp


# ---------- admin ----------

@admin_bp.route("")
@admin_required
def list_posts():
    rows = [view(r) for r in query(f"{POST_SQL} ORDER BY COALESCE(published_at, updated_at) DESC, posts.id DESC")]
    show = request.args.get("show", "all")
    counts = {s: sum(1 for r in rows if r["state"] == s) for s in ("draft", "scheduled", "published")}
    if show in counts:
        rows = [r for r in rows if r["state"] == show]
    return render_template("admin/blog_list.html", posts=rows, counts=counts, show=show)


@admin_bp.route("/new", methods=("POST",))
@admin_required
def new_post():
    now = utcnow()
    db = get_db()
    cur = db.execute("INSERT INTO posts (slug, author_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                     (f"draft-{secrets.token_hex(4)}", g.user["id"], now, now))
    db.commit()
    return redirect(url_for("blog_admin.edit", post_id=cur.lastrowid))


def _load(post_id):
    row = query(f"{POST_SQL} WHERE posts.id = ?", (post_id,), one=True)
    if row is None:
        abort(404)
    return row


@admin_bp.route("/<int:post_id>/edit")
@admin_required
def edit(post_id):
    p = view(_load(post_id))
    authors = query("SELECT id, name FROM users WHERE role = 'admin' ORDER BY name")
    return render_template("admin/blog_edit.html", post=p, authors=authors, limits=LIMITS)


def _parse_when(value):
    """The editor sends the chosen publish time as UTC ISO (it converts from the admin's own time zone)."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fields(row):
    f = request.form
    out = {}
    for key, limit in LIMITS.items():
        if key in f:
            out[key] = re.sub(r"\s+", " ", f.get(key, "")).strip()[:limit]
    if "body" in f:  # absent if the editor's script didn't run: keep what's saved
        out["body"] = richtext.clean(f["body"])
    if "cover" in f:
        name = f.get("cover", "").strip()
        out["cover"] = name if name and query("SELECT 1 FROM blog_media WHERE name = ?", (name,), one=True) else None
    if "author_id" in f and f.get("author_id", "").isdigit():
        author = query("SELECT id FROM users WHERE id = ? AND role = 'admin'", (int(f["author_id"]),), one=True)
        if author:
            out["author_id"] = author["id"]
    if "slug" in out:
        out["slug"] = slugify(out["slug"])
    return out


@admin_bp.route("/<int:post_id>", methods=("POST",))
@admin_required
def save(post_id):
    row = _load(post_id)
    action = request.form.get("action", "save")
    fields = _fields(row)
    merged = {**dict(row), **fields}
    errors = []
    now = utcnow()

    if action == "autosave" and row["status"] != "draft":
        return jsonify(ok=False, error="Published posts save when you press Update."), 409

    publishing = action in ("publish", "update") or (action == "save" and row["status"] == "published")
    if publishing or action == "publish":
        if not merged["title"]:
            errors.append("Add a title before publishing.")
        if not richtext.plain_text(merged["body"]).strip():
            errors.append("Write something before publishing.")

    # The address: from the title until the post is first published, then fixed unless edited.
    wanted = merged.get("slug") or ""
    if row["status"] == "draft" and (not wanted or wanted.startswith("draft-")) and merged["title"]:
        wanted = slugify(merged["title"])
    if wanted and wanted != row["slug"]:
        if not SLUG.match(wanted):
            errors.append("The address can only use letters, numbers and hyphens.")
        elif row["status"] == "published" and query("SELECT 1 FROM posts WHERE slug = ? AND id != ?",
                                                     (wanted, post_id), one=True):
            errors.append(f"Another post already uses /blog/{wanted}.")
    if errors:
        if action == "autosave":
            return jsonify(ok=False, error=errors[0]), 400
        for e in errors:
            flash(e, "error")
        return redirect(url_for("blog_admin.edit", post_id=post_id))

    slug = unique_slug(wanted, post_id) if wanted and wanted != row["slug"] else row["slug"]
    status, published_at = row["status"], row["published_at"]
    if action == "publish":
        status, published_at = "published", _parse_when(request.form.get("publish_at")) or now
    elif action == "unpublish":
        status = "draft"
    elif action == "update" and row["status"] == "published" and "publish_at" in request.form:
        published_at = _parse_when(request.form.get("publish_at")) or published_at

    words = len(richtext.plain_text(merged["body"]).split())
    db = get_db()
    if slug != row["slug"] and row["status"] == "published":
        db.execute("INSERT OR REPLACE INTO post_slugs (slug, post_id) VALUES (?, ?)", (row["slug"], post_id))
    db.execute("DELETE FROM post_slugs WHERE slug = ?", (slug,))  # a live post wins over an old address
    db.execute(
        "UPDATE posts SET slug = ?, title = ?, subtitle = ?, body = ?, excerpt = ?, cover = ?, cover_alt = ?,"
        " author_id = ?, status = ?, published_at = ?, words = ?, updated_at = ? WHERE id = ?",
        (slug, merged["title"], merged["subtitle"], merged["body"], merged["excerpt"], merged["cover"],
         merged["cover_alt"], merged["author_id"], status, published_at, words, now, post_id))
    db.commit()

    if action == "autosave":
        return jsonify(ok=True, saved_at=now, slug=slug, words=words, minutes=read_minutes(words))
    after = view(_load(post_id))
    messages = {
        "publish": "Scheduled for " + after["date"] + "." if after["state"] == "scheduled" else "Published.",
        "update": "Updated.", "unpublish": "Moved back to drafts. It's no longer on the blog.", "save": "Saved.",
    }
    flash(messages.get(action, "Saved."), "success")
    return redirect(url_for("blog_admin.edit", post_id=post_id))


@admin_bp.route("/<int:post_id>/delete", methods=("POST",))
@admin_required
def delete(post_id):
    row = _load(post_id)
    db = get_db()
    db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    db.commit()
    flash(f"Deleted “{row['title'] or 'Untitled'}”.", "success")
    return redirect(url_for("blog_admin.list_posts"))


@admin_bp.route("/<int:post_id>/preview")
@admin_required
def preview(post_id):
    return _render_post(view(_load(post_id)), preview=True)


@admin_bp.route("/media", methods=("POST",))
@admin_required
def upload_media():
    file = request.files.get("image")
    if not file or not file.filename:
        return jsonify(ok=False, error="Choose an image."), 400
    try:
        name, width, height = save_image(file, g.user["id"])
    except ImageError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, name=name, url=media_url(name), small=media_url(name, "800"), width=width, height=height)


def init_app(app):
    app.register_blueprint(bp)
    app.register_blueprint(admin_bp)

    @app.context_processor
    def blog_links():
        # Show "Blog" in the header and footer only once there's something to read.
        try:
            has_posts = bool(query("SELECT 1 FROM posts WHERE status = 'published' AND published_at <= ? LIMIT 1",
                                   (utcnow(),), one=True))
        except Exception:  # no request/app context with a database (e.g. some CLI renders)
            has_posts = False
        return {"blog_live": has_posts}
