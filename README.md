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

## Review time and the timer

**Stopwatch, never a countdown.** After uploading, customers see a timer counting up from 0:00: how long their review has taken so far. It's on the document page (with a ring that sweeps once a minute), in the browser tab title, and on their dashboard. Customers are never shown time remaining, an expected-by time or a deadline; when feedback is sent they see how long it took ("3 minutes after you uploaded"). Timers use the server's clock, so a phone set to the wrong time still shows the right figure.

**The open page keeps itself current.** It checks every 15 seconds for changes (picked up, question asked, feedback sent) and refreshes; if the customer is typing, it shows a banner instead so nothing is lost.

**Fair clock.** While a reviewer waits for the customer to answer a question, the timer pauses (customers see it frozen with "paused"), and that time doesn't count toward any figure.

**`REVIEW_MINUTES` (default 20) is for reviewers only.** In the queue and workspace the same count-up time turns amber in the last 5 minutes and red past the target, and the queue header shows the overdue count and 7-day on-time rate. The homepage's "about 20 minutes" line also comes from this setting; set it to 0 to hide that line and the reviewer highlighting.

## Pricing

Prices live in one place, `app/pricing.py`; the pricing page, homepage section and FAQ all read from it. Current list: single check $4.99, 10 checks $39.99 ($4 each, save 20%), 20 checks $69.99 ($3.50, save 30%), monthly $240 for 80 checks ($3, save 40%). One check covers a document up to 10,000 words (`WORDS_PER_CHECK`); packs last 12 months; monthly checks carry over one month.

`tests/test_pricing.py` fails if an edit breaks the ladder: a bigger option must cost less per check, save at least 5 points more than the one before, and the monthly plan must be the cheapest per check.

## Payments (Paystack)

Customers buy checks on **Checks** (or from the pricing page) and pay on Paystack's checkout. Each upload spends one check per 10,000 words, counted from the file; formats we can't read (old .doc, scanned PDFs) use one check, and reviewers can refund from the document page.

**Set up**
1. In the Paystack dashboard, copy your secret key into `PAYSTACK_SECRET_KEY` (start with the test key).
2. Set the webhook URL to `https://YOUR-SITE/billing/webhook` and the callback URL to `https://YOUR-SITE/billing/callback` (the app also sends the callback with each payment).
3. Run `flask --app wsgi paystack-create-plan` once and set `PAYSTACK_MONTHLY_PLAN_CODE` to the code it prints.
4. Test a pack and the monthly plan with Paystack's test cards, then switch to the live key.

**How it's kept safe**
- Checks are added only after we verify the transaction with Paystack and the amount and currency match what we charged.
- Webhooks are accepted only with a valid `x-paystack-signature` (HMAC-SHA512 of the body with your secret key).
- Each payment adds checks once, however often Paystack retries; renewals are recorded by transaction and invoice, and a subscription can't be credited twice within 20 days.
- Spending and the balance check happen in one database transaction, so two uploads at once can't overspend.
- Every change is in a ledger the customer sees on their Checks page and admins see on the user's page.

**Monthly plan**: card only (renewals need a saved card). Each paid month adds 80 checks that last two months (this month plus one carried over). Customers can update their card or cancel from the Checks page; a failed renewal emails them.

**Currency**: prices are in USD. Paystack Kenya charges 3.8% on international cards, 2.9% on local cards and 1.5% on M-Pesa, and M-Pesa payments are in KES. To accept M-Pesa, set `CURRENCY_CODE = "KES"` and KES prices in `app/pricing.py`, and run `paystack-create-plan` again.

**Admins** can add or remove checks with a reason (for example a manual M-Pesa payment), refund an upload's checks once, and see all payments under **Payments**.

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
