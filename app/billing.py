"""Check balances and Paystack payments.

How money becomes checks:
  1. A customer picks a plan. We record a pending payment and ask Paystack for a checkout page.
  2. Paystack sends them back to /billing/callback. We verify the transaction with Paystack
     (status, amount and currency must match what we asked for) and grant the checks.
  3. Paystack also posts signed webhooks. charge.success grants the same payment if the customer
     never made it back; invoice.update (paid) grants the monthly plan's checks on each renewal.

Nothing is granted twice: a payment moves to "paid" exactly once, webhook keys are recorded, and a
subscription can't be granted more than once every MONTHLY_GRANT_GAP_DAYS days.
"""
import hashlib
import hmac
import json
import logging
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import click
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from . import local, pricing
from .auth import login_required
from .db import get_db, query, utcnow
from .helpers import parse_ts

log = logging.getLogger(__name__)
bp = Blueprint("billing", __name__, url_prefix="/billing")

API = "https://api.paystack.co"
CHECKOUT_HOSTS = ("https://checkout.paystack.com/", "https://paystack.com/", "https://paystack.shop/")
MONTHLY_GRANT_GAP_DAYS = 20      # renewals are monthly; anything closer together is a duplicate
MONTHLY_CARRY_OVER_MONTHS = 1    # unused monthly checks last this many extra months
ADMIN_GRANT_VALID_MONTHS = 12


class PaymentError(Exception):
    """Something the customer should be told in plain words."""


class NotEnoughChecks(Exception):
    def __init__(self, needed, available):
        super().__init__(f"needs {needed}, has {available}")
        self.needed, self.available = needed, available


# ---- time helpers -------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def add_months(dt, months):
    m = dt.month - 1 + months
    year, month = dt.year + m // 12, m % 12 + 1
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return dt.replace(year=year, month=month, day=min(dt.day, days))


