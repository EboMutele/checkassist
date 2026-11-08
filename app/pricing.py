"""Plans and prices in one place. The pricing page, homepage and tests all read from here.

Two price lists: USD for the public site, and KES for people who come through the private /local
link (and accounts an admin switches to KES). Change prices in PRICES. `ladder_problems()`
(enforced by tests/test_pricing.py for every currency) catches a price list that stops making sense,
for example a bigger pack that costs more per check.
"""
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal

CURRENCY_CODE = "USD"       # the public site's currency (search results, structured data)
CURRENCY = "$"
LOCAL_CURRENCY = "KES"      # for the private /local link; Paystack takes M-Pesa only in KES

# symbol, and the lowest sensible price (a typo guard for ladder_problems)
CURRENCIES = {
    "USD": ("$", Decimal("1")),
    "KES": ("KSh\u00a0", Decimal("100")),
}

# KES: the USD prices at about KSh 129 to the dollar (Feb 2026: 128.6), rounded, with the same
# 20/30/40% savings ladder. Kept at parity on purpose, so the /local link isn't a discount if it spreads.
PRICES = {
    "USD": {"single": "4.99", "pack10": "39.99", "pack20": "69.99", "monthly": "240"},
    "KES": {"single": "650", "pack10": "5200", "pack20": "9100", "monthly": "31200"},
}
WORDS_PER_CHECK = 10000     # one check covers a document up to this many words
PACK_VALID_MONTHS = 12      # packs: checks stay valid this long
MIN_STEP_POINTS = 5         # each bigger option must save at least this many more % per check

CENT = Decimal("0.01")


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    checks: int
    price: Decimal
    kind: str            # "single", "pack" or "monthly"
    blurb: str
    currency: str = CURRENCY_CODE

    @property
    def per_check(self) -> Decimal:
        return (self.price / self.checks).quantize(CENT, ROUND_HALF_UP)


_BASE = (
    Plan("single", "Single check", 1, Decimal("4.99"), "single",
         "One document, whenever you need it."),
    Plan("pack10", "10 checks", 10, Decimal("39.99"), "pack",
         f"Every draft of a term's essays. Use them within {PACK_VALID_MONTHS} months."),
    Plan("pack20", "20 checks", 20, Decimal("69.99"), "pack",
         f"For a full year of coursework or applications. Use them within {PACK_VALID_MONTHS} months."),
    Plan("monthly", "Monthly", 80, Decimal("240"), "monthly",
         "80 checks every month for tutors and teams. Unused checks carry over one month. Cancel anytime."),
)


def plans(currency=CURRENCY_CODE):
    """The plans priced in `currency`."""
    table = PRICES[currency]
    return tuple(replace(p, price=Decimal(table[p.id]), currency=currency) for p in _BASE)


PLANS = plans(CURRENCY_CODE)


def money(amount: Decimal, currency=CURRENCY_CODE) -> str:
    """$4.99, $240, KSh 5,200 (no .00 on whole amounts)."""
    symbol = CURRENCIES[currency][0]
    amount = Decimal(amount).quantize(CENT, ROUND_HALF_UP)
    if amount == amount.to_integral_value():
        return f"{symbol}{int(amount):,}"
    return f"{symbol}{amount:,.2f}"


def savings_pct(plan: Plan, plans=PLANS) -> int:
    """Whole-percent saving per check against the single-check price (exact, not rounded prices)."""
    base = plans[0].price / plans[0].checks
    return int(((1 - (plan.price / plan.checks) / base) * 100).quantize(Decimal("1"), ROUND_HALF_UP))


def checks_for_words(words: int) -> int:
    """How many checks a document of `words` words uses."""
    return max(1, -(-int(words) // WORDS_PER_CHECK))


def ladder_problems(plans=PLANS):
    """Reasons a price list doesn't make sense; empty when it's coherent."""
    problems = []
    if not plans or plans[0].kind != "single" or plans[0].checks != 1:
        return ["The first plan must be a single check, the anchor every saving is measured from."]
    for prev, cur in zip(plans, plans[1:]):
        if cur.checks <= prev.checks:
            problems.append(f"{cur.name} should include more checks than {prev.name}.")
        if cur.price / cur.checks >= prev.price / prev.checks:
            problems.append(f"{cur.name} costs as much or more per check than {prev.name}.")
        elif savings_pct(cur, plans) - savings_pct(prev, plans) < MIN_STEP_POINTS:
            problems.append(
                f"{cur.name} saves only {savings_pct(cur, plans) - savings_pct(prev, plans)} points more than "
                f"{prev.name}; customers need a clear reason to commit more."
            )
    monthly = [p for p in plans if p.kind == "monthly"]
    if monthly and min(plans, key=lambda p: p.price / p.checks) is not monthly[0]:
        problems.append("The monthly plan should be the cheapest per check: it's the only one that asks for a commitment.")
    for p in plans:
        floor = CURRENCIES[p.currency][1]
        if p.price < floor:
            problems.append(f"{p.name} is priced below {money(floor, p.currency)}.")
    return problems


def plans_for_display(currency=CURRENCY_CODE):
    priced = plans(currency)
    out = []
    for p in priced:
        out.append({
            "id": p.id, "name": p.name, "checks": p.checks, "kind": p.kind, "blurb": p.blurb, "currency": currency,
            "price": money(p.price, currency), "per_check": money(p.per_check, currency),
            "savings": savings_pct(p, priced), "period": "/month" if p.kind == "monthly" else "",
        })
    return out


def single_price(currency=CURRENCY_CODE) -> str:
    return money(plans(currency)[0].price, currency)


def lowest_per_check(currency=CURRENCY_CODE) -> str:
    return money(min(p.per_check for p in plans(currency)), currency)


def pack_cost(checks, currency=CURRENCY_CODE, pack_id="pack10") -> str:
    """What `checks` checks cost at a pack's per-check price, e.g. for 'a 25,000-word dissertation costs…'."""
    pack = next(p for p in plans(currency) if p.id == pack_id)
    return money(pack.price / pack.checks * checks, currency)
