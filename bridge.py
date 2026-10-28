#!/usr/bin/env python3
"""
YouTube -> Yandex Station Lite 2 bridge.

Audio path:
    yt-dlp (search/download) -> audio file -> local HTTP server -> Station playbin

Control path:
    web UI / HTTP API -> this process -> SSH -> maind --gst-launch
    voice interception (Plan B) -> this process -> SSH -> maind --gst-launch

Run:
    python3 bridge.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
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
PLAYER_MODE = os.environ.get("PLAYER_MODE", "gst")  # gst | glagol (glagol not implemented yet)
START_DELAY = float(os.environ.get("START_DELAY", "2.0"))
AUTO_NEXT = os.environ.get("AUTO_NEXT", "1") == "1"

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
    # Plan B: intercept the Station's native Alice music and play YouTube.
    "native_watch": True,
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


def as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("", "0", "false", "off", "no")


def public_settings(settings: dict | None = None) -> dict:
    s = settings or load_settings()
    cfile = str(s.get("cookies_file") or "").strip()
    browser = str(s.get("cookies_from_browser") or "").strip()
    cfile_exists = bool(cfile) and Path(cfile).expanduser().exists()
    try:
        limit = int(s.get("playlist_limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    return {
        "cookies_from_browser": browser,
        "cookies_file": cfile,
        "cookies_file_exists": cfile_exists,
        "login_source": "file" if cfile_exists else ("browser" if browser and browser != "none" else ""),
        "js_runtimes": str(s.get("js_runtimes") or ""),
        "playlist_limit": limit,
        "proxy": str(s.get("proxy") or ""),
        "native_watch": as_bool(s.get("native_watch"), True),
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
        "native_watch",
        "native_auto_next",
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
        elif key in ("native_watch", "native_auto_next"):
            clean[key] = as_bool(value)
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


def ytdlp_base(settings: dict | None = None, include_cookies: bool = True) -> list[str]:
    """Common yt-dlp arguments: JS challenge solver + YouTube cookies."""
    s = settings or load_settings()
    args = [YTDLP, "--no-warnings", "--force-ipv4"]
    proxy = str(s.get("proxy") or "").strip()
    if proxy:
        args += ["--proxy", proxy]
    js = str(s.get("js_runtimes") or "").strip()
    if js:
        args += ["--js-runtimes", js]
    if include_cookies:
        args += ytdlp_cookie_args(s)
    return args


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

SSH_CTL = os.path.expanduser("~/.ssh/ytstation-ctl")
SSH_BASE = [
    "ssh",
    "-T",
    "-p", str(STATION_PORT),
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=6",
    "-o", "ControlMaster=auto",
    "-o", f"ControlPath={SSH_CTL}",
    "-o", "ControlPersist=600",
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
            old_pid = self.pid
            self.generation += 1
            self.paused = False
            self.ducked = False
            self.pid = None
            self.track = None
            self.started_at = 0.0
            self.pause_started = None
            self.paused_total = 0.0
            self.duration = 0.0
        # Kill only the process we started.  Scanning /proc for every
        # gst-launch takes ~1.5 s on the Station and delays every track start.
        if old_pid:
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

        Used by async playlist/query starts so the web UI never waits for SSH.
        Only the old PID is killed, so a delayed background kill cannot hit a
        new track.
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
            }


STATE = State()


def cancel_playlist_load() -> None:
    """Invalidate an in-flight playlist fetch without touching playback."""
    with STATE.lock:
        PLAYLIST_LOAD["id"] += 1
        if STATE.playlist and STATE.playlist.get("loading"):
            STATE.playlist = None
            STATE.downloading = False


def yt_search(query: str, limit: int = 1, timeout: int = 45) -> list[dict]:
    cmd = ytdlp_base() + [
        "--flat-playlist",
        "--print", "%(id)s\t%(title)s\t%(duration)s",
        f"ytsearch{limit}:{query}",
    ]
    out = subprocess.check_output(cmd, text=True, timeout=timeout, stderr=subprocess.DEVNULL)
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


def yt_mix(video_id: str, limit: int = 25, timeout: int = 90) -> list[dict]:
    """YouTube Music mix seeded by one video (the auto-generated RD playlist)."""
    vid = (video_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,}", vid):
        return []
    try:
        max_items = max(1, min(100, int(limit or 25)))
    except (TypeError, ValueError):
        max_items = 25
    settings = load_settings()
    cmd = ytdlp_base(settings) + [
        "--flat-playlist",
        "--ignore-errors",
        "--playlist-end", str(max_items),
        "--print", "%(id)s\t%(title)s\t%(duration)s",
        f"https://www.youtube.com/playlist?list=RD{vid}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"mix: timeout for {vid}", flush=True)
        return []
    entries: list[dict] = []
    seen: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0].strip():
            continue
        item_id = parts[0].strip()
        if item_id == vid or item_id in seen:
            continue
        seen.add(item_id)
        try:
            duration = (
                float(parts[2])
                if len(parts) > 2 and parts[2] not in ("NA", "None", "")
                else 0.0
            )
        except ValueError:
            duration = 0.0
        entries.append(
            {
                "id": item_id,
                "title": parts[1].strip() or item_id,
                "duration": duration,
            }
        )
    return entries


def schedule_mix(video_id: str, limit: int = 25) -> None:
    """Fetch a mix in the background and use it as the queue for this track."""
    vid = (video_id or "").strip()
    if not vid:
        return

    def worker() -> None:
        try:
            items = yt_mix(vid, limit=limit)
        except Exception as exc:
            print(f"mix: {vid}: {exc}", flush=True)
            return
        if not items:
            print(f"mix: no tracks for {vid}", flush=True)
            return
        with STATE.lock:
            current = STATE.track or {}
            if str(current.get("id") or "") != vid or STATE.playlist:
                return
            STATE.queue = items
            STATE.queue_pos = 0
        print(
            f"[{time.strftime('%H:%M:%S')}] mix: {len(items)} tracks after {vid}",
            flush=True,
        )

    threading.Thread(target=worker, daemon=True).start()


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


def fetch_playlist(
    ref: str,
    limit: int | None = None,
    fallback_name: str = "",
    timeout: int = 180,
) -> dict:
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
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Плейлист не успел загрузиться.")

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


def _probe_youtube_login(
    settings: dict,
    extra_cookie_args: list[str] | None = None,
) -> tuple[bool, str]:
    """Ask yt-dlp whether the current session can see private YouTube data."""
    base = ytdlp_base(settings, include_cookies=extra_cookie_args is None)
    if extra_cookie_args:
        base += extra_cookie_args

    ll_cmd = base + [
        "--flat-playlist",
        "--playlist-end", "1",
        "--print", "%(id)s\t%(playlist_title)s",
        "https://www.youtube.com/playlist?list=LL",
    ]
    try:
        proc = subprocess.run(ll_cmd, capture_output=True, text=True, timeout=60)
        lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
        if proc.returncode == 0 and lines:
            return True, "Вход работает: плейлист «Мне понравилось» доступен."

        feed_cmd = base + [
            "--flat-playlist",
            "--playlist-end", "5",
            "--print", "%(id)s\t%(title)s",
            "https://www.youtube.com/feed/playlists",
        ]
        feed = subprocess.run(feed_cmd, capture_output=True, text=True, timeout=60)
        feed_lines = [line for line in (feed.stdout or "").splitlines() if line.strip()]
        if feed.returncode == 0 and feed_lines:
            return True, "Вход работает: YouTube отдал список плейлистов."

        err = ((proc.stderr or "") + "\n" + (feed.stderr or "")).strip()
        low = err.lower()
        if any(word in low for word in ("private", "sign in", "login", "cookies")):
            return (
                False,
                "YouTube не отдал приватный плейлист. "
                "Проверь, что ты вошёл в YouTube в браузере на этом компьютере.",
            )
        last = err.splitlines()[-1][:300] if err else "yt-dlp вернул пустой ответ"
        return False, f"Не удалось проверить вход: {last}"
    except subprocess.TimeoutExpired:
        return False, "Проверка входа не успела за 60 секунд."
    except Exception as exc:
        return False, f"Ошибка проверки входа: {exc}"


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
        elif not ytdlp_cookie_args(settings):
            result["logged_in"] = False
            result["message"] = "Аккаунт не подключён: нажми «Войти»."
        else:
            result["logged_in"] = None
            result["message"] = "Нажми «Проверить», чтобы проверить вход."
        return result

    if not os.path.exists(YTDLP):
        result["message"] = f"yt-dlp не найден: {YTDLP}"
        _invalidate_yt_status()
        YT_STATUS_CACHE.update({"at": now, "data": result})
        return result
    if not ytdlp_cookie_args(settings):
        result["message"] = "Аккаунт не подключён: нажми «Войти» в блоке YouTube."
        YT_STATUS_CACHE.update({"at": now, "data": result})
        return result

    logged_in, message = _probe_youtube_login(settings)
    result["logged_in"] = logged_in
    result["message"] = message
    YT_STATUS_CACHE.update({"at": now, "data": result})
    return result


def ytdlp_python() -> str | None:
    """Python interpreter of the yt-dlp venv (used to export browser cookies)."""
    path = Path(YTDLP)
    venv_py = path.parent / "python"
    if venv_py.exists():
        return str(venv_py)
    try:
        first = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
        if first.startswith("#!"):
            exe = first[2:].strip().split()[0]
            if Path(exe).exists():
                return exe
    except OSError:
        pass
    return None


def _cookie_file_has_youtube_auth(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, name = parts[0].lower(), parts[5]
        if "youtube.com" in domain and name in (
            "SID",
            "SAPISID",
            "__Secure-1PSID",
            "__Secure-3PSID",
        ):
            return True
    return False


def youtube_login_open() -> dict:
    """Open the YouTube login page in the browser on this computer."""
    url = (
        "https://accounts.google.com/ServiceLogin?service=youtube"
        "&continue=https%3A%2F%2Fwww.youtube.com%2F"
    )
    env = os.environ.copy()
    runtime = Path(f"/run/user/{os.getuid()}")
    env.setdefault("XDG_RUNTIME_DIR", str(runtime))
    if runtime.exists():
        bus = runtime / "bus"
        if bus.exists():
            env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={bus}")
        wayland = sorted(runtime.glob("wayland-*"))
        if wayland:
            env.setdefault("WAYLAND_DISPLAY", wayland[0].name)
    if not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        env["DISPLAY"] = ":1"
    attempts: list[str] = []
    for name, rest in (("xdg-open", [url]), ("firefox", [url])):
        exe = shutil.which(name)
        if not exe:
            attempts.append(f"{name}: не найден")
            continue
        try:
            proc = subprocess.Popen(
                [exe, *rest],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                code = proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                code = 0
            if code == 0:
                return {"ok": True, "opened": True, "url": url, "browser": name}
            attempts.append(f"{name}: код {code}")
        except Exception as exc:
            attempts.append(f"{name}: {exc}")
    return {
        "ok": False,
        "opened": False,
        "url": url,
        "error": "; ".join(attempts) or "не удалось открыть браузер",
    }


def youtube_login_import() -> dict:
    """Export the YouTube session from the browser into a private cookie file."""
    settings = load_settings()
    browser = str(settings.get("cookies_from_browser") or "").strip()
    if not browser or browser == "none":
        browser = "firefox"
    browser_name, _, profile = browser.partition(":")
    py = ytdlp_python()
    if not py:
        return {"ok": False, "error": "Не удалось найти Python из окружения yt-dlp."}

    COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = COOKIES_FILE.with_name(COOKIES_FILE.name + ".tmp")
    script = (
        "import sys\n"
        "from yt_dlp.cookies import extract_cookies_from_browser\n"
        "profile = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None\n"
        "jar = extract_cookies_from_browser(sys.argv[1], profile)\n"
        "jar.save(sys.argv[3], ignore_discard=True, ignore_expires=True)\n"
    )
    try:
        proc = subprocess.run(
            [py, "-c", script, browser_name, profile, str(tmp)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Не удалось прочитать cookies: таймаут."}
    except Exception as exc:
        return {"ok": False, "error": f"Не удалось прочитать cookies: {exc}"}

    if proc.returncode != 0 or not tmp.exists():
        err = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip().splitlines()
        last = err[-1][:300] if err else "неизвестная ошибка"
        try:
            tmp.unlink()
        except OSError:
            pass
        return {"ok": False, "error": f"Браузер {browser}: {last}"}

    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass

    if not _cookie_file_has_youtube_auth(tmp):
        try:
            tmp.unlink()
        except OSError:
            pass
        return {
            "ok": False,
            "error": "В браузере нет входа в YouTube. Войди в аккаунт и нажми «Я вошёл» ещё раз.",
        }

    ok, message = _probe_youtube_login(settings, extra_cookie_args=["--cookies", str(tmp)])
    if not ok:
        try:
            tmp.unlink()
        except OSError:
            pass
        return {"ok": False, "error": message}

    try:
        os.replace(tmp, COOKIES_FILE)
        os.chmod(COOKIES_FILE, 0o600)
    except OSError as exc:
        return {"ok": False, "error": f"Не удалось сохранить cookies: {exc}"}

    save_settings({"cookies_file": str(COOKIES_FILE), "cookies_from_browser": ""})
    _invalidate_yt_status()
    result = {
        "ok": True,
        "logged_in": True,
        "message": message,
        "settings": public_settings(load_settings()),
        "playlists": load_playlists(),
    }
    YT_STATUS_CACHE.update({"at": time.time(), "data": result})
    return {"ok": True, "message": "Аккаунт подключён.", "status": result}


def youtube_logout() -> dict:
    """Stop using the current account and remove the managed cookie file."""
    settings = load_settings()
    removed = False
    cfile = str(settings.get("cookies_file") or "").strip()
    for raw in {cfile, str(COOKIES_FILE)}:
        if not raw:
            continue
        path = Path(raw).expanduser()
        try:
            if path.exists() and path.is_file():
                path.unlink()
                removed = True
        except OSError:
            pass
    save_settings({"cookies_file": "", "cookies_from_browser": ""})
    _invalidate_yt_status()
    return {"ok": True, "removed": removed}


AUDIO_EXTS = (".m4a", ".mp4", ".mp3", ".webm", ".opus", ".ogg")


def audio_duration(path: Path) -> float:
    """Read the real file duration when yt-dlp did not provide one."""
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return max(0.0, float((proc.stdout or "").strip()))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0


def cached_audio_files(limit: int = 30) -> list[Path]:
    """Recently downloaded tracks, used as an offline fallback for music."""
    try:
        files = [
            p
            for p in CACHE.iterdir()
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS and p.stat().st_size > 1024
        ]
    except OSError:
        return []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[: max(1, int(limit or 30))]


def download(video_id: str) -> Path:
    """Fetch the original audio stream; no MP3 transcode (saves ~15 s).

    The Station's playbin decodes AAC (m4a) and Opus (webm) directly, so the
    downloaded file is served as-is.
    """
    for ext in AUDIO_EXTS:
        target = CACHE / f"{video_id}{ext}"
        if target.exists() and target.stat().st_size > 1024:
            return target
    outtmpl = str(CACHE / "%(id)s.%(ext)s")
    cmd = ytdlp_base() + [
        "--no-playlist",
        "--retries", "5",
        "--fragment-retries", "5",
        "--socket-timeout", "20",
        "-f", "bestaudio[ext=m4a]/bestaudio[ext=mp4]/bestaudio",
        "-o", outtmpl,
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    last = "yt-dlp download failed"
    for attempt in range(3):
        proc = subprocess.run(
            cmd,
            timeout=600,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode == 0:
            break
        err = (proc.stderr or "").strip().splitlines()
        last = err[-1][:300] if err else "yt-dlp download failed"
        transient = any(
            token in last
            for token in (
                "405", "403", "429", "timed out", "timeout",
                "Connection", "Incomplete", "unable to download",
            )
        )
        if attempt < 2 and transient:
            print(f"yt-dlp: retry {video_id} after: {last[:120]}", flush=True)
            time.sleep(1.5 * (attempt + 1))
            continue
        raise RuntimeError(last)
    else:
        raise RuntimeError(last)
    candidates = [
        p for p in CACHE.glob(f"{video_id}.*") if p.suffix.lower() in AUDIO_EXTS
    ]
    if not candidates:
        raise RuntimeError("download produced no file")
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def _play_file(path: Path, track: dict, record: bool = True) -> None:
    url = f"http://{PC_IP}:{HTTP_PORT}/stream/{urllib.parse.quote(path.name)}"
    gen = STATE.generation
    duration = float(track.get("duration") or 0)
    if duration <= 0:
        duration = audio_duration(path)
        if duration > 0:
            track = dict(track)
            track["duration"] = duration

    if PLAYER_MODE == "gst":
        remote = (
            "sleep 0.2; "
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
            STATE.duration = duration
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
            if STATE.track and STATE.track.get("native_id"):
                native_continue = True
            else:
                return
        else:
            native_continue = False
    if native_continue:
        # A voice-requested track: continue with the local YouTube queue
        # instead of asking Yandex Music for another track.  Off by default.
        try:
            enabled = as_bool(load_settings().get("native_auto_next"), False)
        except Exception:
            enabled = False
        if enabled:
            play_next()
        return
    play_next()


def play_query(query: str) -> dict:
    """Synchronous search, then download+play in the background.

    Used by the legacy /play endpoint.  The web UI uses start_query_async()
    so the request returns immediately.
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
    # Publish the queue as soon as search results are known, so "next" works
    # while the first track is still downloading.
    with STATE.lock:
        STATE.queue = queue
        STATE.queue_pos = 0

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
            _play_file(path, track)
            schedule_mix(track["id"])
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
    """Fast path for the web UI: return immediately, work in the background."""
    query = query.strip()
    if not query:
        return {"ok": False, "error": "empty query"}

    # A new search cancels any playlist that is still being fetched.
    cancel_playlist_load()
    # Clear state immediately; the old gst process is killed in background
    # so the HTTP request does not wait for SSH.
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
            # Publish search results immediately, before the first download.
            with STATE.lock:
                if gen != STATE.generation:
                    return
                STATE.queue = queue
                STATE.queue_pos = 0
            path = download(track["id"])
            with STATE.lock:
                if gen != STATE.generation:
                    return
            time.sleep(START_DELAY)
            with STATE.lock:
                if gen != STATE.generation:
                    return
            _play_file(path, track)
            schedule_mix(track["id"])
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
    ctype = {
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".webm": "audio/webm",
        ".opus": "audio/ogg",
        ".ogg": "audio/ogg",
    }.get(path.suffix.lower(), "application/octet-stream")
    handler.send_response(status)
    handler.send_header("Content-Type", ctype)
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


