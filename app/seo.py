"""Search engines: page titles and descriptions, canonical URLs, robots.txt, sitemap.xml,
structured data, and noindex on every page that isn't on the public list.

Default-deny: a new page is hidden from search until it's added to PAGES below.
"""
import hashlib
import logging
import os
from dataclasses import dataclass
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from flask import (Blueprint, Response, current_app, g, has_request_context, redirect, render_template, request,
                   send_from_directory)

from . import local, pricing

bp = Blueprint("seo", __name__)
log = logging.getLogger(__name__)

YEAR = 31536000

# Paths crawlers have no reason to visit (all need a login). Login and sign-up stay
# crawlable so search engines can see their noindex.
PRIVATE_PREFIXES = ("/admin/", "/billing/", "/submissions/", "/files/", "/dashboard", "/notifications",
                    "/account", "/submit")


@dataclass(frozen=True)
class Page:
    endpoint: str
    path: str
    title: str          # before " – AppName"; {soon} {price} {lowest} {words} {plan_list} are filled in
    description: str    # aim for 120–160 characters once filled in
    updated: str        # when the content last changed (sitemap lastmod); bump it when you edit the page
    template: str = ""  # set for pages this module serves
    label: str = ""     # short link text
    summary: str = ""   # one line for the home page's list of services
    service: str = ""   # schema.org serviceType, for service pages
    guide: bool = False # listed under Guides in the footer


PAGES = (
    Page("portal.index", "/",
         "Check your paper before you hand it in",
         "Upload your essay, report or application for a plagiarism and AI report{soon}, with missing "
         "citations and weak arguments flagged. {price} a check.",
         "2026-11-11"),
    Page("portal.pricing_page", "/pricing",
         "Pricing: {price} a check, from {lowest} on a plan",
         "One check covers a document up to {words} words, read in full by a person: {plan_list}.",
         "2026-11-07", label="Pricing"),
    Page("seo.ai_detection_report", "/ai-detection-report",
         "AI detection and plagiarism reports",
         "See what an AI detector and a plagiarism checker make of your document before you submit: an AI "
         "score and a similarity report, explained{soon}.",
         "2026-11-11", "services/ai_detection_report.html", "AI detection reports",
         "An AI score and a similarity report, explained.", "AI content detection"),
    Page("seo.essay_review", "/essay-review",
         "Essay review by a real person",
         "Get your essay read by a real reviewer before you submit: notes on your argument, evidence and "
         "clarity, plus a marked-up copy{soon}. {price} a check.",
         "2026-11-07", "services/essay_review.html", "Essay review",
         "Argument, structure, evidence and every sentence.", "Essay review"),
    Page("seo.proofreading", "/proofreading",
         "Fast proofreading by a real person",
         "Proofreading for essays, reports and applications by a person, not software. Grammar, spelling, "
         "punctuation and clarity marked in your own file{soon}.",
         "2026-11-07", "services/proofreading.html", "Proofreading",
         "Grammar, spelling, punctuation and clarity, marked in your file.", "Proofreading"),
    Page("seo.citation_check", "/citation-check",
         "Citation check for APA, MLA and Harvard",
         "A reviewer checks that every claim that needs a source has one, and that your in-text citations "
         "match your reference list in APA, MLA, Harvard or Chicago.",
         "2026-11-11", "services/citation_check.html", "Citation check",
         "Claims that need a source, and references that don't match.", "Citation checking"),
    Page("seo.dissertation_review", "/dissertation-review",
         "Dissertation and thesis chapter review",
         "Send a chapter, a research proposal or a full draft. A reviewer reads every page for structure, "
         "argument, citations and clarity. One check covers {words} words.",
         "2026-11-07", "services/dissertation_review.html", "Dissertations and theses",
         "Chapters, proposals and full drafts, read start to finish.", "Dissertation review"),
    Page("seo.personal_statement_review", "/personal-statement-review",
         "Personal statement review by a real person",
         "Your personal statement, scholarship essay or cover letter, read by a real person. Notes on your "
         "story, structure and every line{soon}.",
         "2026-11-07", "services/personal_statement_review.html", "Personal statements",
         "Applications, scholarship essays and cover letters.", "Personal statement review"),
    Page("seo.how_we_review", "/how-we-review",
         "How we review, and what we won't do",
         "What a reviewer checks in every document, what we never do (like rewriting your work for you), and "
         "how we keep your files private.",
         "2026-11-11", "how_we_review.html", "How we review"),
    # Guides: honest answers to what students search for around Turnitin, AI detectors and Chegg.
    # They say plainly what this site does (reports, and a person's notes on what's behind them) and
    # won't do (rewrite work to get past a detector). Keep it that way: pages that promise more get found
    # and then distrusted.
    Page("seo.turnitin_similarity_score", "/turnitin-similarity-score",
         "Turnitin similarity score: what it means",
         "What a Turnitin similarity score does and doesn't tell your marker, why matches happen, and how to "
         "fix the citation problems behind them before you submit.",
         "2026-11-11", "guides/turnitin_similarity_score.html", "Turnitin scores explained", guide=True),
    Page("seo.ai_detection", "/ai-detection",
         "Flagged by an AI detector? What to do next",
         "AI detectors give probabilities, not proof, and they do flag human writing. How to show your work "
         "is yours, what to do if flagged, and why humanizers backfire.",
         "2026-11-11", "guides/ai_detection.html", "AI detectors and your work", guide=True),
    Page("seo.chegg_alternative", "/chegg-alternative",
         "A Chegg alternative for essay feedback",
         "Chegg is built for homework help and automated writing checks. If you want a real person to read "
         "your essay and tell you what to fix, here's how we compare.",
         "2026-11-08", "guides/chegg_alternative.html", "Chegg alternative", guide=True),
)
BY_ENDPOINT = {p.endpoint: p for p in PAGES}
SERVICES = tuple(p for p in PAGES if p.service)
GUIDES = tuple(p for p in PAGES if p.guide)


