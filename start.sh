#!/bin/sh
# Start the YouTube bridge and the localhost.run tunnel if they are not running.
cd "$(dirname "$0")"

# Optional local config written by install.sh (station IP/port/user).
[ -f station.env ] && . ./station.env

if [ -f pinproxy.pid ] && kill -0 "$(cat pinproxy.pid)" 2>/dev/null; then
    echo "pinproxy already running: $(cat pinproxy.pid)"
else
    nohup python3 -u pinproxy.py >> pinproxy.log 2>&1 &
    echo $! > pinproxy.pid
    echo "pinproxy started: $(cat pinproxy.pid)"
fi

if [ -f bridge.pid ] && kill -0 "$(cat bridge.pid)" 2>/dev/null; then
    echo "bridge already running: $(cat bridge.pid)"
else
    nohup python3 -u bridge.py >> bridge.log 2>&1 &
    echo $! > bridge.pid
    echo "bridge started: $(cat bridge.pid)"
fi

if [ -f localhostrun.pid ] && kill -0 "$(cat localhostrun.pid)" 2>/dev/null; then
    echo "tunnel already running: $(cat localhostrun.pid)"
else
    nohup ssh -T -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=10 -o ServerAliveInterval=30 \
        -R 80:localhost:8889 nokey@localhost.run >> localhostrun.log 2>&1 &
    echo $! > localhostrun.pid
    echo "tunnel started: $(cat localhostrun.pid)"
    sleep 4
fi

# Give the bridge a moment to bind its ports before checking the tunnel.
for _ in 1 2 3 4 5; do
    curl -fsS --max-time 2 http://127.0.0.1:8888/health >/dev/null 2>&1 && break
    sleep 1
done

URL=$(grep -aoE 'https://[a-z0-9]+\.lhr\.life' localhostrun.log 2>/dev/null | tail -1)
if [ ! -x "$HOME/.local/opt/tailscale_1.104.2_amd64/tailscaled" ] && [ -n "$URL" ] \
        && ! curl -fsS --max-time 6 "$URL/health" 2>/dev/null | grep -q '"ok"'; then
    echo "warning: $URL is not answering; if the skill stops working, run ./stop.sh && ./start.sh"
fi
URL=""
IP=$(python3 -c 'import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(("8.8.8.8",80)); print(s.getsockname()[0])' 2>/dev/null)
[ -n "$IP" ] || IP="127.0.0.1"

# --- Tailscale Funnel: stable public webhook --------------------------------
# Runs in userspace mode, so no root is needed. The node name and the Funnel
# address do not change between restarts.
TS_DIR="${TS_DIR:-$HOME/.local/opt/tailscale_1.104.2_amd64}"
TS_SOCK="$HOME/.local/state/tailscale/tailscaled.sock"
TS_STATE="$HOME/.local/state/tailscale/tailscaled.state"
TS_URL=""

if [ -x "$TS_DIR/tailscaled" ]; then
    if "$TS_DIR/tailscale" --socket="$TS_SOCK" status >/dev/null 2>&1; then
        echo "tailscaled already running"
    else
        mkdir -p "$(dirname "$TS_STATE")"
        nohup "$TS_DIR/tailscaled" --tun=userspace-networking \
            --state="$TS_STATE" --socket="$TS_SOCK" \
            >> "$(dirname "$TS_STATE")/tailscaled.log" 2>&1 &
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            [ -S "$TS_SOCK" ] && break
            sleep 1
        done
        echo "tailscaled started"
    fi
    "$TS_DIR/tailscale" --socket="$TS_SOCK" funnel --bg 8889 >/dev/null 2>&1
    TS_URL=$("$TS_DIR/tailscale" --socket="$TS_SOCK" status --json 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    name = (d.get("Self") or {}).get("DNSName", "").rstrip(".")
    print("https://" + name if name else "")
except Exception:
    print("")
' 2>/dev/null)
fi

# localhost.run stays as a fallback; its domain rotates, so it is not the
# address the skill should use while Funnel is available.
URL=${TS_URL:-$(grep -aoE 'https://[a-z0-9]+\.lhr\.life' localhostrun.log 2>/dev/null | tail -1)}

echo "web ui:  http://$IP:8888/"
echo "webhook: ${URL:-<tunnel not ready>}/alice"
