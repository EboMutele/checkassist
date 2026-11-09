"""Admin pages: review queue, submission workspace, sending results, users."""
import re

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from .auth import admin_required
from .db import execute, get_db, query, utcnow
from .helpers import (
    FIRST_SENT_SQL, OPEN_STATUSES, STATUSES, extract_preview, load_timings, ref, status_events_of, timing, word_count,
)
from .notify import notify
from . import attachments, billing, pricing, local
from .sms import feedback_ready_text, mask_phone, send_sms
from .storage import get_storage
from .user_views import add_event, store_file

bp = Blueprint("admin", __name__, url_prefix="/admin")

PER_PAGE = 25
TABS = [
    ("open", "Open"),
    ("submitted", "New"),
    ("in_review", "In review"),
    ("needs_info", "Waiting on user"),
    ("mine", "Mine"),
    ("completed", "Completed"),
    ("all", "All"),
]


def _page():
    try:
        return max(int(request.args.get("page", 1)), 1)
    except ValueError:
        return 1


def _search_clause(q, columns):
    """LIKE search across columns, plus exact match on a CA-00012 style reference."""
    like = f"%{q}%"
    parts = [f"{c} LIKE ?" for c in columns]
    args = [like] * len(columns)
    m = re.fullmatch(r"(?i)(?:ca-?)?0*(\d{1,9})", q)
    if m:
        parts.append("s.id = ?")
        args.append(int(m.group(1)))
    return "(" + " OR ".join(parts) + ")", args


def get_submission_or_404(sub_id):
    sub = query(
        "SELECT s.*, u.name AS user_name, u.email AS user_email, a.name AS assignee_name"
        " FROM submissions s JOIN users u ON u.id = s.user_id LEFT JOIN users a ON a.id = s.assigned_to"
        " WHERE s.id = ?",
        (sub_id,),
        one=True,
    )
    if sub is None:
        abort(404)
    return sub


def _back(sub_id, anchor=""):
    return redirect(url_for("admin.submission", sub_id=sub_id) + anchor)


def _turnaround_stats():
    """Last 7 days of first sends measured against the promise, plus what's overdue right now."""
    # Computed in Python (not SQL) so time spent waiting on customers is excluded, same as the timers.
    minutes = current_app.config["REVIEW_MINUTES"]
    sent_rows = query(
        f"SELECT * FROM (SELECT s.id, s.created_at, s.status, {FIRST_SENT_SQL} AS first_sent_at FROM submissions s)"
        " WHERE first_sent_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-7 days')"
    )
    took = [t["took"] for t in load_timings(sent_rows, minutes).values() if t["state"] == "done"]
    open_rows = query(
        f"SELECT * FROM (SELECT s.id, s.created_at, s.status, {FIRST_SENT_SQL} AS first_sent_at FROM submissions s"
        " WHERE s.status IN ('submitted', 'in_review')) WHERE first_sent_at IS NULL"
    )
    overdue = sum(1 for t in load_timings(open_rows, minutes).values() if t["state"] == "overdue")
    return {
        "minutes": minutes,
        "sent_7d": len(took),
        "avg_min_7d": round(sum(took) / len(took)) if took else None,
        "on_time_pct": round(100 * sum(1 for m in took if m <= minutes) / len(took)) if took and minutes else None,
        "overdue": overdue,
    }


