import hashlib
import hmac
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from conftest import Session, sub_id_from, upload

from app import billing, create_app
from app.db import create_user

SECRET = "sk_test_123"
PLAN_CODE = "PLN_monthly"


class FakePaystack:
    """Stands in for api.paystack.co: records calls, returns scripted answers."""

    def __init__(self):
        self.calls = []
        self.transactions = {}   # reference -> verify payload
        self.fail_next = None

    def __call__(self, method, url, headers, body):
        payload = json.loads(body) if body else None
        self.calls.append({"method": method, "url": url, "headers": headers, "payload": payload})
        assert headers["Authorization"] == f"Bearer {SECRET}"
        if self.fail_next:
            msg, self.fail_next = self.fail_next, None
            return 400, {"status": False, "message": msg}
        path = url.replace("https://api.paystack.co", "")
        if path == "/transaction/initialize":
            ref = payload["reference"]
            return 200, {"status": True, "data": {"authorization_url": f"https://checkout.paystack.com/{ref}",
                                                  "access_code": "ac_" + ref, "reference": ref}}
        if path.startswith("/transaction/verify/"):
            ref = path.rsplit("/", 1)[1]
            return 200, {"status": True, "data": self.transactions.get(ref, {"status": "abandoned", "reference": ref})}
        if path == "/subscription/disable":
            return 200, {"status": True, "message": "Subscription disabled successfully"}
        if path.endswith("/manage/link"):
            return 200, {"status": True, "data": {"link": "https://paystack.com/manage/subscriptions/abc"}}
        if path == "/plan":
            return 200, {"status": True, "data": {"plan_code": PLAN_CODE}}
        return 404, {"status": False, "message": "not found"}

    def paid(self, ref, amount, currency="USD", **extra):
        self.transactions[ref] = {"status": "success", "reference": ref, "amount": amount, "currency": currency,
                                  "channel": "card", "customer": {"email": "nia@example.com"}, **extra}


@pytest.fixture
def fake(monkeypatch):
    f = FakePaystack()
    monkeypatch.setattr(billing, "_send", f)
    return f


@pytest.fixture
def pay_app(tmp_path):
    return create_app({"TESTING": True, "SECRET_KEY": "t", "DATABASE": str(tmp_path / "d.sqlite3"),
                       "UPLOAD_DIR": str(tmp_path / "u"), "BASE_URL": "https://checkassist.test",
                       "PAYSTACK_SECRET_KEY": SECRET, "PAYSTACK_MONTHLY_PLAN_CODE": PLAN_CODE,
                       "MAIL_OUTBOX": [], "SMS_OUTBOX": []})


@pytest.fixture
def nia(pay_app):
    with pay_app.app_context():
        uid = create_user("nia@example.com", "Nia Kamau", "password123", phone="+254712000111")
    s = Session(pay_app)
    s.login("nia@example.com")
    s.uid = uid
    return s


def _balance(app, uid):
    with app.app_context():
        return billing.balance(uid)


def _payment(app, **where):
    with app.app_context():
        from app.db import query
        key, val = next(iter(where.items()))
        return query(f"SELECT * FROM payments WHERE {key} = ?", (val,), one=True)


def _checkout(s, plan_id):
    r = s.post(f"/billing/checkout/{plan_id}")
    assert r.status_code == 303, r.status_code
    return r.headers["Location"].rsplit("/", 1)[1]  # our reference, from the fake checkout URL


def _sign(body):
    return hmac.new(SECRET.encode(), body, hashlib.sha512).hexdigest()


def _webhook(app, event, data, signature=None):
    body = json.dumps({"event": event, "data": data}).encode()
    return app.test_client().post("/billing/webhook", data=body, content_type="application/json",
                                  headers={"x-paystack-signature": signature or _sign(body)})


# ---- checkout ------------------------------------------------------------------------

