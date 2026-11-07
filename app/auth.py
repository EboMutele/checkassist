"""Registration, login, logout and access-control decorators."""
import re
from functools import wraps

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .db import create_user, execute, query, utcnow
from .security import csrf_token, login_throttle
from .sms import normalize_phone


def phone_error():
    cc = current_app.config["DEFAULT_COUNTRY_CODE"]
    if cc:
        return f"Enter a valid mobile number, for example 0712 345 678 or +{cc} 712 345 678."
    return "Enter your mobile number starting with + and your country code."


def read_phone(raw):
    """Return (e164_number, error_message)."""
    raw = (raw or "").strip()[:40]
    if not raw:
        return None, "Enter your mobile number so we can text you when your feedback is ready."
    phone = normalize_phone(raw, current_app.config["DEFAULT_COUNTRY_CODE"])
    return (phone, None) if phone else (None, phone_error())

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@bp.before_app_request
def load_user():
    g.user = None
    uid = session.get("uid")
    if uid is not None:
        user = query("SELECT * FROM users WHERE id = ? AND is_active = 1", (uid,), one=True)
        if user is None:
            session.clear()
        g.user = user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.full_path))
        if g.user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def home_for(user):
    return url_for("admin.queue") if user["role"] == "admin" else url_for("portal.dashboard")


def _safe_next(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


def _start_session(user_id):
    session.clear()  # prevent session fixation
    session.permanent = True
    session["uid"] = user_id
    csrf_token()  # rotate the CSRF token with the new session
    execute("UPDATE users SET last_login_at = ? WHERE id = ?", (utcnow(), user_id))


@bp.route("/register", methods=("GET", "POST"))
def register():
    if g.user:
        return redirect(home_for(g.user))
    form = {"name": "", "email": "", "phone": ""}
    if request.method == "POST":
        form["name"] = request.form.get("name", "").strip()[:100]
        form["email"] = request.form.get("email", "").strip().lower()[:254]
        form["phone"] = request.form.get("phone", "").strip()[:40]
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        error = None
        phone, phone_err = read_phone(form["phone"])
        if not form["name"]:
            error = "Enter your name."
        elif not EMAIL_RE.match(form["email"]):
            error = "Enter a valid email address."
        elif phone_err:
            error = phone_err
        elif len(password) < 8:
            error = "Use a password with at least 8 characters."
        elif password != confirm:
            error = "Passwords don't match."
        elif query("SELECT 1 FROM users WHERE email = ?", (form["email"],), one=True):
            error = "An account with that email already exists. Log in instead."
        if error:
            flash(error, "error")
        else:
            uid = create_user(form["email"], form["name"], password, phone=phone)
            _start_session(uid)
            flash("Welcome! Upload your first document whenever you're ready.", "success")
            return redirect(url_for("portal.dashboard"))
    return render_template("auth/register.html", form=form)


@bp.route("/login", methods=("GET", "POST"))
def login():
    if g.user:
        return redirect(home_for(g.user))
    email = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        key = f"{request.remote_addr}|{email}"
        if login_throttle.blocked(key):
            flash("Too many failed attempts. Wait 15 minutes and try again.", "error")
            return render_template("auth/login.html", email=email), 429
        user = query("SELECT * FROM users WHERE email = ?", (email,), one=True)
        if user and user["is_active"] and check_password_hash(user["password_hash"], password):
            login_throttle.reset(key)
            next_url = _safe_next(request.args.get("next"))
            _start_session(user["id"])
            return redirect(next_url or home_for(user))
        login_throttle.fail(key)
        flash("That email and password don't match an account.", "error")
    return render_template("auth/login.html", email=email)


@bp.route("/account", methods=("GET", "POST"))
@login_required
def account():
    form = {"name": g.user["name"], "phone": g.user["phone"] or ""}
    if request.method == "POST":
        form["name"] = request.form.get("name", "").strip()[:100]
        form["phone"] = request.form.get("phone", "").strip()[:40]
        if g.user["role"] == "admin" and not form["phone"]:
            phone, error = None, None  # reviewers don't get "feedback ready" texts
        else:
            phone, error = read_phone(form["phone"])
        if not form["name"]:
            error = "Enter your name."
        if error:
            flash(error, "error")
            return render_template("auth/account.html", form=form), 400
        execute("UPDATE users SET name = ?, phone = ? WHERE id = ?", (form["name"], phone, g.user["id"]))
        if g.user["role"] == "admin":
            flash("Saved.", "success")
        else:
            flash(f"Saved. We'll text {phone} when your feedback is ready.", "success")
        return redirect(url_for("auth.account"))
    return render_template("auth/account.html", form=form)


@bp.route("/logout", methods=("POST",))
def logout():
    session.clear()
    flash("You're logged out.", "success")
    return redirect(url_for("portal.index"))