@bp.route("/")
@admin_required
def queue():
    tab = request.args.get("tab", "open")
    if tab not in dict(TABS):
        tab = "open"
    q = request.args.get("q", "").strip()[:100]
    page = _page()

    where, args = [], []
    if tab == "open":
        where.append(f"s.status IN ({','.join('?' * len(OPEN_STATUSES))})")
        args.extend(OPEN_STATUSES)
    elif tab == "mine":
        where.append("s.assigned_to = ? AND s.status != 'completed'")
        args.append(g.user["id"])
    elif tab in STATUSES:
        where.append("s.status = ?")
        args.append(tab)
    if q:
        clause, extra = _search_clause(q, ["s.title", "u.email", "u.name"])
        where.append(clause)
        args.extend(extra)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    # Open work is first-in, first-out; finished work shows newest first.
    order = "s.created_at ASC" if tab in ("open", "submitted", "in_review", "needs_info", "mine") else "s.updated_at DESC"
    rows = query(
        f"SELECT s.*, u.name AS user_name, u.email AS user_email, a.name AS assignee_name, {FIRST_SENT_SQL} AS first_sent_at"
        " FROM submissions s JOIN users u ON u.id = s.user_id LEFT JOIN users a ON a.id = s.assigned_to"
        f" {where_sql} ORDER BY {order} LIMIT ? OFFSET ?",
        (*args, PER_PAGE + 1, (page - 1) * PER_PAGE),
    )
    has_next = len(rows) > PER_PAGE
    rows = rows[:PER_PAGE]

    counts = {r["status"]: r["n"] for r in query("SELECT status, COUNT(*) AS n FROM submissions GROUP BY status")}
    counts["open"] = sum(counts.get(s, 0) for s in OPEN_STATUSES)
    counts["all"] = sum(counts.get(s, 0) for s in STATUSES)
    counts["mine"] = query(
        "SELECT COUNT(*) AS n FROM submissions WHERE assigned_to = ? AND status != 'completed'",
        (g.user["id"],),
        one=True,
    )["n"]
    stats = _turnaround_stats()
    timings = load_timings(rows, current_app.config["REVIEW_MINUTES"])
    return render_template(
        "admin/queue.html", rows=rows, tab=tab, tabs=TABS, q=q, page=page, has_next=has_next,
        counts=counts, stats=stats, timings=timings,
    )


@bp.route("/submissions/<int:sub_id>")
@admin_required
def submission(sub_id):
    sub = get_submission_or_404(sub_id)
    files = query("SELECT * FROM files WHERE submission_id = ? ORDER BY id", (sub_id,))
    originals = [f for f in files if f["kind"] == "original"]
    results = [f for f in files if f["kind"] == "result"]
    preview = None
    if originals:
        first = originals[0]
        preview = extract_preview(get_storage().path(first["stored_name"]), first["ext"])
    events = query(
        "SELECT e.*, u.name AS actor_name, u.role AS actor_role FROM events e LEFT JOIN users u ON u.id = e.actor_id"
        " WHERE e.submission_id = ? ORDER BY e.created_at, e.id",
        (sub_id,),
    )
    admins = query("SELECT id, name FROM users WHERE role = 'admin' AND is_active = 1 ORDER BY name")
    other_count = query(
        "SELECT COUNT(*) AS n FROM submissions WHERE user_id = ? AND id != ?", (sub["user_id"], sub_id), one=True
    )["n"]
    execute(
        "UPDATE notifications SET is_read = 1 WHERE user_id = ? AND submission_id = ? AND is_read = 0",
        (g.user["id"], sub_id),
    )
    first_sent = next((e["created_at"] for e in events if e["type"] == "results_sent"), None)
    t = timing(sub["created_at"], sub["status"], first_sent, current_app.config["REVIEW_MINUTES"],
               status_events=status_events_of(events))
    return render_template(
        "admin/detail.html", sub=sub, originals=originals, results=results, preview=preview,
        preview_words=word_count(preview), events=events, admins=admins, other_count=other_count, t=t,
        attached=attachments.by_event(sub_id), attach_accept=attachments.ACCEPT,
    )


def _set_status(sub, new_status, notify_user=True):
    """Change status, log it and tell the customer. Caller commits."""
    if new_status == sub["status"]:
        return False
    get_db().execute(
        "UPDATE submissions SET status = ?, updated_at = ? WHERE id = ?", (new_status, utcnow(), sub["id"])
    )
    add_event(sub["id"], g.user["id"], "status", new_status)
    return notify_user


@bp.route("/submissions/<int:sub_id>/claim", methods=("POST",))
@admin_required
def claim(sub_id):
    sub = get_submission_or_404(sub_id)
    conn = get_db()
    conn.execute(
        "UPDATE submissions SET assigned_to = ?, updated_at = ? WHERE id = ?", (g.user["id"], utcnow(), sub_id)
    )
    add_event(sub_id, g.user["id"], "assigned", g.user["name"], internal=True)
    changed = sub["status"] == "submitted" and _set_status(sub, "in_review")
    conn.commit()
    if changed:
        notify([sub["user_id"]], f"A reviewer has started on “{sub['title']}”.", sub_id)
    flash("You're now reviewing this document.", "success")
    return _back(sub_id)


