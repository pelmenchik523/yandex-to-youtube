#!/bin/sh
# Start the YouTube bridge and the localhost.run tunnel if they are not running.
cd "$(dirname "$0")"

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
if [ -n "$URL" ] && ! curl -fsS --max-time 6 "$URL/health" 2>/dev/null | grep -q '"ok"'; then
    echo "warning: $URL is not answering; if the skill stops working, run ./stop.sh && ./start.sh"
fi
IP=$(python3 -c 'import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(("8.8.8.8",80)); print(s.getsockname()[0])' 2>/dev/null)
[ -n "$IP" ] || IP="127.0.0.1"
echo "web ui:  http://$IP:8888/"
echo "webhook: ${URL:-<tunnel not ready>}/alice"