def _immediate(conn):
    """Start a write transaction now, so two requests can't spend the same checks."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")


# ---- balance ----------------------------------------------------------------------------

def balance(user_id, now=None):
    row = query(
        "SELECT COALESCE(SUM(remaining), 0) AS n FROM credit_grants WHERE user_id = ? AND remaining > 0"
        " AND (expires_at IS NULL OR expires_at > ?)",
        (user_id, _iso(now or _now())),
        one=True,
    )
    return row["n"]


def live_grants(user_id, now=None):
    """Grants with checks left, soonest-expiring first (the order they're spent in)."""
    return query(
        "SELECT * FROM credit_grants WHERE user_id = ? AND remaining > 0 AND (expires_at IS NULL OR expires_at > ?)"
        " ORDER BY expires_at IS NULL, expires_at, id",
        (user_id, _iso(now or _now())),
    )


def grant(conn, user_id, checks, source, note, expires_at=None, payment_id=None, actor_id=None):
    """Add checks. Caller owns the transaction."""
    now = utcnow()
    conn.execute(
        "INSERT INTO credit_grants (user_id, source, checks, remaining, expires_at, payment_id, note, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, source, checks, checks, expires_at, payment_id, note, now),
    )
    conn.execute(
        "INSERT INTO ledger (user_id, change, reason, payment_id, actor_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, checks, note, payment_id, actor_id, now),
    )


def take(conn, user_id, checks, reason, submission_id=None, actor_id=None, now=None):
    """Remove checks, soonest-expiring first. Raises NotEnoughChecks. Caller owns the transaction."""
    rows = conn.execute(
        "SELECT id, remaining FROM credit_grants WHERE user_id = ? AND remaining > 0"
        " AND (expires_at IS NULL OR expires_at > ?) ORDER BY expires_at IS NULL, expires_at, id",
        (user_id, _iso(now or _now())),
    ).fetchall()
    available = sum(r["remaining"] for r in rows)
    if available < checks:
        raise NotEnoughChecks(checks, available)
    left = checks
    for r in rows:
        used = min(left, r["remaining"])
        conn.execute("UPDATE credit_grants SET remaining = remaining - ? WHERE id = ?", (used, r["id"]))
        left -= used
        if not left:
            break
    conn.execute(
        "INSERT INTO ledger (user_id, change, reason, submission_id, actor_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, -checks, reason, submission_id, actor_id, utcnow()),
    )


def admin_adjust(user_id, change, reason, actor_id):
    """Admin adds (positive) or removes (negative) checks, with a reason on the ledger."""
    conn = get_db()
    _immediate(conn)
    try:
        if change > 0:
            expires = _iso(add_months(_now(), ADMIN_GRANT_VALID_MONTHS))
            grant(conn, user_id, change, "admin", reason, expires, actor_id=actor_id)
        else:
            take(conn, user_id, -change, reason, actor_id=actor_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def refund_submission(sub, actor_id):
    """Give back the checks an upload used. Only once per upload. Returns checks refunded."""
    conn = get_db()
    _immediate(conn)
    try:
        cur = conn.execute(
            "UPDATE submissions SET checks_refunded = checks_spent WHERE id = ? AND checks_refunded = 0 AND checks_spent > 0",
            (sub["id"],),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return 0
        n = sub["checks_spent"]
        expires = _iso(add_months(_now(), ADMIN_GRANT_VALID_MONTHS))
        grant(conn, sub["user_id"], n, "refund", f"Refund for CA-{sub['id']:05d}", expires, actor_id=actor_id)
        conn.execute("UPDATE ledger SET submission_id = ? WHERE id = last_insert_rowid()", (sub["id"],))
        conn.commit()
        return n
    except Exception:
        conn.rollback()
        raise


# ---- Paystack API -----------------------------------------------------------------------

def _send(method, url, headers, body):
    """The one place that talks HTTP to Paystack (tests replace it)."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status, text = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read().decode("utf-8", "replace")
    try:
        return status, json.loads(text or "{}")
    except ValueError:  # not Paystack answering: e.g. a firewall page ("error code: 1010")
        return status, {"_raw": text[:200]}


def user_agent():
    """Paystack's API sits behind Cloudflare, which turns away Python's default "Python-urllib" agent
    with 403 "error code: 1010". Name the app instead (also used for text messages)."""
    return f"{current_app.config['APP_NAME']}/1.0 (+{current_app.config['BASE_URL'].rstrip('/')})"


def paystack(method, path, payload=None):
    key = current_app.config["PAYSTACK_SECRET_KEY"]
    if not key:
        raise PaymentError("Payments aren't set up yet. Please try again later.")
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json", "Content-Type": "application/json",
               "User-Agent": user_agent()}
    body = json.dumps(payload).encode() if payload is not None else None
    try:
        status, data = _send(method, API + path, headers, body)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("Paystack unreachable: %s", e)
        raise PaymentError("We couldn't reach the payment provider. Please try again in a moment.") from e
    if not (200 <= status < 300) or not data.get("status"):
        log.warning("Paystack %s %s -> HTTP %s: %s", method, path, status, data.get("message") or data.get("_raw") or "(empty)")
        if data.get("message"):
            raise PaymentError(data["message"])
        raise PaymentError(f"The payment provider turned the request away (HTTP {status}). Please try again in a few minutes.")
    return data.get("data") or {}


def subunits(amount: Decimal) -> int:
    return int((Decimal(amount) * 100).to_integral_value())


def plan_by_id(plan_id, currency=pricing.CURRENCY_CODE):
    if currency not in pricing.PRICES:
        return None
    return next((p for p in pricing.plans(currency) if p.id == plan_id), None)


def monthly_plan(currency=pricing.CURRENCY_CODE):
    return next((p for p in pricing.plans(currency) if p.kind == "monthly"), None)


def monthly_plan_code(currency=pricing.CURRENCY_CODE):
    """Paystack plan code for the monthly plan in this currency. Only USD has one today; a currency that
    adds a monthly plan needs its own Paystack plan, saved as PAYSTACK_MONTHLY_PLAN_CODE_<CURRENCY>."""
    if not pricing.has_monthly(currency):
        return ""
    key = "PAYSTACK_MONTHLY_PLAN_CODE" + ("" if currency == pricing.CURRENCY_CODE else f"_{currency}")
    return current_app.config.get(key, "")


def monthly_currencies():
    return [c for c in pricing.PRICES if pricing.has_monthly(c)]


def monthly_plan_codes():
    return {c for c in (monthly_plan_code(cur) for cur in monthly_currencies()) if c}


def payments_ready():
    return bool(current_app.config["PAYSTACK_SECRET_KEY"])


# ---- subscriptions -------------------------------------------------------------------------

def current_subscription(user_id):
    return query(
        "SELECT * FROM subscriptions WHERE user_id = ? AND status IN ('pending', 'active', 'attention', 'non-renewing')"
        " ORDER BY id DESC LIMIT 1",
        (user_id,),
        one=True,
    )


def _user_by_email(email):
    return query("SELECT * FROM users WHERE email = ?", ((email or "").strip().lower(),), one=True) if email else None


def _subscription_for(conn, data, create_for_user=None, create=False):
    """Find our subscription row for a Paystack payload, linking Paystack's code the first time we see it.

    Only checkout and subscription.create may create a row; renewals must match an existing one.
    """
    code = data.get("subscription_code") or (data.get("subscription") or {}).get("subscription_code")
    if code:
        row = conn.execute("SELECT * FROM subscriptions WHERE subscription_code = ?", (code,)).fetchone()
        if row:
            return row
    user = create_for_user or _user_by_email((data.get("customer") or {}).get("email"))
    if user is None:
        return None
    if code:  # a code we haven't seen: attach it to this customer's subscription that has none yet
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE user_id = ? AND subscription_code IS NULL AND status != 'cancelled'"
            " ORDER BY id DESC LIMIT 1",
            (user["id"],),
        ).fetchone()
    else:     # no code in the payload (e.g. a renewal charge): the customer's current subscription
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE user_id = ? AND status != 'cancelled' ORDER BY id DESC LIMIT 1",
            (user["id"],),
        ).fetchone()
    now = utcnow()
    if row is None and not (create or create_for_user):
        return None
    if row is None:
        cur = conn.execute(
            "INSERT INTO subscriptions (user_id, plan_id, status, created_at, updated_at) VALUES (?, ?, 'pending', ?, ?)",
            (user["id"], monthly_plan().id, now, now),
        )
        row = conn.execute("SELECT * FROM subscriptions WHERE id = ?", (cur.lastrowid,)).fetchone()
    if code:
        conn.execute("UPDATE subscriptions SET subscription_code = ?, updated_at = ? WHERE id = ?", (code, now, row["id"]))
        row = conn.execute("SELECT * FROM subscriptions WHERE id = ?", (row["id"],)).fetchone()
    return row


def _grant_monthly(conn, sub, payment_id=None, now=None):
    """Grant one month of the monthly plan, unless this subscription was granted in the last 20 days."""
    now = now or _now()
    last = parse_ts(sub["last_grant_at"]) if sub["last_grant_at"] else None
    if last and now - last < timedelta(days=MONTHLY_GRANT_GAP_DAYS):
        return False
    plan = monthly_plan()
    expires = _iso(add_months(now, 1 + MONTHLY_CARRY_OVER_MONTHS))
    grant(conn, sub["user_id"], plan.checks, "subscription", f"{plan.name} plan", expires, payment_id)
    conn.execute(
        "UPDATE subscriptions SET last_grant_at = ?, status = CASE WHEN status = 'non-renewing' THEN status ELSE 'active' END,"
        " updated_at = ? WHERE id = ?",
        (_iso(now), utcnow(), sub["id"]),
    )
    return True


# ---- fulfilment -----------------------------------------------------------------------------

def fulfill(reference, tx):
    """Grant what a verified Paystack transaction paid for. Safe to call any number of times."""
    pay = query("SELECT * FROM payments WHERE reference = ?", (reference,), one=True)
    if pay is None:
        return False
    conn = get_db()
    if tx.get("status") != "success":
        if tx.get("status") in ("failed", "abandoned", "reversed"):
            conn.execute("UPDATE payments SET status = 'failed' WHERE id = ? AND status = 'pending'", (pay["id"],))
            conn.commit()
        return False
    if int(tx.get("amount") or -1) != pay["amount"] or (tx.get("currency") or "").upper() != pay["currency"]:
        log.error("Payment %s: Paystack says %s %s, expected %s %s; not granting",
                  reference, tx.get("amount"), tx.get("currency"), pay["amount"], pay["currency"])
        return False
    plan = plan_by_id(pay["plan_id"], pay["currency"])
    _immediate(conn)
    try:
        cur = conn.execute(
            "UPDATE payments SET status = 'paid', paid_at = ?, channel = ? WHERE id = ? AND status != 'paid'",
            (utcnow(), tx.get("channel"), pay["id"]),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return False
        user = conn.execute("SELECT * FROM users WHERE id = ?", (pay["user_id"],)).fetchone()
        if plan.kind == "monthly":
            sub = _subscription_for(conn, tx, create_for_user=user)
            _grant_monthly(conn, sub, payment_id=pay["id"])
        else:
            expires = _iso(add_months(_now(), pricing.PACK_VALID_MONTHS))
            note = plan.name if plan.checks == 1 else f"{plan.checks}-check pack"
            grant(conn, pay["user_id"], plan.checks, "purchase", note, expires, pay["id"])
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def _seen(conn, key):
    """Record a webhook key; False if we've handled it before."""
    cur = conn.execute("INSERT OR IGNORE INTO processed_events (key, received_at) VALUES (?, ?)", (key, utcnow()))
    return cur.rowcount == 1


def _record_renewal(conn, sub, reference, data):
    currency = (data.get("currency") or pricing.CURRENCY_CODE).upper()
    plan = monthly_plan(currency) or monthly_plan()
    amount = int(data.get("amount") or subunits(plan.price))
    cur = conn.execute(
        "INSERT OR IGNORE INTO payments (user_id, reference, plan_id, kind, amount, currency, status, channel, created_at, paid_at)"
        " VALUES (?, ?, ?, 'renewal', ?, ?, 'paid', ?, ?, ?)",
        (sub["user_id"], reference, sub["plan_id"], amount, currency,
         data.get("channel"), utcnow(), utcnow()),
    )
    if cur.rowcount != 1:
        return None
    return cur.lastrowid


def handle_event(event, data):
    """Apply one verified webhook event."""
    conn = get_db()

    if event == "charge.success":
        reference = data.get("reference")
        if reference and query("SELECT 1 FROM payments WHERE reference = ? AND kind = 'checkout'", (reference,), one=True):
            fulfill(reference, data)
            return
        plan_code = (data.get("plan") or {}).get("plan_code")
        if reference and plan_code and plan_code in monthly_plan_codes():  # a renewal charge
            _immediate(conn)
            try:
                sub = _subscription_for(conn, data)
                if sub and _seen(conn, f"charge:{reference}"):
                    pid = _record_renewal(conn, sub, reference, data)
                    if pid:  # not already recorded via invoice.update
                        _grant_monthly(conn, sub, payment_id=pid)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return

    if event == "subscription.create":
        _immediate(conn)
        try:
            sub = _subscription_for(conn, data, create=True)
            if sub:
                conn.execute(
                    "UPDATE subscriptions SET email_token = ?, customer_code = ?, next_payment_date = ?,"
                    " status = CASE WHEN status = 'pending' THEN 'active' ELSE status END, updated_at = ? WHERE id = ?",
                    (data.get("email_token"), (data.get("customer") or {}).get("customer_code"),
                     data.get("next_payment_date"), utcnow(), sub["id"]),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return

    if event == "invoice.update" and (data.get("paid") or data.get("status") == "success"):
        invoice = data.get("invoice_code")
        _immediate(conn)
        try:
            sub = _subscription_for(conn, data)
            if sub and invoice and _seen(conn, f"invoice:{invoice}"):
                reference = (data.get("transaction") or {}).get("reference") or f"invoice-{invoice}"
                pid = _record_renewal(conn, sub, reference, data)
                if pid:  # not already recorded via charge.success
                    _grant_monthly(conn, sub, payment_id=pid)
                next_date = (data.get("subscription") or {}).get("next_payment_date")
                if next_date:
                    conn.execute("UPDATE subscriptions SET next_payment_date = ? WHERE id = ?", (next_date, sub["id"]))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return

    status_for = {"invoice.payment_failed": "attention", "subscription.not_renew": "non-renewing",
                  "subscription.disable": "cancelled"}
    if event in status_for:
        sub = _subscription_for(conn, data)
        if sub:
            conn.execute("UPDATE subscriptions SET status = ?, updated_at = ? WHERE id = ?",
                         (status_for[event], utcnow(), sub["id"]))
            conn.commit()
            if event == "invoice.payment_failed":
                from .notify import notify
                notify([sub["user_id"]], "We couldn't renew your monthly plan. Update your card to keep your checks coming.",
                       email_subject="Your monthly plan couldn't renew")


# ---- pages ---------------------------------------------------------------------------------

@bp.route("/")
@login_required
def page():
    uid = g.user["id"]
    history = query("SELECT * FROM ledger WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT 50", (uid,))
    return render_template(
        "billing.html", balance=balance(uid), grants=live_grants(uid), history=history,
        plans=pricing.plans_for_display(local.currency_for(g.user)), subscription=current_subscription(uid),
        ready=payments_ready(), words_per_check=pricing.WORDS_PER_CHECK,
    )


@bp.route("/checkout/<plan_id>", methods=("POST",))
@login_required
def checkout(plan_id):
    if g.user["role"] == "admin":
        flash("Reviewer accounts don't buy checks. Use a customer account.", "info")
        return redirect(url_for("billing.page"))
    currency = local.currency_for(g.user)
    plan = plan_by_id(plan_id, currency)
    if plan is None:
        abort(404)
    monthly_code = monthly_plan_code(currency)
    if plan.kind == "monthly":
        if not monthly_code:
            flash("The monthly plan isn't available yet. Choose a pack for now.", "error")
            return redirect(url_for("billing.page"))
        if current_subscription(g.user["id"]):
            flash("You already have the monthly plan.", "info")
            return redirect(url_for("billing.page"))
    reference = f"ca_{secrets.token_hex(10)}"
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO payments (user_id, reference, plan_id, kind, amount, currency, created_at)"
        " VALUES (?, ?, ?, 'checkout', ?, ?, ?)",
        (g.user["id"], reference, plan.id, subunits(plan.price), currency, utcnow()),
    )
    conn.commit()
    base = current_app.config["BASE_URL"].rstrip("/")
    payload = {
        "email": g.user["email"],
        "amount": subunits(plan.price),
        "currency": currency,
        "reference": reference,
        "callback_url": base + url_for("billing.callback"),
        "metadata": {"payment_id": cur.lastrowid, "user_id": g.user["id"], "plan_id": plan.id,
                     "cancel_action": base + url_for("billing.page")},
    }
    if plan.kind == "monthly":
        payload["plan"] = monthly_code
        payload["channels"] = ["card"]  # renewals need a reusable card
    elif current_app.config["PAYSTACK_CHANNELS"]:
        payload["channels"] = current_app.config["PAYSTACK_CHANNELS"]
    try:
        data = paystack("POST", "/transaction/initialize", payload)
    except PaymentError as e:
        conn.execute("UPDATE payments SET status = 'failed' WHERE reference = ?", (reference,))
        conn.commit()
        flash(str(e), "error")
        return redirect(url_for("billing.page"))
    url = data.get("authorization_url") or ""
    if not url.startswith(CHECKOUT_HOSTS):
        log.error("Unexpected Paystack checkout URL %r", url)
        flash("Something went wrong starting the payment. Please try again.", "error")
        return redirect(url_for("billing.page"))
    conn.execute("UPDATE payments SET access_code = ? WHERE reference = ?", (data.get("access_code"), reference))
    conn.commit()
    return redirect(url, code=303)


@bp.route("/callback")
@login_required
def callback():
    reference = request.args.get("reference") or request.args.get("trxref") or ""
    pay = query("SELECT * FROM payments WHERE reference = ? AND user_id = ?", (reference, g.user["id"]), one=True)
    if pay is None:
        abort(404)
    if pay["status"] != "paid":
        try:
            tx = paystack("GET", f"/transaction/verify/{urllib.request.quote(reference)}")
        except PaymentError as e:
            flash(f"We couldn't confirm your payment yet: {e} If you were charged, your checks will appear shortly.", "error")
            return redirect(url_for("billing.page"))
        fulfill(reference, tx)
        pay = query("SELECT * FROM payments WHERE id = ?", (pay["id"],), one=True)
    plan = plan_by_id(pay["plan_id"], pay["currency"]) or plan_by_id(pay["plan_id"])
    if pay["status"] == "paid":
        flash(f"Payment received. {plan.name} added: you now have {balance(g.user['id'])} checks.", "success")
    elif pay["status"] == "failed":
        flash("The payment didn't go through, so you haven't been charged. You can try again.", "error")
    else:
        flash("Your payment is still processing. Your checks will appear here as soon as it's confirmed.", "info")
    return redirect(url_for("billing.page"))


@bp.route("/webhook", methods=("POST",))
def webhook():
    """Paystack calls this. Exempt from CSRF; trusted only if the signature matches our secret key."""
    secret = current_app.config["PAYSTACK_SECRET_KEY"]
    raw = request.get_data()
    sent = request.headers.get("x-paystack-signature", "")
    expected = hmac.new(secret.encode(), raw, hashlib.sha512).hexdigest() if secret else ""
    if not secret or not hmac.compare_digest(sent, expected):
        abort(401)
    try:
        event = json.loads(raw)
    except ValueError:
        abort(400)
    try:
        handle_event(event.get("event"), event.get("data") or {})
    except Exception:
        log.exception("Webhook %s failed; Paystack will retry", event.get("event"))
        return ("", 500)
    return ("", 200)


@bp.route("/subscription/cancel", methods=("POST",))
@login_required
def cancel_subscription():
    sub = current_subscription(g.user["id"])
    if not sub or not sub["subscription_code"] or not sub["email_token"]:
        flash("We couldn't find an active monthly plan to cancel. Contact us if you're still being charged.", "error")
        return redirect(url_for("billing.page"))
    try:
        paystack("POST", "/subscription/disable", {"code": sub["subscription_code"], "token": sub["email_token"]})
    except PaymentError as e:
        flash(str(e), "error")
        return redirect(url_for("billing.page"))
    get_db().execute("UPDATE subscriptions SET status = 'cancelled', updated_at = ? WHERE id = ?", (utcnow(), sub["id"]))
    get_db().commit()
    flash("Monthly plan cancelled. You won't be charged again, and the checks you have stay until they expire.", "success")
    return redirect(url_for("billing.page"))


@bp.route("/subscription/card", methods=("POST",))
@login_required
def update_card():
    sub = current_subscription(g.user["id"])
    if not sub or not sub["subscription_code"]:
        abort(404)
    try:
        data = paystack("GET", f"/subscription/{urllib.request.quote(sub['subscription_code'])}/manage/link")
    except PaymentError as e:
        flash(str(e), "error")
        return redirect(url_for("billing.page"))
    link = data.get("link") or ""
    if not link.startswith(CHECKOUT_HOSTS):
        flash("Something went wrong opening the card page. Please try again.", "error")
        return redirect(url_for("billing.page"))
    return redirect(link, code=303)


def init_app(app):
    app.register_blueprint(bp)

    @app.cli.command("paystack-create-plan")
    @click.option("--currency", type=click.Choice(sorted(c for c in pricing.PRICES if pricing.has_monthly(c)),
                                                  case_sensitive=False),
                  default=pricing.CURRENCY_CODE, show_default=True, help="Currency of the monthly plan.")
    def create_plan_command(currency):
        """Create the monthly plan on Paystack and print the setting to save its code in."""
        currency = currency.upper()
        plan = monthly_plan(currency)
        data = paystack("POST", "/plan", {
            "name": f"{app.config['APP_NAME']} {plan.name} ({plan.checks} checks, {currency})",
            "interval": "monthly", "amount": subunits(plan.price), "currency": currency,
        })
        key = "PAYSTACK_MONTHLY_PLAN_CODE" + ("" if currency == pricing.CURRENCY_CODE else f"_{currency}")
        click.echo(f"Created. Set {key}={data.get('plan_code')}")