@bp.route("/submissions/<int:sub_id>/assign", methods=("POST",))
@admin_required
def assign(sub_id):
    get_submission_or_404(sub_id)
    raw = request.form.get("assignee", "")
    assignee = None
    if raw:
        row = query("SELECT id, name FROM users WHERE id = ? AND role = 'admin' AND is_active = 1", (raw,), one=True)
        if row is None:
            abort(400, description="Pick a valid reviewer.")
        assignee = row
    conn = get_db()
    conn.execute(
        "UPDATE submissions SET assigned_to = ?, updated_at = ? WHERE id = ?",
        (assignee["id"] if assignee else None, utcnow(), sub_id),
    )
    add_event(sub_id, g.user["id"], "assigned", assignee["name"] if assignee else "Unassigned", internal=True)
    conn.commit()
    if assignee and assignee["id"] != g.user["id"]:
        notify([assignee["id"]], f"{g.user['name']} assigned you a document.", sub_id)
    flash("Assignment updated.", "success")
    return _back(sub_id)


@bp.route("/submissions/<int:sub_id>/status", methods=("POST",))
@admin_required
def set_status(sub_id):
    sub = get_submission_or_404(sub_id)
    new_status = request.form.get("status", "")
    if new_status not in STATUSES:
        abort(400, description="Unknown status.")
    if new_status == "completed" and not sub["feedback_sent_at"]:
        flash("Use “Send results” to complete a review so the customer gets their feedback.", "error")
        return _back(sub_id, "#results")
    conn = get_db()
    changed = _set_status(sub, new_status)
    conn.commit()
    if changed:
        label = STATUSES[new_status]["label"]
        notify(
            [sub["user_id"]],
            f"“{sub['title']}” is now: {label}.",
            sub_id,
            email_subject=f"Your reviewer needs something for “{sub['title']}”" if new_status == "needs_info" else None,
            link_endpoint="portal.submission",
            email={
                "kicker": "Waiting for you",
                "heading": "Your reviewer needs something from you",
                "intro": ["They've paused your review until they hear back. Open your document to see "
                          "what they need, then reply there."],
                "document": sub["title"],
                "button": "See what's needed",
            },
        )
        flash(f"Status set to {label}.", "success")
    return _back(sub_id)


@bp.route("/submissions/<int:sub_id>/message", methods=("POST",))
@admin_required
def message(sub_id):
    sub = get_submission_or_404(sub_id)
    body = request.form.get("body", "").strip()[:5000]
    internal = request.form.get("visibility") == "internal"
    ask = request.form.get("request_info") == "1" and not internal
    prepared, error = attachments.check(request.files.getlist("attachments"))
    if not error and not body and not prepared:
        error = "Write a message or attach a file first."
    if error:
        flash(error, "error")
        return _back(sub_id, "#conversation")
    conn = get_db()
    event_id = add_event(sub_id, g.user["id"], "note" if internal else "message", body, internal=internal)
    attachments.save(prepared, sub_id, event_id, g.user["id"])
    if ask:
        _set_status(sub, "needs_info")
    conn.execute("UPDATE submissions SET updated_at = ? WHERE id = ?", (utcnow(), sub_id))
    conn.commit()
    if not internal:
        text = (
            f"Your reviewer has a question about “{sub['title']}”."
            if ask
            else f"New message from your reviewer on “{sub['title']}”."
        )
        attached = f"{len(prepared)} {'file' if len(prepared) == 1 else 'files'} attached" if prepared else ""
        notify(
            [sub["user_id"]], text, sub_id,
            email_subject=(f"Your reviewer has a question about “{sub['title']}”" if ask
                           else f"Message about “{sub['title']}”"),
            link_endpoint="portal.submission",
            email={
                "kicker": "Question" if ask else "New message",
                "heading": "Your reviewer has a question" if ask else "A message from your reviewer",
                "intro": ["Reply when you can and your review carries on straight away." if ask
                          else "Here's what they wrote about your document."],
                "document": sub["title"],
                "details": [("Attachments", attached)] if attached else [],
                "quote": body or ("(See the attached files.)" if prepared else ""),
                "quote_by": g.user["name"].split(" ")[0] + ", your reviewer",
                "button": "Reply to your reviewer" if ask else "Open the conversation",
                "preheader": (body or "Your reviewer sent you a file.")[:120],
            },
        )
    flash("Internal note added." if internal else "Message sent to the customer.", "success")
    return _back(sub_id, "#conversation")