# ---------------------------------------------------------------------------
# Plan B: play YouTube instead of the Station's native Alice music
#
# The Station's local unbound resolves *.strm.yandex.net to 127.0.0.1, so the
# native Yandex audio never actually sounds.  platformd still receives the
# AUDIO_PLAY directive from Alice and writes the Yandex Music track id to
# maind.log.  We resolve that id to "artist title" through the Music API (via
# an SSH SOCKS tunnel, because the PC exits through a non-RU VPN and the API
# answers 451 there), search YouTube and play the result with the same
# maind --gst-launch path the rest of the bridge uses.
#
# Alice's own voice commands keep producing the usual directives ("пауза" =>
# player_pause, "громче" => volume, "следующий трек" => a new audio_play), so
# they are mapped onto the bridge playback.
# ---------------------------------------------------------------------------

NATIVE_LOG = "/data/quasar/daemons_logs/maind.log"
NATIVE_RE_STREAM = re.compile(r"AUDIO_PLAY streamId=(\d+)")
NATIVE_RE_DIRECTIVE = re.compile(
    r"routeDirective \([^)]+\)([a-z_]+)(?:\.[A-Za-z_]+)?\s+via"
)
NATIVE_RE_PHRASE = re.compile(r"recognizedPhrase='([^']*)'")

