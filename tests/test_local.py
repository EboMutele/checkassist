"""The private Kenya link: KES prices, M-Pesa through Paystack, Kenyan numbers, kept out of search."""
import json
import re
from decimal import Decimal

import pytest

from conftest import Session
from test_billing import FakePaystack, SECRET, _payment, _sign, _webhook, fake, nia, pay_app  # noqa: F401
from test_seo import parse

from app import billing, create_app, pricing, seo
from app.db import create_user, query

KES_PLAN = "PLN_kes"
NBSP = " "


@pytest.fixture
def kes_app(pay_app):
    pay_app.config.update(PAYSTACK_MONTHLY_PLAN_CODE_KES=KES_PLAN, ALLOW_INDEXING=True, DEFAULT_COUNTRY_CODE="")
    return pay_app


def _register(s, email="wanjiru@example.com", phone="0712 345 678"):
    return s.post("/register", {"name": "Wanjiru M", "email": email, "phone": phone,
                                "password": "password123", "confirm": "password123"})


def _user(app, email):
    with app.app_context():
        return query("SELECT * FROM users WHERE email = ?", (email,), one=True)


def _kes_customer(app):
    s = Session(app)
    s.get("/local")
    assert _register(s).status_code == 302
    s.uid = _user(app, "wanjiru@example.com")["id"]
    return s


# ---- private, but shareable ---------------------------------------------------------------

def test_link_stays_out_of_search(kes_app):
    c = kes_app.test_client()
    resp = c.get("/local")
    assert resp.status_code == 200
    assert resp.headers["X-Robots-Tag"] == "noindex"
    h = parse(resp)
    assert h.meta["robots"] == "noindex" and "canonical" not in h.links and not h.jsonld
    assert "/local" not in c.get("/sitemap.xml").get_data(as_text=True)
    assert "local" not in c.get("/robots.txt").get_data(as_text=True)  # robots.txt is public: don't advertise it
    for path in [p.path for p in seo.PAGES] + ["/login", "/register"]:
        assert "/local" not in c.get(path).get_data(as_text=True), path


def test_link_has_a_whatsapp_preview_in_shillings(kes_app):
    h = parse(kes_app.test_client().get("/local"))
    assert h.meta["og:url"] == "https://checkassist.test/local"
    assert h.meta["og:image"].endswith("/static/og.png")
    assert f"KSh{NBSP}650" in h.meta["og:description"] and "M-Pesa" in h.meta["og:description"]


# ---- who pays in shillings ------------------------------------------------------------------

def test_signing_up_through_the_link_prices_the_account_in_kes(kes_app):
    s = Session(kes_app)
    page = s.get("/local").get_data(as_text=True)
    assert f"KSh{NBSP}650" in page and "$4.99" not in page
    form = s.get("/register").get_data(as_text=True)
    assert "Prices in Kenyan shillings" in form and 'placeholder="0712 345 678"' in form
    _register(s)
    user = _user(kes_app, "wanjiru@example.com")
    assert user["currency"] == "KES" and user["signup_source"] == "local"
    assert user["phone"] == "+254712345678"  # a local number works without +254
    billing_page = s.get("/billing/").get_data(as_text=True)
    assert f"KSh{NBSP}5,200" in billing_page and "Prices in Kenyan shillings" in billing_page


def test_signing_up_on_the_public_site_stays_in_usd(kes_app):
    s = Session(kes_app)
    s.get("/")
    assert "Prices in Kenyan shillings" not in s.get("/register").get_data(as_text=True)
    assert _register(s, phone="0712 345 678").status_code == 200  # no country assumed for global visitors
    assert _user(kes_app, "wanjiru@example.com") is None
    _register(s, phone="+1 415 555 0134")
    user = _user(kes_app, "wanjiru@example.com")
    assert user["currency"] == "USD" and user["signup_source"] is None


def test_public_pages_show_shillings_to_link_visitors_but_search_engines_see_dollars(kes_app):
    visitor, stranger = Session(kes_app), Session(kes_app)
    visitor.get("/local")
    for path in ("/pricing", "/essay-review", "/"):
        resp = visitor.get(path)
        body = resp.get_data(as_text=True).split("</head>", 1)[1]
        assert f"KSh{NBSP}650" in body and "$" not in body, path  # every visible price, not just the rate card
        assert "$4.99" in parse(resp).meta["description"], path  # the search snippet stays in dollars
        assert "KSh" not in stranger.get(path).get_data(as_text=True), path
    graph = parse(visitor.get("/pricing")).jsonld[0]["@graph"]
    offers = next(n for n in graph if n["@type"] == "Service")["hasOfferCatalog"]["itemListElement"]
    assert {o["priceCurrency"] for o in offers} == {"USD"}


def test_signed_in_customer_can_switch_to_shillings(kes_app, nia, fake):
    page = nia.get("/local").get_data(as_text=True)
    assert "Use shilling prices on my account" in page
    resp = nia.post("/local/use-shillings")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/billing/")
    assert _user(kes_app, "nia@example.com")["currency"] == "KES"
    assert "Use shilling prices" not in nia.get("/local").get_data(as_text=True)


# ---- paying in KES ----------------------------------------------------------------------------