@bp.route("/submissions/<int:sub_id>/results", methods=("POST",))
@admin_required
def results(sub_id):
    sub = get_submission_or_404(sub_id)
    action = request.form.get("action", "save")
    feedback = request.form.get("feedback", "").strip()[:50_000]
    prepared, error = attachments.check(request.files.getlist("result_files"))
    if error:
        flash(error, "error")
        return _back(sub_id, "#results")
    checked = [(upload, ext) for upload, ext, _, _ in prepared]  # screenshots arrive re-encoded

    conn = get_db()
    for upload, ext in checked:
        store_file(upload, ext, sub_id, "result", g.user["id"], visible=False)
        add_event(sub_id, g.user["id"], "file_added", upload.filename, internal=True)
    conn.execute("UPDATE submissions SET feedback = ?, updated_at = ? WHERE id = ?", (feedback, utcnow(), sub_id))

    if action == "send":
        has_files = query(
            "SELECT 1 FROM files WHERE submission_id = ? AND kind = 'result'", (sub_id,), one=True
        ) or checked
        if not feedback and not has_files:
            conn.rollback()
            flash("Add written feedback or attach a reviewed file before sending.", "error")
            return _back(sub_id, "#results")
        now = utcnow()
        resend = bool(sub["feedback_sent_at"])
        conn.execute(
            "UPDATE submissions SET feedback_sent_at = ?, assigned_to = COALESCE(assigned_to, ?), updated_at = ?"
            " WHERE id = ?",
            (now, g.user["id"], now, sub_id),
        )
        conn.execute("UPDATE files SET visible_to_user = 1 WHERE submission_id = ? AND kind = 'result'", (sub_id,))
        add_event(sub_id, g.user["id"], "results_sent", "updated" if resend else "")
        _set_status(sub, "completed")
        conn.commit()
        n_files = query("SELECT COUNT(*) AS n FROM files WHERE submission_id = ? AND kind = 'result'",
                        (sub_id,), one=True)["n"]
        emails = notify(
            [sub["user_id"]],
            f"Your {'updated ' if resend else ''}feedback for “{sub['title']}” is ready.",
            sub_id,
            email_subject=f"Your {'updated ' if resend else ''}feedback is ready: {sub['title']}",
            link_endpoint="portal.submission",
            email={
                "kicker": "Updated feedback" if resend else "Feedback ready",
                "heading": "Your updated feedback is ready" if resend else "Your feedback is ready",
                "intro": ["Your reviewer has updated their notes." if resend else
                          "A reviewer has read your document and sent back their notes."],
                "document": sub["title"],
                "details": [("Marked-up files", str(n_files))] if n_files else [],
                # the opening of the notes, so the email itself is worth reading; the rest is on the site
                "quote": feedback if len(feedback) <= 400 else feedback[:400].rsplit(" ", 1)[0] + "…",
                "quote_by": g.user["name"].split(" ")[0] + ", your reviewer",
                "button": "Click to view your report",
                "preheader": "Your reviewer's notes are waiting for you.",
            },
        )
        emailed = "sent" in emails
        add_event(sub_id, g.user["id"], "email", "Email sent." if emailed else (
            "No email sent: email isn't set up (RESEND_API_KEY or SMTP_HOST)." if "not_configured" in emails
            else "The email failed. See Messages."), internal=True)
        get_db().commit()
        texted = _text_feedback_ready(sub, resend)
        flash("Results sent. " + {
            (True, True): "The customer was emailed and texted.",
            (True, False): "The customer was emailed. No text went out; see the timeline.",
            (False, True): "The customer was texted. No email went out; see Messages.",
            (False, False): "They're on the customer's page, but no email or text went out. See Messages.",
        }[(emailed, texted)], "success" if emailed or texted else "info")
    else:
        conn.commit()
        flash("Draft saved. The customer can't see it until you send results.", "success")
    return _back(sub_id, "#results")


