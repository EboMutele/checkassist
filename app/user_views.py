"""Customer-facing pages: landing, dashboard, uploads, results, notifications, file downloads."""
import os
import re

from flask import (
    Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, send_file, url_for,
)

from .auth import home_for, login_required
from .db import execute, get_db, query, utcnow
from .helpers import FIRST_SENT_SQL, OPEN_STATUSES, count_words, load_timings, status_events_of, timing
from . import alerts, attachments, billing, pricing, local
from .notify import notify_admins
from .security import INLINE_PREVIEW_TYPES, validate_upload
from .storage import get_storage

bp = Blueprint("portal", __name__)

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def title_from_filename(name: str) -> str:
    """'History_essay-final v2.docx' -> 'History essay final v2': the title when none is given."""
    stem = clean_filename(name or "").rsplit(".", 1)[0]
    return re.sub(r"\s+", " ", re.sub(r"[_\-]+", " ", stem)).strip()[:200] or "Untitled document"


def clean_filename(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/"))
    name = _CTRL_RE.sub("", name).strip() or "document"
    if len(name) > 150:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: 140] + dot + ext) if dot else name[:150]
    return name


def store_file(upload, ext, submission_id, kind, uploader_id, visible=True):
    """Save an upload to storage and record it. Caller commits."""
    storage = get_storage()
    stored_name, size, sha = storage.save(upload)
    try:
        get_db().execute(
            "INSERT INTO files (submission_id, kind, original_name, stored_name, ext, size, sha256,"
            " visible_to_user, uploaded_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (submission_id, kind, clean_filename(upload.filename), stored_name, ext, size, sha,
             1 if visible else 0, uploader_id, utcnow()),
        )
    except Exception:
        storage.delete(stored_name)
        raise
    return stored_name


def add_event(submission_id, actor_id, type_, body="", internal=False):
    return get_db().execute(
        "INSERT INTO events (submission_id, actor_id, type, body, is_internal, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (submission_id, actor_id, type_, body, 1 if internal else 0, utcnow()),
    ).lastrowid


def own_submission_or_404(sub_id):
    sub = query("SELECT * FROM submissions WHERE id = ? AND user_id = ?", (sub_id, g.user["id"]), one=True)
    if sub is None:
        abort(404)
    return sub


@bp.route("/")
def index():
    if g.user:
        return redirect(home_for(g.user))
    from .blog import live_posts
    return render_template("index.html", plans=pricing.plans_for_display(local.visitor_currency()),
                           latest_posts=live_posts(3), **_pricing_context())


def _pricing_context():
    """Prices in the visitor's currency: USD, or KES once they've opened the private /local link."""
    currency = local.visitor_currency()
    return {
        "words_per_check": pricing.WORDS_PER_CHECK,
        "single_price": pricing.single_price(currency),
        "lowest_per_check": pricing.lowest_per_check(currency),
        "pack_months": pricing.PACK_VALID_MONTHS,
    }


@bp.route("/pricing")
def pricing_page():
    if local.visitor_currency() != pricing.CURRENCY_CODE:
        # Local prices live on the local side only: the private page, or the account's Checks page.
        if g.user is None and "local.page" in current_app.view_functions:
            return redirect(url_for("local.page"))
        return redirect(url_for("billing.page"))
    example_words = 15000
    return render_template(
        "pricing.html", plans=pricing.plans_for_display(local.visitor_currency()), example_words=example_words,
        example_checks=pricing.checks_for_words(example_words), **_pricing_context(),
    )


@bp.route("/dashboard")
@login_required
def dashboard():
    if g.user["role"] == "admin":
        return redirect(url_for("admin.queue"))
    subs = query(
        "SELECT s.*, (SELECT original_name FROM files f WHERE f.submission_id = s.id AND f.kind = 'original'"
        f" ORDER BY f.id LIMIT 1) AS file_name, {FIRST_SENT_SQL} AS first_sent_at"
        " FROM submissions s WHERE s.user_id = ? ORDER BY s.updated_at DESC",
        (g.user["id"],),
    )
    open_count = sum(1 for s in subs if s["status"] in OPEN_STATUSES)
    waiting_on_user = [s for s in subs if s["status"] == "needs_info"]
    timings = load_timings(subs, current_app.config["REVIEW_MINUTES"])
    return render_template(
        "portal/dashboard.html", subs=subs, open_count=open_count, waiting_on_user=waiting_on_user, timings=timings
    )


def _upload_context():
    return {"words_per_check": pricing.WORDS_PER_CHECK,
            "plans": pricing.plans_for_display(local.currency_for(g.user))}