def test_kes_checkout_charges_shillings_and_grants_on_a_matching_payment(kes_app, fake):
    s = _kes_customer(kes_app)
    r = s.post("/billing/checkout/pack10")
    assert r.status_code == 303
    call = fake.calls[-1]["payload"]
    assert call["currency"] == "KES" and call["amount"] == 520000  # KSh 5,200 in cents
    ref = call["reference"]
    assert _payment(kes_app, reference=ref)["currency"] == "KES"

    fake.paid(ref, 3999, "USD")  # the dollar price must not buy the shilling pack
    s.get(f"/billing/callback?reference={ref}")
    with kes_app.app_context():
        assert billing.balance(s.uid) == 0
    fake.paid(ref, 520000, "KES", channel="mobile_money")
    s.get(f"/billing/callback?reference={ref}")
    with kes_app.app_context():
        assert billing.balance(s.uid) == 10
    assert _payment(kes_app, reference=ref)["channel"] == "mobile_money"


def test_kes_monthly_uses_its_own_plan_and_renews(kes_app, fake):
    kes_app.config["PAYSTACK_MONTHLY_PLAN_CODE_KES"] = ""
    s = _kes_customer(kes_app)
    r = s.post("/billing/checkout/monthly")
    assert r.status_code == 302 and not any(c["url"].endswith("/transaction/initialize") for c in fake.calls)

    kes_app.config["PAYSTACK_MONTHLY_PLAN_CODE_KES"] = KES_PLAN
    s.post("/billing/checkout/monthly")
    call = fake.calls[-1]["payload"]
    assert call["plan"] == KES_PLAN and call["currency"] == "KES" and call["amount"] == 3120000
    assert call["channels"] == ["card"]
    fake.paid(call["reference"], 3120000, "KES", plan={"plan_code": KES_PLAN},
              customer={"email": "wanjiru@example.com"})
    s.get(f"/billing/callback?reference={call['reference']}")
    _webhook(kes_app, "subscription.create", {
        "subscription_code": "SUB_K", "email_token": "tok_k", "plan": {"plan_code": KES_PLAN},
        "customer": {"email": "wanjiru@example.com", "customer_code": "CUS_K"}})
    with kes_app.app_context():
        assert billing.balance(s.uid) == 80
        from app.db import execute
        execute("UPDATE subscriptions SET last_grant_at = '2026-01-01T00:00:00Z'")
    _webhook(kes_app, "charge.success", {"reference": "renew_k1", "status": "success", "amount": 3120000,
                                          "currency": "KES", "plan": {"plan_code": KES_PLAN},
                                          "customer": {"email": "wanjiru@example.com"}})
    with kes_app.app_context():
        assert billing.balance(s.uid) == 160
    renewal = _payment(kes_app, reference="renew_k1")
    assert renewal["currency"] == "KES" and renewal["amount"] == 3120000


def test_create_kes_plan_command(kes_app, fake):
    result = kes_app.test_cli_runner().invoke(args=["paystack-create-plan", "--currency", "KES"])
    assert "PAYSTACK_MONTHLY_PLAN_CODE_KES=" in result.output
    payload = fake.calls[-1]["payload"]
    assert payload["currency"] == "KES" and payload["amount"] == 3120000


# ---- admin ------------------------------------------------------------------------------------

def test_admin_can_price_a_direct_client_in_kes(kes_app, nia):
    with kes_app.app_context():
        create_user("rita@example.com", "Rita", "password123", role="admin")
    admin = Session(kes_app)
    admin.login("rita@example.com")
    page = admin.get(f"/admin/users/{nia.uid}").get_data(as_text=True)
    assert "Kenyan shillings (M-Pesa)" in page
    assert admin.post(f"/admin/users/{nia.uid}/currency", {"currency": "KES"}).status_code == 302
    assert _user(kes_app, "nia@example.com")["currency"] == "KES"
    assert admin.post(f"/admin/users/{nia.uid}/currency", {"currency": "EUR"}).status_code == 400
    assert nia.post(f"/admin/users/{nia.uid}/currency", {"currency": "USD"}).status_code in (302, 403)
    assert _user(kes_app, "nia@example.com")["currency"] == "KES"

    _kes_customer(kes_app)
    payments = admin.get("/admin/payments").get_data(as_text=True)
    assert "Kenya link <code>/local</code>: 1 sign-up, 2 accounts pay in KES" in payments


def test_link_can_be_moved_or_switched_off(tmp_path):
    def app_with(path):
        return create_app({"TESTING": True, "SECRET_KEY": "t", "DATABASE": str(tmp_path / f"{path or 'off'}.db"),
                           "UPLOAD_DIR": str(tmp_path / "u"), "LOCAL_LINK_PATH": path})
    moved = app_with("/karibu").test_client()
    assert moved.get("/karibu").status_code == 200 and moved.get("/local").status_code == 404
    off = app_with("").test_client()
    assert off.get("/local").status_code == 404 and off.get("/pricing").status_code == 200


# ---- prices ---------------------------------------------------------------------------------

def test_kes_prices_make_sense_and_match_the_dollar_prices():
    """Parity on purpose: a forwarded link shouldn't be a discount. Change this test if you decide to
    price Kenya lower, and then expect to move the link (LOCAL_LINK_PATH) when it spreads."""
    assert pricing.ladder_problems(pricing.plans("KES")) == []
    for usd, kes in zip(pricing.plans("USD"), pricing.plans("KES")):
        assert abs(kes.price / (usd.price * Decimal("129")) - 1) < Decimal("0.03"), usd.id
    assert pricing.money(Decimal("5200"), "KES") == f"KSh{NBSP}5,200"