def test_checkout_sends_the_right_amount_and_redirects_to_paystack(pay_app, nia, fake):
    ref = _checkout(nia, "pack10")
    call = fake.calls[-1]["payload"]
    assert call["amount"] == 3999 and call["currency"] == "USD" and call["email"] == "nia@example.com"
    assert call["reference"] == ref and call["callback_url"] == "https://checkassist.test/billing/callback"
    assert call["metadata"]["plan_id"] == "pack10" and "plan" not in call
    pay = _payment(pay_app, reference=ref)
    assert pay["status"] == "pending" and pay["amount"] == 3999 and pay["user_id"] == nia.uid


def test_callback_verifies_with_paystack_and_grants_once(pay_app, nia, fake):
    ref = _checkout(nia, "pack10")
    fake.paid(ref, 3999)
    r = nia.get(f"/billing/callback?reference={ref}&trxref={ref}", follow_redirects=True)
    assert "Payment received" in r.get_data(as_text=True)
    assert _balance(pay_app, nia.uid) == 10
    nia.get(f"/billing/callback?reference={ref}")             # back button / refresh
    _webhook(pay_app, "charge.success", fake.transactions[ref])  # and Paystack's webhook
    assert _balance(pay_app, nia.uid) == 10
    assert _payment(pay_app, reference=ref)["status"] == "paid"


def test_wrong_amount_or_currency_grants_nothing(pay_app, nia, fake):
    ref = _checkout(nia, "pack20")
    fake.paid(ref, 100)            # someone tampered with the amount
    nia.get(f"/billing/callback?reference={ref}")
    ref2 = _checkout(nia, "pack20")
    fake.paid(ref2, 6999, currency="NGN")
    nia.get(f"/billing/callback?reference={ref2}")
    assert _balance(pay_app, nia.uid) == 0


def test_abandoned_payment_is_marked_failed(pay_app, nia, fake):
    ref = _checkout(nia, "single")
    r = nia.get(f"/billing/callback?reference={ref}", follow_redirects=True)
    assert "didn&#39;t go through" in r.get_data(as_text=True)
    assert _payment(pay_app, reference=ref)["status"] == "failed" and _balance(pay_app, nia.uid) == 0


def test_someone_elses_reference_is_404(pay_app, nia, fake):
    ref = _checkout(nia, "single")
    with pay_app.app_context():
        create_user("eve@example.com", "Eve", "password123", phone="+254700000001")
    eve = Session(pay_app)
    eve.login("eve@example.com")
    assert eve.get(f"/billing/callback?reference={ref}").status_code == 404


def test_paystack_errors_are_shown_plainly(pay_app, nia, fake):
    fake.fail_next = "Invalid key"
    r = nia.post("/billing/checkout/single", follow_redirects=True)
    assert "Invalid key" in r.get_data(as_text=True)


def test_without_keys_buying_is_off_but_admins_can_grant(client, users):
    s = Session(client.application)
    s.login("bob@example.com")
    r = s.post("/billing/checkout/single", follow_redirects=True)
    assert "Payments aren&#39;t set up yet" in r.get_data(as_text=True)
    assert "Online payments aren't switched on yet" in s.get("/billing/").get_data(as_text=True)


# ---- webhooks --------------------------------------------------------------------------

def test_webhook_rejects_bad_signatures_and_needs_no_csrf(pay_app, nia, fake):
    ref = _checkout(nia, "pack10")
    fake.paid(ref, 3999)
    assert _webhook(pay_app, "charge.success", fake.transactions[ref], signature="nope").status_code == 401
    assert _balance(pay_app, nia.uid) == 0
    assert _webhook(pay_app, "charge.success", fake.transactions[ref]).status_code == 200  # no CSRF token needed
    assert _balance(pay_app, nia.uid) == 10


