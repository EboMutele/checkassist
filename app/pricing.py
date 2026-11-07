"""Plans and prices in one place. The pricing page, homepage and tests all read from here.

Change prices by editing PLANS. `ladder_problems()` (enforced by tests/test_pricing.py) catches
a price list that stops making sense, for example a bigger pack that costs more per check.
"""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CURRENCY = "$"
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

    @property
    def per_check(self) -> Decimal:
        return (self.price / self.checks).quantize(CENT, ROUND_HALF_UP)


PLANS = (
    Plan("single", "Single check", 1, Decimal("4.99"), "single",
         "One document, whenever you need it."),
    Plan("pack10", "10 checks", 10, Decimal("39.99"), "pack",
         f"Every draft of a term's essays. Use them within {PACK_VALID_MONTHS} months."),
    Plan("pack20", "20 checks", 20, Decimal("69.99"), "pack",
         f"For a full year of coursework or applications. Use them within {PACK_VALID_MONTHS} months."),
    Plan("monthly", "Monthly", 80, Decimal("240"), "monthly",
         "80 checks every month for tutors and teams. Unused checks carry over one month. Cancel anytime."),
)


def money(amount: Decimal) -> str:
    """$4.99, $240 (no .00 on whole amounts)."""
    amount = Decimal(amount).quantize(CENT, ROUND_HALF_UP)
    if amount == amount.to_integral_value():
        return f"{CURRENCY}{int(amount):,}"
    return f"{CURRENCY}{amount:,.2f}"


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
        if p.price < 1:
            problems.append(f"{p.name} is priced below $1.")
    return problems


def plans_for_display(plans=PLANS):
    out = []
    for p in plans:
        out.append({
            "id": p.id, "name": p.name, "checks": p.checks, "kind": p.kind, "blurb": p.blurb,
            "price": money(p.price), "per_check": money(p.per_check), "savings": savings_pct(p, plans),
            "period": "/month" if p.kind == "monthly" else "",
        })
    return out


def lowest_per_check(plans=PLANS) -> str:
    return money(min(p.per_check for p in plans))
