# Testing

## Automated tests

```bash
pip install -r requirements-dev.txt
pytest
```

About 190 tests cover sign-up and login, access between customers and reviewers, uploads, the whole
review journey and its notifications (`tests/test_journey.py`), payments with a simulated Paystack,
the Kenya link, pricing, SEO, the blog's HTML cleaner and images, and SMS.

GitHub runs them on every pull request and every push to `main` (`.github/workflows/tests.yml`). A
second job builds the production Docker image from `requirements.txt` alone and loads real pages, so a
missing package or a start-up error shows up as a red check before it reaches the live site.

## Trying it yourself on a test copy

Use a separate copy of the site (a staging deploy, or your own computer), never the live one.

1. **Settings.** Copy `.env.example` to `.env` and set:
   - `APP_ENV=staging`. Anything but `production` keeps search engines out and allows the demo command.
   - `PAYSTACK_SECRET_KEY=sk_test_…`: your Paystack **test** secret key. No real money moves.
   - Optional: `SMS_PROVIDER=africastalking` with `AT_USERNAME=sandbox` and your sandbox API key. Texts then appear in Africa's Talking's simulator instead of on a phone.
   - Optional: SMTP settings for a test inbox service, so emails land somewhere you can read them.
2. **Demo accounts.** `flask --app wsgi seed-demo --phone +2547XXXXXXXX` (your own number). That creates a reviewer, a USD customer and a KES customer with 5 checks each, all with password `demo-pass-2026`. The `.test` addresses can't receive email, so watch **Admin > Messages** to see what would have been sent.
3. **Paystack test payments.** Use Paystack's test details from their [test payments page](https://paystack.com/docs/payments/test-payments/). At the time of writing that's card 4084 0840 8408 4081, any future expiry, CVV 408. For M-Pesa (KES): +254 710 000 000. Paystack can only reach your webhook on a public address. On your own computer, payments still complete through the return page; renewals of the monthly plan need a deployed test copy.

## Run-through checklist

Use two browsers, or one normal window and one private window: one as the customer, one as the reviewer.

| # | Do this | You should see |
|---|---|---|
| 1 | Sign up as a new customer with your mobile number | Dashboard; Checks 0 |
| 2 | Checks → buy a single check with the test card | Back on Checks with 1 check and the payment in History |
| 3 | Upload a .docx or PDF | Document page with a live timer. The reviewer gets a notification (and an email if set up) |
| 4 | Reviewer: Queue → open it → **Start reviewing** | Customer page says In review (it updates by itself) |
| 5 | Reviewer: send a message with **This is a question** ticked | Customer sees Needs your input and gets an email; the timer pauses |
| 6 | Customer: reply | Back to In review; the reviewer is notified |
| 7 | Reviewer: write notes, attach a marked-up file, **Send results** | The confirmation says exactly what went out. Customer gets in-app + email + text, and their page shows the notes and the file |
| 8 | Customer: download the marked-up file | The reviewer's file |
| 9 | Reviewer: on the document, **Refund 1 check** | Customer balance goes back up, with a notification |
| 10 | Admin → Messages | Every email and text from this run, with Sent or Not set up |
| 11 | Open `/local` in a private window and sign up with 07XX XXX XXX | Prices in KSh; buy with the M-Pesa test number |
| 12 | Admin → Blog → New post, write, add a cover, **Publish** | The post on `/blog` with its cover and table of contents |
| 13 | Open `/robots.txt` on the test copy | `Disallow: /`, so it can't compete with the live site |

If anything doesn't match, note the step number and what you saw.