def test_webhook_alone_completes_a_payment_if_the_customer_never_returns(pay_app, nia, fake):
    ref = _checkout(nia, "pack20")
    fake.paid(ref, 6999)
    _webhook(pay_app, "charge.success", fake.transactions[ref])
    _webhook(pay_app, "charge.success", fake.transactions[ref])  # Paystack retries
    assert _balance(pay_app, nia.uid) == 20


# ---- monthly plan ------------------------------------------------------------------------

def _subscribe(pay_app, nia, fake):
    ref = _checkout(nia, "monthly")
    call = fake.calls[-1]["payload"]
    assert call["plan"] == PLAN_CODE and call["channels"] == ["card"] and call["amount"] == 24000
    fake.paid(ref, 24000, plan={"plan_code": PLAN_CODE})
    nia.get(f"/billing/callback?reference={ref}")
    _webhook(pay_app, "subscription.create", {
        "subscription_code": "SUB_1", "email_token": "tok_1", "status": "active", "next_payment_date": "2026-12-07T00:00:00.000Z",
        "plan": {"plan_code": PLAN_CODE}, "customer": {"email": "nia@example.com", "customer_code": "CUS_1"}})
    return ref


def _age_last_grant(app, days):
    with app.app_context():
        from app.db import execute
        then = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        execute("UPDATE subscriptions SET last_grant_at = ?", (then,))


def test_monthly_plan_grants_80_and_renewals_grant_again(pay_app, nia, fake):
    _subscribe(pay_app, nia, fake)
    assert _balance(pay_app, nia.uid) == 80
    page = nia.get("/billing/").get_data(as_text=True)
    assert "Active" in page and "Cancel plan" in page

    # Paystack's invoice for the first period must not double up
    _webhook(pay_app, "invoice.update", {"invoice_code": "INV_0", "paid": True, "status": "success", "amount": 24000,
                                         "subscription": {"subscription_code": "SUB_1"}, "customer": {"email": "nia@example.com"}})
    assert _balance(pay_app, nia.uid) == 80

    _age_last_grant(pay_app, 31)  # a month later
    renewal = {"invoice_code": "INV_1", "paid": True, "status": "success", "amount": 24000, "currency": "USD",
               "subscription": {"subscription_code": "SUB_1", "next_payment_date": "2027-01-07T00:00:00.000Z"},
               "transaction": {"reference": "renew_1"}, "customer": {"email": "nia@example.com"}}
    _webhook(pay_app, "invoice.update", renewal)
    _webhook(pay_app, "invoice.update", renewal)  # retry
    _webhook(pay_app, "charge.success", {"reference": "renew_1", "status": "success", "amount": 24000, "currency": "USD",
                                         "plan": {"plan_code": PLAN_CODE}, "customer": {"email": "nia@example.com"}})
    assert _balance(pay_app, nia.uid) == 160
    assert _payment(pay_app, reference="renew_1")["kind"] == "renewal"


def test_unused_monthly_checks_carry_over_one_month_then_expire(pay_app, nia, fake):
    _subscribe(pay_app, nia, fake)
    with pay_app.app_context():
        assert billing.balance(nia.uid, now=datetime.now(timezone.utc) + timedelta(days=45)) == 80   # carried over
        assert billing.balance(nia.uid, now=datetime.now(timezone.utc) + timedelta(days=63)) == 0    # gone after 2 months


def test_cancel_and_failed_renewal(pay_app, nia, fake):
    _subscribe(pay_app, nia, fake)
    _webhook(pay_app, "invoice.payment_failed", {"subscription": {"subscription_code": "SUB_1"}, "customer": {"email": "nia@example.com"}})
    assert "Payment failed" in nia.get("/billing/").get_data(as_text=True)
    assert any("couldn't renew" in m["subject"] for m in pay_app.config["MAIL_OUTBOX"])
    r = nia.post("/billing/subscription/cancel", follow_redirects=True)
    assert fake.calls[-1]["payload"] == {"code": "SUB_1", "token": "tok_1"}
    assert "Monthly plan cancelled" in r.get_data(as_text=True)
    assert _balance(pay_app, nia.uid) == 80  # keeps what they paid for


