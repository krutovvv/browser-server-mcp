#!/bin/sh
set -e
# Config: /data/config.json overrides the baked default (copied on first start so it can be edited)
[ -f /data/config.json ] || cp /etc/browser/config.json /data/config.json
mkdir -p /data/profile /data/output
# snapshots/console logs from MCP pile up in /data/output: keep 2 days
( while true; do find /data/output -type f -mmin +2880 -delete 2>/dev/null; sleep 3600; done ) &
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 "$SCREEN" -nolisten tcp >/dev/null 2>&1 &
sleep 1
# VNC for manual captcha / login: noVNC web client on :6080 -> http://host:6080/
# x11vnc listens on localhost only. No password unless VNC_PASSWORD is set; VNC=0 disables it.
if [ "${VNC:-1}" = "1" ]; then
    if [ -n "$VNC_PASSWORD" ]; then
        x11vnc -storepasswd "$VNC_PASSWORD" /tmp/vncpass >/dev/null
        VNC_AUTH="-rfbauth /tmp/vncpass"
    else
        VNC_AUTH="-nopw"
    fi
    x11vnc -display :99 -forever -shared -localhost -rfbport 5900 $VNC_AUTH -quiet -bg -o /tmp/x11vnc.log
    websockify --web /usr/share/novnc 6080 localhost:5900 >/tmp/novnc.log 2>&1 &
fi
# --shared-browser-context: all MCP sessions share one persistent Camoufox (one profile, no lock fights).
# live.py is camoufox-playwright-mcp itself (the same flags) plus the live view on the same port, /live.
exec python3 /opt/live.py \
    --config /data/config.json \
    --host 0.0.0.0 --port "$MCP_PORT" --allowed-hosts "*" \
    --user-data-dir /data/profile --output-dir /data/output \
    --shared-browser-context \
    "$@"
