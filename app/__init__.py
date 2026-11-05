"""CheckAssist: document upload with manual human review."""
import os
from datetime import timedelta

from dotenv import load_dotenv
from flask import Flask, g, render_template

from . import db, security


def _bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def create_app(test_config=None):
    load_dotenv()
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)

    app.config.from_mapping(
        APP_NAME=os.environ.get("APP_NAME", "CheckAssist"),
        APP_ENV=os.environ.get("APP_ENV", "development"),
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        DATABASE=os.environ.get("DATABASE_PATH", os.path.join(app.instance_path, "checkassist.sqlite3")),
        UPLOAD_DIR=os.environ.get("UPLOAD_DIR", os.path.join(app.instance_path, "uploads")),
        MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_MB", "25")) * 1024 * 1024,
        BASE_URL=os.environ.get("BASE_URL", "http://localhost:5000"),
        SMTP_HOST=os.environ.get("SMTP_HOST", ""),
        SMTP_PORT=int(os.environ.get("SMTP_PORT", "587")),
        SMTP_USER=os.environ.get("SMTP_USER", ""),
        SMTP_PASSWORD=os.environ.get("SMTP_PASSWORD", ""),
        SMTP_FROM=os.environ.get("SMTP_FROM", "CheckAssist <no-reply@example.com>"),
        SMTP_TLS=_bool("SMTP_TLS", True),
        ADMIN_EMAIL_ALERTS=_bool("ADMIN_EMAIL_ALERTS", False),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_bool("SESSION_COOKIE_SECURE", False),
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
        CSRF_ENABLED=True,
        MAIL_OUTBOX=None,
    )
    if test_config:
        app.config.update(test_config)

    if app.config["APP_ENV"] == "production" and app.config["SECRET_KEY"] == "dev-only-change-me":
        raise RuntimeError("Set SECRET_KEY before running in production.")

    db.init_app(app)
    security.init_app(app)

    from . import admin_views, auth, helpers, user_views

    app.register_blueprint(auth.bp)
    app.register_blueprint(user_views.bp)
    app.register_blueprint(admin_views.bp)

    app.jinja_env.filters.update(
        human_date=helpers.human_date,
        time_ago=helpers.time_ago,
        file_size=helpers.file_size,
        ref=helpers.ref,
    )

    @app.context_processor
    def inject_globals():
        unread = 0
        if getattr(g, "user", None):
            row = db.query(
                "SELECT COUNT(*) AS n FROM notifications WHERE user_id = ? AND is_read = 0",
                (g.user["id"],),
                one=True,
            )
            unread = row["n"]
        return {
            "current_user": getattr(g, "user", None),
            "unread_count": unread,
            "STATUSES": helpers.STATUSES,
            "app_name": app.config["APP_NAME"],
            "allowed_label": security.ALLOWED_LABEL,
            "max_upload_mb": app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024),
        }

    def _error(code, title, message):
        return render_template("error.html", code=code, title=title, message=message), code

    @app.errorhandler(400)
    def bad_request(e):
        return _error(400, "Something went wrong", getattr(e, "description", "") or "Bad request.")

    @app.errorhandler(403)
    def forbidden(_e):
        return _error(403, "No access", "You don't have permission to view this page.")

    @app.errorhandler(404)
    def not_found(_e):
        return _error(404, "Not found", "We couldn't find that page.")

    @app.errorhandler(413)
    def too_large(_e):
        return _error(
            413, "File too large", f"Files can be up to {app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)} MB."
        )

    return app