def test_update_card_goes_to_paystack(pay_app, nia, fake):
    _subscribe(pay_app, nia, fake)
    r = nia.post("/billing/subscription/card")
    assert r.status_code == 303 and r.headers["Location"].startswith("https://paystack.com/manage/")


def test_cannot_buy_monthly_twice(pay_app, nia, fake):
    _subscribe(pay_app, nia, fake)
    r = nia.post("/billing/checkout/monthly", follow_redirects=True)
    assert "already have the monthly plan" in r.get_data(as_text=True)


# ---- spending ----------------------------------------------------------------------------

def _docx(words):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>" + " ".join(["word"] * words)
                    + "</w:t></w:r></w:p></w:body></w:document>")
    return buf.getvalue()


def test_no_checks_no_upload_and_nothing_stored(pay_app, nia, fake, tmp_path):
    assert "You're out of checks" in nia.get("/submit").get_data(as_text=True)
    r = upload(nia, content=_docx(500), filename="essay.docx")
    assert r.status_code == 402 and "You have 0 checks" in r.get_data(as_text=True)
    with pay_app.app_context():
        from app.db import query
        assert query("SELECT COUNT(*) AS n FROM submissions", one=True)["n"] == 0
    assert not any((tmp_path / "u").rglob("*")) or all(p.is_dir() for p in (tmp_path / "u").rglob("*"))


def test_long_documents_use_one_check_per_10000_words(pay_app, nia, fake):
    ref = _checkout(nia, "single")
    fake.paid(ref, 499)
    nia.get(f"/billing/callback?reference={ref}")
    r = upload(nia, content=_docx(15000), filename="dissertation.docx")
    assert r.status_code == 402 and "15,000 words, so it needs 2 checks" in r.get_data(as_text=True)
    sid = sub_id_from(upload(nia, content=_docx(9000), filename="essay.docx"))
    assert _balance(pay_app, nia.uid) == 0
    page = nia.get(f"/submissions/{sid}").get_data(as_text=True)
    assert "9,000 words, used 1 check" in page
    hist = nia.get("/billing/").get_data(as_text=True)
    assert f"Review of CA-{sid:05d}" in hist and "Single check" in hist


def test_spends_the_soonest_expiring_checks_first(pay_app, nia, fake):
    _subscribe(pay_app, nia, fake)              # 80 expiring in 2 months
    ref = _checkout(nia, "pack10")              # 10 expiring in 12 months
    fake.paid(ref, 3999)
    nia.get(f"/billing/callback?reference={ref}")
    upload(nia, content=_docx(800), filename="a.docx")
    with pay_app.app_context():
        from app.db import query
        rows = {r["source"]: r["remaining"] for r in query("SELECT source, remaining FROM credit_grants")}
    assert rows == {"subscription": 79, "purchase": 10}


# ---- admin tools ---------------------------------------------------------------------------

def test_admin_adds_and_removes_checks_with_a_reason(pay_app, nia, fake):
    with pay_app.app_context():
        create_user("rita@example.com", "Rita", "password123", role="admin")
    admin = Session(pay_app)
    admin.login("rita@example.com")
    admin.post(f"/admin/users/{nia.uid}/checks", {"change": "5", "reason": "Paid by M-Pesa, ref QJK3"})
    assert _balance(pay_app, nia.uid) == 5
    r = admin.post(f"/admin/users/{nia.uid}/checks", {"change": "-9", "reason": "oops"}, follow_redirects=True)
    assert "only has 5 checks" in r.get_data(as_text=True) and _balance(pay_app, nia.uid) == 5
    admin.post(f"/admin/users/{nia.uid}/checks", {"change": "-2", "reason": "Correction"})
    assert _balance(pay_app, nia.uid) == 3
    assert "Paid by M-Pesa, ref QJK3" in nia.get("/billing/").get_data(as_text=True)
    page = admin.get(f"/admin/users/{nia.uid}").get_data(as_text=True)
    assert "Correction" in page and "Rita" in page
    assert admin.get("/admin/payments").status_code == 200


