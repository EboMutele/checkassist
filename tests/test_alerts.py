"""Instant alerts to the business: an email and a text for every sign-up, new order and payment."""
import html

import pytest

from conftest import sub_id_from, upload
from test_billing import (_age_last_grant, _checkout, _subscribe, _webhook, fake, nia, pay_app)  # noqa: F401

from app import alerts
from app.db import query

OWNERS = "owner@example.com\npartner@example.com"
OWNER_PHONE = "+254722000111"


def set_alerts(app, emails=OWNERS, phones=OWNER_PHONE, events=("order", "payment")):
    """Put the same people on the given events' lists; the other lists stay empty."""
    with app.app_context():
        e, _ = alerts.parse_emails(emails)
        p, _ = alerts.parse_phones(phones)
        alerts.save_recipients({ev: (e, p) if ev in events else ([], []) for ev, _ in alerts.EVENTS})


def form(**lists):
    """The Messages page form: form(order=("a@x.com", "07…"), signup=("b@x.com", "")) and so on."""
    data = {}
    for event, _ in alerts.EVENTS:
        emails, phones = lists.get(event, ("", ""))
        data[f"emails_{event}"], data[f"phones_{event}"] = emails, phones
    return data


def to(box, address):
    return [m for m in box if m["to"] == address]


# ---- who gets them -------------------------------------------------------------------------------

def test_reading_the_lists_people_type(app):
    with app.app_context():
        assert alerts.parse_emails(" Owner@Example.com,\npartner@example.com  owner@example.com ") == (
            ["owner@example.com", "partner@example.com"], None)
        assert alerts.parse_emails("owner@example") == (None, "“owner@example” isn't an email address.")
        assert alerts.parse_emails("\n".join(f"a{i}@example.com" for i in range(6)))[1] == "Add up to 5 email addresses."
        assert alerts.parse_phones("+254 722 000 111") == (["+254722000111"], None)
        assert alerts.parse_phones("0722 000 111\n+254722000111, +44 7700 900123") == (
            ["+254722000111", "+447700900123"], None)  # local numbers are Kenyan; duplicates dropped
        numbers, error = alerts.parse_phones("12345")
        assert numbers is None and error.startswith("12345: That doesn't look like a mobile number in Kenya")
        assert alerts.parse_emails("") == ([], None) and alerts.parse_phones("") == ([], None)