NATIVE: dict = {
    "prep": {},          # stream id -> preparation dict
    "last_prefetch": "",
    "last_prefetch_at": 0.0,
    "handled": {},       # stream id -> time.time() when playback started
    "activating": "",
    "self_pause_until": 0.0,
    "continue_until": 0.0,
    "continuing": {},    # stream id -> time until the native restart is ignored
    "local_switch_until": 0.0,  # next/prev handled locally; ignore native start
    "next_handled_at": 0.0,     # debounce voice + player_next for the same skip
    "suppress_until": 0.0,      # debounce for native_suppress
    "voice": None,       # last recognised voice request
    "voice_timer": None,
}
NATIVE_LOCK = threading.RLock()

NATIVE_CLI: dict = {"proc": None, "ready": None, "prompt": None}
NATIVE_CLI_LOCK = threading.Lock()


NATIVE_MEDIA: dict = {"pid": 0, "at": 0.0}

NATIVE_SETTING: dict = {"value": True, "at": 0.0}


def native_setting() -> bool:
    now = time.time()
    if now - float(NATIVE_SETTING["at"]) < 2.0:
        return bool(NATIVE_SETTING["value"])
    try:
        value = as_bool(load_settings().get("native_watch"), True)
    except Exception:
        value = True
    NATIVE_SETTING["value"] = value
    NATIVE_SETTING["at"] = now
    return value


