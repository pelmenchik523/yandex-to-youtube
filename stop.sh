#!/bin/sh
# Stop the bridge (and its station playback) and the public tunnel.
cd "$(dirname "$0")"

curl -s --max-time 5 http://127.0.0.1:8888/api/stop >/dev/null 2>&1

TS_DIR="${TS_DIR:-$HOME/.local/opt/tailscale_1.104.2_amd64}"
if [ -x "$TS_DIR/tailscale" ]; then
    "$TS_DIR/tailscale" --socket="$HOME/.local/state/tailscale/tailscaled.sock" \
        funnel --https=443 off >/dev/null 2>&1 && echo "funnel off"
fi

for f in bridge.pid localhostrun.pid pinproxy.pid; do
    if [ -f "$f" ]; then
        pid=$(cat "$f")
        kill "$pid" 2>/dev/null
        rm -f "$f"
        echo "stopped $f (pid $pid)"
    fi
done