def test_admin_sets_them_on_the_messages_page(app, admin):
    page = admin.get("/admin/messages").get_data(as_text=True)
    assert "Instant alerts" in page and "Off" in page
    r = admin.post("/admin/messages", {**form(order=(OWNERS, "0722 000 111"), payment=("partner@example.com", "0722 000 111"),
                                              signup=("owner@example.com", "")), "action": "save"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin/messages")
    page = admin.get("/admin/messages").get_data(as_text=True)
    assert "Alerts saved." in page and "2 emails, 1 phone" in page and "+254722000111" in page
    with app.app_context():
        assert alerts.recipients("order") == (["owner@example.com", "partner@example.com"], ["+254722000111"])
        assert alerts.recipients("payment") == (["partner@example.com"], ["+254722000111"])
        assert alerts.recipients("signup") == (["owner@example.com"], [])


def test_a_typo_is_explained_and_what_was_typed_stays(app, admin):
    set_alerts(app)
    r = admin.post("/admin/messages", form(order=(OWNERS, OWNER_PHONE), payment=("owner@example.com\npartner@gmail", "")))
    page = html.unescape(r.get_data(as_text=True))
    assert r.status_code == 400 and "Payments: “partner@gmail” isn't an email address." in page
    assert "partner@gmail</textarea>" in page
    with app.app_context():
        assert alerts.recipients("payment")[0] == ["owner@example.com", "partner@example.com"]  # unchanged


def test_customers_cannot_change_them(app, alice):
    r = alice.post("/admin/messages", form(order=("me@example.com", "")))
    assert r.status_code in (302, 403, 404)
    with app.app_context():
        assert alerts.recipients("order") == ([], [])


def test_test_button_sends_now_and_says_what_happened(app, admin):
    r = admin.post("/admin/messages", {**form(order=(OWNERS, OWNER_PHONE), payment=("partner@example.com", OWNER_PHONE),
                                              signup=("owner@example.com", "")), "action": "test"})
    page = admin.get(r.headers["Location"]).get_data(as_text=True)
    assert "Test sent to +254722000111, owner@example.com, partner@example.com" in page
    box = app.config["MAIL_OUTBOX"]
    assert [m["subject"] for m in box] == ["Test alert from NerdAccess"] * 2  # one each, not one per list
    assert "email for new orders and sign-ups, the moment" in to(box, "owner@example.com")[0]["body"]
    assert "email for new orders and payments, the moment" in to(box, "partner@example.com")[0]["body"]
    assert app.config["SMS_OUTBOX"][0] == {"to": "+254722000111", "message":
                                           "NerdAccess: test alert. You'll get a text for new orders and payments."}


def test_test_button_tells_you_texts_are_off(app, admin):
    app.config.update(SMS_OUTBOX=None, SMS_PROVIDER="")
    r = admin.post("/admin/messages", {**form(order=("owner@example.com", OWNER_PHONE)), "action": "test"})
    page = html.unescape(admin.get(r.headers["Location"]).get_data(as_text=True))
    assert "Test sent to owner@example.com. Not sent: +254722000111 (not set up)." in page
    assert "Texts are switched off on the server" in page


# ---- new orders ----------------------------------------------------------------------------------

def test_new_order_emails_and_texts_the_owners(app, alice, users):
    set_alerts(app)
    sub_id = sub_id_from(upload(alice, title="The economics of urban farming in Nairobi", instructions="Harvard please"))
    box = app.config["MAIL_OUTBOX"]
    for owner in ("owner@example.com", "partner@example.com"):
        (mail,) = to(box, owner)
        assert mail["subject"] == "New order from Alice Doe: The economics of urban farming in Nairobi"
        assert "Alice Doe placed an order" in mail["html"] and f"CA-{sub_id:05d}" in mail["html"]
        assert "alice@example.com" in mail["html"] and "+254712345678" in mail["html"] and "Harvard please" in mail["html"]
        assert f"https://example.test/admin/submissions/{sub_id}" in mail["html"]
        assert "Hi " not in mail["body"] and "alert list" in mail["body"]
    (text,) = app.config["SMS_OUTBOX"]
    assert text["to"] == OWNER_PHONE
    assert text["message"].startswith('NerdAccess: New order from Alice Doe: "The economics of urban farming in Nairobi"')
    assert text["message"].endswith(f"https://example.test/admin/submissions/{sub_id}") and len(text["message"]) <= 160


def test_reviewers_on_the_list_are_not_emailed_twice(app, alice, users):
    set_alerts(app, emails="admin@example.com\nowner@example.com")
    upload(alice)
    box = app.config["MAIL_OUTBOX"]
    assert len(to(box, "admin@example.com")) == 1  # the reviewer email only
    assert len(to(box, "owner@example.com")) == 1


def test_nobody_on_the_list_means_no_alerts(app, alice, users):
    upload(alice)
    assert not [m for m in app.config["MAIL_OUTBOX"] if m["subject"].startswith("New order")]
    assert app.config["SMS_OUTBOX"] == []


def test_a_broken_alert_never_breaks_the_upload(app, alice, users, monkeypatch):
    set_alerts(app)
    monkeypatch.setattr(alerts, "compose_email", lambda *a, **k: 1 / 0)
    r = upload(alice)
    assert r.status_code == 302 and "/submissions/" in r.headers["Location"]


def test_alerts_go_out_on_a_background_thread(app, alice, users):
    sub_id = sub_id_from(upload(alice))
    set_alerts(app)
    app.config.update(ALERTS_IN_BACKGROUND=True, MAIL_OUTBOX=[])
    with app.test_request_context():
        from app.db import create_user
        uid = create_user("zoe@example.com", "Zoë Ngũgĩ", "password123")
        customer = query("SELECT * FROM users WHERE id = ?", (uid,), one=True)
        before = query("SELECT COUNT(*) AS n FROM message_log", one=True)["n"]
        thread = alerts.new_order(sub_id, customer, "Essay", 1200, 1)
    assert thread is not None
    thread.join(timeout=10)
    assert len(app.config["MAIL_OUTBOX"]) == 2
    assert app.config["SMS_OUTBOX"][0]["message"].startswith('NerdAccess: New order from Zoe Ngugi: "Essay", 1,200 words.')
    with app.app_context():  # and the background thread recorded them, like any other message
        assert query("SELECT COUNT(*) AS n FROM message_log", one=True)["n"] == before + 3


# ---- payments ------------------------------------------------------------------------------------

def test_payment_alerts_once_however_paystack_tells_us(pay_app, nia, fake):
    set_alerts(pay_app)
    ref = _checkout(nia, "pack10")
    fake.paid(ref, 3999)
    nia.get(f"/billing/callback?reference={ref}")             # the customer comes back
    nia.get(f"/billing/callback?reference={ref}")             # and refreshes
    _webhook(pay_app, "charge.success", fake.transactions[ref])  # Paystack's webhook too
    _webhook(pay_app, "charge.success", fake.transactions[ref])  # and its retry
    box = pay_app.config["MAIL_OUTBOX"]
    (mail,) = to(box, "owner@example.com")
    assert mail["subject"] == "Payment received: $39.99 from Nia Kamau"
    assert "Nia just paid for a 10-check pack. They now have 10 checks." in mail["body"]
    assert "Paid with: card" in mail["body"] and f"Paystack reference: {ref}" in mail["body"]
    assert "nia@example.com" in mail["body"] and "https://checkassist.test/admin/payments" in mail["body"]
    assert len(to(box, "partner@example.com")) == 1
    (text,) = pay_app.config["SMS_OUTBOX"]
    assert text["message"] == ("NerdAccess: Payment $39.99 from Nia Kamau for a 10-check pack (card). "
                               "https://checkassist.test/admin/payments")


def test_kenyan_mobile_money_payment_text_is_plain_gsm(pay_app, nia, fake):
    set_alerts(pay_app)
    from app import local
    with pay_app.app_context():
        local.set_currency(nia.uid, "KES")
    ref = _checkout(nia, "pack10")
    fake.paid(ref, 170000, currency="KES", channel="mobile_money")
    _webhook(pay_app, "charge.success", fake.transactions[ref])
    (text,) = pay_app.config["SMS_OUTBOX"]
    assert text["message"].startswith("NerdAccess: Payment KSh 1,700 from Nia Kamau for a 10-check pack (mobile money).")
    assert all(ch in alerts._GSM7 for ch in text["message"])  # the no-break space after KSh is gone


def test_unpaid_and_failed_payments_send_nothing(pay_app, nia, fake):
    set_alerts(pay_app)
    ref = _checkout(nia, "pack10")
    nia.get(f"/billing/callback?reference={ref}")  # abandoned
    fake.paid(ref, 100)                              # wrong amount
    _webhook(pay_app, "charge.success", fake.transactions[ref])
    assert pay_app.config["MAIL_OUTBOX"] == [] and pay_app.config["SMS_OUTBOX"] == []


def test_monthly_renewals_alert_once(with_monthly, pay_app, nia, fake):
    set_alerts(pay_app, emails="owner@example.com", phones="")
    _subscribe(pay_app, nia, fake)
    _age_last_grant(pay_app, 31)
    renewal = {"invoice_code": "INV_1", "paid": True, "status": "success", "amount": 24000, "currency": "USD",
               "subscription": {"subscription_code": "SUB_1"}, "transaction": {"reference": "renew_1"},
               "customer": {"email": "nia@example.com"}}
    _webhook(pay_app, "invoice.update", renewal)
    _webhook(pay_app, "charge.success", {"reference": "renew_1", "status": "success", "amount": 24000, "currency": "USD",
                                         "plan": {"plan_code": "PLN_monthly"}, "customer": {"email": "nia@example.com"}})
    subjects = [m["subject"] for m in to(pay_app.config["MAIL_OUTBOX"], "owner@example.com")]
    assert subjects == ["Payment received: $240 from Nia Kamau"] * 2  # the first month, then the renewal
    assert "a monthly plan renewal" in to(pay_app.config["MAIL_OUTBOX"], "owner@example.com")[1]["body"]


# ---- one text, not three ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, plain", [
    ("“Quotes” – and ‘dashes’…", '"Quotes" - and \'dashes\'...'),
    ("Zoë Ngũgĩ", "Zoe Ngugi"),
    ("KSh 1,700", "KSh 1,700"),
    ("Essay 📚 draft", "Essay draft"),
])
def test_texts_stay_in_the_gsm_alphabet(raw, plain):
    assert alerts.gsm7(raw) == plain