NATIVE_MUSIC_VERBS = (
    "включи", "включить", "поставь", "поставить", "сыграй", "сыграть",
    "запусти", "запустить", "вруби", "врубить", "найди", "найти",
)
NATIVE_QUERY_SKIP = {
    "музыку", "музыка", "радио", "плейлист", "любимые", "смотреть позже",
    "новинки", "что-нибудь", "что нибудь", "наугад", "подряд", "дальше",
    "следующий", "следующую", "следующий трек", "следующую песню",
    "предыдущий", "предыдущую", "предыдущий трек", "предыдущую песню",
}


NATIVE_NEXT_COMMANDS = {
    "дальше", "далее", "следующий", "следующая", "следующую",
    "следующий трек", "следующую песню", "следующая песня",
    "переключи", "переключи трек", "пропусти", "пропусти трек", "скип",
    "next", "next track", "skip",
}


def native_next_command(text: str) -> bool:
    """True for Alice's "next track" commands (not for song titles)."""
    low = " ".join((text or "").casefold().replace("ё", "е").strip(" .!,").split())
    low = re.sub(r"^(?:а|ну|давай|включи|поставь|переключи(?: на)?)\s+", "", low)
    low = re.sub(r"\s+пожалуйста$", "", low)
    return low in NATIVE_NEXT_COMMANDS


NATIVE_GENERIC_REQUESTS = {
    "музыку", "музыка", "музыки", "что-нибудь", "что нибудь", "что-то",
    "что то", "послушать музыку", "мою волну", "моя волна", "песню",
    "песня", "трек", "композицию",
}


def native_generic_music_command(text: str) -> bool:
    """True for "включи музыку" without a concrete song or artist."""
    low = " ".join((text or "").casefold().replace("ё", "е").strip(" .!,").split())
    low = re.sub(
        r"^(?:а|ну|давай|мне|включи|поставь|вруби|запусти|сыграй|найди)\s+",
        "",
        low,
    )
    low = re.sub(r"\s+пожалуйста$", "", low)
    low = re.sub(r"^(?:мне|пожалуйста)\s+", "", low)
    return low in NATIVE_GENERIC_REQUESTS


def native_local_next(source: str = "voice") -> None:
    """Switch to the next YouTube queue item without asking Yandex Music."""
    now = time.time()
    with NATIVE_LOCK:
        # Suppress the cloud's prefetch/player_start that follows the same
        # "next" command. A real new song request clears this marker earlier.
        NATIVE["local_switch_until"] = now + 12.0
        NATIVE["suppress_until"] = 0.0
        if now - float(NATIVE.get("next_handled_at") or 0.0) < 3.0:
            return
        NATIVE["next_handled_at"] = now
    res = play_next()
    if res.get("ok"):
        print(f"native: {source} next -> local queue", flush=True)
    else:
        print(f"native: {source} next: {res.get('error') or 'queue is empty'}", flush=True)


def native_music_query(text: str) -> str:
    """Return the track part of a music request, or "" for other commands."""
    s = (text or "").strip().strip(".!,")
    low = s.casefold()
    verb = ""
    for word in NATIVE_MUSIC_VERBS:
        if low == word or low.startswith(word + " "):
            verb = word
            s = s[len(word):].strip(" ,.!")
            break
    if not verb:
        return ""
    for word in ("пожалуйста", "мне", "песню", "песня", "трек", "композицию", "музыку"):
        low = s.casefold()
        if low.startswith(word + " "):
            s = s[len(word):].strip(" ,.!")
    s = s.strip(" ,.!\"'«»")
    if not s or s.casefold() in NATIVE_QUERY_SKIP:
        return ""
    return s


def native_voice_done(reason: str = "") -> None:
    """Alice found a track: cancel the YouTube fallback for this request."""
    with NATIVE_LOCK:
        voice = NATIVE.get("voice")
        if not voice or voice.get("done"):
            return
        voice["done"] = True
        timer = NATIVE.get("voice_timer")
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
            NATIVE["voice_timer"] = None
    if reason:
        print(f"native: Alice handled '{voice.get('text')}' ({reason})", flush=True)


