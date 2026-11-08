#!/bin/sh
# Back up the database and uploaded files, keeping 14 days. Run nightly from cron:
#   0 3 * * * /root/nerdaccess/deploy/backup.sh >> /var/log/nerdaccess-backup.log 2>&1
# These copies live on the same server, so also turn on DigitalOcean's Droplet backups.
set -eu
cd "$(dirname "$0")/.."
DEST="${BACKUP_DIR:-/root/backups}"
STAMP="$(date -u +%Y%m%d-%H%M)"
mkdir -p "$DEST"
# SQLite's own backup API gives a consistent copy even while the site is in use.
docker compose exec -T app python -c "
import sqlite3
src = sqlite3.connect('/data/nerdaccess.sqlite3')
dst = sqlite3.connect('/data/backup.sqlite3')
src.backup(dst)
dst.close()
"
docker compose exec -T app tar -C /data -czf - backup.sqlite3 uploads > "$DEST/nerdaccess-$STAMP.tar.gz"
docker compose exec -T app rm -f /data/backup.sqlite3
find "$DEST" -name 'nerdaccess-*.tar.gz' -mtime +14 -delete
echo "$(date -u) backup ok: $DEST/nerdaccess-$STAMP.tar.gz ($(du -h "$DEST/nerdaccess-$STAMP.tar.gz" | cut -f1))"
