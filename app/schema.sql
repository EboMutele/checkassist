-- CheckAssist schema (SQLite). Safe to run repeatedly.

CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    name           TEXT    NOT NULL,
    password_hash  TEXT    NOT NULL,
    role           TEXT    NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
    is_active      INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT    NOT NULL,
    last_login_at  TEXT
);

CREATE TABLE IF NOT EXISTS submissions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title            TEXT    NOT NULL,
    instructions     TEXT    NOT NULL DEFAULT '',
    status           TEXT    NOT NULL DEFAULT 'submitted'
                     CHECK (status IN ('submitted', 'in_review', 'needs_info', 'completed')),
    assigned_to      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    feedback         TEXT    NOT NULL DEFAULT '',
    feedback_sent_at TEXT,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_submissions_user   ON submissions(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status, created_at);

-- Originals uploaded by users and result files attached by reviewers.
CREATE TABLE IF NOT EXISTS files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id   INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    kind            TEXT    NOT NULL CHECK (kind IN ('original', 'result')),
    original_name   TEXT    NOT NULL,
    stored_name     TEXT    NOT NULL UNIQUE,
    ext             TEXT    NOT NULL,
    size            INTEGER NOT NULL,
    sha256          TEXT    NOT NULL,
    visible_to_user INTEGER NOT NULL DEFAULT 1,
    uploaded_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_submission ON files(submission_id);

-- Timeline: status changes, messages, internal notes, results sent.
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    actor_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    type          TEXT    NOT NULL,
    body          TEXT    NOT NULL DEFAULT '',
    is_internal   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_submission ON events(submission_id, created_at);

CREATE TABLE IF NOT EXISTS notifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    submission_id INTEGER REFERENCES submissions(id) ON DELETE CASCADE,
    message       TEXT    NOT NULL,
    is_read       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read);