def native_on_voice_text(text: str) -> None:
    if not native_setting():
        return
    duck(4.0)
    low = (text or "").casefold().strip(" .!,")
    if native_next_command(text):
        native_local_next("voice")
        return
    if low in ("пауза", "стоп", "остановись", "останови", "хватит"):
        pause()
        return
    if low in ("продолжи", "продолжить", "продолжай", "играй"):
        # The cloud may answer "продолжи" with a fresh audio_play of the same
        # track (no audio_player_continue_directive).  Treat that restart as a
        # continuation: silence the native session and keep our position.
        with NATIVE_LOCK:
            NATIVE["continue_until"] = time.time() + 6.0
            sid = NATIVE.get("last_prefetch") or ""
            if sid:
                NATIVE["continuing"][sid] = time.time() + 10.0
        resume()
        return
    if native_generic_music_command(text):
        now = time.time()
        with NATIVE_LOCK:
            NATIVE["local_switch_until"] = 0.0
            NATIVE["next_handled_at"] = 0.0
            NATIVE["suppress_until"] = 0.0
            timer = NATIVE.get("voice_timer")
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass
            NATIVE["voice_timer"] = None
            NATIVE["voice"] = {
                "text": text,
                "query": "",
                "generic": True,
                "at": now,
                "done": False,
                "token": f"{now:.3f}",
            }
        print(
            f"native: heard generic music request '{text}', using local queue/mix",
            flush=True,
        )
        return

    query = native_music_query(text)
    if not query:
        return
    now = time.time()
    with NATIVE_LOCK:
        # A concrete song request always wins over a previous next/prev mark.
        NATIVE["local_switch_until"] = 0.0
        NATIVE["next_handled_at"] = 0.0
        NATIVE["suppress_until"] = 0.0
        prev = NATIVE.get("voice") or {}
        if (
            prev.get("text") == text
            and not prev.get("done")
            and now - float(prev.get("at") or 0.0) < 3.0
        ):
            return
        timer = NATIVE.get("voice_timer")
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        token = f"{now:.3f}"
        NATIVE["voice"] = {
            "text": text,
            "query": query,
            "at": now,
            "done": False,
            "token": token,
        }
        t = threading.Timer(5.0, native_voice_fallback, args=(token,))
        t.daemon = True
        NATIVE["voice_timer"] = t
    t.start()
    print(
        f"native: heard '{text}', YouTube fallback in 5 s if Alice has no track",
        flush=True,
    )


def native_voice_fallback(token: str) -> None:
    with NATIVE_LOCK:
        voice = NATIVE.get("voice") or {}
        if voice.get("token") != token or voice.get("done"):
            return
        voice["done"] = True
        NATIVE["voice_timer"] = None
        query = voice.get("query") or ""
    if not query:
        return
    print(f"native: Alice has no track, searching YouTube for '{query}'", flush=True)
    try:
        items = yt_search(query, limit=3)
        if not items:
            print("native: fallback: YouTube search returned nothing", flush=True)
            return
        item = None
        path = None
        last_exc = "no downloadable result"
        for cand in items[:3]:
            try:
                path = download(cand["id"])
                item = cand
                break
            except Exception as exc:
                last_exc = exc
        if path is None or item is None:
            raise RuntimeError(str(last_exc)[:200])
        track = {
            "id": item["id"],
            "title": item.get("title") or query,
            "duration": float(item.get("duration") or 0.0),
        }
        STATE.kill_current()
        with STATE.lock:
            STATE.queue = [c for c in items if c.get("id") != item.get("id")]
            STATE.queue_pos = 0
            STATE.playlist = None
        time.sleep(0.2)
        _play_file(path, track)
        schedule_mix(track["id"])
        print(
            f"[{time.strftime('%H:%M:%S')}] native: fallback playing {track['title']}",
            flush=True,
        )
    except Exception as exc:
        print(f"native: fallback failed: {exc}", flush=True)


def native_on_prefetch(stream_id: str, late: bool = False) -> None:
    if not native_setting():
        return
    now = time.time()
    with NATIVE_LOCK:
        if now < float(NATIVE.get("local_switch_until") or 0.0):
            # This prefetch is the cloud's own copy of "next"/"prev": we have
            # already switched locally, so do not resolve it through Yandex
            # Music or download another track.
            NATIVE["last_prefetch"] = stream_id
            NATIVE["last_prefetch_at"] = now
            print(
                f"[{time.strftime('%H:%M:%S')}] native: ignore prefetch {stream_id} "
                "(local next)",
                flush=True,
            )
            return
        if stream_id != NATIVE.get("last_prefetch"):
            NATIVE["suppress_until"] = 0.0
        handled_at = NATIVE["handled"].get(stream_id) or 0.0
        if now - handled_at < 1.0:
            return
        prev = NATIVE["prep"].get(stream_id)
        if prev and now - float(prev.get("at") or 0.0) < 120:
            return
        # Plan B no longer calls the Yandex Music API.  The query comes from
        # the phrase Alice just recognised; the stream id is only a marker.
        voice = NATIVE.get("voice") or {}
        voice_query = ""
        generic = False
        if voice and now - float(voice.get("at") or 0.0) < 30.0:
            voice_query = str(voice.get("query") or "").strip()
            generic = bool(voice.get("generic"))
        prep = {
            "id": stream_id,
            "query": voice_query,
            "generic": generic,
            "track": None,
            "path": None,
            "error": "",
            "event": threading.Event(),
            "track_event": threading.Event(),
            "late": bool(late),
            "at": now,
        }
        NATIVE["prep"][stream_id] = prep
        NATIVE["last_prefetch"] = stream_id
        NATIVE["last_prefetch_at"] = now
    detail = f" -> {voice_query}" if voice_query else (" (generic music)" if generic else "")
    print(
        f"[{time.strftime('%H:%M:%S')}] native: prefetch {stream_id}"
        f"{' (late)' if late else ''}{detail}",
        flush=True,
    )
    threading.Thread(target=native_prepare, args=(prep,), daemon=True).start()


def native_prepare_cached(prep: dict) -> bool:
    """Fallback to downloaded tracks when YouTube cannot provide music."""
    cached = cached_audio_files()
    if not cached:
        return False
    with STATE.lock:
        current_id = str((STATE.track or {}).get("id") or "")
        titles = {
            str(h.get("id")): str(h.get("title") or "")
            for h in STATE.history
            if h.get("id")
        }
    pool = [p for p in cached if p.stem != current_id] or cached
    chosen = random.choice(pool)
    prep["track"] = {
        "id": chosen.stem,
        "title": titles.get(chosen.stem) or chosen.stem,
        "duration": 0.0,
    }
    prep["others"] = [
        {
            "id": p.stem,
            "title": titles.get(p.stem) or p.stem,
            "duration": 0.0,
        }
        for p in cached
        if p != chosen
    ]
    prep["path"] = chosen
    prep["keep_queue"] = True
    print(
        f"[{time.strftime('%H:%M:%S')}] native: generic offline -> cached "
        f"{prep['track']['title']} ({len(prep['others'])} more)",
        flush=True,
    )
    return True