def base_url():
    return current_app.config["BASE_URL"].rstrip("/")


def indexing_allowed():
    return current_app.config["ALLOW_INDEXING"]


def page_for(endpoint):
    return BY_ENDPOINT.get(endpoint)


def fill(text):
    """Fill a title or description with live settings, so copy never disagrees with the pricing page."""
    minutes = current_app.config["REVIEW_MINUTES"]
    words = f"{pricing.WORDS_PER_CHECK:,}"
    plan_list = []
    for p in pricing.PLANS:
        price = pricing.money(p.price)
        if p.kind == "monthly":
            plan_list.append(f"or {price} a month for {p.checks}")
        else:
            plan_list.append(f"{price} for {'one' if p.checks == 1 else p.checks}")
    return text.format(
        soon=f" in about {minutes} minutes" if minutes else "",
        price=pricing.money(pricing.PLANS[0].price),
        lowest=pricing.lowest_per_check(),
        words=words,
        plan_list=", ".join(plan_list[:-1]) + (" " if plan_list[-1].startswith("or ") else " or ") + plan_list[-1]
        if len(plan_list) > 1 else "".join(plan_list),
    )


def describe(title, description, path, *, image=None, og_type="website", structured=None, indexable=True,
             published=None, modified=None, feed=False):
    """For pages that aren't in PAGES because their content lives in the database (blog posts and the
    blog index): the view calls this before rendering. Unlike PAGES, it can say the page shouldn't be
    indexed (an empty blog, a later page of the list)."""
    g.seo_page = {
        "title": f"{title} – {current_app.config['APP_NAME']}", "description": description,
        "canonical": base_url() + path, "image": image, "og_type": og_type, "structured_data": structured,
        "indexable": indexable, "published": published, "modified": modified, "feed": feed,
    }


