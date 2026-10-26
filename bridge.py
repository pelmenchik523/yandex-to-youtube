#!/usr/bin/env python3
"""
YouTube -> Yandex Station Lite 2 bridge.

Audio path:
    yt-dlp (search/download) -> MP3 file -> local HTTP server -> Station playbin

Control path:
    Alice skill webhook (HTTPS tunnel) -> this process -> SSH -> maind --gst-launch

Run:
    python3 bridge.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
CACHE = BASE / "cache"
CACHE.mkdir(parents=True, exist_ok=True)
WEB_DIR = BASE / "web"

YTDLP = os.environ.get("YTDLP") or shutil.which("yt-dlp") or str(Path.home() / ".local/bin/yt-dlp")
STATION_HOST = os.environ.get("STATION_HOST", "192.168.1.206")
STATION_PORT = int(os.environ.get("STATION_PORT", "2222"))
STATION_USER = os.environ.get("STATION_USER", "root")
STATION_GST = "/system/vendor/quasar/maind --gst-launch"
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8888"))
SKILL_PORT = int(os.environ.get("SKILL_PORT", "8889"))
PLAYER_MODE = os.environ.get("PLAYER_MODE", "gst")  # gst | glagol (glagol not implemented yet)
START_DELAY = float(os.environ.get("START_DELAY", "2.0"))
AUTO_NEXT = os.environ.get("AUTO_NEXT", "1") == "1"
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

SETTINGS_FILE = Path(os.environ.get("BRIDGE_SETTINGS_FILE", str(BASE / "settings.json")))
PLAYLISTS_FILE = Path(os.environ.get("BRIDGE_PLAYLISTS_FILE", str(BASE / "playlists.json")))
COOKIES_FILE = Path(os.environ.get("BRIDGE_COOKIES_FILE", str(BASE / "cookies.txt")))

DEFAULT_SETTINGS = {
    # Firefox on the PC already has the user's YouTube session.
    "cookies_from_browser": "firefox",
    "cookies_file": "",
    "js_runtimes": "node",
    "playlist_limit": 50,
    # Optional: http://host:port or socks5://host:port for blocked YouTube.
    "proxy": "",
}

PLAYLIST_NAMES = {
    "LL": "Мне понравилось",
    "WL": "Смотреть позже",
}

YT_STATUS_CACHE: dict = {"at": 0.0, "data": None}
PLAYLIST_LOAD: dict = {"id": 0}


def load_settings() -> dict:
    data = dict(DEFAULT_SETTINGS)
    try:
        if SETTINGS_FILE.exists():
            loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
    except Exception:
        pass
    if os.environ.get("YT_COOKIES"):
        data["cookies_file"] = os.environ["YT_COOKIES"]
        data["cookies_from_browser"] = ""
    if os.environ.get("YT_COOKIES_FROM_BROWSER"):
        data["cookies_from_browser"] = os.environ["YT_COOKIES_FROM_BROWSER"]
        data["cookies_file"] = ""
    return data


def save_settings(patch: dict) -> dict:
    data = load_settings()
    data.update(patch)
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return data


def public_settings(settings: dict | None = None) -> dict:
    s = settings or load_settings()
    cfile = str(s.get("cookies_file") or "").strip()
    try:
        limit = int(s.get("playlist_limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    return {
        "cookies_from_browser": str(s.get("cookies_from_browser") or ""),
        "cookies_file": cfile,
        "cookies_file_exists": bool(cfile) and Path(cfile).expanduser().exists(),
        "js_runtimes": str(s.get("js_runtimes") or ""),
        "playlist_limit": limit,
        "proxy": str(s.get("proxy") or ""),
    }


def _invalidate_yt_status() -> None:
    YT_STATUS_CACHE["at"] = 0.0
    YT_STATUS_CACHE["data"] = None


def update_youtube_settings(patch: dict) -> dict:
    """Save only the YouTube-related settings, with light validation."""
    allowed = {
        "cookies_from_browser",
        "cookies_file",
        "js_runtimes",
        "playlist_limit",
        "proxy",
    }
    clean: dict = {}
    for key, value in patch.items():
        if key not in allowed:
            continue
        if key == "playlist_limit":
            try:
                clean[key] = max(1, min(500, int(value)))
            except (TypeError, ValueError):
                continue
        else:
            clean[key] = str(value or "").strip()
    data = save_settings(clean)
    _invalidate_yt_status()
    return {"ok": True, "settings": public_settings(data)}


def ytdlp_cookie_args(settings: dict | None = None) -> list[str]:
    s = settings or load_settings()
    cfile = str(s.get("cookies_file") or "").strip()
    if cfile:
        path = Path(cfile).expanduser()
        if path.exists():
            return ["--cookies", str(path)]
    browser = str(s.get("cookies_from_browser") or "").strip()
    if browser and browser != "none":
        return ["--cookies-from-browser", browser]
    return []


def ytdlp_base(settings: dict | None = None) -> list[str]:
    """Common yt-dlp arguments: JS challenge solver + YouTube cookies."""
    s = settings or load_settings()
    args = [YTDLP, "--no-warnings", "--force-ipv4"]
    proxy = str(s.get("proxy") or "").strip()
    if proxy:
        args += ["--proxy", proxy]
    js = str(s.get("js_runtimes") or "").strip()
    if js:
        args += ["--js-runtimes", js]
    args += ytdlp_cookie_args(s)
    return args


def current_public_url() -> str:
    """Return the newest localhost.run URL from its log, if available."""
    try:
        log = (BASE / "localhostrun.log").read_text(encoding="utf-8", errors="ignore")
        urls = re.findall(r"https://[a-z0-9]+\.lhr\.life", log)
        if urls:
            return urls[-1].rstrip("/")
    except OSError:
        pass
    return PUBLIC_URL


def lan_ip() -> str:
    """Best-effort LAN IP of this machine."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


PC_IP = os.environ.get("PC_IP") or lan_ip()

SSH_BASE = [
    "ssh",
    "-T",
    "-p", str(STATION_PORT),
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=6",
    f"{STATION_USER}@{STATION_HOST}",
]