def native_prepare_generic(prep: dict) -> None:
    """Choose music for a generic "включи музыку" without Yandex Music."""
    sid = prep["id"]
    entries: list[dict] = []
    with STATE.lock:
        if STATE.queue_pos < len(STATE.queue):
            entries = [dict(x) for x in STATE.queue[STATE.queue_pos:]]
    if entries:
        print("native: generic request -> continuing local queue", flush=True)
    else:
        seed = ""
        with STATE.lock:
            current = STATE.track or {}
            if current.get("id"):
                seed = str(current["id"])
            elif STATE.history:
                pos = STATE.history_pos
                last = STATE.history[pos] if 0 <= pos < len(STATE.history) else STATE.history[-1]
                seed = str(last.get("id") or "")
        if seed:
            print(f"native: generic request -> mix from {seed}", flush=True)
            entries = yt_mix(seed, limit=25, timeout=8)
        if not entries:
            try:
                settings = load_settings()
                limit = int(settings.get("playlist_limit") or 30)
                pl = fetch_playlist(
                    "LL",
                    limit=min(50, max(1, limit)),
                    fallback_name="Мне понравилось",
                    timeout=8,
                )
                entries = pl["entries"]
                if entries:
                    print("native: generic request -> liked playlist", flush=True)
            except Exception as exc:
                print(f"native: generic liked playlist failed: {exc}", flush=True)
        if not entries:
            print("native: generic request -> YouTube search 'музыка'", flush=True)
            entries = yt_search("музыка", limit=10, timeout=8)
    if not entries:
        if native_prepare_cached(prep):
            return
        raise RuntimeError("no music source")

    last_exc = "no downloadable result"
    for cand in entries[:4]:
        try:
            path = download(cand["id"])
        except Exception as exc:
            last_exc = str(exc)[:200]
            print(f"native: generic download {cand['id']} failed: {last_exc}", flush=True)
            continue
        track = dict(cand)
        track["native_id"] = sid
        track["native_query"] = "музыка"
        prep["track"] = track
        prep["others"] = [i for i in entries if i["id"] != cand["id"]]
        prep["path"] = path
        prep["keep_queue"] = True
        print(
            f"[{time.strftime('%H:%M:%S')}] native: ready generic {track.get('title')} [{sid}]",
            flush=True,
        )
        return
    if native_prepare_cached(prep):
        return
    raise RuntimeError(last_exc)


def native_prepare(prep: dict) -> None:
    sid = prep["id"]
    try:
        if prep.get("generic"):
            native_prepare_generic(prep)
            return
        query = str(prep.get("query") or "").strip()
        if not query:
            # maind.log sometimes writes the directive just before the
            # recognisedPhrase line.  Give the voice text a moment to arrive.
            deadline = time.time() + 2.5
            while not query and time.time() < deadline:
                time.sleep(0.05)
                with NATIVE_LOCK:
                    voice = NATIVE.get("voice") or {}
                    if voice and time.time() - float(voice.get("at") or 0.0) < 30.0:
                        query = str(voice.get("query") or "").strip()
                        if voice.get("generic"):
                            prep["generic"] = True
                            break
            prep["query"] = query
            if prep.get("generic"):
                native_prepare_generic(prep)
                return
        if not query:
            prep["error"] = "no recognised voice query"
            return
        items = yt_search(query, limit=5)
        if not items:
            prep["error"] = "nothing found on YouTube"
            return
        order = list(items)
        last_exc = "no downloadable result"
        for cand in order[:4]:
            try:
                path = download(cand["id"])
            except Exception as exc:
                last_exc = str(exc)[:200]
                print(f"native: download {cand['id']} failed: {last_exc}", flush=True)
                continue
            track = dict(cand)
            track["native_id"] = sid
            track["native_query"] = prep["query"]
            prep["track"] = track
            prep["others"] = [i for i in items if i["id"] != cand["id"]]
            prep["path"] = path
            prep["track_event"].set()
            print(f"[{time.strftime('%H:%M:%S')}] native: ready {track.get('title')} [{sid}]", flush=True)
            return
        raise RuntimeError(last_exc)
    except Exception as exc:
        prep["error"] = str(exc)[:200]
        print(f"native: prepare {sid} failed: {exc}", flush=True)
    finally:
        prep["track_event"].set()
        prep["event"].set()


def native_on_start() -> None:
    if not native_setting():
        return
    print(f"[{time.strftime('%H:%M:%S')}] native: player_start", flush=True)
    now = time.time()
    suppress = False
    with NATIVE_LOCK:
        sid = NATIVE["last_prefetch"]
        cont = bool(sid) and now < float(NATIVE.get("continue_until") or 0.0)
        local = now < float(NATIVE.get("local_switch_until") or 0.0)
        prep = NATIVE["prep"].get(sid) if sid else None
        if cont:
            NATIVE["continue_until"] = 0.0
            NATIVE["continuing"][sid] = now + 8.0
            suppress = True
        elif local:
            # We already switched locally (next/prev); the native start is the
            # cloud's copy of that command.  Stop it, keep our own playback.
            if now >= float(NATIVE.get("suppress_until") or 0.0):
                NATIVE["suppress_until"] = now + 3.0
                suppress = True
            else:
                return
        elif not prep:
            return
        elif prep.get("started"):
            if NATIVE.get("activating") == sid:
                return
            if prep.get("error") or not prep.get("path"):
                if now - float(prep.get("at") or 0.0) < 2.0:
                    return
                # A failed request is being replayed (e.g. "previous track"):
                # prepare it again instead of letting Yandex play.
                prep["started"] = False
                prep["error"] = ""
                prep["path"] = None
                prep["event"].clear()
                print(f"native: retry {sid}", flush=True)
                threading.Thread(
                    target=native_prepare, args=(prep,), daemon=True
                ).start()
            else:
                return
        if not suppress:
            prep["started"] = True
            NATIVE["activating"] = sid
    if suppress:
        threading.Thread(target=native_suppress, args=(sid,), daemon=True).start()
        return
    threading.Thread(target=native_activate, args=(sid, prep), daemon=True).start()


def native_mediad_pid(force: bool = False) -> int:
    """PID of maind --mediad (the native player), cached for 30 s."""
    now = time.time()
    with NATIVE_LOCK:
        pid = int(NATIVE_MEDIA.get("pid") or 0)
        if pid and not force and now - float(NATIVE_MEDIA.get("at") or 0.0) < 30.0:
            return pid
    try:
        out = ssh(
            "ps -ef | grep '[m]aind --mediad' | awk '{print $1}' | head -1",
            timeout=8,
        ).stdout.strip()
        pid = int(out.split()[0]) if out else 0
    except Exception:
        pid = 0
    with NATIVE_LOCK:
        NATIVE_MEDIA["pid"] = pid
        NATIVE_MEDIA["at"] = now
    return pid


