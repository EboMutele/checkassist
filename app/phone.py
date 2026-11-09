"""The mobile number field: a country picker plus the number, read in whatever form people type it.

Country data comes from Google's libphonenumber (dialling code, trunk prefix, mobile number lengths,
an example number) via tools/build_phone_countries.py.
"""
import json
import pathlib
import re
from functools import lru_cache

from flask import current_app, g, has_request_context, request

from . import local

_JUNK = re.compile(r"[^\d+]")
_ALLOWED = re.compile(r"^[\d\s+().\-/]*$")


@lru_cache(maxsize=1)
def _data():
    countries = json.loads((pathlib.Path(__file__).parent / "phone_countries.json").read_text())
    by_iso = {c["iso"]: c for c in countries}
    by_dial = {}
    for c in countries:
        by_dial.setdefault(c["dial"], []).append(c)
    return countries, by_iso, by_dial


def countries():
    return _data()[0]


def country(iso):
    return _data()[1].get((iso or "").upper())


def _for_number(dial, national):
    """The country an international number belongs to (+1 416… is Canada, +1 212… the US)."""
    group = sorted(_data()[2].get(dial) or [], key=lambda c: not c["main"])  # main country first, like libphonenumber
    for c in group:
        if (c["leading"] and re.match(c["leading"], national)) or (not c["leading"] and c["pattern"]
                                                                   and re.fullmatch(c["pattern"], national)):
            return c
    return group[0] if group else None


def _split_international(digits):
    for size in (1, 2, 3):
        dial, national = digits[:size], digits[size:]
        if dial in _data()[2]:
            return _for_number(dial, national), national
    return None, ""


def _candidates(c, digits):
    """National numbers the typed digits could mean, most literal first."""
    yield digits
    if c["strip"]:
        m = re.match(c["strip"], digits)
        if m and m.end():
            rest = digits[m.end():]
            if c["transform"] and m.lastindex:
                yield m.expand(c["transform"].replace("$", "\\")) + rest
            yield rest
    if c["prefix"] and digits.startswith(c["prefix"]):
        yield digits[len(c["prefix"]):]
    if digits.startswith(c["dial"]):  # typed the country code without the +
        yield from _candidates(c, digits[len(c["dial"]):]) if digits != c["dial"] else ()


def _fits(c, national):
    if not national.isdigit() or (c["prefix"] and national.startswith(c["prefix"])):
        return False  # a mobile number never starts with the trunk prefix (the 0 in 0712…)
    return len(national) in c["lengths"] if c["lengths"] else 4 <= len(national) <= 14


def lengths_text(c):
    n = c["lengths"]
    if not n:
        return ""
    if len(n) == 1:
        return f"{n[0]} digits"
    if n == list(range(n[0], n[-1] + 1)):
        return f"{n[0]} to {n[-1]} digits"
    return ", ".join(map(str, n[:-1])) + f" or {n[-1]} digits"


def parse(raw, iso):
    """Read what someone typed in the number box, with the country picker set to `iso`.

    Returns (e164, country, error). "+44 7700 900123" or "0044…" is taken as written whatever
    the picker says; "0712 345 678", "712 345 678" and "254712345678" all work with Kenya picked.
    """
    raw = (raw or "").strip()[:40]
    c = country(iso) or country("US")
    if not raw:
        return None, c, "Enter your mobile number so we can text you when your feedback is ready."
    if not _ALLOWED.match(raw):
        return None, c, "Use only digits in your mobile number (spaces and dashes are fine)."
    digits = _JUNK.sub("", raw.replace("(0)", ""))
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("+"):
        found, national = _split_international(digits[1:].replace("+", ""))
        if not found:
            return None, c, "That country code isn't one we recognise. Pick your country and type the number without it."
        c = found
        candidates = list(_candidates(c, national))
    else:
        candidates = list(_candidates(c, digits.replace("+", "")))
    for national in candidates:
        if _fits(c, national):
            e164 = f"+{c['dial']}{national}"
            if len(e164) <= 16:
                return e164, c, None
    example = f" For example +{c['dial']} {c['example']}." if c["example"] else ""
    size = f" Mobile numbers there have {lengths_text(c)} after +{c['dial']}." if c["lengths"] else ""
    return None, c, f"That doesn't look like a mobile number in {c['name']}.{size}{example}"


def split(e164):
    """(country, national digits) for a stored +E.164 number, to fill the form back in."""
    if not e164 or not e164.startswith("+"):
        return None, ""
    return _split_international(e164[1:])


def _from_language():
    for lang, _ in request.accept_languages:
        parts = re.split(r"[-_]", lang)
        if len(parts) > 1 and country(parts[-1]):
            return parts[-1].upper()
    return None


def preselect(user=None, submitted=None):
    """(iso, is_guess): the country to show in the picker. is_guess lets phone.js correct it from the
    browser's time zone, which says more about where someone is than their browser's language."""
    if submitted and country(submitted):
        return submitted.upper(), False
    if user is None and has_request_context():
        user = getattr(g, "user", None)
    if user is not None and user["phone"]:
        found, _ = split(user["phone"])
        if found:
            return found["iso"], False
    if local.phone_country_code(user) == local.KENYA_COUNTRY_CODE:
        return "KE", user is None and not local.came_through_link()
    if has_request_context():
        from_language = _from_language()
        if from_language:
            return from_language, True
    default = current_app.config.get("DEFAULT_COUNTRY_CODE") or ""
    for c in _data()[2].get(default, []):
        if c["main"]:
            return c["iso"], True
    return "US", True


def field(form_phone="", user=None, submitted=None):
    """What _phone_field.html needs."""
    iso, guess = preselect(user, submitted)
    number = form_phone
    if not submitted and user is not None and user["phone"] and form_phone == user["phone"]:
        number = split(user["phone"])[1]
    return {"selected": iso, "guess": guess, "number": number, "countries": countries(), "chosen": country(iso)}
