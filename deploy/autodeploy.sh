#!/bin/bash
# NerdAccess auto-update: puts each new commit on main live, then checks the app answers. If it
# doesn't, it goes back to the version that was running and skips that commit until a newer one lands.
#
#   Install once, as root in ~/nerdaccess:   bash deploy/autodeploy.sh --install
#   After that it runs every 2 minutes.      Log: /var/log/nerdaccess-deploy.log
#   Deploy right now:                        nerdaccess-update
#   Pause automatic updates:                 touch /root/nerdaccess.pause-deploys   (rm it to resume)
#
# Everything is inside main() so a `git pull` that changes this file can't trip up the copy running.
main() {
  set -uo pipefail
  DIR="${NERDACCESS_DIR:-/root/nerdaccess}"
  LOG="${NERDACCESS_DEPLOY_LOG:-/var/log/nerdaccess-deploy.log}"
  SKIP="${NERDACCESS_SKIP_FILE:-/root/.nerdaccess-skip-commit}"   # the last commit that failed its check
  PAUSE="${NERDACCESS_PAUSE_FILE:-/root/nerdaccess.pause-deploys}"
  LOCK="${NERDACCESS_LOCK:-/run/nerdaccess-deploy.lock}"
  ETC="${NERDACCESS_ETC:-/etc}"
  BIN="${NERDACCESS_BIN:-/usr/local/bin}"
  cd "$DIR" || { echo "no $DIR"; return 1; }
  log() { echo "$(date -u '+%F %T') $*"; }
  now=""

  if [ "${1:-}" = "--install" ]; then
    echo "*/2 * * * * root bash $DIR/deploy/autodeploy.sh >> $LOG 2>&1" > "$ETC/cron.d/nerdaccess-autodeploy"
    printf '%s\n' "$LOG /var/log/nerdaccess-backup.log /var/log/nerdaccess-setup.log {" \
      "  weekly" "  rotate 8" "  compress" "  missingok" "  notifempty" "}" > "$ETC/logrotate.d/nerdaccess"
    printf '#!/bin/sh\nbash %s/deploy/autodeploy.sh --now 2>&1 | tee -a %s\n' "$DIR" "$LOG" > "$BIN/nerdaccess-update"
    chmod +x "$BIN/nerdaccess-update"
    log "auto-update installed: checks main every 2 minutes. Deploying now."
    now=1
  fi
  [ "${1:-}" = "--now" ] && now=1

  exec 9>"$LOCK"
  flock -n 9 || return 0                       # another deploy is running
  [ -z "$now" ] && [ -e "$PAUSE" ] && return 0

  git fetch -q origin main || { log "could not reach GitHub (git fetch failed)"; return 1; }
  current="$(git rev-parse HEAD)"
  target="$(git rev-parse origin/main)"
  if [ -z "$now" ]; then
    [ "$current" = "$target" ] && return 0      # nothing new
    [ "$target" = "$(cat "$SKIP" 2>/dev/null)" ] && return 0   # already failed once; wait for a newer commit
  fi

  log "deploying $(git log -1 --format='%h %s' "$target") (running: $(git rev-parse --short HEAD))"
  if ! git merge -q --ff-only "$target"; then
    log "main can't fast-forward from the running version; nothing changed. Fix by hand: git status"
    return 1
  fi
  if docker compose up -d --build && healthy; then
    rm -f "$SKIP"
    docker image prune -f >/dev/null 2>&1
    log "live: $(git log -1 --format='%h %s')"
    return 0
  fi

  log "the new version did not answer; going back to $(git rev-parse --short "$current"). App log:"
  docker compose logs --tail 25 app 2>&1 | sed 's/^/    /'
  echo "$target" > "$SKIP"
  git reset -q --hard "$current"
  if docker compose up -d --build && healthy; then
    log "rolled back: the site is running $(git rev-parse --short HEAD) again. $(git rev-parse --short "$target") will be skipped until a newer commit lands on main."
  else
    log "ROLLBACK DID NOT ANSWER EITHER. Check: cd $DIR && docker compose logs --tail 50 app"
  fi
  return 1
}

# The app answers "/" and "/robots.txt" with 200 from inside its container (up to ~90 seconds to start).
healthy() {
  local host
  host="$(sed -n 's#^BASE_URL=https\?://\([^/:]*\).*#\1#p' .env 2>/dev/null | head -n1)"
  for _ in $(seq 1 30); do
    if docker compose exec -T app python - "${host:-localhost}" >/dev/null 2>&1 <<'PY'
import http.client, sys
for path in ("/", "/robots.txt"):
    conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=5)
    conn.request("GET", path, headers={"Host": sys.argv[1], "X-Forwarded-Proto": "https"})
    if conn.getresponse().status != 200:
        sys.exit(1)
PY
    then return 0; fi
    sleep "${NERDACCESS_HEALTH_WAIT:-3}"
  done
  return 1
}

main "$@"
exit $?