def native_freeze(seconds: float = 10.0) -> bool:
    """Freeze the native audio daemon (SIGSTOP) without touching our player.

    Our YouTube audio runs in a separate `maind --gst-launch` process, so it
    keeps sounding while mediad (Alice TTS + native music) is stopped.  A
    remote watchdog resumes mediad even if the bridge dies.
    """
    pid = native_mediad_pid()
    if not pid:
        return False
    try:
        gen = int(time.time() * 1000)
        # Stop the audio before spawning the watchdog: the fewer remote
        # processes between the SSH command and kill -STOP, the smaller the
        # audible native fragment.
        ssh(
            f"echo {gen} > /tmp/yt_native_freeze_gen; "
            f"[ -d /proc/{pid} ] && kill -STOP {pid} 2>/dev/null; "
            f"nohup sh -c 'sleep {int(max(2, seconds))}; "
            f'[ "$(cat /tmp/yt_native_freeze_gen 2>/dev/null)" = "{gen}" ] '
            f"&& kill -CONT {pid} 2>/dev/null' >/dev/null 2>&1 & true",
            timeout=8,
        )
        return True
    except Exception:
        return False


def native_thaw() -> None:
    pid = int(NATIVE_MEDIA.get("pid") or 0)
    if not pid:
        return
    try:
        ssh(f"kill -CONT {pid} 2>/dev/null; true", timeout=8)
    except Exception:
        pass


def native_suppress(sid: str) -> None:
    """Silence the native player that Alice restarted for "Продолжи".

    The cloud answers "продолжи" by resuming its own player.  We already
    resumed our playback, so freeze mediad (our gst process keeps playing),
    stop the native session, and unfreeze mediad once it is gone.  No amp
    mute: the user's music never goes silent.
    """
    frozen = False
    muted = False
    try:
        frozen = native_freeze(8.0)
        if not frozen:
            # Safety fallback: if mediad cannot be stopped, silence the amp
            # instead of letting the native track leak through.
            try:
                amp_set(False)
                muted = True
            except Exception:
                pass
        with NATIVE_LOCK:
            NATIVE["self_pause_until"] = time.time() + 4.0
        sent = time.time()
        native_cli_send("o")
        time.sleep(max(1.4, 1.9 - (time.time() - sent)))
        if frozen:
            native_thaw()
            frozen = False
        print(f"native: continued (native {sid} stopped)", flush=True)
    finally:
        if frozen:
            native_thaw()
        if muted:
            try:
                amp_set(True)
            except Exception:
                pass


def native_activate(sid: str, prep: dict) -> None:
    muted = False
    frozen = False
    played = False
    try:
        late = bool(prep.get("late"))
        # If the Music API already said "no metadata" (audiobook, podcast, an
        # unknown id), leave the native playback alone instead of muting it.
        if (
            not late
            and prep.get("error")
            and not (prep.get("info") or {}).get("title")
        ):
            print(f"native: {sid}: {prep['error']}; native keeps playing", flush=True)
            with NATIVE_LOCK:
                NATIVE["prep"].pop(sid, None)
            return
        # Silence the native track first.  If our own music is playing, freeze
        # mediad and leave the amplifier on: the old track keeps playing until
        # the new file is ready, then we switch without a silent gap.  If
        # nothing is playing, just mute the amp.
        preserve = bool(STATE.pid and not STATE.paused)
        t0 = time.time()
        if preserve:
            frozen = native_freeze(12.0)
            if not frozen:
                # Fallback if mediad could not be frozen: mute as before.
                try:
                    amp_set(False)
                    muted = True
                except Exception:
                    pass
            print(
                f"native: froze native audio in {time.time() - t0:.2f}s, pause sent",
                flush=True,
            )
        else:
            try:
                amp_set(False)
                muted = True
            except Exception:
                pass
            print(f"native: mute in {time.time() - t0:.2f}s, pause sent", flush=True)
        with NATIVE_LOCK:
            NATIVE["self_pause_until"] = time.time() + 4.0
        sent = time.time()
        native_cli_send("o")
        if frozen:
            # The cloud needs ~1.5 s to pause the native session.  Unfreeze
            # after that: the old track keeps playing while the download
            # finishes, and Alice's TTS/mic work again.
            remain = 2.0 - (time.time() - sent)
            if remain > 0:
                time.sleep(remain)
            native_thaw()
            frozen = False
        # Late requests have no prefetch, so search+download happen while the
        # speaker is muted. Generic "включи музыку" may try a mix + playlist.
        prep["event"].wait(35 if prep.get("generic") else 25)
        if prep.get("error") or not prep.get("path"):
            print(f"native: not playing {sid}: {prep.get('error') or 'no file'}", flush=True)
            if frozen:
                native_thaw()
                frozen = False
            if muted:
                try:
                    amp_set(True)
                except Exception:
                    pass
            return
        track = dict(prep.get("track") or {})
        if not track:
            return
        with NATIVE_LOCK:
            NATIVE["handled"][sid] = time.time()
            NATIVE["prep"].pop(sid, None)
        STATE.kill_current()
        with STATE.lock:
            # Keep the other YouTube search results as a local queue, so the
            # web UI "next" button works for voice requests without asking
            # Yandex Music for another track.
            STATE.queue = list(prep.get("others") or [])
            STATE.queue_pos = 0
            STATE.playlist = None
        time.sleep(0.2)
        _play_file(prep["path"], track)
        if not prep.get("keep_queue"):
            schedule_mix(track["id"])
        if muted:
            time.sleep(0.7)
            try:
                amp_set(True)
            except Exception:
                pass
            muted = False
        played = True
        print(f"[{time.strftime('%H:%M:%S')}] native: playing {track.get('title')} [{sid}]", flush=True)
    except Exception as exc:
        print(f"native: activate {sid} failed: {exc}", flush=True)
    finally:
        if frozen:
            native_thaw()
        if muted and not played:
            try:
                amp_set(True)
            except Exception:
                pass
        with NATIVE_LOCK:
            if NATIVE.get("activating") == sid:
                NATIVE["activating"] = ""


