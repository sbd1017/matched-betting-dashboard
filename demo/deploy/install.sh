#!/bin/sh
# Install or update the public demo on this machine. Run from the project folder with sudo:
#
#     sudo demo/deploy/install.sh
#
# Code: the committed HEAD of this checkout (git archive, never var/ or anything uncommitted) into
# /opt/demos/bets/app, with its own virtualenv. Data: /srv/demos/bets/var, owned by the `demo` user.
# Units: demo-bets.service (127.0.0.1:8911) and the nightly reset at 04:00 UK time
# (demo-reset-setup on the mubar.uk host; a plain timer of our own otherwise). Then a fresh set of records.
set -eu
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
SRC=$(cd "$(dirname "$0")/../.." && pwd)
APP=/opt/demos/bets
DATA=/srv/demos/bets
LIB=/usr/local/lib/demos/bets

id demo >/dev/null 2>&1 || useradd --system --home-dir /srv/demos --shell /usr/sbin/nologin demo
install -d -o root -g root -m 0755 /opt/demos "$APP" /usr/local/lib/demos "$LIB"
install -d -o demo -g demo -m 0750 "$DATA"

rm -rf "$APP/app.new"
install -d -o root -g root -m 0755 "$APP/app.new"
git -c safe.directory="$SRC" -C "$SRC" archive --format=tar HEAD | tar -x -C "$APP/app.new"
chown -R root:root "$APP/app.new"
chmod -R u=rwX,go=rX "$APP/app.new"
[ -d "$APP/venv" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --disable-pip-version-check -r "$APP/app.new/requirements.txt"
rm -rf "$APP/app.old"
if [ -d "$APP/app" ]; then mv "$APP/app" "$APP/app.old"; fi
mv "$APP/app.new" "$APP/app"
rm -rf "$APP/app.old"

install -o root -g root -m 0755 "$SRC/demo/deploy/reset" "$LIB/reset"
install -o root -g root -m 0644 "$SRC/demo/deploy/demo-bets.service" /etc/systemd/system/demo-bets.service
systemctl daemon-reload
systemctl enable demo-bets.service

if command -v demo-reset-setup >/dev/null 2>&1; then
    demo-reset-setup bets demo-bets.service
else
    cat > /etc/systemd/system/demo-bets-reset.service <<UNIT
[Unit]
Description=Reset the Matched Betting demo
[Service]
Type=oneshot
User=demo
Group=demo
Environment=DEMO_NAME=bets DEMO_DIR=$DATA
WorkingDirectory=$DATA
ExecStartPre=+/bin/systemctl stop demo-bets.service
ExecStart=$LIB/reset
ExecStopPost=+/bin/systemctl start demo-bets.service
UNIT
    cat > /etc/systemd/system/demo-bets-reset.timer <<UNIT
[Unit]
Description=Reset the Matched Betting demo every night at 04:00 UK time
[Timer]
OnCalendar=*-*-* 04:00:00 Europe/London
Persistent=true
[Install]
WantedBy=timers.target
UNIT
    systemctl daemon-reload
    systemctl enable --now demo-bets-reset.timer
fi
systemctl start demo-bets-reset.service          # fresh records now (stops and starts demo-bets around it)
systemctl --no-pager --lines=0 status demo-bets.service
