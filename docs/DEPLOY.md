# Putting NerdAccess live on nerdaccess.store (DigitalOcean)

One Droplet (a small Linux server) runs two containers: the app, and Caddy in front of it. Caddy gets
and renews the HTTPS certificate for nerdaccess.store by itself, sends www to the bare domain, and
compresses pages. The database and uploaded files live on a Docker volume on the same Droplet.

Allow about an hour, most of it waiting for DNS.

## 1. Create the Droplet

In DigitalOcean: **Create → Droplets**.

- **Region:** London (LON1) is a good middle point for US, UK and Kenyan visitors.
- **Image:** Marketplace → **Docker** (Ubuntu with Docker and Docker Compose already installed).
- **Size:** Basic, Regular, **2 GB / 1 vCPU ($12/month)**. The 1 GB plan ($6) runs it, but resizing
  large cover images and building updates can run out of memory.
- **Backups:** turn on **weekly backups** (20% of the Droplet price). This copies the whole server,
  including the database and files.
- **Authentication:** SSH key (add yours), not a password.
- **Hostname:** `nerdaccess`.

Note the Droplet's **IPv4 address**.

## 2. Firewall

**Networking → Firewalls → Create firewall**, apply it to the Droplet:

| Type | Protocol | Port | Sources |
|---|---|---|---|
| SSH | TCP | 22 | Your IP if it's fixed; otherwise all |
| HTTP | TCP | 80 | All |
| HTTPS | TCP | 443 | All |
| Custom | UDP | 443 | All (HTTP/3) |

Use DigitalOcean's firewall rather than `ufw` on the server: Docker opens published ports past `ufw`.

## 3. Point the domain at it

Where you bought nerdaccess.store, open its DNS settings and add:

| Type | Name | Value |
|---|---|---|
| A | `@` | the Droplet's IPv4 address |
| A | `www` | the same address |

