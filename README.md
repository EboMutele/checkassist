# CheckAssist

Customers upload documents. Your reviewers read them and send back written feedback and marked-up files.

## What's in it

**Customers**
- Create an account (name, email, mobile number), upload a PDF, Word (.doc/.docx), ODT, RTF or TXT file (up to 25 MB) with instructions
- Track progress (Submitted → In review → Feedback ready) and answer reviewer questions
- See when to expect feedback (20 minutes by default), then read it and download reviewed files
- Get a text and an email the moment feedback is sent; update their mobile number under Account

**Admins / reviewers** (`/admin`)
- Review queue with tabs, search by title, customer or reference (`CA-00012`), a time-left/overdue column, overdue count in the tab title, and on-time rate for the last 7 days. Refreshes itself every minute
- Review workspace: customer instructions, original download, in-page text preview (DOCX, ODT, PDF, TXT), assign/claim, status
- **Send results**: written feedback plus reviewed files. Drafts stay hidden from the customer until sent, and sending emails them
- Messages to the customer, or internal notes they never see; "mark as waiting on customer" for questions
- Users list (searchable by phone) and per-user history; every text's outcome is logged on the submission timeline for reviewers

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

1. Set `APP_ENV=production`, a long random `SECRET_KEY`, `BASE_URL`, and `SESSION_COOKIE_SECURE=1`. Set `REVIEW_MINUTES` if your promise differs from 20 minutes.
2. Point `DATABASE_PATH` and `UPLOAD_DIR` at the persistent disk (the Dockerfile uses `/data`).
3. Start command: see `Procfile` (gunicorn). Or `docker build -t checkassist . && docker run -p 8000:8000 -v checkassist-data:/data --env-file .env checkassist`.
4. Run `flask --app wsgi create-admin` once on the server.
5. For email, set the `SMTP_*` values (any provider: Postmark, SES, Resend, Mailgun…). Without them, notifications are in-app only.
6. For texts, set `SMS_PROVIDER` to `africastalking` or `twilio` plus that provider's keys (see `.env.example`), and `DEFAULT_COUNTRY_CODE` if customers will type local numbers. Test with `AT_USERNAME=sandbox` first. Without a provider, sending results still works and the timeline notes that no text went out.

## The 20-minute promise

`REVIEW_MINUTES` (default 20) is measured from upload to the first time results are sent. Customers see an expected-by time in their own time zone; past it, they see a "taking a little longer" note instead of a countdown. While a reviewer is waiting on the customer's reply, the customer's countdown is hidden. Reviewers see minutes left or overdue on every open document.

**Live timer.** After uploading, customers see a ring timer counting down to the promise, also shown in the browser tab title, and their dashboard and the reviewers' queue count down second by second. Timers use the server's clock, so a phone set to the wrong time still shows the right countdown. At zero the timer is replaced by an "almost there" note rather than counting into negative time. The open document page checks every 15 seconds for changes (picked up, question asked, feedback sent) and refreshes itself; if the customer is typing, it shows a banner instead so nothing is lost.

**Fair clock.** Time spent waiting for the customer to answer a reviewer's question doesn't count, for the customer's timer, the reviewer's due time, or the on-time stats.

## Where integrations plug in

| Later | Where |
|---|---|
| Cloud file storage (S3, R2) | `app/storage.py`: add a class with `save`, `path`/streaming, `delete` |
| Email provider API, Slack, WhatsApp, webhooks | `app/notify.py`: every notification already flows through `notify()` |
| Another SMS gateway | `app/sms.py`: add a function to `PROVIDERS` |
| Payments before review | gate `portal.new_submission` in `app/user_views.py` |
| Postgres | `app/db.py` + `schema.sql` (queries are plain SQL) |

## Before launch

- Decide the copy that's yours to make: pricing, terms of service and privacy policy (you're storing people's documents). The homepage's privacy promises must stay true if you add integrations.
- Decide how long you keep files, then add a cleanup job.
- Back up the `/data` disk.
