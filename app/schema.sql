-- CheckAssist schema (SQLite). Safe to run repeatedly.

CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    name           TEXT    NOT NULL,
    phone          TEXT,   -- E.164, e.g. +254712345678; used for "feedback ready" texts
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
    words            INTEGER,              -- counted on upload; NULL when the format can't be read
    checks_spent     INTEGER NOT NULL DEFAULT 0,
    checks_refunded  INTEGER NOT NULL DEFAULT 0,
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

-- ---- billing ---------------------------------------------------------------------

-- One row per Paystack transaction: checkouts we start, and renewals Paystack starts.
CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    reference   TEXT    NOT NULL UNIQUE,
    plan_id     TEXT    NOT NULL,
    kind        TEXT    NOT NULL CHECK (kind IN ('checkout', 'renewal')),
    amount      INTEGER NOT NULL,          -- in the currency's smallest unit (cents)
    currency    TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paid', 'failed')),
    channel     TEXT,
    access_code TEXT,
    created_at  TEXT    NOT NULL,
    paid_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id, created_at);

-- Checks a customer holds. Spending takes from the soonest-expiring grant first.
CREATE TABLE IF NOT EXISTS credit_grants (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source      TEXT    NOT NULL CHECK (source IN ('purchase', 'subscription', 'admin', 'refund', 'signup')),
    checks      INTEGER NOT NULL CHECK (checks > 0),
    remaining   INTEGER NOT NULL CHECK (remaining >= 0),
    expires_at  TEXT,
    payment_id  INTEGER REFERENCES payments(id) ON DELETE SET NULL,
    note        TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_grants_user ON credit_grants(user_id, expires_at);

-- Every change to a balance, for customers' history and admin audit.
CREATE TABLE IF NOT EXISTS ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    change        INTEGER NOT NULL,
    reason        TEXT    NOT NULL,
    submission_id INTEGER REFERENCES submissions(id) ON DELETE SET NULL,
    payment_id    INTEGER REFERENCES payments(id) ON DELETE SET NULL,
    actor_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id, created_at);

CREATE TABLE IF NOT EXISTS subscriptions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan_id           TEXT    NOT NULL,
    subscription_code TEXT    UNIQUE,
    email_token       TEXT,
    customer_code     TEXT,
    status            TEXT    NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'active', 'attention', 'non-renewing', 'cancelled')),
    next_payment_date TEXT,
    last_grant_at     TEXT,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id, created_at);

-- Webhook keys already handled (e.g. invoice codes), so retries never grant twice.
CREATE TABLE IF NOT EXISTS processed_events (
    key         TEXT PRIMARY KEY,
    received_at TEXT NOT NULL
);