def _text_feedback_ready(sub, resend):
    """Text the customer that results are ready, and log the outcome on the timeline for reviewers."""
    customer = query("SELECT phone FROM users WHERE id = ?", (sub["user_id"],), one=True)
    phone = customer["phone"] if customer else None
    if not phone:
        result_note, ok = "No text sent: the customer has no mobile number saved.", False
    else:
        link = current_app.config["BASE_URL"].rstrip("/") + url_for("portal.submission", sub_id=sub["id"])
        message = feedback_ready_text(current_app.config["APP_NAME"], ref(sub["id"]), link, updated=resend)
        result = send_sms(phone, message, sub["id"])
        ok = result.ok
        result_note = f"Text sent to {mask_phone(phone)}." if ok else f"Text to {mask_phone(phone)} failed: {result.error}"
    add_event(sub["id"], g.user["id"], "sms", result_note, internal=True)
    get_db().commit()
    return ok


@bp.route("/files/<int:file_id>/delete", methods=("POST",))
@admin_required
def delete_file(file_id):
    f = query("SELECT * FROM files WHERE id = ?", (file_id,), one=True)
    if f is None:
        abort(404)
    if f["kind"] != "result":
        abort(400, description="Customer originals can't be deleted here.")
    conn = get_db()
    conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
    add_event(f["submission_id"], g.user["id"], "file_removed", f["original_name"], internal=True)
    conn.commit()
    get_storage().delete(f["stored_name"])
    flash(f"Removed {f['original_name']}.", "success")
    return _back(f["submission_id"], "#results")


@bp.route("/users")
@admin_required
def users():
    q = request.args.get("q", "").strip()[:100]
    page = _page()
    where, args = "", []
    if q:
        where = "WHERE (u.email LIKE ? OR u.name LIKE ? OR u.phone LIKE ?)"
        args = [f"%{q}%", f"%{q}%", f"%{q.replace(' ', '')}%"]
    rows = query(
        "SELECT u.*, COUNT(s.id) AS total,"
        " SUM(CASE WHEN s.status IN ('submitted','in_review','needs_info') THEN 1 ELSE 0 END) AS open,"
        " MAX(s.updated_at) AS last_activity"
        f" FROM users u LEFT JOIN submissions s ON s.user_id = u.id {where}"
        " GROUP BY u.id ORDER BY u.created_at DESC LIMIT ? OFFSET ?",
        (*args, PER_PAGE + 1, (page - 1) * PER_PAGE),
    )
    has_next = len(rows) > PER_PAGE
    return render_template("admin/users.html", rows=rows[:PER_PAGE], q=q, page=page, has_next=has_next)


@bp.route("/users/<int:user_id>")
@admin_required
def user_detail(user_id):
    user = query("SELECT * FROM users WHERE id = ?", (user_id,), one=True)
    if user is None:
        abort(404)
    subs = query(
        "SELECT s.*, a.name AS assignee_name FROM submissions s LEFT JOIN users a ON a.id = s.assigned_to"
        " WHERE s.user_id = ? ORDER BY s.created_at DESC",
        (user_id,),
    )
    ledger = query(
        "SELECT l.*, a.name AS actor_name FROM ledger l LEFT JOIN users a ON a.id = l.actor_id"
        " WHERE l.user_id = ? ORDER BY l.created_at DESC, l.id DESC LIMIT 100",
        (user_id,),
    )
    return render_template(
        "admin/user_detail.html", user=user, subs=subs, ledger=ledger,
        balance=billing.balance(user_id), subscription=billing.current_subscription(user_id),
        currency_choices=CURRENCY_CHOICES,
    )


CURRENCY_CHOICES = (("USD", "US dollars"), ("KES", "Kenyan shillings (M-Pesa)"))


@bp.route("/messages")
@admin_required
def messages():
    """Every email and text the site tried to send, newest first, and whether it went out."""
    show = request.args.get("show", "all")
    q = request.args.get("q", "").strip()[:100]
    where, args = [], []
    if show == "problems":
        where.append("status != 'sent'")
    if q:
        where.append("(recipient LIKE ? OR subject LIKE ? OR body LIKE ?)")
        args += [f"%{q}%"] * 3
    sql = "SELECT * FROM message_log" + (" WHERE " + " AND ".join(where) if where else "")
    rows = query(sql + " ORDER BY id DESC LIMIT 200", args)
    counts = query("SELECT SUM(status = 'sent') AS sent, SUM(status != 'sent') AS problems FROM message_log"
                   " WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-7 days')", one=True)
    cfg = current_app.config
    return render_template("admin/messages.html", rows=rows, show=show, q=q,
                           sent=counts["sent"] or 0, problems=counts["problems"] or 0,
                           email_ready=bool(cfg.get("RESEND_API_KEY") or cfg.get("SMTP_HOST")),
                           sms_ready=bool(cfg.get("SMS_PROVIDER")))


