from conftest import Session


def test_register_logs_in_and_lands_on_dashboard(app):
    s = Session(app)
    r = s.post("/register", {"name": "Nia", "email": "NIA@example.com", "phone": "+254 712 000 111", "password": "longenough", "confirm": "longenough"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard")
    page = s.get("/dashboard").get_data(as_text=True)
    assert "Upload your first document" in page


def test_register_rejects_duplicates_short_passwords_and_mismatch(app, users):
    s = Session(app)
    r = s.post("/register", {"name": "A", "email": "alice@example.com", "phone": "+447700900123", "password": "password123", "confirm": "password123"})
    assert "already exists" in r.get_data(as_text=True)
    r = s.post("/register", {"name": "A", "email": "new@example.com", "phone": "+447700900123", "password": "short", "confirm": "short"})
    assert "at least 8 characters" in r.get_data(as_text=True)
    r = s.post("/register", {"name": "A", "email": "new@example.com", "phone": "+447700900123", "password": "password123", "confirm": "different1"})
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


def test_behind_a_proxy_one_attacker_cannot_lock_out_everyone(tmp_path):
    """With PROXY_COUNT=1 (Caddy in front), the login limit is per real visitor IP, not per proxy."""
    from app import create_app
    from app.security import login_throttle
    from conftest import Session
    login_throttle._failures.clear()
    app = create_app({"TESTING": True, "SECRET_KEY": "t", "DATABASE": str(tmp_path / "p.db"),
                      "UPLOAD_DIR": str(tmp_path / "u"), "PROXY_COUNT": 1})
    with app.app_context():
        from app.db import create_user
        create_user("alice@example.com", "Alice", "password123")
    attacker, victim = Session(app), Session(app)
    for _ in range(9):
        attacker.c.post("/login", data={"email": "alice@example.com", "password": "nope", "csrf_token": attacker.token()},
                        headers={"X-Forwarded-For": "203.0.113.9"})
    r = victim.c.post("/login", data={"email": "alice@example.com", "password": "password123", "csrf_token": victim.token()},
                      headers={"X-Forwarded-For": "198.51.100.7"})
    assert r.status_code == 302  # the real Alice still gets in


def test_without_a_proxy_forwarded_headers_are_ignored(tmp_path):
    """PROXY_COUNT=0: a client can't dodge the login limit by inventing an X-Forwarded-For header."""
    from app import create_app
    from app.security import login_throttle
    from conftest import Session
    login_throttle._failures.clear()
    app = create_app({"TESTING": True, "SECRET_KEY": "t", "DATABASE": str(tmp_path / "n.db"), "UPLOAD_DIR": str(tmp_path / "u")})
    with app.app_context():
        from app.db import create_user
        create_user("alice@example.com", "Alice", "password123")
    s = Session(app)
    for i in range(9):
        s.c.post("/login", data={"email": "alice@example.com", "password": "nope", "csrf_token": s.token()},
                 headers={"X-Forwarded-For": f"203.0.113.{i}"})
    r = s.c.post("/login", data={"email": "alice@example.com", "password": "password123", "csrf_token": s.token()},
                 headers={"X-Forwarded-For": "198.51.100.7"})
    assert r.status_code != 302  # still limited: the fake header changed nothing
