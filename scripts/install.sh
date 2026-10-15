#!/bin/bash
# Install/refresh the matched betting app. Idempotent. Run with sudo from the project directory.
#  * system user bets (no login, no home)
#  * var/ (database, login hash, session key, backups) owned by bets, mode 0700
#  * .venv from requirements.txt, database migrations
#  * first install only: a random login, written to ~$OWNER/.config/matched-betting-login.txt (0600)
#  * systemd service (127.0.0.1:8760) and daily backup timer
# nginx is configured separately (deploy/nginx/, see README).
set -euo pipefail
[ "$EUID" -eq 0 ] || { echo "Run with sudo."; exit 1; }
APP=/opt/matched-betting
SVC=bets
OWNER="${SUDO_USER:-$USER}"
cd "$APP"
if ! id "$SVC" >/dev/null 2>&1; then
  useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$SVC"
fi
install -d -o "$SVC" -g "$SVC" -m 0700 "$APP/var" "$APP/var/backups"
if [ ! -x "$APP/.venv/bin/python" ]; then sudo -u "$OWNER" python3 -m venv "$APP/.venv"; fi
sudo -u "$OWNER" "$APP/.venv/bin/pip" install --quiet --disable-pip-version-check -r "$APP/requirements.txt"
sudo -u "$SVC" "$APP/.venv/bin/python" -m bets.cli migrate
if [ ! -e "$APP/var/auth.json" ]; then
  TARGET=/home/$OWNER/.config/matched-betting-login.txt
  install -d -o "$OWNER" -g "$OWNER" -m 0700 "/home/$OWNER/.config"
  ( umask 077; sudo -u "$SVC" "$APP/.venv/bin/python" -m bets.cli init-login - "$OWNER" > "$TARGET" )
  chown "$OWNER:$OWNER" "$TARGET"; chmod 0600 "$TARGET"
  echo "Initial login written to $TARGET (0600). Move it to your password manager, then delete the file."
fi
for unit in matched-betting.service matched-betting-backup.service matched-betting-backup.timer; do
  install -o root -g root -m 0644 "$APP/deploy/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --quiet matched-betting.service matched-betting-backup.timer
systemctl restart matched-betting.service
systemctl start matched-betting-backup.timer
for i in $(seq 1 20); do curl -fsS -o /dev/null http://127.0.0.1:8760/bets/healthz && break; sleep 1; done
systemctl is-active matched-betting.service
