"""Commands for trying the app out. Never available in production."""
import click

from . import billing, pricing
from .db import create_user, get_db, query

DEMO_PASSWORD = "demo-pass-2026"
DEMO_ACCOUNTS = (
    # email, name, role, currency, source, checks
    ("reviewer@demo.test", "Demo Reviewer", "admin", "USD", None, 0),
    ("student@demo.test", "Demo Student", "user", "USD", None, 5),
    ("wanjiru@demo.test", "Demo Wanjiru", "user", "KES", "local", 5),
)


def init_app(app):
    @app.cli.command("seed-demo")
    @click.option("--phone", default="", help="Your own mobile number (+…), so 'feedback ready' texts reach you.")
    def seed_demo(phone):
        """Create demo accounts with checks, for trying the app on a test copy."""
        if app.config["APP_ENV"] == "production":
            raise click.ClickException("seed-demo is for test copies only (APP_ENV is production).")
        from .sms import normalize_phone
        number = normalize_phone(phone, app.config["DEFAULT_COUNTRY_CODE"]) if phone else None
        if phone and not number:
            raise click.ClickException(f"{phone} doesn't look like a mobile number. Start with + and the country code.")
        db = get_db()
        click.echo(f"Password for every demo account: {DEMO_PASSWORD}\n")
        for email, name, role, currency, source, checks in DEMO_ACCOUNTS:
            row = query("SELECT id FROM users WHERE email = ?", (email,), one=True)
            if row:
                uid = row["id"]
                status = "already there"
            else:
                uid = create_user(email, name, DEMO_PASSWORD, role=role, currency=currency, signup_source=source,
                                  phone=number if role == "user" else None)
                status = "created"
                if checks:
                    billing.grant(db, uid, checks, "admin", "Demo checks")
                    db.commit()
            extra = f", {checks} checks, pays in {currency}" if role == "user" else ", reviewer (admin)"
            click.echo(f"  {email:<22} {status}{extra}")
        click.echo("\nThe .test addresses can't receive email. Watch Admin > Messages to see what would be sent,"
                   "\nor sign up with your own email to receive it (needs the SMTP settings).")