def native_cli_ensure():
    proc = NATIVE_CLI["proc"]
    if proc is not None and proc.poll() is None:
        return proc
    ready = threading.Event()
    try:
        proc = subprocess.Popen(
            [
                "ssh", "-T",
                "-p", str(STATION_PORT),
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=8",
                "-o", "ServerAliveInterval=30",
                f"{STATION_USER}@{STATION_HOST}",
                "/system/vendor/quasar/maind --cli",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except Exception as exc:
        print(f"native: cli start failed: {exc}", flush=True)
        return None

    prompt = threading.Event()

    def reader() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                if "Connected to DirectiveSequencer" in line:
                    ready.set()
                if "UserInput:" in line:
                    prompt.set()
        except Exception:
            pass
        ready.set()
        prompt.set()

    threading.Thread(target=reader, daemon=True).start()
    NATIVE_CLI["proc"] = proc
    NATIVE_CLI["ready"] = ready
    NATIVE_CLI["prompt"] = prompt
    # The CLI prints the command list before its endpoint is connected to the
    # daemon; commands written too early are silently dropped.
    ready.wait(6)
    time.sleep(0.2)
    return proc


def native_cli_send(text: str) -> bool:
    with NATIVE_CLI_LOCK:
        proc = native_cli_ensure()
        if proc is None or proc.stdin is None:
            return False
        ready = NATIVE_CLI["ready"]
        if ready is not None and not ready.wait(6):
            return False
        try:
            proc.stdin.write(text + "\n")
            proc.stdin.flush()
            return True
        except Exception as exc:
            print(f"native: cli write failed: {exc}", flush=True)
            NATIVE_CLI["proc"] = None
            return False


def native_mark_local_switch(seconds: float = 4.0) -> None:
    """Ignore the cloud's own player_start after we switched track locally."""
    with NATIVE_LOCK:
        NATIVE["local_switch_until"] = time.time() + seconds
        NATIVE["suppress_until"] = 0.0


def native_handle_line(line: str) -> None:
    if not native_setting():
        return
    if "recognizedPhrase='" in line:
        m = NATIVE_RE_PHRASE.search(line)
        if m:
            native_on_voice_text(m.group(1))
        return
    if "AUDIO_PLAY" in line:
        # Alice resolved the request to a track; no YouTube fallback needed.
        native_voice_done("native audio")
    if "prefetchDirective" in line and "AUDIO_PLAY" in line:
        m = NATIVE_RE_STREAM.search(line)
        if m:
            native_on_prefetch(m.group(1))
        return
    if "Music session changed to player_start" in line:
        native_on_start()
        return
    if "handleDirective" in line and "AUDIO_PLAY" in line and "[Started" in line:
        # Fallback: maind.log rotates often and can lose player_start, and
        # "next track" directives are not prefetched.
        m = NATIVE_RE_STREAM.search(line)
        if m:
            sid = m.group(1)
            now = time.time()
            with NATIVE_LOCK:
                cont = now < float(NATIVE.get("continue_until") or 0.0)
                local = now < float(NATIVE.get("local_switch_until") or 0.0)
                if cont:
                    NATIVE["continuing"][sid] = now + 10.0
                prev = NATIVE["prep"].get(sid)
                if (
                    prev
                    and prev.get("started")
                    and NATIVE.get("activating") != sid
                    and now - float(prev.get("at") or 0.0) > 2.0
                ):
                    # Forget a failed (or already played) attempt: a fresh
                    # player_start for this id is a new request.
                    NATIVE["prep"].pop(sid, None)
                    prev = None
                known = (
                    cont
                    or local
                    or prev is not None
                    or NATIVE.get("activating") == sid
                    or now < float(NATIVE["continuing"].get(sid) or 0.0)
                )
                NATIVE["last_prefetch"] = sid
            if not known:
                native_on_prefetch(sid, late=True)
        native_on_start()
        return
    if "alice_request.TEXT_INPUT" in line:
        duck(4.0)
        return
    if "routeDirective" in line:
        m = NATIVE_RE_DIRECTIVE.search(line)
        if not m:
            return
        name = m.group(1)
        if name in ("player_pause", "clear_queue"):
            with NATIVE_LOCK:
                if time.time() < float(NATIVE.get("self_pause_until") or 0.0):
                    return
            pause()
        elif name in ("audio_player_continue_directive", "playback_toggle_play_pause"):
            with NATIVE_LOCK:
                NATIVE["continue_until"] = time.time() + 6.0
                sid = NATIVE.get("last_prefetch") or ""
                if sid:
                    NATIVE["continuing"][sid] = time.time() + 8.0
            resume()
        elif name == "player_next":
            # Keep the cloud out of it: play the next item from our own queue
            # instead of asking Yandex Music for the next track.
            native_local_next("directive")
        elif name == "player_prev":
            if play_prev().get("ok"):
                native_mark_local_switch(4.0)


def native_watch_loop() -> None:
    while True:
        proc = None
        try:
            proc = subprocess.Popen(
                SSH_BASE[:-1]
                + ["-o", "ServerAliveInterval=20", f"{STATION_USER}@{STATION_HOST}",
                   f"tail -n 0 -F {NATIVE_LOG}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            for line in proc.stdout:
                try:
                    native_handle_line(line)
                except Exception as exc:
                    print(f"native: {exc}", flush=True)
        except Exception as exc:
            print(f"native: tail failed: {exc}", flush=True)
        finally:
            try:
                if proc is not None and proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
        time.sleep(5)


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

    def do_GET(self) -> None:
        u = urllib.parse.urlparse(self.path)
        p = u.path
        qs = urllib.parse.parse_qs(u.query)

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
        if p == "/api/youtube/login/open":
            self._json(youtube_login_open())
            return
        if p == "/api/youtube/login/import":
            self._json(youtube_login_import())
            return
        if p == "/api/youtube/logout":
            self._json(youtube_logout())
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
            if "native" in qs:
                patch["native_watch"] = (qs.get("native") or ["1"])[0] not in ("0", "false", "off")
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
        if p == "/api/native_voice":
            text = (qs.get("text") or [""])[0]
            native_on_voice_text(text)
            self._json({"ok": True, "text": text})
            return
        if p == "/health":
            self._json({"ok": True, "ytdlp": YTDLP, "pc_ip": PC_IP, "station": STATION_HOST})
            return

        self.send_error(404)


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

    threading.Thread(target=native_watch_loop, daemon=True).start()
    threading.Thread(target=lambda: ssh(KILL_GST, timeout=10), daemon=True).start()
    threading.Thread(
        target=lambda: (native_mediad_pid(force=True), native_thaw()), daemon=True
    ).start()
    threading.Thread(target=native_cli_ensure, daemon=True).start()
    if native_setting():
        print("native: intercepting Station music -> YouTube")
    else:
        print("native: interception disabled in settings")

    print(f"web ui:       http://{PC_IP}:{HTTP_PORT}/")
    print(f"audio/control: http://{PC_IP}:{HTTP_PORT}/api/status")

    try:
        audio.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