def ssh(cmd: str, timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        SSH_BASE + [cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# The Station runs Buildroot/busybox and has no pkill/pgrep.  Find the
# gst-launch helper processes through /proc instead and kill only those.
KILL_GST = (
    "for d in /proc/[0-9]*; do "
    "[ \"$d\" = \"/proc/$$\" ] && continue; "
    "[ \"$d\" = \"/proc/$PPID\" ] && continue; "
    "c=$(tr '\\0' ' ' < \"$d/cmdline\" 2>/dev/null); "
    "case \"$c\" in *'maind --gst-launch'*) kill -KILL \"${d#/proc/}\" 2>/dev/null;; esac; "
    "done; true"
)


class State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.pid: int | None = None
        self.track: dict | None = None
        self.queue: list[dict] = []
        self.queue_pos = 0
        self.history: list[dict] = []
        self.history_pos = -1
        self.paused = False
        self.ducked = False
        self.started_at = 0.0
        self.pause_started: float | None = None
        self.paused_total = 0.0
        self.duration = 0.0
        self.generation = 0
        self.last_error = ""
        self.downloading = False
        self.volume = 40
        self.amp_on = True
        self.led = {"mode": "auto", "r": 0, "g": 0, "b": 255, "brightness": 50}
        self.playlist: dict | None = None

    def kill_current(self) -> None:
        """Stop the current GStreamer process but keep queue/history."""
        with self.lock:
            self.generation += 1
            self.paused = False
            self.ducked = False
            self.pid = None
            self.track = None
            self.started_at = 0.0
            self.pause_started = None
            self.paused_total = 0.0
            self.duration = 0.0
        # kill only our gst-launch processes, never the main daemons
        ssh(KILL_GST, timeout=10)

    def stop(self) -> None:
        cancel_playlist_load()
        self.kill_current()
        with self.lock:
            self.track = None
            self.queue = []
            self.queue_pos = 0
            self.history = []
            self.history_pos = -1
            self.playlist = None

    def stop_async(self) -> int:
        """Clear state immediately and stop the old gst process in background.

        Used by the Alice webhook so the answer never waits for SSH.  Only the
        old PID is killed, so a delayed background kill cannot hit a new track.
        """
        with self.lock:
            old_pid = self.pid
            self.generation += 1
            gen = self.generation
            self.paused = False
            self.ducked = False
            self.pid = None
            self.track = None
            self.started_at = 0.0
            self.pause_started = None
            self.paused_total = 0.0
            self.duration = 0.0
            self.queue = []
            self.queue_pos = 0
            self.history = []
            self.history_pos = -1
            self.playlist = None

        if old_pid:
            def bg_kill() -> None:
                try:
                    ssh(
                        f"pid={old_pid}; "
                        "c=$(tr '\\0' ' ' < /proc/$pid/cmdline 2>/dev/null); "
                        'case "$c" in *"maind --gst-launch"*) kill -KILL $pid 2>/dev/null;; esac; '
                        "true",
                        timeout=8,
                    )
                except Exception:
                    pass

            threading.Thread(target=bg_kill, daemon=True).start()
        return gen

    def status(self) -> dict:
        with self.lock:
            elapsed = 0.0
            if self.started_at:
                elapsed = time.time() - self.started_at - self.paused_total
                if self.pause_started:
                    elapsed -= time.time() - self.pause_started
                elapsed = max(0.0, elapsed)
            queue = [
                {"i": i, **self.queue[i]}
                for i in range(self.queue_pos, len(self.queue))
            ][:12]
            return {
                "track": self.track,
                "pid": self.pid,
                "paused": self.paused,
                "ducked": self.ducked,
                "elapsed": round(elapsed, 1),
                "duration": self.duration,
                "queue_len": max(0, len(self.queue) - self.queue_pos),
                "queue": queue,
                "history_len": len(self.history),
                "history_pos": self.history_pos,
                "downloading": self.downloading,
                "last_error": self.last_error,
                "playlist": dict(self.playlist) if self.playlist else None,
                "player": PLAYER_MODE,
                "pc_ip": PC_IP,
                "volume": self.volume,
                "amp_on": self.amp_on,
                "led": dict(self.led),
                "public_url": current_public_url(),
            }


STATE = State()


def cancel_playlist_load() -> None:
    """Invalidate an in-flight playlist fetch without touching playback."""
    with STATE.lock:
        PLAYLIST_LOAD["id"] += 1
        if STATE.playlist and STATE.playlist.get("loading"):
            STATE.playlist = None
            STATE.downloading = False


def yt_search(query: str, limit: int = 1) -> list[dict]:
    cmd = ytdlp_base() + [
        "--flat-playlist",
        "--print", "%(id)s\t%(title)s\t%(duration)s",
        f"ytsearch{limit}:{query}",
    ]
    out = subprocess.check_output(cmd, text=True, timeout=45, stderr=subprocess.DEVNULL)
    items = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0]:
            try:
                dur = float(parts[2]) if len(parts) > 2 and parts[2] not in ("NA", "None", "") else 0.0
            except ValueError:
                dur = 0.0
            items.append({"id": parts[0], "title": parts[1], "duration": dur})
    return items


_PLAYLIST_ID_RE = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")


def parse_playlist_ref(ref: str) -> dict:
    """Accept a playlist URL, a raw playlist id, or the LL/WL shortcuts."""
    raw = (ref or "").strip()
    if not raw:
        raise ValueError("Укажи ссылку или ID плейлиста.")
    if raw.lower() in ("ll", "wl"):
        playlist_id = raw.upper()
    else:
        m = _PLAYLIST_ID_RE.search(raw)
        if m:
            playlist_id = m.group(1)
            if playlist_id.upper() in ("LL", "WL"):
                playlist_id = playlist_id.upper()
            elif len(playlist_id) < 8:
                raise ValueError(
                    "ID плейлиста выглядит слишком коротким. "
                    "Вставь полную ссылку или ID из YouTube."
                )
        elif re.fullmatch(r"[A-Za-z0-9_-]{10,100}", raw):
            playlist_id = raw
        else:
            raise ValueError("Это не похоже на плейлист YouTube.")
    return {
        "id": playlist_id,
        "url": f"https://www.youtube.com/playlist?list={playlist_id}",
    }


def fetch_playlist(ref: str, limit: int | None = None, fallback_name: str = "") -> dict:
    """Read the first `limit` entries of a playlist, including private ones."""
    p = parse_playlist_ref(ref)
    settings = load_settings()
    try:
        max_items = int(limit or settings.get("playlist_limit") or 50)
    except (TypeError, ValueError):
        max_items = 50
    max_items = max(1, min(500, max_items))

    cmd = ytdlp_base(settings) + [
        "--flat-playlist",
        "--ignore-errors",
        "--playlist-end", str(max_items),
        "--print", "%(id)s\t%(title)s\t%(duration)s\t%(playlist_title)s\t%(playlist_index)s",
        p["url"],
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Плейлист не успел загрузиться за 3 минуты.")

    entries: list[dict] = []
    playlist_title = ""
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0].strip():
            continue
        title = parts[1].strip() or parts[0].strip()
        try:
            duration = (
                float(parts[2])
                if len(parts) > 2 and parts[2] not in ("NA", "None", "")
                else 0.0
            )
        except ValueError:
            duration = 0.0
        if not playlist_title and len(parts) > 3 and parts[3] not in ("NA", "None"):
            playlist_title = parts[3].strip()
        entries.append({"id": parts[0].strip(), "title": title, "duration": duration})

    if not entries:
        err = (proc.stderr or "").strip()
        low = err.lower()
        if "private" in low:
            raise RuntimeError("Плейлист приватный. Проверь вход в YouTube и cookies.")
        if "sign in" in low or "login" in low or "cookies" in low:
            raise RuntimeError("YouTube требует вход. Проверь cookies браузера.")
        if "does not exist" in low or "not found" in low:
            raise RuntimeError("Плейлист не найден.")
        raise RuntimeError("Плейлист пуст или недоступен.")

    name = (
        PLAYLIST_NAMES.get(p["id"])
        or playlist_title
        or fallback_name.strip()
        or "Плейлист"
    )
    return {"id": p["id"], "url": p["url"], "name": name, "entries": entries}


def load_playlists() -> list[dict]:
    """Load saved playlists; never fail because of a broken JSON file."""
    try:
        data = json.loads(PLAYLISTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        pid = str(item["id"])
        try:
            count = int(item.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        out.append(
            {
                "id": pid,
                "url": str(item.get("url") or f"https://www.youtube.com/playlist?list={pid}"),
                "name": str(item.get("name") or PLAYLIST_NAMES.get(pid) or "Плейлист"),
                "count": max(0, count),
                "added": int(item.get("added") or 0),
            }
        )
    return out


def _write_playlists(items: list[dict]) -> None:
    PLAYLISTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PLAYLISTS_FILE.with_name(PLAYLISTS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PLAYLISTS_FILE)


def save_playlist(ref: str, name: str = "", count: int = 0) -> dict:
    p = parse_playlist_ref(ref)
    items = [x for x in load_playlists() if x.get("id") != p["id"]]
    item = {
        "id": p["id"],
        "url": p["url"],
        "name": name.strip() or PLAYLIST_NAMES.get(p["id"]) or "Плейлист",
        "count": max(0, int(count or 0)),
        "added": int(time.time()),
    }
    items.insert(0, item)
    _write_playlists(items[:50])
    _invalidate_yt_status()
    return item


def delete_playlist(playlist_id: str) -> dict:
    pid = (playlist_id or "").strip()
    if not pid:
        return {"ok": False, "error": "Не указан плейлист."}
    items = [x for x in load_playlists() if x.get("id") != pid]
    _write_playlists(items)
    _invalidate_yt_status()
    return {"ok": True, "playlists": items}


def youtube_status(force: bool = False, quick: bool = False) -> dict:
    """Check whether yt-dlp can see the private LL playlist via cookies."""
    now = time.time()
    cached = YT_STATUS_CACHE.get("data")
    if not force and cached and now - float(YT_STATUS_CACHE.get("at") or 0) < 30:
        return cached

    settings = load_settings()
    result: dict = {
        "ok": True,
        "logged_in": False,
        "message": "",
        "settings": public_settings(settings),
        "playlists": load_playlists(),
    }

    if quick:
        if cached and now - float(YT_STATUS_CACHE.get("at") or 0) < 300:
            result["logged_in"] = cached.get("logged_in")
            result["message"] = cached.get("message") or ""
        else:
            result["logged_in"] = None
            result["message"] = "Нажми «Проверить вход», чтобы проверить cookies."
        return result

    if not os.path.exists(YTDLP):
        result["message"] = f"yt-dlp не найден: {YTDLP}"
        _invalidate_yt_status()
        YT_STATUS_CACHE.update({"at": now, "data": result})
        return result
    if not ytdlp_cookie_args(settings):
        result["message"] = (
            "Cookies не настроены: войди в YouTube в браузере на этом ПК "
            "и укажи браузер либо путь к cookies.txt."
        )
        YT_STATUS_CACHE.update({"at": now, "data": result})
        return result

    ll_cmd = ytdlp_base(settings) + [
        "--flat-playlist",
        "--playlist-end", "1",
        "--print", "%(id)s\t%(playlist_title)s",
        "https://www.youtube.com/playlist?list=LL",
    ]
    try:
        proc = subprocess.run(ll_cmd, capture_output=True, text=True, timeout=60)
        lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
        if proc.returncode == 0 and lines:
            result["logged_in"] = True
            result["message"] = "Вход работает: плейлист «Мне понравилось» доступен."
        else:
            feed_cmd = ytdlp_base(settings) + [
                "--flat-playlist",
                "--playlist-end", "5",
                "--print", "%(id)s\t%(title)s",
                "https://www.youtube.com/feed/playlists",
            ]
            feed = subprocess.run(feed_cmd, capture_output=True, text=True, timeout=60)
            feed_lines = [line for line in (feed.stdout or "").splitlines() if line.strip()]
            if feed.returncode == 0 and feed_lines:
                result["logged_in"] = True
                result["message"] = "Вход работает: YouTube отдал список плейлистов."
            else:
                err = ((proc.stderr or "") + "\n" + (feed.stderr or "")).strip()
                low = err.lower()
                if any(word in low for word in ("private", "sign in", "login", "cookies")):
                    result["message"] = (
                        "YouTube не отдал приватный плейлист. "
                        "Проверь, что ты вошёл в выбранном браузере."
                    )
                else:
                    last = err.splitlines()[-1][:300] if err else "yt-dlp вернул пустой ответ"
                    result["message"] = f"Не удалось проверить вход: {last}"
    except subprocess.TimeoutExpired:
        result["message"] = "Проверка входа не успела за 60 секунд."
    except Exception as exc:
        result["message"] = f"Ошибка проверки входа: {exc}"

    YT_STATUS_CACHE.update({"at": now, "data": result})
    return result


def download(video_id: str) -> Path:
    target = CACHE / f"{video_id}.mp3"
    if target.exists() and target.stat().st_size > 1024:
        return target
    outtmpl = str(CACHE / "%(id)s.%(ext)s")
    cmd = ytdlp_base() + [
        "--no-playlist",
        "-f", "bestaudio/best",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "192K",
        "-o", outtmpl,
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    proc = subprocess.run(
        cmd,
        timeout=600,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        raise RuntimeError(err[-1][:300] if err else "yt-dlp download failed")
    if not target.exists():
        # yt-dlp may have produced a different extension if ffmpeg failed
        candidates = sorted(CACHE.glob(f"{video_id}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            raise RuntimeError("download produced no file")
        target = candidates[0]
    return target


def _play_file(path: Path, track: dict, record: bool = True) -> None:
    url = f"http://{PC_IP}:{HTTP_PORT}/stream/{urllib.parse.quote(path.name)}"
    gen = STATE.generation

    if PLAYER_MODE == "gst":
        remote = (
            f"{KILL_GST}; "
            "sleep 0.3; "
            f"nohup {STATION_GST} \"playbin uri={url}\" "
            ">/tmp/ytstation_play.log 2>&1 & echo $!"
        )
        proc = ssh(remote, timeout=15)
        pid_text = (proc.stdout or "").strip().splitlines()
        pid = int(pid_text[-1]) if pid_text and pid_text[-1].isdigit() else None
        with STATE.lock:
            if gen != STATE.generation:
                if pid:
                    ssh(f"kill {pid} 2>/dev/null; true", timeout=8)
                return
            STATE.pid = pid
            STATE.track = track
            STATE.started_at = time.time()
            STATE.pause_started = None
            STATE.paused_total = 0.0
            STATE.duration = float(track.get("duration") or 0)
            STATE.paused = False
            STATE.ducked = False
            if record:
                if STATE.history_pos < len(STATE.history) - 1:
                    STATE.history = STATE.history[: STATE.history_pos + 1]
                STATE.history.append(track)
                STATE.history_pos = len(STATE.history) - 1
    else:
        raise RuntimeError("PLAYER_MODE=glagol is not implemented yet")

    if AUTO_NEXT and STATE.duration > 0:
        threading.Timer(STATE.duration + 3.0, _auto_next, args=(gen,)).start()


def _auto_next(gen: int) -> None:
    with STATE.lock:
        if gen != STATE.generation or not STATE.pid:
            return
        # Never advance while paused or ducked: retry shortly instead.
        if STATE.paused or STATE.ducked:
            threading.Timer(1.0, _auto_next, args=(gen,)).start()
            return
        elapsed = time.time() - STATE.started_at - STATE.paused_total
        if STATE.pause_started:
            elapsed -= time.time() - STATE.pause_started
        remaining = STATE.duration - max(0.0, elapsed)
        if remaining > 1.0:
            threading.Timer(remaining + 1.0, _auto_next, args=(gen,)).start()
            return
        if STATE.queue_pos >= len(STATE.queue):
            return
    play_next()


def play_query(query: str) -> dict:
    """Synchronous search, then download+play in the background.

    Used by the manual /play endpoint.  The Alice webhook uses
    start_query_async() instead, because private skills must answer quickly.
    """
    query = query.strip()
    if not query:
        return {"ok": False, "error": "empty query"}

    # Stop the previous track immediately so it does not play over Alice's TTS.
    STATE.stop()

    with STATE.lock:
        gen = STATE.generation + 1
        STATE.generation = gen
        STATE.downloading = True
        STATE.last_error = ""

    try:
        items = yt_search(query, limit=5)
    except Exception as exc:
        with STATE.lock:
            STATE.downloading = False
            STATE.last_error = f"search: {exc}"
        return {"ok": False, "error": f"search failed: {exc}"}

    if not items:
        with STATE.lock:
            STATE.downloading = False
            STATE.last_error = "nothing found"
        return {"ok": False, "error": "nothing found"}

    track = items[0]
    queue = items[1:]

    def worker() -> None:
        try:
            path = download(track["id"])
            with STATE.lock:
                if gen != STATE.generation:
                    return
                STATE.queue = queue
                STATE.queue_pos = 0
            time.sleep(START_DELAY)
            with STATE.lock:
                if gen != STATE.generation:
                    return
            _play_file(path, track)
        except Exception as exc:
            with STATE.lock:
                STATE.last_error = f"download/play: {exc}"
        finally:
            with STATE.lock:
                if gen == STATE.generation:
                    STATE.downloading = False

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "track": track, "queued": len(queue)}


def start_query_async(query: str) -> dict:
    """Fast path for the Alice skill: answer now, work in the background."""
    query = query.strip()
    if not query:
        return {"ok": False, "error": "empty query"}

    # A new search cancels any playlist that is still being fetched.
    cancel_playlist_load()
    # Clear state immediately; the old gst process is killed in background
    # so the Alice webhook does not wait for SSH.
    gen = STATE.stop_async()
    with STATE.lock:
        STATE.downloading = True
        STATE.last_error = ""

    def worker() -> None:
        try:
            items = yt_search(query, limit=5)
            if not items:
                raise RuntimeError("nothing found")
            track = items[0]
            queue = items[1:]
            path = download(track["id"])
            with STATE.lock:
                if gen != STATE.generation:
                    return
                STATE.queue = queue
                STATE.queue_pos = 0
            time.sleep(START_DELAY)
            with STATE.lock:
                if gen != STATE.generation:
                    return
            _play_file(path, track)
        except Exception as exc:
            with STATE.lock:
                if gen == STATE.generation:
                    STATE.last_error = f"async: {exc}"
        finally:
            with STATE.lock:
                if gen == STATE.generation:
                    STATE.downloading = False

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "async": True}


def start_playlist_async(
    ref: str,
    name: str = "",
    limit: int | None = None,
    save: bool = False,
) -> dict:
    """Load a playlist in the background and start its first track.

    The current track keeps playing while the playlist is being fetched.
    If the fetch fails (private playlist, bad URL, network), the old track is
    left alone.
    """
    try:
        p = parse_playlist_ref(ref)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    display_name = name.strip() or PLAYLIST_NAMES.get(p["id"]) or ""
    with STATE.lock:
        PLAYLIST_LOAD["id"] += 1
        load_id = PLAYLIST_LOAD["id"]
        previous_playlist = dict(STATE.playlist) if STATE.playlist else None
        STATE.playlist = {
            "id": p["id"],
            "url": p["url"],
            "name": display_name,
            "count": 0,
            "loading": True,
        }
        STATE.downloading = True
        STATE.last_error = ""

    def worker() -> None:
        gen = 0
        switched = False
        try:
            pl = fetch_playlist(p["url"], limit=limit, fallback_name=name)
            entries = pl["entries"]
            if not entries:
                raise RuntimeError("Плейлист пуст.")
            track = entries[0]
            queue = entries[1:]

            # Only now, when the playlist is known to work, stop the old track.
            with STATE.lock:
                if load_id != PLAYLIST_LOAD["id"]:
                    return
            gen = STATE.stop_async()
            switched = True
            with STATE.lock:
                if load_id != PLAYLIST_LOAD["id"] or gen != STATE.generation:
                    return
                STATE.playlist = {
                    "id": pl["id"],
                    "url": pl["url"],
                    "name": pl["name"],
                    "count": len(entries),
                    "loading": False,
                }
                STATE.queue = queue
                STATE.queue_pos = 0
                STATE.downloading = True

            if save:
                try:
                    save_playlist(pl["url"], pl["name"], len(entries))
                except Exception:
                    pass
            path = download(track["id"])
            with STATE.lock:
                if load_id != PLAYLIST_LOAD["id"] or gen != STATE.generation:
                    return
            time.sleep(START_DELAY)
            with STATE.lock:
                if load_id != PLAYLIST_LOAD["id"] or gen != STATE.generation:
                    return
            _play_file(path, track)
        except Exception as exc:
            with STATE.lock:
                if load_id == PLAYLIST_LOAD["id"]:
                    STATE.last_error = f"playlist: {exc}"
                    if switched and STATE.playlist:
                        STATE.playlist["loading"] = False
                    else:
                        STATE.playlist = previous_playlist
        finally:
            with STATE.lock:
                if load_id == PLAYLIST_LOAD["id"]:
                    STATE.downloading = False

    threading.Thread(target=worker, daemon=True).start()
    return {
        "ok": True,
        "async": True,
        "playlist": {
            "id": p["id"],
            "url": p["url"],
            "name": display_name,
            "count": 0,
            "loading": True,
        },
    }


def playlist_save(ref: str = "", name: str = "") -> dict:
    """Save a URL, or the playlist that is currently loaded, without playing it."""
    raw = (ref or "").strip()
    if raw:
        try:
            settings = load_settings()
            pl = fetch_playlist(
                raw,
                limit=settings.get("playlist_limit") or 50,
                fallback_name=name,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        item = save_playlist(pl["url"], name or pl["name"], len(pl["entries"]))
        return {"ok": True, "playlist": item, "playlists": load_playlists()}

    with STATE.lock:
        current = dict(STATE.playlist or {})
    if not current.get("id"):
        return {"ok": False, "error": "Сначала включи или загрузи плейлист."}
    if current.get("loading"):
        return {"ok": False, "error": "Плейлист ещё загружается."}
    item = save_playlist(
        str(current.get("url") or current["id"]),
        name or str(current.get("name") or ""),
        int(current.get("count") or 0),
    )
    return {"ok": True, "playlist": item, "playlists": load_playlists()}


def _start_track(track: dict, record: bool = True) -> dict:
    """Stop the current track and start another one in the background."""
    STATE.kill_current()
    with STATE.lock:
        gen = STATE.generation + 1
        STATE.generation = gen
        STATE.downloading = True
        STATE.last_error = ""

    def worker() -> None:
        try:
            path = download(track["id"])
            with STATE.lock:
                if gen != STATE.generation:
                    return
            time.sleep(START_DELAY)
            with STATE.lock:
                if gen != STATE.generation:
                    return
            _play_file(path, track, record=record)
        except Exception as exc:
            with STATE.lock:
                STATE.last_error = f"start: {exc}"
        finally:
            with STATE.lock:
                if gen == STATE.generation:
                    STATE.downloading = False

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "track": track}


def play_next() -> dict:
    cancel_playlist_load()
    with STATE.lock:
        # If the user went back with "prev", go forward through history first.
        if STATE.history_pos < len(STATE.history) - 1:
            STATE.history_pos += 1
            track = STATE.history[STATE.history_pos]
            return _start_track(track, record=False)
        if STATE.queue_pos >= len(STATE.queue):
            return {"ok": False, "error": "queue is empty"}
        track = STATE.queue[STATE.queue_pos]
        STATE.queue_pos += 1
    return _start_track(track, record=True)


def play_prev() -> dict:
    cancel_playlist_load()
    with STATE.lock:
        if STATE.history_pos <= 0:
            return {"ok": False, "error": "no previous track"}
        STATE.history_pos -= 1
        track = STATE.history[STATE.history_pos]
    return _start_track(track, record=False)


def play_index(index: int) -> dict:
    cancel_playlist_load()
    with STATE.lock:
        if index < 0 or index >= len(STATE.queue):
            return {"ok": False, "error": "bad queue index"}
        track = STATE.queue[index]
        STATE.queue_pos = index + 1
    return _start_track(track, record=True)


def pause() -> dict:
    with STATE.lock:
        pid = STATE.pid
        if not pid:
            return {"ok": False, "error": "nothing is playing"}
        if STATE.paused:
            return {"ok": True, "paused": True}
        STATE.paused = True
        STATE.ducked = False
        if STATE.pause_started is None:
            STATE.pause_started = time.time()
    ssh(f"kill -STOP {pid} 2>/dev/null; true", timeout=8)
    return {"ok": True, "paused": True}


def resume() -> dict:
    with STATE.lock:
        pid = STATE.pid
        if not pid:
            return {"ok": False, "error": "nothing is playing"}
        if not STATE.paused and not STATE.ducked:
            return {"ok": True, "paused": False}
        if STATE.pause_started is not None:
            STATE.paused_total += time.time() - STATE.pause_started
            STATE.pause_started = None
        STATE.paused = False
        STATE.ducked = False
    ssh(f"kill -CONT {pid} 2>/dev/null; true", timeout=8)
    return {"ok": True, "paused": False}


def duck(seconds: float = 2.8) -> None:
    """Briefly pause music so Alice's TTS is audible over it."""
    with STATE.lock:
        pid = STATE.pid
        if not pid or STATE.paused or STATE.ducked:
            return
        STATE.ducked = True
        if STATE.pause_started is None:
            STATE.pause_started = time.time()
    ssh(f"kill -STOP {pid} 2>/dev/null; true", timeout=8)

    def unduck() -> None:
        with STATE.lock:
            if STATE.pid != pid or not STATE.ducked:
                return
            if STATE.pause_started is not None:
                STATE.paused_total += time.time() - STATE.pause_started
                STATE.pause_started = None
            STATE.ducked = False
        ssh(f"kill -CONT {pid} 2>/dev/null; true", timeout=8)

    threading.Timer(seconds, unduck).start()


def set_volume(delta: int | None = None, absolute: int | None = None) -> dict:
    with STATE.lock:
        if absolute is not None:
            STATE.volume = max(0, min(100, absolute))
        else:
            STATE.volume = max(0, min(100, STATE.volume + (delta or 0)))
        vol = STATE.volume
    ssh(f"amixer -q set Master {vol}% 2>/dev/null; true", timeout=8)
    return {"ok": True, "volume": vol}


# --------------------------------------------------------------------------
# LED ring (plum: 3 RGB rings exposed through sysfs)
# --------------------------------------------------------------------------

LED_RINGS = (1, 2, 3)
LED_CHANNELS = ("red", "green", "blue")


def _led_kill_effect() -> None:
    ssh(
        "if [ -f /tmp/ytstation_led.pid ]; then "
        "pid=$(cat /tmp/ytstation_led.pid); "
        "if [ -n \"$pid\" ] && grep -q ytstation_led /proc/$pid/cmdline 2>/dev/null; then "
        "kill -KILL $pid 2>/dev/null; fi; "
        "rm -f /tmp/ytstation_led.pid; fi; true",
        timeout=8,
    )


def _led_raw_cmd(r: int, g: int, b: int) -> str:
    parts = []
    for channel, value in (("red", r), ("green", g), ("blue", b)):
        for ring in LED_RINGS:
            base = f"/sys/class/leds/plum:{channel}:ring{ring}"
            parts.append(f"echo none > {base}/trigger 2>/dev/null")
            parts.append(f"echo {max(0, min(255, int(value)))} > {base}/brightness 2>/dev/null")
    return "; ".join(parts) + "; true"


def _led_scaled(r: int, g: int, b: int, brightness: int) -> tuple[int, int, int]:
    br = max(0, min(100, int(brightness)))
    return (
        max(0, min(255, int(r * br / 100))),
        max(0, min(255, int(g * br / 100))),
        max(0, min(255, int(b * br / 100))),
    )


def led_static(r: int, g: int, b: int, brightness: int = 50) -> dict:
    _led_kill_effect()
    sr, sg, sb = _led_scaled(r, g, b, brightness)
    ssh(_led_raw_cmd(sr, sg, sb), timeout=10)
    with STATE.lock:
        STATE.led = {
            "mode": "static",
            "r": max(0, min(255, int(r))),
            "g": max(0, min(255, int(g))),
            "b": max(0, min(255, int(b))),
            "brightness": max(0, min(100, int(brightness))),
        }
        led = dict(STATE.led)
    return {"ok": True, "led": led}


def led_off() -> dict:
    _led_kill_effect()
    ssh(_led_raw_cmd(0, 0, 0), timeout=10)
    with STATE.lock:
        STATE.led["mode"] = "off"
        led = dict(STATE.led)
    return {"ok": True, "led": led}


def led_auto() -> dict:
    _led_kill_effect()
    ssh(
        "for c in red green blue; do "
        "for r in 1 2 3; do "
        "echo led_ring_fb > /sys/class/leds/plum:$c:ring$r/trigger 2>/dev/null; "
        "done; done; true",
        timeout=10,
    )
    with STATE.lock:
        STATE.led["mode"] = "auto"
        led = dict(STATE.led)
    return {"ok": True, "led": led}


def led_breathing(r: int, g: int, b: int, brightness: int = 50) -> dict:
    _led_kill_effect()
    R, G, B = _led_scaled(r, g, b, brightness)
    script = f"""#!/bin/sh
R={R}; G={G}; B={B}
for c in red green blue; do
  for ring in 1 2 3; do
    echo none > /sys/class/leds/plum:$c:ring$ring/trigger 2>/dev/null
  done
done
i=0; d=5
while true; do
  vr=$((R*i/100)); vg=$((G*i/100)); vb=$((B*i/100))
  for ring in 1 2 3; do
    echo $vr > /sys/class/leds/plum:red:ring$ring/brightness
    echo $vg > /sys/class/leds/plum:green:ring$ring/brightness
    echo $vb > /sys/class/leds/plum:blue:ring$ring/brightness
  done
  i=$((i+d))
  if [ $i -ge 100 ]; then i=100; d=-5; fi
  if [ $i -le 0 ]; then i=0; d=5; fi
  sleep 0.08
done
"""
    b64 = base64.b64encode(script.encode()).decode()
    ssh(
        f"echo {b64} | base64 -d > /tmp/ytstation_led.sh; "
        "chmod +x /tmp/ytstation_led.sh; "
        "nohup /tmp/ytstation_led.sh >/dev/null 2>&1 & echo $! > /tmp/ytstation_led.pid; "
        "cat /tmp/ytstation_led.pid",
        timeout=10,
    )
    with STATE.lock:
        STATE.led = {
            "mode": "breathing",
            "r": max(0, min(255, int(r))),
            "g": max(0, min(255, int(g))),
            "b": max(0, min(255, int(b))),
            "brightness": max(0, min(100, int(brightness))),
        }
        led = dict(STATE.led)
    return {"ok": True, "led": led}


# --------------------------------------------------------------------------
# Speaker amplifier / power
# --------------------------------------------------------------------------

def amp_query() -> bool:
    try:
        proc = ssh("amixer cget numid=3 2>/dev/null | tail -1", timeout=8)
        return "values=on" in (proc.stdout or "")
    except Exception:
        return True


def volume_query() -> int:
    try:
        proc = ssh("amixer get Master 2>/dev/null | tail -1", timeout=8)
        m = re.search(r"\[(\d+)%\]", proc.stdout or "")
        if m:
            return max(0, min(100, int(m.group(1))))
    except Exception:
        pass
    return 40


def amp_set(on: bool) -> dict:
    # numid=3 / numid=4 are the FS2105 left/right amplifier channel
    # switches.  They are the reliable "speaker power" control on plum.
    state = "on" if on else "off"
    cmd = (
        f"amixer -q cset numid=3 {state} 2>/dev/null; "
        f"amixer -q cset numid=4 {state} 2>/dev/null; true"
    )
    ssh(cmd, timeout=8)
    with STATE.lock:
        STATE.amp_on = bool(on)
    return {"ok": True, "amp_on": bool(on)}


def power_action(action: str) -> dict:
    if action == "amp_on":
        return amp_set(True)
    if action == "amp_off":
        return amp_set(False)
    if action in ("reboot", "poweroff"):
        # Invalidate auto-next timers and clear the UI state before the
        # station goes away.
        with STATE.lock:
            STATE.generation += 1
            STATE.paused = False
            STATE.ducked = False
            STATE.pid = None
            STATE.track = None
            STATE.started_at = 0.0
            STATE.pause_started = None
            STATE.paused_total = 0.0
            STATE.duration = 0.0
            STATE.queue = []
            STATE.queue_pos = 0
            STATE.history = []
            STATE.history_pos = -1
            STATE.downloading = False
        try:
            if action == "reboot":
                ssh("sync; /sbin/reboot", timeout=6)
            else:
                ssh("sync; /sbin/poweroff", timeout=6)
        except Exception:
            pass
        return {"ok": True, "action": action}
    return {"ok": False, "error": "unknown power action"}


def serve_audio(handler: BaseHTTPRequestHandler, path: Path) -> None:
    if not path.exists():
        handler.send_error(404, "not found")
        return
    size = path.stat().st_size
    start, end = 0, size - 1
    status = 200
    range_header = handler.headers.get("Range")
    if range_header:
        m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
        if m:
            a, b = m.group(1), m.group(2)
            if a:
                start = int(a)
            if b:
                end = int(b)
            if start > end or start >= size:
                handler.send_response(416)
                handler.send_header("Content-Range", f"bytes */{size}")
                handler.end_headers()
                return
            status = 206

    length = end - start + 1
    handler.send_response(status)
    handler.send_header("Content-Type", "audio/mpeg")
    handler.send_header("Content-Length", str(length))
    handler.send_header("Accept-Ranges", "bytes")
    if status == 206:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.end_headers()
    try:
        with path.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)
    except (BrokenPipeError, ConnectionResetError):
        pass


def alice_reply(text: str, end: bool = False) -> dict:
    return {
        "response": {"text": text, "tts": text, "end_session": end},
        "version": "1.0",
    }


def strip_query(command: str) -> str:
    q = command.lower().strip(" .!?,")
    q = re.sub(r"^(алиса[,\s]+)?", "", q)
    q = re.sub(r"^(пожалуйста[,\s]+)?", "", q)
    q = re.sub(r"^(включи|включай|поставь|вруби|запусти|играй|найди|сыграй)\s+", "", q)
    q = re.sub(r"^(на\s+)?(ютубе|ютьюбе|youtube|ютубе|ютуб)\s+", "", q)
    q = re.sub(r"^(песню|трек|композицию|музыку)\s+", "", q)
    q = re.sub(r"\s+(на\s+)?(ютубе|ютьюбе|youtube)$", "", q)
    return q.strip(" .!?,")


def handle_alice(body: dict) -> dict:
    req = body.get("request") or {}
    command = (req.get("command") or req.get("original_utterance") or "").strip()
    low = command.lower()

    if not command:
        return alice_reply("Скажи, что включить. Например: включи Моргенштерна.")

    if re.search(r"\b(стоп|останови|выключи|хватит)\b", low):
        STATE.stop()
        return alice_reply("Остановил.")

    if re.search(r"\b(пауза|приостанови|замри)\b", low):
        r = pause()
        return alice_reply("Пауза." if r.get("ok") else "Сейчас ничего не играет.")

    if re.search(r"\b(продолжи|продолжай|возобнови)\b", low):
        r = resume()
        return alice_reply("Продолжаю." if r.get("ok") else "Сейчас ничего не играет.")

    if re.search(r"\b(назад|предыдущий|предыдущую|верни)\b", low):
        r = play_prev()
        if r.get("ok"):
            return alice_reply(f"Включаю {r['track']['title']}.")
        return alice_reply("Это был первый трек.")

    if re.search(r"\b(дальше|следующий|следующую|переключи|некст)\b", low):
        r = play_next()
        if r.get("ok"):
            return alice_reply(f"Включаю {r['track']['title']}.")
        return alice_reply("Очередь пуста. Скажи, что включить.")

    m = re.search(r"громкость\s+(\d{1,3})", low)
    if m:
        duck()
        r = set_volume(absolute=int(m.group(1)))
        return alice_reply(f"Громкость {r['volume']} процентов.")
    if re.search(r"\b(громче|прибавь)\b", low):
        duck()
        r = set_volume(delta=10)
        return alice_reply(f"Громкость {r['volume']} процентов.")
    if re.search(r"\b(тише|убавь)\b", low):
        duck()
        r = set_volume(delta=-10)
        return alice_reply(f"Громкость {r['volume']} процентов.")

    if re.search(r"\b(что играет|что сейчас|статус)\b", low):
        duck()
        st = STATE.status()
        if st.get("track"):
            return alice_reply(f"Играет {st['track']['title']}.")
        return alice_reply("Сейчас ничего не играет.")

    if re.search(r"\b(помощь|что ты умеешь|команды)\b", low):
        return alice_reply(
            "Скажи: включи название трека. Ещё умею: пауза, продолжи, дальше, стоп, громче, тише."
        )

    if re.search(r"\b(плейлист|любимые|любимое|понравилось|смотреть позже|watch later)\b", low):
        ref = ""
        name = ""
        if "позже" in low or "watch later" in low:
            ref, name = "WL", "Смотреть позже"
        elif "понрав" in low or "любим" in low:
            ref, name = "LL", "Мне понравилось"
        else:
            words = re.sub(
                r"\b(алиса|включи|включай|поставь|вруби|запусти|плейлист|пожалуйста|мне|мой|мою)\b",
                " ",
                low,
            )
            words = re.sub(r"\s+", " ", words).strip(" .!?,")
            for item in load_playlists():
                nm = str(item.get("name") or "").lower()
                if nm and (nm in low or (words and (words in nm or nm in words))):
                    ref = str(item.get("url") or item.get("id") or "")
                    name = str(item.get("name") or "")
                    break
        if ref:
            r = start_playlist_async(ref, name=name)
            if r.get("ok"):
                return alice_reply(f"Включаю плейлист «{name or 'YouTube'}».")
            return alice_reply(f"Не получилось: {r.get('error', 'ошибка')}")
        return alice_reply(
            "Какой плейлист включить? Скажи «включи любимые» "
            "или «включи смотреть позже»."
        )

    query = strip_query(command)
    if not query:
        return alice_reply("Что включить?")

    r = start_query_async(query)
    if not r.get("ok"):
        return alice_reply("Не понял, что включить. Повтори название трека.")
    return alice_reply("Ищу и включаю.")


class Handler(BaseHTTPRequestHandler):
    server_version = "YTStation/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}", flush=True)

    def _json(self, obj: dict, code: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _html(self, path: Path) -> None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            self.send_error(404, "web ui not found")
            return
        # Inject the current state so the first paint is already populated.
        initial = json.dumps(STATE.status(), ensure_ascii=False).replace("</", "<\\/")
        text = text.replace(
            "window.__INITIAL_STATE__ = null;",
            f"window.__INITIAL_STATE__ = {initial};",
            1,
        )
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _is_skill_server(self) -> bool:
        return self.server.server_port == SKILL_PORT

    def do_GET(self) -> None:
        u = urllib.parse.urlparse(self.path)
        p = u.path
        qs = urllib.parse.parse_qs(u.query)

        if self._is_skill_server():
            if p == "/health":
                self._json({"ok": True, "role": "alice-webhook"})
            else:
                self.send_error(404)
            return

        if p in ("/", "/index.html"):
            self._html(WEB_DIR / "index.html")
            return
        if p == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        if p.startswith("/stream/"):
            name = urllib.parse.unquote(p[len("/stream/"):])
            if "/" in name or ".." in name:
                self.send_error(400, "bad name")
                return
            serve_audio(self, CACHE / name)
            return

        if p in ("/status", "/api/status"):
            self._json(STATE.status())
            return
        if p in ("/play", "/api/play"):
            q = (qs.get("q") or [""])[0]
            if p == "/api/play":
                self._json(start_query_async(q))
            else:
                self._json(play_query(q))
            return
        if p in ("/next", "/api/next"):
            self._json(play_next())
            return
        if p == "/api/prev":
            self._json(play_prev())
            return
        if p == "/api/play_index":
            try:
                i = int((qs.get("i") or ["-1"])[0])
            except ValueError:
                i = -1
            self._json(play_index(i))
            return
        if p in ("/stop", "/api/stop"):
            STATE.stop()
            self._json({"ok": True})
            return
        if p in ("/pause", "/api/pause"):
            self._json(pause())
            return
        if p in ("/resume", "/api/resume"):
            self._json(resume())
            return
        if p in ("/volume", "/api/volume"):
            try:
                v = int((qs.get("v") or ["50"])[0])
            except ValueError:
                v = 50
            self._json(set_volume(absolute=v))
            return
        if p == "/api/led":
            mode = (qs.get("mode") or ["static"])[0]
            with STATE.lock:
                cur = dict(STATE.led)
            try:
                r = int((qs.get("r") or [cur.get("r", 0)])[0])
                g = int((qs.get("g") or [cur.get("g", 0)])[0])
                b = int((qs.get("b") or [cur.get("b", 255)])[0])
                br = int((qs.get("brightness") or [cur.get("brightness", 50)])[0])
            except ValueError:
                r, g, b, br = cur.get("r", 0), cur.get("g", 0), cur.get("b", 255), cur.get("brightness", 50)
            if mode == "auto":
                self._json(led_auto())
            elif mode == "off":
                self._json(led_off())
            elif mode == "breathing":
                self._json(led_breathing(r, g, b, br))
            else:
                self._json(led_static(r, g, b, br))
            return
        if p == "/api/power":
            action = (qs.get("action") or [""])[0]
            self._json(power_action(action))
            return
        if p == "/api/youtube/status":
            force = (qs.get("force") or ["0"])[0].lower() in ("1", "true", "yes")
            quick = (qs.get("quick") or ["0"])[0].lower() in ("1", "true", "yes")
            self._json(youtube_status(force=force, quick=quick))
            return
        if p == "/api/youtube/settings":
            patch: dict = {}
            if "browser" in qs:
                patch["cookies_from_browser"] = (qs.get("browser") or [""])[0]
            if "cookies_file" in qs:
                patch["cookies_file"] = (qs.get("cookies_file") or [""])[0]
            if "js" in qs:
                patch["js_runtimes"] = (qs.get("js") or [""])[0]
            if "limit" in qs:
                patch["playlist_limit"] = (qs.get("limit") or ["50"])[0]
            if "proxy" in qs:
                patch["proxy"] = (qs.get("proxy") or [""])[0]
            self._json(update_youtube_settings(patch))
            return
        if p in ("/api/playlist", "/api/play_playlist"):
            ref = (qs.get("url") or qs.get("id") or [""])[0]
            name = (qs.get("name") or [""])[0]
            try:
                limit = int((qs.get("limit") or ["0"])[0]) or None
            except ValueError:
                limit = None
            save = (qs.get("save") or ["0"])[0].lower() in ("1", "true", "yes")
            self._json(start_playlist_async(ref, name=name, limit=limit, save=save))
            return
        if p == "/api/playlists":
            self._json(
                {
                    "ok": True,
                    "playlists": load_playlists(),
                    "current": (STATE.status() or {}).get("playlist"),
                }
            )
            return
        if p == "/api/playlist_save":
            ref = (qs.get("url") or qs.get("id") or [""])[0]
            name = (qs.get("name") or [""])[0]
            self._json(playlist_save(ref, name))
            return
        if p == "/api/playlist_delete":
            pid = (qs.get("id") or [""])[0]
            self._json(delete_playlist(pid))
            return
        if p == "/health":
            self._json({"ok": True, "ytdlp": YTDLP, "pc_ip": PC_IP, "station": STATION_HOST})
            return

        self.send_error(404)

    def do_POST(self) -> None:
        u = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}

        if self._is_skill_server():
            if u.path != "/alice":
                self.send_error(404)
                return
        else:
            if u.path != "/alice":
                self.send_error(404)
                return

        try:
            self._json(handle_alice(body))
        except Exception as exc:
            print("alice error:", exc, flush=True)
            self._json(alice_reply("Что-то пошло не так. Попробуй ещё раз."))


def main() -> None:
    print(f"yt-dlp: {YTDLP}")
    print(f"PC IP: {PC_IP}")
    print(f"Station: {STATION_USER}@{STATION_HOST}:{STATION_PORT} (mode={PLAYER_MODE})")

    # A previous bridge may have left a gst-launch process playing.
    # BRIDGE_NO_STATION_INIT=1 is useful for testing the web/API layer
    # without touching a station that is currently playing.
    if os.environ.get("BRIDGE_NO_STATION_INIT") != "1":
        try:
            ssh(KILL_GST, timeout=10)
        except Exception as exc:
            print(f"gst cleanup: {exc}")

        # Make the hardware state match the web UI on a bridge restart.
        try:
            led_auto()
        except Exception as exc:
            print(f"led init: {exc}")
        with STATE.lock:
            STATE.amp_on = amp_query()
            STATE.volume = volume_query()

    audio = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    skill = ThreadingHTTPServer(("0.0.0.0", SKILL_PORT), Handler)

    threading.Thread(target=audio.serve_forever, daemon=True).start()
    print(f"web ui:       http://{PC_IP}:{HTTP_PORT}/")
    print(f"audio/control: http://{PC_IP}:{HTTP_PORT}/api/status")
    print(f"alice webhook: http://{PC_IP}:{SKILL_PORT}/alice")
    public_url = current_public_url()
    if public_url:
        print(f"public skill: {public_url}/alice")

    try:
        skill.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
