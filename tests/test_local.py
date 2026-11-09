"""The private Kenya link: local KES prices, M-Pesa through Paystack, Kenyan numbers, kept out of search."""
import re
from decimal import Decimal

import pytest

from conftest import Session
from test_billing import FakePaystack, SECRET, _payment, _sign, _webhook, fake, nia, pay_app  # noqa: F401
from test_seo import parse

from app import billing, create_app, pricing, seo
from app.db import create_user, query

NBSP = " "


@pytest.fixture
def kes_app(pay_app):
    pay_app.config.update(ALLOW_INDEXING=True, DEFAULT_COUNTRY_CODE="")
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


# ---- the local price list -------------------------------------------------------------------

def test_local_prices_are_the_ones_you_set():
    kes = pricing.plans("KES")
    assert [(p.id, p.checks, p.price) for p in kes] == [
        ("single", 1, Decimal("199")), ("pack5", 5, Decimal("900")), ("pack10", 10, Decimal("1700"))]
    assert [pricing.money(p.per_check, "KES") for p in kes] == [f"KSh{NBSP}199", f"KSh{NBSP}180", f"KSh{NBSP}170"]
    assert pricing.ladder_problems(kes) == []
    assert not pricing.has_monthly("KES") and not pricing.has_monthly("USD")
    assert [p.id for p in pricing.PLANS] == ["single", "pack10", "pack20", "pack80"]  # public prices untouched
    assert pricing.best_value("KES").id == "pack10"


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
    stranger = kes_app.test_client()
    for path in [p.path for p in seo.PAGES] + ["/login", "/register"]:
        html = stranger.get(path).get_data(as_text=True)
        assert "/local" not in html and "KSh" not in html, path


def test_link_has_a_whatsapp_preview_in_shillings(kes_app):
    h = parse(kes_app.test_client().get("/local"))
    assert h.meta["og:url"] == "https://checkassist.test/local"
    assert h.meta["og:image"].endswith("/static/og.png")
    assert f"KSh{NBSP}199" in h.meta["og:description"] and "M-Pesa" in h.meta["og:description"]


def test_local_page_shows_singles_and_packs_only(kes_app):
    html = kes_app.test_client().get("/local").get_data(as_text=True)
    for price in ("199", "900", "1,700"):
        assert f"KSh{NBSP}{price}" in html
    assert "monthly" not in html.lower() and "$" not in html.split("</head>", 1)[1]


# ---- who pays local prices ------------------------------------------------------------------

def test_signing_up_through_the_link_prices_the_account_in_kes(kes_app):
    s = Session(kes_app)
    s.get("/local")
    form = s.get("/register").get_data(as_text=True)
    assert "Kenyan shillings" not in form and "KSh" not in form  # no pricing talk on the sign-up form itself
    assert '<option value="KE" data-dial="254"' in form and re.search(r'value="KE"[^>]* selected>', form)
    assert 'data-guess' not in form  # the link says Kenya; the browser's time zone doesn't override it
    _register(s)
    user = _user(kes_app, "wanjiru@example.com")
    assert user["currency"] == "KES" and user["signup_source"] == "local"
    assert user["phone"] == "+254712345678"  # a local number works without +254
    page = s.get("/billing/").get_data(as_text=True)
    assert f"KSh{NBSP}900" in page and "Buy 5 checks" in page
    assert "Monthly plan" not in page and "Start monthly" not in page


def test_signing_up_on_the_public_site_stays_in_usd(kes_app):
    s = Session(kes_app)
    s.get("/")
    assert "Kenyan shillings" not in s.get("/register").get_data(as_text=True)
    assert _register(s, phone="0712 345 678").status_code == 200  # no country assumed for global visitors
    assert _user(kes_app, "wanjiru@example.com") is None
    _register(s, phone="+1 415 555 0134")
    user = _user(kes_app, "wanjiru@example.com")
    assert user["currency"] == "USD" and user["signup_source"] is None


def test_local_prices_stay_on_the_local_side(kes_app):
    visitor, stranger = Session(kes_app), Session(kes_app)
    visitor.get("/local")
    # /pricing is public and indexable, so local visitors are sent back to the private page instead
    resp = visitor.get("/pricing")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/local")
    assert stranger.get("/pricing").status_code == 200
    # elsewhere, visible prices follow the visitor; search snippets and structured data stay USD
    for path in ("/essay-review", "/proofreading", "/"):
        resp = visitor.get(path)
        body = resp.get_data(as_text=True).split("</head>", 1)[1]
        assert f"KSh{NBSP}199" in body and "$" not in body and "monthly" not in body.lower(), path
        assert "KSh" not in parse(resp).meta["description"], path
        assert "KSh" not in stranger.get(path).get_data(as_text=True), path
    assert "$4.99" in parse(visitor.get("/essay-review")).meta["description"]
    graph = parse(stranger.get("/pricing")).jsonld[0]["@graph"]
    offers = next(n for n in graph if n["@type"] == "Service")["hasOfferCatalog"]["itemListElement"]
    assert {o["priceCurrency"] for o in offers} == {"USD"}


