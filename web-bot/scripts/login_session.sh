#!/bin/bash
# Headful-логин Zoom v persistent-profille (NT-5): Xvfb + x11vnc + noVNC.
# Ispol'zovanie:
#   docker compose run --rm --no-deps -p 8080:6080 web-bot bash scripts/login_session.sh
# Zavesti brauzer na http://localhost:8080/vnc.html, vojti v Zoom-akkaunt
# operatora, zatym zavershit' (Ctrl-C v etom terminale).
set -e
Xvfb :99 -screen 0 1440x900x24 &
sleep 1
x11vnc -display :99 -forever -nopw -shared -rfbport 5900 &
sleep 1
websockify --web=/usr/share/novnc 6080 localhost:5900 &
sleep 1
export DISPLAY=:99
node src/login_session.js "${1:-https://app.zoom.us/wc/home}"
# po vyhodu iz skripta — ubrat' fony
kill %1 %2 %3 2>/dev/null || true