def test_long_titles_are_shortened_to_fit_one_text():
    msg = alerts.fit_sms('NerdAccess: New order from Alice Doe: "', "A very long title " * 20,
                         '", 12,000 words. https://nerdaccess.store/admin/submissions/12345')
    assert len(msg) == 160 and '..."' in msg and msg.endswith("/admin/submissions/12345")


def test_line_breaks_in_a_title_stay_out_of_subjects_and_texts(app, alice, users):
    set_alerts(app)
    upload(alice, title="Essay\r\nBcc: someone@example.com")
    subjects = {m["subject"] for m in to(app.config["MAIL_OUTBOX"], "owner@example.com")}
    assert subjects == {"New order from Alice Doe: Essay Bcc: someone@example.com"}
    assert "\n" not in app.config["SMS_OUTBOX"][0]["message"]


# ---- each event has its own list ---------------------------------------------------------------------

def test_each_event_goes_only_to_its_own_list(pay_app, nia, fake):
    with pay_app.app_context():
        alerts.save_recipients({"order": (["orders@example.com"], []), "payment": (["money@example.com"], []),
                                "signup": (["people@example.com"], [])})
    ref = _checkout(nia, "pack10")
    fake.paid(ref, 3999)
    _webhook(pay_app, "charge.success", fake.transactions[ref])
    assert [m["to"] for m in pay_app.config["MAIL_OUTBOX"]] == ["money@example.com"]