@bp.route("/submit", methods=("GET", "POST"))
@login_required
def new_submission():
    if g.user["role"] == "admin":
        flash("Admins review documents; sign in with a customer account to submit one.", "info")
        return redirect(url_for("admin.queue"))
    form = {"title": "", "instructions": ""}
    if request.method == "POST":
        form["title"] = request.form.get("title", "").strip()[:200]
        form["instructions"] = request.form.get("instructions", "").strip()[:5000]
        upload = request.files.get("document")
        ext, error = validate_upload(upload)
        if error:
            flash(error, "error")
            return render_template("portal/new.html", form=form, **_upload_context()), 400

        title = form["title"] or title_from_filename(upload.filename)
        storage = get_storage()
        stored_name, size, sha = storage.save(upload)
        words = count_words(storage.path(stored_name), ext)
        needed = pricing.checks_for_words(words or 0) if current_app.config["CHECKS_REQUIRED"] else 0
        conn = get_db()
        billing._immediate(conn)  # balance check and spend happen atomically
        try:
            now = utcnow()
            cur = conn.execute(
                "INSERT INTO submissions (user_id, title, instructions, words, checks_spent, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (g.user["id"], title, form["instructions"], words, needed, now, now),
            )
            sub_id = cur.lastrowid
            conn.execute(
                "INSERT INTO files (submission_id, kind, original_name, stored_name, ext, size, sha256,"
                " visible_to_user, uploaded_by, created_at) VALUES (?, 'original', ?, ?, ?, ?, ?, 1, ?, ?)",
                (sub_id, clean_filename(upload.filename), stored_name, ext, size, sha, g.user["id"], now),
            )
            add_event(sub_id, g.user["id"], "created")
            if needed:
                billing.take(conn, g.user["id"], needed, f"Review of CA-{sub_id:05d}", submission_id=sub_id)
            conn.commit()
        except billing.NotEnoughChecks as e:
            conn.rollback()
            storage.delete(stored_name)
            words_note = f"This document is {words:,} words, so it needs {e.needed} checks. " if words and e.needed > 1 else ""
            flash(f"{words_note}You have {e.available} {'check' if e.available == 1 else 'checks'}. "
                  "Buy more to upload it.", "error")
            return render_template("portal/new.html", form=form, **_upload_context()), 402
        except Exception:
            conn.rollback()
            storage.delete(stored_name)
            raise
        notify_admins(f"New document from {g.user['name']}: {title}", sub_id, content={
            "kicker": "New document",
            "heading": f"{g.user['name']} sent a document",
            "intro": [f"The review clock has started: aim to send feedback within "
                      f"{current_app.config.get('REVIEW_MINUTES') or 20} minutes."],
            "document": title,
            "details": [(label, value) for label, value in (
                ("Words", f"{words:,}" if words else ""),
                ("Checks used", str(needed) if needed else ""),
                ("Instructions", (form["instructions"] or "")[:200]),
            ) if value],
            "button": "Open in admin",
        })
        alerts.new_order(sub_id, g.user, title, words, needed, form["instructions"])
        used = f" It used {needed} {'check' if needed == 1 else 'checks'}." if needed else ""
        flash(f"Uploaded.{used} We'll let you know as soon as your feedback is ready.", "success")
        return redirect(url_for("portal.submission", sub_id=sub_id))
    return render_template("portal/new.html", form=form, **_upload_context())


@bp.route("/submissions/<int:sub_id>")
@login_required
def submission(sub_id):
    if g.user["role"] == "admin":
        return redirect(url_for("admin.submission", sub_id=sub_id))
    sub = own_submission_or_404(sub_id)
    originals = query("SELECT * FROM files WHERE submission_id = ? AND kind = 'original' ORDER BY id", (sub_id,))
    results = query(
        "SELECT * FROM files WHERE submission_id = ? AND kind = 'result' AND visible_to_user = 1 ORDER BY id",
        (sub_id,),
    )
    events = query(
        "SELECT e.*, u.name AS actor_name, u.role AS actor_role FROM events e LEFT JOIN users u ON u.id = e.actor_id"
        " WHERE e.submission_id = ? AND e.is_internal = 0 ORDER BY e.created_at, e.id",
        (sub_id,),
    )
    reviewer = None
    if sub["assigned_to"]:
        row = query("SELECT name FROM users WHERE id = ?", (sub["assigned_to"],), one=True)
        if row and row["name"].strip():
            reviewer = row["name"].split()[0]  # first name only
    execute(
        "UPDATE notifications SET is_read = 1 WHERE user_id = ? AND submission_id = ? AND is_read = 0",
        (g.user["id"], sub_id),
    )
    first_sent = next((e["created_at"] for e in events if e["type"] == "results_sent"), None)
    t = timing(sub["created_at"], sub["status"], first_sent, current_app.config["REVIEW_MINUTES"],
               status_events=status_events_of(events))
    return render_template(
        "portal/detail.html", sub=sub, originals=originals, results=results, events=events, reviewer=reviewer, t=t,
        attached=attachments.by_event(sub_id), attach_accept=attachments.ACCEPT,
    )


