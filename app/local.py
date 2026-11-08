"""The private Kenya link (LOCAL_LINK_PATH, /local by default): prices in Kenyan shillings, M-Pesa
through Paystack, and Kenyan mobile numbers that work without +254.

Local prices are lower than the public ones, and the link is the only gate: anyone who opens it can
sign up at local prices. So it stays private: it isn't in seo.PAGES, so it's served with noindex; it
isn't in the sitemap; and it isn't in robots.txt either, because robots.txt is public and listing it
there would advertise it. Nothing on the public site links to it, and /pricing sends local visitors
here rather than showing local prices on a public URL. If it spreads further than you want, change
LOCAL_LINK_PATH; accounts that already use shillings keep them.

Who pays in shillings: anyone who signs up after opening the link, anyone signed in who opens it and
chooses to switch, and any account an admin switches. Everyone else sees and pays USD.
"""
from flask import Blueprint, current_app, flash, g, has_request_context, redirect, render_template, session, url_for

from . import pricing
from .db import execute

KENYA_COUNTRY_CODE = "254"
SESSION_KEY = "local"
SOURCE = "local"


def currency_for(user):
    """The currency an account pays in."""
    if user is None or user["role"] != "user":
        return pricing.CURRENCY_CODE
    return (user["currency"] if "currency" in user.keys() else None) or pricing.CURRENCY_CODE


def visitor_currency():
    """Signed in: the account's currency. Signed out: KES once they've opened the link, else USD."""
    if not has_request_context():
        return pricing.CURRENCY_CODE
    user = getattr(g, "user", None)
    if user is not None:
        return currency_for(user)
    return pricing.LOCAL_CURRENCY if session.get(SESSION_KEY) else pricing.CURRENCY_CODE


def came_through_link():
    return has_request_context() and getattr(g, "user", None) is None and bool(session.get(SESSION_KEY))


def phone_country_code(user=None):
    """Country code assumed for numbers typed without +: Kenyan for shilling accounts and link visitors."""
    if user is None and has_request_context():
        user = getattr(g, "user", None)
    if (user is not None and currency_for(user) == pricing.LOCAL_CURRENCY) or (user is None and came_through_link()):
        return KENYA_COUNTRY_CODE
    return current_app.config["DEFAULT_COUNTRY_CODE"]


def set_currency(user_id, currency):
    if currency not in pricing.PRICES:
        raise ValueError(currency)
    execute("UPDATE users SET currency = ? WHERE id = ? AND role = 'user'", (currency, user_id))


def page():
    user = g.user
    if user is None:
        session[SESSION_KEY] = True
    currency = pricing.LOCAL_CURRENCY
    return render_template(
        "local.html",
        plans=pricing.plans_for_display(currency),
        single_price=pricing.single_price(currency),
        lowest_per_check=pricing.lowest_per_check(currency),
        words_per_check=pricing.WORDS_PER_CHECK,
        pack_months=pricing.PACK_VALID_MONTHS,
        already_local=user is not None and currency_for(user) == currency,
        can_switch=user is not None and user["role"] == "user" and currency_for(user) != currency,
    )


def use_shillings():
    user = g.user
    if user is None:
        session[SESSION_KEY] = True
        return redirect(url_for("auth.register"))
    if user["role"] != "user":
        flash("Reviewer accounts don't buy checks.", "info")
        return redirect(url_for("local.page"))
    set_currency(user["id"], pricing.LOCAL_CURRENCY)
    flash("Done. Your prices are now in Kenyan shillings, and you can pay with M-Pesa.", "success")
    return redirect(url_for("billing.page"))


def init_app(app):
    path = "/" + (app.config["LOCAL_LINK_PATH"] or "").strip().strip("/")
    if path == "/":
        return  # link switched off
    bp = Blueprint("local", __name__)
    bp.add_url_rule(path, "page", page)
    bp.add_url_rule(path + "/use-shillings", "use_shillings", use_shillings, methods=("POST",))
    app.register_blueprint(bp)