def test_sign_up_alert(app, users):
    from conftest import Session
    set_alerts(app, emails="people@example.com", phones=OWNER_PHONE, events=("signup",))
    s = Session(app)
    r = s.post("/register", {"name": "Wanjiku Mwangi", "email": "wanjiku@example.com", "phone": "+254 712 000 222",
                             "password": "longenough", "confirm": "longenough"})
    assert r.status_code == 302
    (mail,) = app.config["MAIL_OUTBOX"]
    assert mail["to"] == "people@example.com" and mail["subject"] == "New sign-up: Wanjiku Mwangi"
    assert "Wanjiku Mwangi just signed up" in mail["html"] and "wanjiku@example.com" in mail["body"]
    assert "That makes 3 customers." in mail["body"]  # alice and bob, then Wanjiku
    with app.app_context():
        uid = query("SELECT id FROM users WHERE email = 'wanjiku@example.com'", one=True)["id"]
    assert f"https://example.test/admin/users/{uid}" in mail["body"]
    (text,) = app.config["SMS_OUTBOX"]
    assert text["message"] == f"NerdAccess: New sign-up: Wanjiku Mwangi. https://example.test/admin/users/{uid}"


def test_sign_ups_send_nothing_when_their_list_is_empty(app, users):
    from conftest import Session
    set_alerts(app)  # orders and payments only
    r = Session(app).post("/register", {"name": "Quiet", "email": "quiet@example.com", "phone": "+254 712 000 333",
                                        "password": "longenough", "confirm": "longenough"})
    assert r.status_code == 302  # signed up
    assert app.config["MAIL_OUTBOX"] == [] and app.config["SMS_OUTBOX"] == []


def test_lists_saved_before_the_split_still_work_for_orders_and_payments(app, admin):
    with app.app_context():
        alerts.set_setting("alert_emails", "owner@example.com")
        alerts.set_setting("alert_phones", OWNER_PHONE)
        from app.db import get_db
        get_db().commit()
        assert alerts.recipients("order") == (["owner@example.com"], [OWNER_PHONE])
        assert alerts.recipients("payment") == (["owner@example.com"], [OWNER_PHONE])
        assert alerts.recipients("signup") == ([], [])
    page = admin.get("/admin/messages").get_data(as_text=True)
    assert page.count("owner@example.com</textarea>") == 2  # shown under orders and payments, ready to edit
    admin.post("/admin/messages", form(order=("owner@example.com", ""), payment=("", ""), signup=("", "")))
    with app.app_context():
        assert alerts.recipients("payment") == ([], [])  # the old shared list is gone once saved
        assert alerts.get_setting("alert_emails", None) is None
