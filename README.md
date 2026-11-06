# CheckAssist

Customers upload documents. Your reviewers read them and send back written feedback and marked-up files.

## What's in it

**Customers**
- Create an account, upload a PDF, Word (.doc/.docx), ODT, RTF or TXT file (up to 25 MB) with instructions
- Track progress (Submitted → In review → Feedback ready) and answer reviewer questions
- Read feedback and download reviewed files; in-app and email notifications

**Admins / reviewers** (`/admin`)
- Review queue with tabs (open, new, in review, waiting on customer, mine, completed), search by title, customer or reference (`CA-00012`), and turnaround stats
- Review workspace: customer instructions, original download, in-page text preview (DOCX, ODT, PDF, TXT), assign/claim, status
- **Send results**: written feedback plus reviewed files. Drafts stay hidden from the customer until sent, and sending emails them
- Messages to the customer, or internal notes they never see; "mark as waiting on customer" for questions
- Users list and per-user history

**Built in:** password hashing, CSRF protection, login throttling, session rotation, file-type checks by content (not just extension), files stored outside the web root under random names, owner/admin-only downloads, security headers.

## Design

Paper on a grey desk, marked up in red pen. Headlines in Newsreader, interface text in Instrument Sans, and Caveat handwriting only for things a reviewer wrote. Fonts are self-hosted in `app/static/fonts` (OFL licences alongside), so pages load no third-party resources and the CSP stays `'self'`-only. Colour pairs meet WCAG AA contrast.

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # set SECRET_KEY
flask --app wsgi create-admin # your reviewer login
flask --app wsgi run --debug  # http://localhost:5000
```

Customers sign up at `/register`. Admins log in at `/login` and land on the queue.

## Tests

```bash
pytest
```

The suite covers auth, CSRF, access control between customers, upload validation, the draft→send results flow, questions/replies, internal notes and email delivery.

## Deploy

Any host that runs Python with a **persistent disk** works (Render, Railway, Fly.io, a VPS). SQLite and uploads both live on that disk.

1. Set `APP_ENV=production`, a long random `SECRET_KEY`, `BASE_URL`, and `SESSION_COOKIE_SECURE=1`. Optionally set `TURNAROUND_TEXT` (e.g. `within 2 working days`) to show a turnaround promise on the homepage.
2. Point `DATABASE_PATH` and `UPLOAD_DIR` at the persistent disk (the Dockerfile uses `/data`).
3. Start command: see `Procfile` (gunicorn). Or `docker build -t checkassist . && docker run -p 8000:8000 -v checkassist-data:/data --env-file .env checkassist`.
4. Run `flask --app wsgi create-admin` once on the server.
5. For email, set the `SMTP_*` values (any provider: Postmark, SES, Resend, Mailgun…). Without them, notifications are in-app only.

## Where integrations plug in

| Later | Where |
|---|---|
| Cloud file storage (S3, R2) | `app/storage.py`: add a class with `save`, `path`/streaming, `delete` |
| Email provider API, Slack, WhatsApp, webhooks | `app/notify.py`: every notification already flows through `notify()` |
| Payments before review | gate `portal.new_submission` in `app/user_views.py` |
| Postgres | `app/db.py` + `schema.sql` (queries are plain SQL) |

## Before launch

- Decide the copy that's yours to make: turnaround promise (`TURNAROUND_TEXT`), pricing, terms of service and privacy policy (you're storing people's documents). The homepage's privacy promises must stay true if you add integrations.
- Decide how long you keep files, then add a cleanup job.
- Back up the `/data` disk.
