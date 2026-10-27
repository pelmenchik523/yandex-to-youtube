#!/bin/sh
# Stop the bridge (and its station playback) and the local YouTube proxy.
cd "$(dirname "$0")"

curl -s --max-time 5 http://127.0.0.1:8888/api/stop >/dev/null 2>&1

for f in bridge.pid pinproxy.pid; do
    if [ -f "$f" ]; then
        pid=$(cat "$f")
        kill "$pid" 2>/dev/null
        rm -f "$f"
        echo "stopped $f (pid $pid)"
    fi
done