@bp.route("/attachments/<int:attachment_id>")
@login_required
def attachment(attachment_id):
    return attachments.serve(attachment_id)


@bp.route("/submissions/<int:sub_id>/status.json")
@login_required
def submission_status(sub_id):
    """Polled by the open document page so it can update the moment feedback is sent."""
    sub = own_submission_or_404(sub_id)
    resp = jsonify(status=sub["status"], feedback_sent_at=sub["feedback_sent_at"], updated_at=sub["updated_at"])
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@bp.route("/submissions/<int:sub_id>/reply", methods=("POST",))
@login_required
def reply(sub_id):
    sub = own_submission_or_404(sub_id)
    body = request.form.get("body", "").strip()[:5000]
    prepared, error = attachments.check(request.files.getlist("attachments"))
    if not error and not body and not prepared:
        error = "Write a message or attach a screenshot before sending."
    if error:
        flash(error, "error")
        return redirect(url_for("portal.submission", sub_id=sub_id) + "#conversation")
    conn = get_db()
    event_id = add_event(sub_id, g.user["id"], "message", body)
    attachments.save(prepared, sub_id, event_id, g.user["id"])
    new_status = sub["status"]
    if sub["status"] == "needs_info":
        new_status = "in_review" if sub["assigned_to"] else "submitted"
        add_event(sub_id, g.user["id"], "status", new_status)
    conn.execute("UPDATE submissions SET status = ?, updated_at = ? WHERE id = ?", (new_status, utcnow(), sub_id))
    conn.commit()
    extra = f" ({len(prepared)} attached)" if prepared else ""
    notify_admins(f"{g.user['name']} replied on {sub['title']}{extra}", sub_id, assigned_to=sub["assigned_to"], content={
        "kicker": "Reply",
        "heading": f"{g.user['name']} replied",
        "intro": ["Their review can carry on now." if sub["status"] == "needs_info" else "New message on their document."],
        "document": sub["title"],
        "details": [("Attachments", str(len(prepared)))] if prepared else [],
        "quote": body or "(Sent files only.)",
        "quote_by": g.user["name"],
        "button": "Open in admin",
        "preheader": (body or "They sent you a file.")[:120],
    })
    flash("Message sent to your reviewer.", "success")
    return redirect(url_for("portal.submission", sub_id=sub_id) + "#conversation")


@bp.route("/files/<int:file_id>")
@login_required
def download(file_id):
    f = query(
        "SELECT files.*, submissions.user_id AS owner_id FROM files"
        " JOIN submissions ON submissions.id = files.submission_id WHERE files.id = ?",
        (file_id,),
        one=True,
    )
    if f is None:
        abort(404)
    if g.user["role"] != "admin":
        if f["owner_id"] != g.user["id"] or (f["kind"] == "result" and not f["visible_to_user"]):
            abort(404)
    path = get_storage().path(f["stored_name"])
    if not path.exists():
        abort(404)
    previewable = {**INLINE_PREVIEW_TYPES, **attachments.IMAGE_MIME}
    inline = request.args.get("inline") == "1" and f["ext"] in previewable
    resp = send_file(
        path,
        mimetype=previewable[f["ext"]] if inline else "application/octet-stream",
        as_attachment=not inline,
        download_name=f["original_name"],
        max_age=0,
    )
    resp.headers["Cache-Control"] = "private, no-store"
    if inline:
        # Plain text renders sandboxed; PDFs need the browser's viewer, so only block framing.
        resp.headers["Content-Security-Policy"] = (
            "sandbox; default-src 'none'" if f["ext"] == "txt"
            else "sandbox; default-src 'none'; img-src 'self'" if f["ext"] in attachments.IMAGE_MIME
            else "frame-ancestors 'none'"
        )
    return resp


@bp.route("/notifications")
@login_required
def notifications():
    items = query(
        "SELECT n.*, s.title AS sub_title FROM notifications n LEFT JOIN submissions s ON s.id = n.submission_id"
        " WHERE n.user_id = ? ORDER BY n.created_at DESC, n.id DESC LIMIT 100",
        (g.user["id"],),
    )
    return render_template("notifications.html", items=items)


@bp.route("/notifications/read-all", methods=("POST",))
@login_required
def notifications_read_all():
    execute("UPDATE notifications SET is_read = 1 WHERE user_id = ? AND is_read = 0", (g.user["id"],))
    return redirect(url_for("portal.notifications"))
