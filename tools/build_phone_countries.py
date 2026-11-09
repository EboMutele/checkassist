"""Build the country list behind the sign-up phone field from Google's libphonenumber metadata.

    git clone --depth 1 --filter=blob:none --sparse https://github.com/google/libphonenumber /tmp/lpn
    git -C /tmp/lpn sparse-checkout set --no-cone /resources/PhoneNumberMetadata.xml
    pip install babel          # country names and time zones; only needed here, not by the app
    python tools/build_phone_countries.py /tmp/lpn/resources/PhoneNumberMetadata.xml

Writes app/phone_countries.json (read by app/phone.py) and app/static/phone-zones.js (time zone ->
country, used by phone.js to pre-select the visitor's country). Re-run it now and then: dialling
plans change a few times a year.
"""
import json
import pathlib
import re
import sys
import xml.etree.ElementTree as ET

from babel import Locale
from babel.core import get_global

ROOT = pathlib.Path(__file__).resolve().parent.parent
TYPES = {"fixedLine", "mobile", "pager", "tollFree", "premiumRate", "sharedCost", "personalNumber", "voip", "uan",
         "voicemail"}


def clean(pattern):
    return re.sub(r"\s+", "", pattern or "")


def lengths(spec):
    """'9' -> [9]; '[8-10]' -> [8, 9, 10]; '[6-8],10' -> [6, 7, 8, 10]."""
    out = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"\[(\d+)-(\d+)\]", part)
        out.update(range(int(m[1]), int(m[2]) + 1) if m else [int(part)])
    return sorted(out)


def flag(iso):
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso)


def formats_of(territory):
    af = territory.find("availableFormats")
    found = []
    for nf in (af.findall("numberFormat") if af is not None else []):
        leading = [clean(ld.text) for ld in nf.findall("leadingDigits")]
        found.append((clean(nf.get("pattern")), leading[-1] if leading else None, nf.find("format").text))
    return found


def pretty(number, formats):
    """Format a national number the way libphonenumber would, e.g. 712123456 -> '712 123456'."""
    for pattern, leading, fmt in formats:
        if leading and not re.match(leading, number):
            continue
        m = re.fullmatch(pattern, number)
        if m:
            return re.sub(r"\$(\d)", lambda g: m.group(int(g.group(1))) or "", fmt)
    return number


def main(xml_path):
    territories = ET.parse(xml_path).getroot().find("territories")
    names = Locale("en").territories
    by_code_formats = {}
    for t in territories:
        if t.get("mainCountryForCode") == "true" or t.get("countryCode") not in by_code_formats:
            if formats_of(t):
                by_code_formats[t.get("countryCode")] = formats_of(t)

    countries = []
    for t in territories:
        iso = t.get("id")
        if not re.fullmatch(r"[A-Z]{2}", iso or "") or iso not in names:
            continue
        mobile = t.find("mobile")
        general = t.find("generalDesc")
        mobile_lengths = lengths(mobile.find("possibleLengths").get("national")) if mobile is not None and mobile.find("possibleLengths") is not None else []
        example = mobile.findtext("exampleNumber") if mobile is not None else None
        countries.append({
            "iso": iso,
            "name": names[iso],
            "flag": flag(iso),
            "dial": t.get("countryCode"),
            "prefix": t.get("nationalPrefix") or "",
            "lengths": mobile_lengths,
            # what to strip before the number proper (trunk prefix, carrier codes), and how to rewrite it
            "strip": clean(t.get("nationalPrefixForParsing")),
            "transform": t.get("nationalPrefixTransformRule") or "",
            "example": pretty(example, formats_of(t) or by_code_formats.get(t.get("countryCode"), [])) if example else "",
            "main": t.get("mainCountryForCode") == "true",
            # which numbers belong here when several countries share a dialling code (+1, +44, +7 …),
            # checked the way libphonenumber does: leadingDigits if given, else the specific number ranges
            "leading": clean(t.get("leadingDigits")),
            "pattern": "|".join(f"(?:{clean(el.findtext('nationalNumberPattern'))})" for el in t
                                if el.tag in TYPES and el.findtext("nationalNumberPattern")),
        })
    shared = {}
    for c in countries:
        shared.setdefault(c["dial"], []).append(c)
    for group in shared.values():
        if len(group) == 1:
            group[0].update(main=True, pattern="", leading="")
        elif not any(c["main"] for c in group):
            group[0]["main"] = True
    countries.sort(key=lambda c: c["name"])

    for c in countries:  # every pattern must work with Python's re, which app/phone.py uses
        for key in ("strip", "pattern", "leading"):
            if c[key]:
                re.compile(c[key])

    known = {c["iso"] for c in countries}
    zones = {tz: iso for tz, iso in get_global("zone_territories").items() if iso in known}
    # Browsers still report some old names (Chrome: Asia/Calcutta, Europe/Kiev); map them too
    for alias, canonical in get_global("zone_aliases").items():
        if canonical in zones:
            zones.setdefault(alias, zones[canonical])

    (ROOT / "app" / "phone_countries.json").write_text(json.dumps(countries, ensure_ascii=False, indent=0) + "\n")
    (ROOT / "app" / "static" / "phone-zones.js").write_text(
        "// Generated by tools/build_phone_countries.py from CLDR time zones: time zone -> country.\n"
        "window.PHONE_ZONES = " + json.dumps(zones, separators=(",", ":"), sort_keys=True) + ";\n")
    print(f"{len(countries)} countries, {len(zones)} time zones")


if __name__ == "__main__":
    main(sys.argv[1])