def head_for(endpoint):
    """Everything base.html needs for <head>. Pages not in PAGES get noindex and no canonical."""
    dynamic = g.get("seo_page") if has_request_context() else None
    if dynamic:
        head = {
            "page": True, "indexable": dynamic["indexable"] and indexing_allowed(),
            "site_name": current_app.config["APP_NAME"],
            "og_image": dynamic["image"] or base_url() + "/static/og.png",
            "google_verification": current_app.config["GOOGLE_SITE_VERIFICATION"],
            "bing_verification": current_app.config["BING_SITE_VERIFICATION"],
        }
        head.update(dynamic)
        if head["structured_data"] is None:
            head["structured_data"] = {"@context": "https://schema.org", "@graph": [_org()]}
        return head
    page = page_for(endpoint)
    app_name = current_app.config["APP_NAME"]
    indexable = bool(page) and indexing_allowed()
    head = {
        "page": page,
        "indexable": indexable,
        "site_name": app_name,
        "og_image": base_url() + "/static/og.png",
        "google_verification": current_app.config["GOOGLE_SITE_VERIFICATION"],
        "bing_verification": current_app.config["BING_SITE_VERIFICATION"],
    }
    if endpoint == "local.page":
        # Not indexed, but it's shared on WhatsApp and social, so give it a proper link preview.
        prices = pricing.plans(pricing.LOCAL_CURRENCY)
        head["share"] = {
            "title": f"Get your paper checked by a real reviewer – {app_name}",
            "description": fill("For students in Kenya: a reviewer reads every page and sends notes and a "
                                "marked-up copy{soon}. ") + f"From {pricing.money(prices[0].price, pricing.LOCAL_CURRENCY)}"
                           " a check, pay with M-Pesa.",
            "url": base_url() + request.path,
        }
    if page:
        head.update(
            title=f"{fill(page.title)} – {app_name}",
            description=fill(page.description),
            canonical=base_url() + page.path,
            structured_data=structured_data(page),
        )
    return head


def _org():
    org = {
        "@type": "Organization",
        "@id": base_url() + "/#org",
        "name": current_app.config["APP_NAME"],
        "url": base_url() + "/",
        "logo": base_url() + "/static/icon-512.png",
    }
    email = current_app.config["SUPPORT_EMAIL"]
    if email:
        org["email"] = email
    return org


def _offer(plan, url):
    offer = {
        "@type": "Offer",
        "name": plan.name,
        "price": f"{plan.price:.2f}",
        "priceCurrency": pricing.CURRENCY_CODE,
        "url": url,
        "description": f"{plan.checks} check{'s' if plan.checks != 1 else ''}, "
                       f"each covering a document up to {pricing.WORDS_PER_CHECK:,} words",
    }
    if plan.kind == "monthly":
        offer["priceSpecification"] = {
            "@type": "UnitPriceSpecification", "price": f"{plan.price:.2f}",
            "priceCurrency": pricing.CURRENCY_CODE, "billingDuration": "P1M",
        }
    return offer


def structured_data(page):
    """schema.org JSON-LD. No FAQPage: Google stopped showing FAQ results for sites like this in 2023."""
    url = base_url() + page.path
    pricing_url = base_url() + "/pricing"
    org = _org()
    if page.endpoint == "portal.index":
        graph = [org, {"@type": "WebSite", "@id": base_url() + "/#website", "url": base_url() + "/",
                       "name": current_app.config["APP_NAME"], "publisher": {"@id": org["@id"]}}]
    elif page.endpoint == "portal.pricing_page":
        graph = [org, {
            "@type": "Service", "name": "Document review", "serviceType": "Document review",
            "provider": {"@id": org["@id"]}, "url": url, "description": fill(page.description),
            "hasOfferCatalog": {"@type": "OfferCatalog", "name": "Checks",
                                "itemListElement": [_offer(p, pricing_url) for p in pricing.PLANS]},
        }]
    elif page.service:
        graph = [org, {
            "@type": "Service", "name": fill(page.title), "serviceType": page.service,
            "provider": {"@id": org["@id"]}, "url": url, "description": fill(page.description),
            "offers": _offer(pricing.PLANS[0], pricing_url),
        }]
    else:
        graph = [org, {"@type": "WebPage", "url": url, "name": fill(page.title),
                       "publisher": {"@id": org["@id"]}}]
    return {"@context": "https://schema.org", "@graph": graph}


# ---------- pages served here ----------

def _date_label(iso_date):
    y, m, d = (int(x) for x in iso_date.split("-"))
    months = "January February March April May June July August September October November December".split()
    return f"{d} {months[m - 1]} {y}"


def _content_view(page):
    def view():
        # Visible prices follow the visitor (KES after the /local link); titles and structured data stay USD.
        currency = local.visitor_currency()
        example_words = 25000
        example_checks = pricing.checks_for_words(example_words)
        return render_template(
            page.template, page=page, services=SERVICES,
            single_price=pricing.single_price(currency),
            lowest_per_check=pricing.lowest_per_check(currency),
            words_per_check=pricing.WORDS_PER_CHECK,
            example_words=example_words, example_checks=example_checks,
            example_pack_cost=pricing.pack_cost(example_checks, currency),
            updated_label=_date_label(page.updated),
        )
    view.__name__ = page.endpoint.split(".", 1)[1]
    return view


