#!/bin/bash
# NerdAccess: sets up a new DigitalOcean Droplet. Run it as root in the Droplet's console:
#   curl -fsSL https://raw.githubusercontent.com/EboMutele/checkassist/main/deploy/setup.sh -o setup.sh && bash setup.sh
# or paste it into Create Droplet > Advanced options > Add initialization scripts (user data).
# Safe to run again if the console disconnects. Progress log: /var/log/nerdaccess-setup.log
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run this as root: sudo bash setup.sh"; exit 1; }
exec > >(tee -a /var/log/nerdaccess-setup.log) 2>&1
echo "== NerdAccess setup started $(date -u)"

DOMAIN=nerdaccess.store
DIR=/root/nerdaccess
REPO=https://github.com/EboMutele/checkassist.git
export DEBIAN_FRONTEND=noninteractive HOME=/root

# 1. Swap space, so building updates never runs out of memory
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile \
    && echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# 2. Docker and git (both already on the Docker Marketplace image)
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh
command -v git >/dev/null || apt-get -o DPkg::Lock::Timeout=600 install -y git
systemctl enable --now docker
docker compose version
# The Docker image turns on ufw; open the web ports there too (DigitalOcean's own firewall is optional)
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp
fi

# 3. The code
[ -d "$DIR/.git" ] || git clone "$REPO" "$DIR"
cd "$DIR"

# 4. Settings, with a random SECRET_KEY made here so it never leaves the server
if [ ! -f .env ]; then
  cp .env.example .env.new
  python3 - "$DOMAIN" <<'PY'
import re, secrets, sys
values = {"SECRET_KEY": secrets.token_hex(32), "BASE_URL": f"https://{sys.argv[1]}",
          "SESSION_COOKIE_SECURE": "1", "REDIRECT_TO_BASE_URL": "1"}
text = open(".env.new").read()
for key, value in values.items():
    text, found = re.subn(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
    if not found:
        text += f"\n{key}={value}\n"
open(".env.new", "w").write(text)
PY
  chmod 600 .env.new && mv .env.new .env
fi

# 5. Build and start the app (the HTTPS front door starts in step 9)
docker compose up -d --build app

# 6. Nightly backups at 03:00 UTC, kept for 14 days
echo "0 3 * * * root $DIR/deploy/backup.sh >> /var/log/nerdaccess-backup.log 2>&1" > /etc/cron.d/nerdaccess-backup

# 7. One command to put new code live later: nerdaccess-update
cat > /usr/local/bin/nerdaccess-update <<EOF
#!/bin/sh
set -e
cd $DIR && git pull --ff-only && docker compose up -d --build && docker image prune -f
EOF
chmod +x /usr/local/bin/nerdaccess-update

# 8. Next steps, shown each time you open the console
cat > /etc/motd <<'EOF'

  NerdAccess  (code in /root/nerdaccess, setup log /var/log/nerdaccess-setup.log)

  First time only:
    cd ~/nerdaccess && docker compose exec app flask --app wsgi create-admin
  Take payments: add PAYSTACK_SECRET_KEY with  nano ~/nerdaccess/.env  then
    cd ~/nerdaccess && docker compose up -d
  Put new code live:   nerdaccess-update
  Watch the app:       cd ~/nerdaccess && docker compose logs -f app

EOF

# 9. Wait until the internet sees nerdaccess.store pointing at this server, then start Caddy so it
#    gets the HTTPS certificate on the first try (asking too early gets you rate-limited for an hour).
#    Public resolvers are what Let's Encrypt sees: they catch nameservers still at the registrar and
#    old answers still cached, which asking DigitalOcean's own DNS would miss.
META=http://169.254.169.254/metadata/v1
MINE="$(echo $(curl -fs $META/interfaces/public/0/ipv4/address || true) $(curl -fs $META/reserved_ip/ipv4/ip_address || true) $(curl -fs $META/floating_ip/ipv4/ip_address || true))"
seen() {  # the IPv4 addresses public DNS gives for $1
  if command -v dig >/dev/null; then
    { dig +short A "$1" @1.1.1.1; dig +short A "$1" @8.8.8.8; } 2>/dev/null | grep -E '^[0-9]+(\.[0-9]+){3}$' | sort -u
  else
    getent ahostsv4 "$1" 2>/dev/null | awk '{print $1}' | sort -u
  fi
}
shown() { out="$(echo $("$@" 2>/dev/null || true))"; echo "${out:-nothing yet}"; }
points_here() {  # every address the internet sees for $1 is this server's
  ips="$(seen "$1" || true)"
  [ -n "$ips" ] || return 1
  for ip in $ips; do case " $MINE " in *" $ip "*) ;; *) return 1 ;; esac; done
}
for i in $(seq 1 240); do
  if points_here "$DOMAIN" && points_here "www.$DOMAIN"; then echo "DNS points here ($MINE)"; break; fi
  if [ $((i % 10)) = 1 ]; then
    echo "Waiting for DNS. This server is $MINE; the internet sees $DOMAIN at: $(shown seen "$DOMAIN")" \
         "and www at: $(shown seen "www.$DOMAIN")."
    if command -v dig >/dev/null; then
      echo "  Nameservers in use: $(shown dig +short NS "$DOMAIN" @1.1.1.1) (records only count where these point)"
    fi
  fi
  sleep 30
done
docker compose up -d
echo "== NerdAccess setup finished $(date -u). Open https://$DOMAIN"