def test_refund_returns_checks_exactly_once(pay_app, nia, fake):
    with pay_app.app_context():
        create_user("rita@example.com", "Rita", "password123", role="admin")
        from app.db import get_db
        billing.grant(get_db(), nia.uid, 2, "admin", "Starter")
        get_db().commit()
    admin = Session(pay_app)
    admin.login("rita@example.com")
    sid = sub_id_from(upload(nia, content=_docx(300), filename="e.docx"))
    assert _balance(pay_app, nia.uid) == 1
    admin.post(f"/admin/submissions/{sid}/refund-checks")
    admin.post(f"/admin/submissions/{sid}/refund-checks")
    assert _balance(pay_app, nia.uid) == 2
    assert "refunded" in nia.get(f"/submissions/{sid}").get_data(as_text=True)


def test_customers_cannot_use_admin_billing_tools(nia):
    assert nia.post(f"/admin/users/{nia.uid}/checks", {"change": "100", "reason": "free"}).status_code == 403
    assert nia.get("/admin/payments").status_code == 403


def test_csp_allows_the_paystack_redirect_only(client):
    csp = client.get("/").headers["Content-Security-Policy"]
    assert "form-action 'self' https://checkout.paystack.com https://paystack.com" in csp


def test_signup_free_checks_setting(tmp_path):
    app = create_app({"TESTING": True, "SECRET_KEY": "t", "DATABASE": str(tmp_path / "d.sqlite3"),
                      "UPLOAD_DIR": str(tmp_path / "u"), "SIGNUP_FREE_CHECKS": 1})
    s = Session(app)
    s.post("/register?next=/billing/", {"name": "Zed", "email": "zed@example.com", "phone": "+254700000009",
                                         "password": "longenough", "confirm": "longenough"})
    page = s.get("/billing/").get_data(as_text=True)
    assert "Welcome checks: <strong>1</strong> left" in page


def test_paystack_create_plan_command(pay_app, fake):
    result = pay_app.test_cli_runner().invoke(args=["paystack-create-plan"])
    assert "PAYSTACK_MONTHLY_PLAN_CODE=PLN_monthly" in result.output
    assert fake.calls[-1]["payload"] == {"name": "NerdAccess Monthly (80 checks, USD)", "interval": "monthly",
                                         "amount": 24000, "currency": "USD"}


def test_paystack_requests_name_the_app_not_python_urllib(pay_app, monkeypatch):
    """Cloudflare in front of api.paystack.co answers "Python-urllib" with 403 error code 1010."""
    seen = {}

    def send(method, url, headers, body):
        seen.update(headers)
        return 200, {"status": True, "data": {}}

    monkeypatch.setattr(billing, "_send", send)
    with pay_app.test_request_context():
        billing.paystack("GET", "/transaction/verify/x")
    assert seen["User-Agent"] == "NerdAccess/1.0 (+https://checkassist.test)"


def test_a_firewall_page_instead_of_paystack_is_logged_and_explained(pay_app, monkeypatch, caplog):
    monkeypatch.setattr(billing, "_send", lambda *a: (403, {"_raw": "error code: 1010"}))
    with pay_app.test_request_context(), pytest.raises(billing.PaymentError) as e:
        billing.paystack("POST", "/transaction/initialize", {})
    assert "HTTP 403" in str(e.value)
    assert "error code: 1010" in caplog.text


def test_send_survives_a_non_json_answer(monkeypatch):
    class Resp:
        status = 403
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"error code: 1010"
    monkeypatch.setattr(billing.urllib.request, "urlopen", lambda req, timeout: Resp())
    assert billing._send("GET", "https://api.paystack.co/x", {}, None) == (403, {"_raw": "error code: 1010"})