for _page in PAGES:
    if _page.template:
        bp.add_url_rule(_page.path, endpoint=_page.endpoint.split(".", 1)[1], view_func=_content_view(_page))


@bp.route("/robots.txt")
def robots():
    if not indexing_allowed():
        body = "# Indexing is off. Set APP_ENV=production (or ALLOW_INDEXING=1) on the live site.\nUser-agent: *\nDisallow: /\n"
    else:
        lines = ["User-agent: *"] + [f"Disallow: {p}" for p in PRIVATE_PREFIXES]
        body = "\n".join(lines) + f"\n\nSitemap: {base_url()}/sitemap.xml\n"
    return Response(body, mimetype="text/plain")


@bp.route("/sitemap.xml")
def sitemap():
    entries = [(p.path, p.updated) for p in PAGES]
    from .blog import sitemap_entries  # the blog index and every live post
    entries += sitemap_entries()
    urls = "".join(
        f"<url><loc>{escape(base_url() + path)}</loc><lastmod>{updated}</lastmod></url>" for path, updated in entries
    ) if indexing_allowed() else ""
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>\n')
    return Response(body, mimetype="application/xml")


@bp.route("/favicon.ico")
def favicon():
    return send_from_directory(current_app.static_folder, "favicon.ico", mimetype="image/vnd.microsoft.icon",
                               max_age=86400)


# ---------- static file versions ----------

_versions = {}


def static_version(filename):
    """Short content hash, so a changed file gets a new URL and old ones can be cached for a year."""
    path = os.path.join(current_app.static_folder, filename)
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return None
    cached = _versions.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()[:10]
    _versions[path] = (mtime, digest)
    return digest


def init_app(app):
    app.register_blueprint(bp)
    cfg = app.config
    if cfg["APP_ENV"] == "production" and urlsplit(cfg["BASE_URL"]).hostname in ("localhost", "127.0.0.1"):
        log.warning("BASE_URL is %s: canonical links and the sitemap will point there. Set it to the live address.",
                    cfg["BASE_URL"])

    @app.url_defaults
    def versioned_static(endpoint, values):
        # Fonts are left alone: style.css asks for them by plain path, and the preload must match.
        if endpoint == "static" and "v" not in values:
            name = values.get("filename", "")
            if name and not name.startswith("fonts/"):
                version = static_version(name)
                if version:
                    values["v"] = version

    @app.before_request
    def canonical_host():
        if not cfg["REDIRECT_TO_BASE_URL"] or request.method not in ("GET", "HEAD"):
            return None
        target = urlsplit(cfg["BASE_URL"])
        if target.hostname and request.host.split(":")[0] != target.hostname:
            query = request.query_string.decode("latin-1")
            return redirect(f"{base_url()}{request.path}{'?' + query if query else ''}", code=301)
        return None

    @app.after_request
    def search_headers(resp):
        endpoint = request.endpoint or ""
        if endpoint == "static":
            if resp.status_code in (200, 304):
                name = request.view_args.get("filename", "") if request.view_args else ""
                age = YEAR if request.args.get("v") else (30 * 86400 if name.startswith("fonts/") else 86400)
                resp.cache_control.no_cache = None
                resp.cache_control.public = True
                resp.cache_control.max_age = age
                if request.args.get("v"):
                    resp.cache_control.immutable = True
            return resp
        if endpoint in ("seo.robots", "seo.sitemap", "seo.favicon", "blog.media", "blog.feed"):
            return resp
        dynamic = g.get("seo_page")
        listed = dynamic["indexable"] if dynamic else endpoint in BY_ENDPOINT
        if not (listed and indexing_allowed() and resp.status_code == 200):
            resp.headers["X-Robots-Tag"] = "noindex"
        return resp

    @app.context_processor
    def inject_seo():
        endpoint = request.endpoint if has_request_context() else None
        return {"seo": head_for(endpoint), "services": SERVICES, "guides": GUIDES,
                "support_email": cfg["SUPPORT_EMAIL"]}
