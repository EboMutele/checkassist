from decimal import Decimal

from app import pricing
from app.pricing import PLANS, Plan, checks_for_words, ladder_problems, money, savings_pct


def test_current_price_list_makes_sense():
    assert ladder_problems() == []


def test_ladder_is_20_30_40_percent_off():
    assert [savings_pct(p) for p in PLANS] == [0, 20, 30, 40]
    assert [money(p.per_check) for p in PLANS] == ["$4.99", "$4", "$3.50", "$3"]


def test_original_proposal_is_flagged():
    original = (
        Plan("single", "Single check", 1, Decimal("4.99"), "single", ""),
        Plan("pack10", "10 checks", 10, Decimal("40.99"), "pack", ""),
        Plan("pack20", "20 checks", 20, Decimal("70"), "pack", ""),
        Plan("monthly", "Monthly", 70, Decimal("240"), "monthly", ""),
    )
    problems = ladder_problems(original)
    assert any("Monthly saves only 1 points more than 20 checks" in p for p in problems), problems


def test_bigger_pack_costing_more_per_check_is_caught():
    bad = (PLANS[0], Plan("pack10", "10 checks", 10, Decimal("55"), "pack", ""))
    assert any("costs as much or more per check" in p for p in ladder_problems(bad))


def test_money_and_word_caps():
    assert [money(Decimal("4.99")), money(Decimal("240")), money(Decimal("1240.5"))] == ["$4.99", "$240", "$1,240.50"]
    assert pricing.WORDS_PER_CHECK == 10000
    assert [checks_for_words(n) for n in (0, 1800, 10000, 10001, 15000, 40000)] == [1, 1, 1, 2, 2, 4]


def test_pricing_page_lists_every_plan(client):
    page = client.get("/pricing").get_data(as_text=True)
    for text in ("Single check", "$4.99", "10 checks", "$39.99", "20 checks", "$69.99", "Monthly", "$240",
                 "save 20%", "save 30%", "save 40%", "10,000 words", "/month", "15,000-word"):
        assert text in page, text
    assert "2 checks" in page  # the worked example for a long document


def test_homepage_and_nav_point_to_pricing(client):
    page = client.get("/").get_data(as_text=True)
    assert 'href="/pricing"' in page and "$4.99" in page and "from $3 a check" in page
