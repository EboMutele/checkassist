"""The mobile number field: country picker + number, read the way people actually type numbers."""
import json
import pathlib
import re

import pytest

from app import phone
from tests.conftest import Session

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("raw, picked, expected, country", [
    # Kenya: with or without the trunk 0, with the code typed without +, with "+254 0…"
    ("0712 345 678", "KE", "+254712345678", "KE"),
    ("712345678", "KE", "+254712345678", "KE"),
    ("254712345678", "KE", "+254712345678", "KE"),
    ("+254 0712 345 678", "KE", "+254712345678", "KE"),
    ("0110 123 456", "KE", "+254110123456", "KE"),
    # UK and US, as people write them
    ("07700 900123", "GB", "+447700900123", "GB"),
    ("+44 (0)7700 900123", "GB", "+447700900123", "GB"),
    ("(201) 555-0123", "US", "+12015550123", "US"),
    ("1 201 555 0123", "US", "+12015550123", "US"),
    # a full international number wins over the picker, and lands on the right country for shared codes
    ("+44 7700 900123", "KE", "+447700900123", "GB"),
    ("0044 7700 900123", "US", "+447700900123", "GB"),
    ("+1 416 555 0134", "US", "+14165550134", "CA"),
    ("+1 876 555 0134", "KE", "+18765550134", "JM"),
    ("+7 771 000 9998", "RU", "+77710009998", "KZ"),
    # trunk prefixes and carrier codes from libphonenumber
    ("8 912 345 67 89", "RU", "+79123456789", "RU"),
    ("011 15 2345 6789", "AR", "+5491123456789", "AR"),
    ("0151 23456789", "DE", "+4915123456789", "DE"),
])
def test_numbers_are_read_like_people_type_them(raw, picked, expected, country):
    number, found, error = phone.parse(raw, picked)
    assert (number, found["iso"], error) == (expected, country, None)


@pytest.mark.parametrize("raw, picked, message", [
    ("071234567", "KE", "Mobile numbers there have 9 digits after +254. For example +254 712 123456."),
    ("07123456789", "KE", "doesn't look like a mobile number in Kenya"),
    ("201-555-012", "US", "10 digits after +1"),
    ("+999 123", "KE", "country code isn't one we recognise"),
    ("call me", "KE", "Use only digits"),
    ("", "GB", "Enter your mobile number"),
])
def test_mistakes_get_a_specific_message(raw, picked, message):
    number, _, error = phone.parse(raw, picked)
    assert number is None and message in error


def test_stored_numbers_split_back_into_country_and_number():
    assert [(c["iso"], n) for c, n in map(phone.split, ["+254712345678", "+447700900123", "+14165550134"])] == [
        ("KE", "712345678"), ("GB", "7700900123"), ("CA", "4165550134")]


def test_country_data_is_complete_and_its_patterns_compile():
    data = phone.countries()
    assert len(data) > 230 and len({c["iso"] for c in data}) == len(data)
    for c in data:
        assert c["dial"].isdigit() and c["flag"] and c["name"]
        for key in ("strip", "pattern", "leading"):
            re.compile(c[key])
    for dial in {c["dial"] for c in data}:
        assert sum(c["main"] for c in data if c["dial"] == dial) == 1, dial
    zones = json.loads(re.search(r"=\s*(\{.*\});", (ROOT / "app/static/phone-zones.js").read_text()).group(1))
    assert zones["Africa/Nairobi"] == "KE" and zones["Europe/London"] == "GB" and zones["America/Chicago"] == "US"
    assert zones["Asia/Calcutta"] == "IN"  # old name Chrome still reports


def _selected(html):
    return re.search(r'<option value="([A-Z]{2})"[^>]*\sselected>', html).group(1)


def test_picker_preselects_from_the_browser_and_lets_time_zone_correct_a_guess(app, client):
    page = client.get("/register", headers={"Accept-Language": "en-GB,en;q=0.8"}).get_data(as_text=True)
    assert _selected(page) == "GB" and "data-guess" in page and 'placeholder="7400 123456"' in page
    page = client.get("/register", headers={"Accept-Language": "fr"}).get_data(as_text=True)
    assert _selected(page) == "US" and "data-guess" in page
    assert "phone-zones.js" in page and "phone.js" in page and "noscript.css" in page


def test_signup_with_picker(app):
    s = Session(app)
    s.get("/register")
    r = s.post("/register", {"name": "Ama", "email": "ama@example.com", "phone_country": "GB", "phone": "07700 900123",
                             "password": "password123", "confirm": "password123"})
    assert r.status_code == 302
    with app.app_context():
        from app.db import query
        assert query("SELECT phone FROM users WHERE email = 'ama@example.com'", one=True)["phone"] == "+447700900123"


def test_a_rejected_number_keeps_what_was_typed_and_picked(app):
    s = Session(app)
    s.get("/register")
    r = s.post("/register", {"name": "Ama", "email": "ama@example.com", "phone_country": "NG", "phone": "0802 123",
                             "password": "password123", "confirm": "password123"})
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "mobile number in Nigeria" in page
    assert _selected(page) == "NG" and 'value="0802 123"' in page and "data-guess" not in page


def test_account_page_shows_the_saved_number_in_the_picker(app, users, alice):
    page = alice.get("/account").get_data(as_text=True)
    assert _selected(page) == "KE" and 'value="712345678"' in page and "data-guess" not in page
    assert alice.post("/account", {"name": "Alice Doe", "phone_country": "US", "phone": "+44 7700 900123"}).status_code == 302
    page = alice.get("/account").get_data(as_text=True)
    assert _selected(page) == "GB" and 'value="7700900123"' in page