@bp.route("/users/<int:user_id>/currency", methods=("POST",))
@admin_required
def set_currency(user_id):
    """For direct clients: price an account in KES (or back to USD). Affects new purchases only."""
    user = query("SELECT * FROM users WHERE id = ? AND role = 'user'", (user_id,), one=True)
    currency = request.form.get("currency", "")
    if user is None or currency not in dict(CURRENCY_CHOICES):
        abort(404 if user is None else 400)
    local.set_currency(user_id, currency)
    flash(f"{user['name']} now pays in {dict(CURRENCY_CHOICES)[currency].split(' (')[0].lower()}.", "success")
    return redirect(url_for("admin.user_detail", user_id=user_id))


@bp.route("/users/<int:user_id>/checks", methods=("POST",))
@admin_required
def adjust_checks(user_id):
    user = query("SELECT * FROM users WHERE id = ? AND role = 'user'", (user_id,), one=True)
    if user is None:
        abort(404)
    try:
        change = int(request.form.get("change", "0"))
    except ValueError:
        change = 0
    reason = request.form.get("reason", "").strip()[:120]
    if not change or abs(change) > 1000 or not reason:
        flash("Enter a number of checks (not zero, up to 1,000) and a reason.", "error")
        return redirect(url_for("admin.user_detail", user_id=user_id))
    try:
        billing.admin_adjust(user_id, change, reason, g.user["id"])
    except billing.NotEnoughChecks as e:
        flash(f"{user['name']} only has {e.available} checks, so you can't remove {-change}.", "error")
        return redirect(url_for("admin.user_detail", user_id=user_id))
    if change > 0:
        notify([user_id], f"{change} {'check was' if change == 1 else 'checks were'} added to your account: {reason}")
    flash(f"{'Added' if change > 0 else 'Removed'} {abs(change)} {'check' if abs(change) == 1 else 'checks'}.", "success")
    return redirect(url_for("admin.user_detail", user_id=user_id))


@bp.route("/submissions/<int:sub_id>/refund-checks", methods=("POST",))
@admin_required
def refund_checks(sub_id):
    sub = get_submission_or_404(sub_id)
    n = billing.refund_submission(sub, g.user["id"])
    if not n:
        flash("Nothing to refund: this upload's checks were already returned.", "info")
        return _back(sub_id)
    add_event(sub_id, g.user["id"], "note", f"Refunded {n} {'check' if n == 1 else 'checks'} to the customer.", internal=True)
    get_db().commit()
    notify([sub["user_id"]], f"We've returned {n} {'check' if n == 1 else 'checks'} for \u201c{sub['title']}\u201d to your balance.", sub_id)
    flash(f"Refunded {n} {'check' if n == 1 else 'checks'}.", "success")
    return _back(sub_id)


@bp.route("/payments")
@admin_required
def payments():
    rows = query(
        "SELECT p.*, u.name AS user_name, u.email AS user_email FROM payments p LEFT JOIN users u ON u.id = p.user_id"
        " ORDER BY p.created_at DESC, p.id DESC LIMIT 200"
    )
    totals = query(
        "SELECT currency, SUM(amount) AS total, COUNT(*) AS n FROM payments WHERE status = 'paid'"
        " AND paid_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-30 days') GROUP BY currency"
    )
    via_link = query("SELECT COUNT(*) AS n FROM users WHERE signup_source = 'local'", one=True)["n"]
    kes_accounts = query("SELECT COUNT(*) AS n FROM users WHERE role = 'user' AND currency = 'KES'", one=True)["n"]
    missing_plans = [c for c in billing.monthly_currencies() if not billing.monthly_plan_code(c)]
    return render_template("admin/payments.html", rows=rows, totals=totals, plans={p.id: p for p in pricing.PLANS},
                           ready=billing.payments_ready(), missing_plans=missing_plans,
                           local_path=current_app.config["LOCAL_LINK_PATH"], via_link=via_link,
                           kes_accounts=kes_accounts)
