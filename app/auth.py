"""Registration, login, logout and access-control decorators."""
import re
from functools import wraps

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .db import create_user, execute, query, utcnow
from .security import csrf_token, login_throttle

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
    form = {"name": "", "email": ""}
    if request.method == "POST":
        form["name"] = request.form.get("name", "").strip()[:100]
        form["email"] = request.form.get("email", "").strip().lower()[:254]
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        error = None
        if not form["name"]:
            error = "Enter your name."
        elif not EMAIL_RE.match(form["email"]):
            error = "Enter a valid email address."
        elif len(password) < 8:
            error = "Use a password with at least 8 characters."
        elif password != confirm:
            error = "Passwords don't match."
        elif query("SELECT 1 FROM users WHERE email = ?", (form["email"],), one=True):
            error = "An account with that email already exists. Log in instead."
        if error:
            flash(error, "error")
        else:
            uid = create_user(form["email"], form["name"], password)
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


@bp.route("/logout", methods=("POST",))
def logout():
    session.clear()
    flash("You're logged out.", "success")
    return redirect(url_for("portal.index"))
