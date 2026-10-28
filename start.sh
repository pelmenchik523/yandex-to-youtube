#!/bin/sh
# Start the YouTube bridge and the local YouTube proxy if they are not running.
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
    # Clean up SSH watchers/sockets left by a previously crashed bridge.
    pkill -f 'tail -n 0 -F /data/quasar/daemons_logs/maind.log' 2>/dev/null || true
    pkill -f 'ytstation-ctl \[mux\]' 2>/dev/null || true
    rm -f "$HOME/.ssh/ytstation-ctl"
    nohup python3 -u bridge.py >> bridge.log 2>&1 &
    echo $! > bridge.pid
    echo "bridge started: $(cat bridge.pid)"
fi

# Give the bridge a moment to bind its port.
for _ in 1 2 3 4 5; do
    curl -fsS --max-time 2 http://127.0.0.1:8888/health >/dev/null 2>&1 && break
    sleep 1
done

IP=$(python3 -c 'import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(("8.8.8.8",80)); print(s.getsockname()[0])' 2>/dev/null)
[ -n "$IP" ] || IP="127.0.0.1"

echo "web ui:  http://$IP:8888/"
