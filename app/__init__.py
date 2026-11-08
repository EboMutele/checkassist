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
        # On by default: with a short review-time promise, reviewers need to hear about uploads at once.
        ADMIN_EMAIL_ALERTS=_bool("ADMIN_EMAIL_ALERTS", True),
        # Texts: see app/sms.py. DEFAULT_COUNTRY_CODE (digits, e.g. 254) lets people type local numbers.
        SMS_PROVIDER=os.environ.get("SMS_PROVIDER", "").strip().lower(),
        AT_USERNAME=os.environ.get("AT_USERNAME", ""),
        AT_API_KEY=os.environ.get("AT_API_KEY", ""),
        AT_SENDER_ID=os.environ.get("AT_SENDER_ID", ""),
        TWILIO_ACCOUNT_SID=os.environ.get("TWILIO_ACCOUNT_SID", ""),
        TWILIO_AUTH_TOKEN=os.environ.get("TWILIO_AUTH_TOKEN", ""),
        TWILIO_FROM=os.environ.get("TWILIO_FROM", ""),
        TWILIO_MESSAGING_SERVICE_SID=os.environ.get("TWILIO_MESSAGING_SERVICE_SID", ""),
        DEFAULT_COUNTRY_CODE=os.environ.get("DEFAULT_COUNTRY_CODE", "").strip().lstrip("+"),
        SMS_OUTBOX=None,
        # Payments (Paystack). Without a secret key, buying is switched off and admins can grant checks.
        PAYSTACK_SECRET_KEY=os.environ.get("PAYSTACK_SECRET_KEY", "").strip(),
        PAYSTACK_MONTHLY_PLAN_CODE=os.environ.get("PAYSTACK_MONTHLY_PLAN_CODE", "").strip(),
        PAYSTACK_MONTHLY_PLAN_CODE_KES=os.environ.get("PAYSTACK_MONTHLY_PLAN_CODE_KES", "").strip(),
        PAYSTACK_CHANNELS=[c.strip() for c in os.environ.get("PAYSTACK_CHANNELS", "").split(",") if c.strip()],
        CHECKS_REQUIRED=_bool("CHECKS_REQUIRED", True),
        SIGNUP_FREE_CHECKS=max(0, int(os.environ.get("SIGNUP_FREE_CHECKS", "0"))),
        CSRF_EXEMPT_ENDPOINTS=("billing.webhook",),
        # Promised minutes from upload to feedback. 0 hides the promise everywhere.
        REVIEW_MINUTES=max(0, int(os.environ.get("REVIEW_MINUTES", "20"))),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_bool("SESSION_COOKIE_SECURE", False),
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
        CSRF_ENABLED=True,
        MAIL_OUTBOX=None,
        # Search engines (app/seo.py). Indexing is on only in production unless ALLOW_INDEXING says otherwise,
        # so staging copies never compete with the live site.
        ALLOW_INDEXING=_bool("ALLOW_INDEXING", None),
        GOOGLE_SITE_VERIFICATION=os.environ.get("GOOGLE_SITE_VERIFICATION", "").strip(),
        BING_SITE_VERIFICATION=os.environ.get("BING_SITE_VERIFICATION", "").strip(),
        SUPPORT_EMAIL=os.environ.get("SUPPORT_EMAIL", "").strip(),
        # The private Kenya link (app/local.py): KES prices and M-Pesa. Empty switches it off.
        LOCAL_LINK_PATH=os.environ.get("LOCAL_LINK_PATH", "/local").strip(),
        REDIRECT_TO_BASE_URL=_bool("REDIRECT_TO_BASE_URL", False),
    )
    if test_config:
        app.config.update(test_config)
    if app.config["ALLOW_INDEXING"] is None:
        app.config["ALLOW_INDEXING"] = app.config["APP_ENV"] == "production"

    if app.config["APP_ENV"] == "production" and app.config["SECRET_KEY"] == "dev-only-change-me":
        raise RuntimeError("Set SECRET_KEY before running in production.")

    db.init_app(app)
    security.init_app(app)

    from . import admin_views, auth, billing, helpers, local, seo, sms, user_views

    app.register_blueprint(auth.bp)
    app.register_blueprint(user_views.bp)
    app.register_blueprint(admin_views.bp)
    billing.init_app(app)
    seo.init_app(app)
    local.init_app(app)

    app.jinja_env.filters.update(
        human_date=helpers.human_date,
        time_ago=helpers.time_ago,
        file_size=helpers.file_size,
        ref=helpers.ref,
        minutes=helpers.minutes_label,
        masked=sms.mask_phone,
        clock=helpers.clock,
    )

    @app.context_processor
    def inject_globals():
        unread = 0
        checks_balance = None
        if getattr(g, "user", None) and g.user["role"] == "user":
            checks_balance = billing.balance(g.user["id"])
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
            "review_minutes": app.config["REVIEW_MINUTES"],
            "phone_cc": local.phone_country_code(),
            "currency": local.visitor_currency(),
            "server_now": db.utcnow(),
            "checks_balance": checks_balance,
            "checks_required": app.config["CHECKS_REQUIRED"],  # lets live timers correct for a wrong device clock
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