def test_local_customers_pricing_link_goes_to_their_checks_page(kes_app):
    s = _kes_customer(kes_app)
    resp = s.get("/pricing")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/billing/")


def test_signed_in_customer_can_switch_to_local_prices(kes_app, nia, fake):
    assert "Use shilling prices on my account" in nia.get("/local").get_data(as_text=True)
    resp = nia.post("/local/use-shillings")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/billing/")
    assert _user(kes_app, "nia@example.com")["currency"] == "KES"
    assert "Use shilling prices" not in nia.get("/local").get_data(as_text=True)


# ---- paying in KES ----------------------------------------------------------------------------

def test_kes_checkout_charges_shillings_and_grants_on_a_matching_payment(kes_app, fake):
    s = _kes_customer(kes_app)
    assert s.post("/billing/checkout/pack5").status_code == 303
    call = fake.calls[-1]["payload"]
    assert call["currency"] == "KES" and call["amount"] == 90000  # KSh 900 in cents
    ref = call["reference"]
    assert _payment(kes_app, reference=ref)["currency"] == "KES"

    fake.paid(ref, 900, "USD")  # the same number in another currency must not buy the pack
    s.get(f"/billing/callback?reference={ref}")
    with kes_app.app_context():
        assert billing.balance(s.uid) == 0
    fake.paid(ref, 90000, "KES", channel="mobile_money")
    s.get(f"/billing/callback?reference={ref}")
    with kes_app.app_context():
        assert billing.balance(s.uid) == 5
    assert _payment(kes_app, reference=ref)["channel"] == "mobile_money"


def test_local_accounts_cant_buy_public_plans(kes_app, fake):
    s = _kes_customer(kes_app)
    for plan_id in ("monthly", "pack20"):
        assert s.post(f"/billing/checkout/{plan_id}").status_code == 404
    assert not any(c["url"].endswith("/transaction/initialize") for c in fake.calls)


def test_public_accounts_cant_buy_local_packs(kes_app, nia, fake):
    assert nia.post("/billing/checkout/pack5").status_code == 404
    nia.post("/billing/checkout/single")
    assert fake.calls[-1]["payload"]["amount"] == 499 and fake.calls[-1]["payload"]["currency"] == "USD"


def test_monthly_plan_command_is_usd_only(kes_app, fake):
    result = kes_app.test_cli_runner().invoke(args=["paystack-create-plan", "--currency", "KES"])
    assert result.exit_code != 0 and not fake.calls


# ---- admin ------------------------------------------------------------------------------------

def test_admin_can_put_a_direct_client_on_local_prices(kes_app, nia):
    with kes_app.app_context():
        create_user("rita@example.com", "Rita", "password123", role="admin")
    admin = Session(kes_app)
    admin.login("rita@example.com")
    assert "Kenyan shillings (M-Pesa)" in admin.get(f"/admin/users/{nia.uid}").get_data(as_text=True)
    assert admin.post(f"/admin/users/{nia.uid}/currency", {"currency": "KES"}).status_code == 302
    assert _user(kes_app, "nia@example.com")["currency"] == "KES"
    assert admin.post(f"/admin/users/{nia.uid}/currency", {"currency": "EUR"}).status_code == 400
    assert nia.post(f"/admin/users/{nia.uid}/currency", {"currency": "USD"}).status_code in (302, 403)
    assert _user(kes_app, "nia@example.com")["currency"] == "KES"

    _kes_customer(kes_app)
    payments = admin.get("/admin/payments").get_data(as_text=True)
    assert "Kenya link <code>/local</code>: 1 sign-up, 2 accounts pay in KES" in payments
    assert "KES monthly plan" not in payments


def test_link_can_be_moved_or_switched_off(tmp_path):
    def app_with(path):
        return create_app({"TESTING": True, "SECRET_KEY": "t", "DATABASE": str(tmp_path / f"{path.strip('/') or 'off'}.db"),
                           "UPLOAD_DIR": str(tmp_path / "u"), "LOCAL_LINK_PATH": path})
    moved = app_with("/karibu").test_client()
    assert moved.get("/karibu").status_code == 200 and moved.get("/local").status_code == 404
    off = app_with("").test_client()
    assert off.get("/local").status_code == 404 and off.get("/pricing").status_code == 200
