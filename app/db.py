"""SQLite access helpers and CLI commands."""
import os
import sqlite3
from datetime import datetime, timezone

import click
from flask import current_app, g
from werkzeug.security import generate_password_hash


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        g.db = conn
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def query(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    cur.close()
    return (rows[0] if rows else None) if one else rows


def execute(sql, args=()):
    """Run a single write statement and commit. Returns lastrowid."""
    conn = get_db()
    cur = conn.execute(sql, args)
    conn.commit()
    return cur.lastrowid


def _migrate(conn):
    """Bring databases created by older versions up to date. Each step is idempotent."""
    user_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "phone" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN phone TEXT")


def init_db():
    conn = get_db()
    with current_app.open_resource("schema.sql") as f:
        conn.executescript(f.read().decode("utf-8"))
    _migrate(conn)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.commit()


def create_user(email, name, password, role="user", phone=None):
    return execute(
        "INSERT INTO users (email, name, phone, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (email.strip().lower(), name.strip(), phone, generate_password_hash(password), role, utcnow()),
    )


def init_app(app):
    app.teardown_appcontext(close_db)

    db_dir = os.path.dirname(app.config["DATABASE"])
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with app.app_context():
        init_db()

    @app.cli.command("init-db")
    def init_db_command():
        """Create tables if they don't exist."""
        init_db()
        click.echo("Database ready.")

    @app.cli.command("create-admin")
    @click.option("--email", prompt=True)
    @click.option("--name", prompt=True)
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def create_admin_command(email, name, password):
        """Create an admin account, or promote an existing user to admin."""
        if len(password) < 8:
            raise click.ClickException("Password must be at least 8 characters.")
        existing = query("SELECT id FROM users WHERE email = ?", (email.strip().lower(),), one=True)
        if existing:
            execute(
                "UPDATE users SET role = 'admin', password_hash = ?, is_active = 1 WHERE id = ?",
                (generate_password_hash(password), existing["id"]),
            )
            click.echo(f"Promoted {email} to admin.")
        else:
            create_user(email, name, password, role="admin")
            click.echo(f"Created admin {email}.")
