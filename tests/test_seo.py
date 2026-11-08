import io
import json
import re
from html.parser import HTMLParser

import pytest
from PIL import Image

from app import pricing, seo
from tests.conftest import upload, sub_id_from

PUBLIC = [p.path for p in seo.PAGES]


@pytest.fixture
def live(app):
    """The site as it runs in production: indexing on."""
    app.config["ALLOW_INDEXING"] = True
    return app


class Head(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta, self.links, self.title, self.jsonld, self.hrefs, self.h1 = {}, {}, "", [], [], 0
        self._in = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = a.get("name") or a.get("property")
            if key:
                self.meta[key] = a.get("content")
        elif tag == "link":
            self.links.setdefault(a.get("rel"), []).append(a.get("href"))
        elif tag == "title":
            self._in = "title"
        elif tag == "script" and a.get("type") == "application/ld+json":
            self._in = "jsonld"
        elif tag == "a" and a.get("href"):
            self.hrefs.append(a["href"])
        elif tag == "h1":
            self.h1 += 1

    def handle_endtag(self, tag):
        self._in = None

    def handle_data(self, data):
        if self._in == "title":
            self.title += data
        elif self._in == "jsonld":
            self.jsonld.append(json.loads(data))


def parse(resp):
    h = Head()
    h.feed(resp.get_data(as_text=True))
    return h


def test_public_pages_are_indexable_with_unique_titles_and_descriptions(live, client):
    titles, descriptions = set(), set()
    for path in PUBLIC:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "X-Robots-Tag" not in resp.headers, path
        h = parse(resp)
        assert "robots" not in h.meta, path
        assert h.h1 == 1, path
        assert h.links["canonical"] == ["https://example.test" + path]
        assert h.meta["og:url"] == "https://example.test" + path
        assert h.meta["og:image"] == "https://example.test/static/og.png"
        assert h.title.endswith(" – CheckAssist") and len(h.title) <= 62, h.title
        desc = h.meta["description"]
        assert 100 <= len(desc) <= 165, (path, len(desc), desc)
        assert "{" not in h.title + desc, "unfilled placeholder"
        titles.add(h.title)
        descriptions.add(desc)
        assert h.jsonld and h.jsonld[0]["@context"] == "https://schema.org"
    assert len(titles) == len(descriptions) == len(PUBLIC)


def test_everything_else_is_noindex(live, app, users, alice, admin, client):
    sub_id = sub_id_from(upload(alice))
    private = [(client, "/login"), (client, "/register"), (client, "/no-such-page"),
               (alice, "/dashboard"), (alice, "/billing/"), (alice, f"/submissions/{sub_id}"), (alice, "/account"),
               (alice, "/notifications"), (admin, "/admin/"), (admin, "/admin/payments")]
    for who, path in private:
        resp = who.get(path)
        assert resp.headers.get("X-Robots-Tag") == "noindex", path
        if resp.status_code == 200 and resp.mimetype == "text/html":
            h = parse(resp)
            assert h.meta.get("robots") == "noindex", path
            assert "canonical" not in h.links, path


def test_indexing_is_off_outside_production(app, client):
    assert app.config["ALLOW_INDEXING"] is False  # tests run as APP_ENV=development
    resp = client.get("/")
    assert resp.headers["X-Robots-Tag"] == "noindex"
    assert parse(resp).meta["robots"] == "noindex"
    robots = client.get("/robots.txt").get_data(as_text=True)
    assert "Disallow: /\n" in robots and "Sitemap" not in robots
    assert "<url>" not in client.get("/sitemap.xml").get_data(as_text=True)


def test_robots_and_sitemap(live, client):
    robots = client.get("/robots.txt")
    assert robots.mimetype == "text/plain"
    body = robots.get_data(as_text=True)
    for prefix in ("/admin/", "/billing/", "/submissions/", "/files/", "/dashboard"):
        assert f"Disallow: {prefix}\n" in body
    assert "Disallow: /\n" not in body
    assert "Sitemap: https://example.test/sitemap.xml" in body

    xml = client.get("/sitemap.xml")
    assert xml.mimetype == "application/xml"
    locs = re.findall(r"<loc>https://example\.test([^<]*)</loc>", xml.get_data(as_text=True))
    assert sorted(locs) == sorted(PUBLIC)
    assert len(re.findall(r"<lastmod>\d{4}-\d\d-\d\d</lastmod>", xml.get_data(as_text=True))) == len(PUBLIC)
    # nothing in robots.txt blocks a page we ask search engines to index
    for path in locs:
        assert not any(path.startswith(p) for p in seo.PRIVATE_PREFIXES), path


def test_internal_links_on_public_pages_all_work(live, client):
    seen = set()
    for path in PUBLIC:
        for href in parse(client.get(path)).hrefs:
            if href.startswith("/") and not href.startswith("//"):
                seen.add(href.split("#")[0] or "/")
    assert {"/essay-review", "/citation-check", "/how-we-review", "/pricing"} <= seen
    for href in seen:
        status = client.get(href).status_code
        assert status in (200, 302), (href, status)


def test_every_service_page_is_linked_from_home(live, client):
    hrefs = parse(client.get("/")).hrefs
    for page in seo.SERVICES:
        assert page.path in hrefs


def test_structured_data_matches_pricing(live, client):
    graph = parse(client.get("/pricing")).jsonld[0]["@graph"]
    service = next(n for n in graph if n["@type"] == "Service")
    offers = service["hasOfferCatalog"]["itemListElement"]
    assert [(o["price"], o["priceCurrency"]) for o in offers] == [
        (f"{p.price:.2f}", pricing.CURRENCY_CODE) for p in pricing.PLANS]

    home = parse(client.get("/")).jsonld[0]["@graph"]
    assert {n["@type"] for n in home} == {"Organization", "WebSite"}
    essay = parse(client.get("/essay-review")).jsonld[0]["@graph"]
    svc = next(n for n in essay if n["@type"] == "Service")
    assert svc["offers"]["price"] == f"{pricing.PLANS[0].price:.2f}"
    assert svc["provider"]["@id"] == "https://example.test/#org"


def test_structured_data_cannot_break_out_of_its_script(live, app, client):
    app.config["APP_NAME"] = "Bad</script><script>alert(1)</script>"
    html = client.get("/").get_data(as_text=True)
    block = html.split('<script type="application/ld+json">', 1)[1].split("</script>", 1)[0]
    assert "<" not in block
    assert json.loads(block)["@graph"][0]["name"] == app.config["APP_NAME"]


def test_copy_follows_review_minutes(live, app, client):
    assert "in about 20 minutes" in parse(client.get("/essay-review")).meta["description"]
    app.config["REVIEW_MINUTES"] = 0
    for path in PUBLIC:
        html = client.get(path).get_data(as_text=True)
        assert "0 minutes" not in html and "about 0" not in html, path


def test_service_pages_quote_live_prices(live, client):
    html = client.get("/dissertation-review").get_data(as_text=True)
    assert "a 25,000-word dissertation uses 3 checks, or $12 with a 10-check pack" in html
    assert f"{pricing.money(pricing.PLANS[0].price)} a check" in html


def test_upload_buttons_go_to_signup_or_upload(live, client, alice):
    assert 'href="/register"' in client.get("/essay-review").get_data(as_text=True)
    assert 'href="/submit"' in alice.get("/essay-review").get_data(as_text=True)


def test_static_files_are_versioned_and_cached(live, client):
    html = client.get("/").get_data(as_text=True)
    css = re.search(r'href="(/static/style\.css\?v=[0-9a-f]{10})"', html).group(1)
    resp = client.get(css)
    assert "max-age=31536000" in resp.headers["Cache-Control"] and "immutable" in resp.headers["Cache-Control"]
    assert "no-cache" not in resp.headers["Cache-Control"]
    # fonts keep plain URLs so the preload matches the URL style.css asks for
    preload = re.search(r'<link rel="preload" href="([^"]+)" as="font"', html).group(1)
    assert preload == "/static/fonts/newsreader.woff"
    assert 'url("fonts/newsreader.woff")' in client.get(css).get_data(as_text=True)
    assert "max-age=2592000" in client.get(preload).headers["Cache-Control"]


def test_icons_manifest_and_share_image(live, client):
    ico = client.get("/favicon.ico")
    assert ico.status_code == 200 and ico.data[:4] == b"\x00\x00\x01\x00"
    manifest = client.get("/static/site.webmanifest")
    assert manifest.status_code == 200
    for icon in json.loads(manifest.data)["icons"]:
        img = Image.open(io.BytesIO(client.get("/static/" + icon["src"]).data))
        assert f"{img.width}x{img.height}" == icon["sizes"]
    og = Image.open(io.BytesIO(client.get("/static/og.png").data))
    assert og.size == (1200, 630)


def test_verification_tags_only_when_set(live, app, client):
    assert "google-site-verification" not in client.get("/").get_data(as_text=True)
    app.config["GOOGLE_SITE_VERIFICATION"] = "abc123"
    assert parse(client.get("/")).meta["google-site-verification"] == "abc123"


def test_redirect_to_base_url_host(live, app, client):
    assert client.get("/pricing", headers={"Host": "www.example.test"}).status_code == 200  # off by default
    app.config["REDIRECT_TO_BASE_URL"] = True
    resp = client.get("/pricing?x=1", headers={"Host": "www.example.test"})
    assert resp.status_code == 301 and resp.headers["Location"] == "https://example.test/pricing?x=1"
    assert client.get("/pricing", headers={"Host": "example.test"}).status_code == 200
    # Paystack's webhook and other POSTs are never redirected
    assert client.post("/billing/webhook", data=b"{}", headers={"Host": "www.example.test"}).status_code != 301


def test_guides_are_linked_from_every_page_footer(live, client):
    hrefs = parse(client.get("/pricing")).hrefs
    for page in seo.GUIDES:
        assert page.path in hrefs


def test_public_pages_never_promise_to_beat_turnitin_or_ai_detectors(live, client):
    """The guides reach people searching for Turnitin, AI detectors and humanizers, but the site must
    stay honest: it doesn't run Turnitin and doesn't disguise AI-written work."""
    promises = re.compile(r"(bypass|beat|pass|fool|evade|get past|undetectable)\W+(\w+\W+){0,3}"
                          r"(turnitin|ai detect|detector)|turnitin (report|check)s? (for|in|from) \$|humaniz(e|er) your",
                          re.I)
    for path in PUBLIC:
        resp = client.get(path)
        h = parse(resp)
        for text in (h.title, h.meta["description"]):
            assert not promises.search(text), (path, text)
        body = re.sub(r"<[^>]+>", " ", resp.get_data(as_text=True).split("</head>", 1)[1])
        for match in promises.finditer(body):
            sentence = body[max(0, match.start() - 80):match.end() + 40]
            # only allowed as a question we answer "No" to, or advice against it
            assert re.search(r"\?|No\.|isn't|aren't|won't|don't|can't|No one", sentence), (path, sentence)
    turnitin = client.get("/turnitin-similarity-score").get_data(as_text=True)
    assert "We don't run Turnitin" in turnitin and "isn't affiliated with or endorsed by Turnitin" in turnitin
    ai = client.get("/ai-detection").get_data(as_text=True)
    assert "Can you make AI-written text pass a detector?" in ai and "<p>No. We review your own writing" in ai
    assert "isn't affiliated with Chegg" in client.get("/chegg-alternative").get_data(as_text=True)
