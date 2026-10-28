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

# The bridge starts `ssh ... tail -F maind.log`; when the bridge is killed the
# SSH child can survive and pile up across restarts.  Drop those watchers and
# the shared control socket so a stale tunnel cannot make the next command hang.
pkill -f 'tail -n 0 -F /data/quasar/daemons_logs/maind.log' 2>/dev/null || true
pkill -f 'ytstation-ctl \[mux\]' 2>/dev/null || true
rm -f "$HOME/.ssh/ytstation-ctl"