Or let DigitalOcean run your DNS: **Networking → Domains → add nerdaccess.store**, add the same two
records there, and at your registrar change the nameservers to `ns1.digitalocean.com`,
`ns2.digitalocean.com` and `ns3.digitalocean.com`. Either way, changes can take from minutes to a few
hours. Check with `dig +short nerdaccess.store` (it should print the Droplet's address).

## Quick way: steps 4 to 6 and 8 in one command

In the Droplet's console (as root):

```bash
curl -fsSL https://raw.githubusercontent.com/EboMutele/checkassist/main/deploy/setup.sh -o setup.sh && bash setup.sh
```

It downloads the code, writes `.env` with a fresh `SECRET_KEY`, starts the site, waits until
nerdaccess.store points at the Droplet before asking for the HTTPS certificate, sets up nightly
backups, and turns on auto-update (below). Run it again if the console
disconnects. Then create your admin login (step 6) and add Paystack (step 7). The rest of this
section is the same thing by hand. The script clones over HTTPS, so it needs the repository to be
public; for a private one, follow step 4.

## Auto-update from main

`deploy/autodeploy.sh` checks GitHub every 2 minutes. When main has a new commit it pulls it, rebuilds,
and checks the app answers `/` and `/robots.txt`. If it doesn't, it puts the previous version back and
skips that commit until a newer one lands, so a broken merge can't keep the site down. Merging a PR is
all it takes to go live.

- Turn it on (servers set up before it existed): `cd ~/nerdaccess && git pull && bash deploy/autodeploy.sh --install`
- What happened: `tail -20 /var/log/nerdaccess-deploy.log`
- Deploy right now: `nerdaccess-update`
- Pause: `touch /root/nerdaccess.pause-deploys`; resume: `rm /root/nerdaccess.pause-deploys`

## 4. Get the code onto the server

```bash
ssh root@YOUR_DROPLET_IP

# A read-only key so the server can pull the private repository
ssh-keygen -t ed25519 -f ~/.ssh/nerdaccess_deploy -N "" -C nerdaccess-droplet
cat ~/.ssh/nerdaccess_deploy.pub
```

Add that public key on GitHub: the repository → **Settings → Deploy keys → Add deploy key** (leave
"Allow write access" off). Then:

```bash
cat >> ~/.ssh/config <<'CFG'
Host github.com
  IdentityFile ~/.ssh/nerdaccess_deploy
CFG
git clone git@github.com:EboMutele/checkassist.git ~/nerdaccess
cd ~/nerdaccess
```

## 5. Settings

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"   # paste as SECRET_KEY
nano .env
```

Set at least:

```
SECRET_KEY=…the long random value…
BASE_URL=https://nerdaccess.store
SESSION_COOKIE_SECURE=1
REDIRECT_TO_BASE_URL=1
PAYSTACK_SECRET_KEY=sk_live_…        # sk_test_… while you're still testing
SUPPORT_EMAIL=support@nerdaccess.store
```

plus the email and SMS settings from steps 9 and 10 when you have them. Leave `APP_ENV`,
`PROXY_COUNT`, `DATABASE_PATH` and `UPLOAD_DIR` alone: `docker-compose.yml` sets them.

## 6. Start it

```bash
docker compose up -d --build
docker compose exec app flask --app wsgi create-admin    # your reviewer login
docker compose logs -f caddy                             # watch it get the certificate; Ctrl+C to stop
```

Open **https://nerdaccess.store**. If the certificate step fails, DNS isn't pointing at the Droplet
yet (step 3); Caddy keeps retrying on its own.

Quick checks:
- `https://www.nerdaccess.store` sends you to `https://nerdaccess.store`
- `https://nerdaccess.store/robots.txt` lists a `Sitemap:` line, not `Disallow: /`

## 7. Paystack

In the Paystack dashboard, **Settings → API Keys & Webhooks**:
- Webhook URL: `https://nerdaccess.store/billing/webhook`
- Callback URL: `https://nerdaccess.store/billing/callback`

Then create the monthly plan once and save its code in `.env`:

```bash
docker compose exec app flask --app wsgi paystack-create-plan
nano .env      # PAYSTACK_MONTHLY_PLAN_CODE=PLN_…
docker compose up -d
```

## 8. Backups

DigitalOcean's weekly backups (step 1) cover the whole server. For nightly copies of just the data:

```bash
crontab -e
# add:
0 3 * * * /root/nerdaccess/deploy/backup.sh >> /var/log/nerdaccess-backup.log 2>&1
```

Those copies sit on the same Droplet. Download one now and then, or copy them to DigitalOcean Spaces,
so a lost server doesn't take every copy with it.

## 9. Email from @nerdaccess.store

You need two things:

- **Sending** (receipts, "feedback ready", questions): an email-sending service such as Postmark,
  Resend, Brevo or Amazon SES. Add nerdaccess.store in their dashboard. They give you DNS records
  (SPF, DKIM, and a return-path or bounce record) to add where your DNS lives. Then fill the `SMTP_*`
  settings and `SMTP_FROM=NerdAccess <no-reply@nerdaccess.store>`.
- **Receiving** at support@nerdaccess.store: a mailbox (Zoho Mail, Google Workspace) or a forwarding
  service to your existing inbox. Each gives you MX records to add.

Also add a DMARC record so mail providers trust your domain:

| Type | Name | Value |
|---|---|---|
| TXT | `_dmarc` | `v=DMARC1; p=none; rua=mailto:support@nerdaccess.store` |

Keep **one** SPF record (`v=spf1 …`) that includes every service that sends as nerdaccess.store.
Two SPF records break both. **Admin → Messages** shows whether emails went out.

## 10. Texts

Set `SMS_PROVIDER` and that provider's keys (see `.env.example`), then `docker compose up -d`.
Africa's Talking suits Kenyan numbers. Twilio covers the US and UK, but texts to US numbers need
the sending number registered first (10DLC or toll-free verification), or carriers block them.

## 11. Google

Add **nerdaccess.store** as a **Domain** property in Google Search Console. It gives you a TXT
record to add to DNS. Then submit `https://nerdaccess.store/sitemap.xml`.

## Day to day

| Task | Command (in `~/nerdaccess`) |
|---|---|
| Deploy the latest code | automatic within 2 minutes of a merge; now: `nerdaccess-update` |
| See what auto-update did | `tail -20 /var/log/nerdaccess-deploy.log` |
| Watch the app | `docker compose logs -f app` |
| Restart | `docker compose restart app` |
| Change a setting | edit `.env`, then `docker compose up -d` |
| Add a reviewer | `docker compose exec app flask --app wsgi create-admin` |
| Back up now | `deploy/backup.sh` |

Ubuntu installs security updates by itself. Run `apt update && apt upgrade` and reboot now and then
for the rest.
