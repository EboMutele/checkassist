import io
import re
import zipfile

import pytest

from app import create_app
from app.db import create_user
from app.security import login_throttle

PDF_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def make_docx(text="Hello reviewer. This is my essay."):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(
            "word/document.xml",
            '<w:document xmlns:w="w"><w:body><w:p><w:r><w:t>' + text + "</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Second paragraph &amp; more.</w:t></w:r></w:p></w:body></w:document>",
        )
    return buf.getvalue()


@pytest.fixture
def app(tmp_path):
    login_throttle._failures.clear()
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test",
        "DATABASE": str(tmp_path / "test.sqlite3"),
        "UPLOAD_DIR": str(tmp_path / "uploads"),
        "MAX_CONTENT_LENGTH": 1024 * 1024,
        "MAIL_OUTBOX": [],
        "SMS_OUTBOX": [],
        "BASE_URL": "https://example.test",
    })
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


class Session:
    """Test client wrapper that adds the CSRF token to every POST."""

    def __init__(self, app):
        self.app = app
        self.c = app.test_client()

    def token(self):
        with self.c.session_transaction() as s:
            tok = s.get("_csrf")
        if tok:
            return tok
        self.c.get("/login")
        with self.c.session_transaction() as s:
            return s.get("_csrf")

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def post(self, url, data=None, **kw):
        data = dict(data or {})
        data.setdefault("csrf_token", self.token())
        return self.c.post(url, data=data, **kw)

    def login(self, email, password="password123"):
        return self.post("/login", {"email": email, "password": password})


@pytest.fixture
def users(app):
    with app.app_context():
        ids = {
            "alice": create_user("alice@example.com", "Alice Doe", "password123", phone="+254712345678"),
            "bob": create_user("bob@example.com", "Bob Roe", "password123"),
            "admin": create_user("admin@example.com", "Rita Reviewer", "password123", role="admin"),
            "admin2": create_user("admin2@example.com", "Sam Second", "password123", role="admin"),
        }
        from app.billing import grant
        from app.db import get_db
        for who in ("alice", "bob"):
            grant(get_db(), ids[who], 50, "admin", "Test balance")
        get_db().commit()
    return ids


@pytest.fixture
def alice(app, users):
    s = Session(app)
    s.login("alice@example.com")
    return s


@pytest.fixture
def bob(app, users):
    s = Session(app)
    s.login("bob@example.com")
    return s


@pytest.fixture
def admin(app, users):
    s = Session(app)
    s.login("admin@example.com")
    return s


def upload(session, title="My essay", content=PDF_BYTES, filename="essay.pdf", **extra):
    data = {
        "title": title,
        "instructions": "Check my argument.",
        "document": (io.BytesIO(content), filename),
    }
    data.update(extra)
    return session.post("/submit", data, content_type="multipart/form-data")


def sub_id_from(resp):
    m = re.search(r"/submissions/(\d+)", resp.headers.get("Location", ""))
    assert m, resp.headers.get("Location")
    return int(m.group(1))


@pytest.fixture
def with_monthly(monkeypatch):
    """The monthly subscription is switched off (no price list offers it), but its code is kept for later.
    Tests of that code switch it back on with the public price list it had."""
    from app import pricing
    monkeypatch.setitem(pricing.PRICES, "USD", {"single": "4.99", "pack10": "39.99", "pack20": "69.99", "monthly": "240"})
    monkeypatch.setattr(pricing, "PLANS", pricing.plans("USD"))
