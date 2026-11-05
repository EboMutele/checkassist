from conftest import Session


def test_register_logs_in_and_lands_on_dashboard(app):
    s = Session(app)
    r = s.post("/register", {"name": "Nia", "email": "NIA@example.com", "password": "longenough", "confirm": "longenough"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard")
    page = s.get("/dashboard").get_data(as_text=True)
    assert "Upload your first document" in page


def test_register_rejects_duplicates_short_passwords_and_mismatch(app, users):
    s = Session(app)
    r = s.post("/register", {"name": "A", "email": "alice@example.com", "password": "password123", "confirm": "password123"})
    assert "already exists" in r.get_data(as_text=True)
    r = s.post("/register", {"name": "A", "email": "new@example.com", "password": "short", "confirm": "short"})
    assert "at least 8 characters" in r.get_data(as_text=True)
    r = s.post("/register", {"name": "A", "email": "new@example.com", "password": "password123", "confirm": "different1"})
    assert "don&#39;t match" in r.get_data(as_text=True)


def test_login_wrong_password_then_correct(app, users):
    s = Session(app)
    r = s.login("alice@example.com", "wrong-password")
    assert r.status_code == 200 and "don&#39;t match an account" in r.get_data(as_text=True)
    r = s.login("alice@example.com")
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard")


def test_admin_lands_on_queue(admin):
    assert "Review queue" in admin.get("/admin/").get_data(as_text=True)
    r = admin.get("/dashboard")
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin/")


def test_login_throttle_blocks_after_repeated_failures(app, users):
    s = Session(app)
    for _ in range(8):
        s.login("alice@example.com", "nope-nope")
    r = s.login("alice@example.com")  # even the right password is refused while blocked
    assert r.status_code == 429


def test_open_redirect_is_ignored(app, users):
    s = Session(app)
    s.token()
    r = s.c.post("/login?next=//evil.example/x", data={
        "email": "alice@example.com", "password": "password123", "csrf_token": s.token()})
    assert r.headers["Location"].endswith("/dashboard")


def test_next_param_returns_to_page(app, users):
    s = Session(app)
    r = s.get("/notifications")
    assert "/login?next=" in r.headers["Location"]
    r = s.c.post("/login?next=/notifications", data={
        "email": "alice@example.com", "password": "password123", "csrf_token": s.token()})
    assert r.headers["Location"].endswith("/notifications")


def test_post_without_csrf_token_is_rejected(app, users):
    s = Session(app)
    r = s.c.post("/login", data={"email": "alice@example.com", "password": "password123"})
    assert r.status_code == 400


def test_logout_requires_post_and_ends_session(alice):
    assert alice.get("/logout").status_code == 405
    alice.post("/logout")
    assert alice.get("/dashboard").status_code == 302


def test_security_headers_present(client):
    r = client.get("/")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_non_admin_cannot_reach_admin_pages(alice):
    for url in ("/admin/", "/admin/users", "/admin/submissions/1"):
        assert alice.get(url).status_code == 403


def test_anonymous_is_sent_to_login(client):
    for url in ("/dashboard", "/admin/", "/submit"):
        r = client.get(url)
        assert r.status_code == 302 and "/login" in r.headers["Location"]
