import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from flask import Flask, jsonify, redirect, render_template_string, request, send_file, url_for

from soulseek_cli import (
    SUPPORTED_EXTENSIONS,
    find_best_result,
    get_value,
    make_client,
    quality_rank,
    query_variants,
    SearchCancelled,
)


ROOT = Path(__file__).resolve().parent
SESSIONS_DIR = ROOT / "sessions"
DOWNLOADS_DIR = ROOT / "slskd-data" / "downloads"
ALT_DOWNLOADS_DIR = ROOT / "slskd-downloads"
ENV_FILE = ROOT / ".env"

app = Flask(__name__)
session_locks: dict[str, threading.Lock] = {}


def load_local_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


load_local_env()

RETRY_AFTER_SECONDS = int(os.environ.get("DJSET_RETRY_AFTER_SECONDS", "300"))
MAX_RETRIES = int(os.environ.get("DJSET_MAX_RETRIES", "3"))
RETRY_STATES = {
    "Completed, Errored",
    "Completed, Failed",
    "Completed, Rejected",
    "Completed, TimedOut",
    "Completed, Cancelled",
    "Queued, Remotely",
}


def session_path(session_id: str) -> Path:
    return SESSIONS_DIR / session_id / "session.json"


def slugify(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower())
    return value.strip("-") or "session"


def parse_track_list(raw_text: str) -> list[str]:
    tracks: list[str] = []
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^\s*\d+[\.)\t ]+", "", line)
        line = re.sub(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\s+", "", line)
        line = line.replace("—", " ").replace("–", " - ")
        line = re.sub(r"\s+", " ", line).strip(" -")
        if line:
            tracks.append(line)
    return tracks


def read_session(session_id: str) -> dict[str, Any]:
    return json.loads(session_path(session_id).read_text(encoding="utf-8"))


def write_session(session: dict[str, Any]) -> None:
    path = session_path(session["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session, indent=2), encoding="utf-8")


def update_session(session_id: str, mutator) -> dict[str, Any]:
    lock = session_locks.setdefault(session_id, threading.Lock())
    with lock:
        session = read_session(session_id)
        mutator(session)
        write_session(session)
        return session


def add_event(session_id: str, message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")

    def mutate(session: dict[str, Any]) -> None:
        events = session.setdefault("events", [])
        events.append({"time": timestamp, "message": message})
        del events[:-80]

    update_session(session_id, mutate)


def is_cancelled(session_id: str) -> bool:
    try:
        session = read_session(session_id)
    except Exception:
        return True
    return bool(session.get("cancelRequested")) or session.get("status") == "cancelled"


def collect_transfer_states(client) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for transfer in client.transfers.get_all_downloads(includeRemoved=True):
        username = get_value(transfer, "username", "?")
        for directory in get_value(transfer, "directories", []) or []:
            for file_item in get_value(directory, "files", []) or []:
                filename = get_value(file_item, "filename", "")
                basename = re.split(r"[\\/]", filename)[-1]
                states[basename] = {
                    "username": username,
                    "state": get_value(file_item, "state", "?"),
                    "percentComplete": get_value(file_item, "percentComplete", 0),
                    "bytesTransferred": get_value(file_item, "bytesTransferred", 0),
                    "size": get_value(file_item, "size", 0),
                }
    return states


def download_roots() -> list[Path]:
    roots = [DOWNLOADS_DIR, ALT_DOWNLOADS_DIR]
    return [root for root in roots if root.exists()]


def find_downloaded_file(basename: str) -> Path | None:
    for root in download_roots():
        direct = root / basename
        if direct.exists():
            return direct
        matches = list(root.rglob(basename))
        if matches:
            return matches[0]
    return None


def sync_download_states(session_id: str) -> None:
    load_local_env()
    try:
        client = make_client()
        states = collect_transfer_states(client)
    except Exception:
        return

    def mutate(session: dict[str, Any]) -> None:
        for item in session.get("items", []):
            basename = item.get("basename")
            if basename and basename in states:
                transfer = states[basename]
                item["transferState"] = transfer["state"]
                item["percentComplete"] = transfer["percentComplete"]
                if transfer["state"] == "Completed, Succeeded":
                    item["status"] = "complete"
                    item["completedAt"] = time.time()
                    local_path = find_downloaded_file(basename)
                    if local_path:
                        item["localPath"] = str(local_path.resolve())

    update_session(session_id, mutate)


def search_args() -> SimpleNamespace:
    return SimpleNamespace(
        responses=50,
        file_limit=10000,
        timeout=20,
        empty_response_abort_seconds=3.0,
        min_speed=0,
        max_queue=1000000,
        ext=SUPPORTED_EXTENSIONS,
        no_broad=False,
        pause=1.0,
        limit=10,
    )


def attempted_candidates(item: dict[str, Any]) -> set[tuple[str, str]]:
    attempts = item.get("attempts", [])
    excluded = {
        (attempt.get("username", ""), attempt.get("filename", ""))
        for attempt in attempts
        if attempt.get("username") and attempt.get("filename")
    }
    if item.get("username") and item.get("filename"):
        excluded.add((item["username"], item["filename"]))
    return excluded


def queue_candidate(session_id: str, index: int, client, args: SimpleNamespace, track: str, retry: bool = False) -> bool:
    if is_cancelled(session_id):
        raise SearchCancelled(track)
    session = read_session(session_id)
    item = session["items"][index]
    best, used_query, _results = find_best_result(
        client,
        args,
        track,
        excluded=attempted_candidates(item),
        cancel_check=lambda: is_cancelled(session_id),
    )
    if is_cancelled(session_id):
        raise SearchCancelled(track)
    if not best:
        update_session(
            session_id,
            lambda s, i=index, q=used_query: s["items"][i].update(
                {"status": "missing" if not retry else "retry_missing", "usedQuery": q, "activeQuery": q}
            ),
        )
        add_event(session_id, f"No alternate result found: {track}" if retry else f"No result found: {track}")
        return False

    ok = client.transfers.enqueue(best["username"], [best["file"]])
    now = time.time()
    status = "queued" if ok else "queue_failed"

    def mutate(s: dict[str, Any]) -> None:
        target = s["items"][index]
        attempts = target.setdefault("attempts", [])
        attempts.append(
            {
                "time": now,
                "username": best["username"],
                "filename": best["filename"],
                "usedQuery": used_query,
                "queued": ok,
            }
        )
        target.update(
            {
                "status": status,
                "usedQuery": used_query,
                "activeQuery": used_query,
                "username": best["username"],
                "filename": best["filename"],
                "basename": re.split(r"[\\/]", best["filename"])[-1],
                "bitRate": best.get("bitRate"),
                "length": best.get("length"),
                "size": best.get("size"),
                "qualityRank": quality_rank(best),
                "queuedAt": now,
                "retryCount": max(0, len(attempts) - 1),
                "lastRetryAt": now if retry else target.get("lastRetryAt"),
                "transferState": None,
                "percentComplete": 0,
            }
        )

    update_session(session_id, mutate)
    queued_basename = re.split(r"[\\/]", best["filename"])[-1]
    add_event(session_id, f"{'Retried with' if retry else 'Queued'}: {queued_basename}")
    return ok


def run_session(session_id: str) -> None:
    load_local_env()
    args = search_args()
    try:
        client = make_client()
    except Exception as exc:
        update_session(session_id, lambda s: s.update({"status": "error", "error": str(exc), "endedAt": time.time()}))
        return

    session = read_session(session_id)
    add_event(session_id, f"Started session with {len(session['tracks'])} tracks.")
    for index, track in enumerate(session["tracks"]):
        if is_cancelled(session_id):
            update_session(session_id, lambda s: s.update({"status": "cancelled", "endedAt": time.time()}))
            add_event(session_id, "Session cancelled.")
            return
        variants = query_variants(track)
        update_session(
            session_id,
            lambda s, i=index, v=variants: (
                s.update({"status": "running", "currentIndex": i}),
                s["items"][i].update({"status": "searching", "variants": v, "activeQuery": v[0] if v else track}),
            ),
        )
        add_event(session_id, f"Searching {index + 1}/{len(session['tracks'])}: {track}")

        try:
            queue_candidate(session_id, index, client, args, track)
        except SearchCancelled:
            update_session(session_id, lambda s: s.update({"status": "cancelled", "endedAt": time.time()}))
            add_event(session_id, "Session cancelled.")
            return
        except Exception as exc:
            update_session(
                session_id,
                lambda s, i=index, e=str(exc): s["items"][i].update({"status": "error", "error": e}),
            )
            add_event(session_id, f"Error on {track}: {exc}")
        sync_download_states(session_id)

    sync_download_states(session_id)
    update_session(session_id, lambda s: s.update({"status": "complete", "endedAt": time.time()}))
    add_event(session_id, "Search session complete.")
    threading.Thread(target=retry_monitor, args=(session_id,), daemon=True).start()


def item_needs_retry(item: dict[str, Any], now: float) -> bool:
    if item.get("status") == "complete":
        return False
    if item.get("retryCount", 0) >= MAX_RETRIES:
        return False
    transfer_state = item.get("transferState")
    if transfer_state in RETRY_STATES:
        queued_at = item.get("queuedAt", now)
        return now - queued_at >= RETRY_AFTER_SECONDS
    if item.get("status") in {"queue_failed", "error"}:
        queued_at = item.get("queuedAt", item.get("lastRetryAt", now))
        return now - queued_at >= min(60, RETRY_AFTER_SECONDS)
    return False


def retry_monitor(session_id: str) -> None:
    load_local_env()
    args = search_args()
    try:
        client = make_client()
    except Exception as exc:
        add_event(session_id, f"Retry monitor could not start: {exc}")
        return

    add_event(session_id, f"Retry monitor active: retry after {RETRY_AFTER_SECONDS // 60} minutes, max {MAX_RETRIES}.")
    while True:
        time.sleep(30)
        if is_cancelled(session_id):
            add_event(session_id, "Retry monitor stopped.")
            return
        sync_download_states(session_id)
        session = read_session(session_id)
        now = time.time()
        pending = [
            (index, item)
            for index, item in enumerate(session.get("items", []))
            if item.get("status") not in {"complete", "missing", "retry_missing"}
        ]
        if not pending:
            add_event(session_id, "Retry monitor finished.")
            return

        for index, item in pending:
            if not item_needs_retry(item, now):
                continue
            track = item["query"]
            add_event(session_id, f"Retrying stalled/failed item: {track}")
            try:
                queue_candidate(session_id, index, client, args, track, retry=True)
            except SearchCancelled:
                add_event(session_id, "Retry monitor stopped.")
                return
            except Exception as exc:
                add_event(session_id, f"Retry failed for {track}: {exc}")


def audio_files_since(start_epoch: float) -> list[Path]:
    extensions = {f".{ext.lower().lstrip('.')}" for ext in SUPPORTED_EXTENSIONS}
    files: list[Path] = []
    for root in download_roots():
        files.extend(
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in extensions and path.stat().st_mtime >= start_epoch
        )
    return files


def item_audio_path(session: dict[str, Any], index: int) -> Path | None:
    items = session.get("items", [])
    if index < 0 or index >= len(items):
        return None
    item = items[index]
    local_path = item.get("localPath")
    if local_path:
        path = Path(local_path)
        if path.exists():
            return path
    basename = item.get("basename")
    if basename:
        return find_downloaded_file(basename)
    return None


def session_audio_files(session: dict[str, Any]) -> list[Path]:
    sync_download_states(session["id"])
    session = read_session(session["id"])
    expected_basenames = {
        item.get("basename")
        for item in session.get("items", [])
        if item.get("basename") and item.get("status") in {"queued", "complete"}
    }
    candidates = audio_files_since(session["startedAt"] - 5)
    matched = [path for path in candidates if path.name in expected_basenames]
    files = matched or candidates
    return sorted(files, key=lambda path: path.stat().st_mtime)


def build_playlist_lines(session: dict[str, Any], files: list[Path]) -> tuple[list[str], list[str]]:
    item_by_basename = {
        item.get("basename"): item
        for item in session.get("items", [])
        if item.get("basename")
    }
    lines = ["#EXTM3U"]
    simple_lines: list[str] = []
    for path in files:
        item = item_by_basename.get(path.name, {})
        duration = int(item.get("length") or -1)
        title = path.stem
        resolved = str(path.resolve())
        lines.append(f"#EXTINF:{duration},{title}")
        lines.append(resolved)
        simple_lines.append(resolved)
    return lines, simple_lines


def create_playlist(session: dict[str, Any]) -> Path:
    files = session_audio_files(session)
    session = read_session(session["id"])

    playlist_path = SESSIONS_DIR / session["id"] / f"{session['id']}.m3u8"
    simple_playlist_path = SESSIONS_DIR / session["id"] / f"{session['id']}.m3u"
    lines, simple_lines = build_playlist_lines(session, files)
    playlist_path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
    simple_playlist_path.write_text("\r\n".join(simple_lines) + "\r\n", encoding="mbcs", newline="")

    update_session(
        session["id"],
        lambda s: s.update(
            {
                "playlistPath": str(playlist_path),
                "simplePlaylistPath": str(simple_playlist_path),
                "playlistTrackCount": len(files),
            }
        ),
    )
    add_event(session["id"], f"Created playlist with {len(files)} tracks.")
    return playlist_path


def create_rekordbox_export(session: dict[str, Any]) -> Path:
    files = session_audio_files(session)
    session = read_session(session["id"])
    playlist_path = SESSIONS_DIR / session["id"] / f"{session['id']}-rekordbox.m3u8"
    lines, _simple_lines = build_playlist_lines(session, files)
    playlist_path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")

    update_session(
        session["id"],
        lambda s: s.update(
            {
                "rekordboxPlaylistPath": str(playlist_path),
                "rekordboxTrackCount": len(files),
            }
        ),
    )
    add_event(session["id"], f"Created Rekordbox playlist with {len(files)} tracks.")
    return playlist_path


HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DJ Set Builder</title>
  <style>
    :root { --bg:#10140f; --panel:#f4efe4; --ink:#171712; --muted:#716b60; --line:#d8cfbf; --bad:#b3261e; --ok:#176b3a; }
    * { box-sizing:border-box; }
    body { margin:0; font-family: Georgia, 'Times New Roman', serif; background: radial-gradient(circle at top left, #34412b, #10140f 36rem); color:var(--panel); }
    main { width:min(1180px, calc(100vw - 32px)); margin:32px auto; }
    h1 { font-size:44px; line-height:1; margin:0 0 20px; font-weight:500; }
    .layout { display:grid; grid-template-columns: 420px 1fr; gap:18px; align-items:start; }
    .panel { background:var(--panel); color:var(--ink); border:1px solid var(--line); border-radius:8px; padding:18px; box-shadow:0 24px 70px rgba(0,0,0,.25); }
    label { display:block; font-size:13px; color:var(--muted); margin:0 0 8px; text-transform:uppercase; letter-spacing:.04em; }
    input, textarea { width:100%; border:1px solid var(--line); border-radius:6px; padding:12px; font:15px Consolas, monospace; background:#fffdf8; color:var(--ink); }
    textarea { min-height:430px; resize:vertical; }
    button, .button { display:inline-flex; align-items:center; justify-content:center; min-height:42px; border:0; border-radius:6px; padding:0 16px; background:var(--ink); color:var(--panel); font:700 14px Arial, sans-serif; cursor:pointer; text-decoration:none; }
    button.secondary, .button.secondary { background:#d8cfbf; color:var(--ink); }
    .actions { display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }
    .meta { display:flex; gap:12px; flex-wrap:wrap; margin-bottom:12px; color:var(--muted); font:14px Arial, sans-serif; }
    .pill { border:1px solid var(--line); border-radius:999px; padding:5px 10px; background:#fffaf0; }
    table { width:100%; border-collapse:collapse; font:14px Arial, sans-serif; }
    th, td { border-bottom:1px solid var(--line); padding:9px 7px; text-align:left; vertical-align:top; }
    th { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
    .status-missing, .status-error, .status-queue_failed, .status-cancelled, .status-cancel_requested { color:var(--bad); font-weight:700; }
    .status-queued, .status-complete { color:var(--ok); font-weight:700; }
    .status-searching { color:#856000; font-weight:700; }
    .filename { color:var(--muted); font-size:12px; margin-top:3px; word-break:break-word; }
    .activity { max-height:220px; overflow:auto; background:#fffdf8; border:1px solid var(--line); border-radius:6px; padding:10px; margin:12px 0; font:13px Consolas, monospace; }
    .activity div { padding:3px 0; border-bottom:1px solid #eee4d3; }
    .progress { color:var(--muted); font-size:12px; margin-top:3px; }
    .mini-actions { margin-top:8px; display:flex; gap:8px; flex-wrap:wrap; }
    .mini-actions button { min-height:30px; padding:0 10px; font-size:12px; }
    .deck { margin:12px 0 0; border:1px solid var(--line); border-radius:10px; background:linear-gradient(180deg, #131718, #090b0c); padding:12px; color:#e5efe8; }
    .deck-bar { display:flex; align-items:center; gap:10px; margin-bottom:10px; }
    .deck-button { min-width:76px; min-height:34px; border:0; border-radius:999px; background:linear-gradient(135deg, #7cff5a, #14a34a); color:#08110a; font:700 12px Arial, sans-serif; cursor:pointer; }
    .deck-time { font:12px Consolas, monospace; color:#9bc7ac; min-width:96px; }
    .deck-label { flex:1; font:12px Arial, sans-serif; color:#d9f1df; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .wave-wrap { position:relative; border-radius:8px; overflow:hidden; background:
      linear-gradient(180deg, rgba(10,18,22,.92), rgba(5,8,10,.98)),
      linear-gradient(90deg, rgba(255,0,0,.15), rgba(0,255,0,.15), rgba(0,120,255,.15)); }
    .wave-canvas { display:block; width:100%; height:180px; cursor:pointer; }
    .wave-overlay { position:absolute; inset:0; pointer-events:none; }
    .wave-playhead { position:absolute; top:0; bottom:0; width:2px; background:#ffffff; box-shadow:0 0 10px rgba(255,255,255,.9); }
    .wave-loading { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font:12px Arial, sans-serif; color:#c6d8cf; background:rgba(5,8,10,.55); pointer-events:none; }
    audio { display:none; }
    @media (max-width: 900px) { .layout { grid-template-columns:1fr; } h1 { font-size:34px; } }
  </style>
</head>
<body>
<main>
  <h1>DJ Set Builder</h1>
  <div class="layout">
    <section class="panel">
      <form method="post" action="{{ url_for('create_session') }}">
        <label for="name">Session name</label>
        <input id="name" name="name" value="download-session">
        <div style="height:12px"></div>
        <label for="tracks">Track list</label>
        <textarea id="tracks" name="tracks" placeholder="Paste tracks here..."></textarea>
        <div class="actions"><button type="submit">Start Session</button></div>
      </form>
    </section>
    <section class="panel">
      {% if session %}
        <div class="meta">
          <span class="pill">Session: {{ session.name }}</span>
          <span class="pill">Status: <span id="status">{{ session.status }}</span></span>
          <span class="pill">Tracks: <span id="counts"></span></span>
        </div>
        <div class="actions">
          <form method="post" action="{{ url_for('make_playlist', session_id=session.id) }}"><button type="submit">Create Playlist</button></form>
          <form method="post" action="{{ url_for('make_rekordbox_export', session_id=session.id) }}"><button type="submit">Create Rekordbox Playlist</button></form>
          <form method="post" action="{{ url_for('cancel_session', session_id=session.id) }}"><button type="submit" class="secondary">Cancel Session</button></form>
          {% if session.playlistPath %}<a class="button secondary" href="{{ url_for('download_playlist', session_id=session.id) }}">Download M3U8</a>{% endif %}
          {% if session.simplePlaylistPath %}<a class="button secondary" href="{{ url_for('download_simple_playlist', session_id=session.id) }}">Download M3U</a>{% endif %}
          {% if session.rekordboxPlaylistPath %}<a class="button secondary" href="{{ url_for('download_rekordbox_export', session_id=session.id) }}">Download Rekordbox M3U8</a>{% endif %}
        </div>
        <div class="activity" id="activity"></div>
        <div class="deck">
          <div class="deck-bar">
            <button id="deck-toggle" type="button" class="deck-button">Play</button>
            <div id="player-meta" class="deck-label">Select a completed track.</div>
            <div id="deck-time" class="deck-time">00:00 / 00:00</div>
          </div>
          <div class="wave-wrap" id="wave-wrap">
            <canvas id="wave-canvas" class="wave-canvas" width="960" height="180"></canvas>
            <div class="wave-overlay">
              <div id="wave-playhead" class="wave-playhead"></div>
              <div id="wave-loading" class="wave-loading">No track loaded</div>
            </div>
          </div>
          <audio id="player" preload="metadata"></audio>
        </div>
        <div style="height:12px"></div>
        <table>
          <thead><tr><th>#</th><th>Search</th><th>Status</th><th>Selected File</th></tr></thead>
          <tbody id="items"></tbody>
        </table>
      {% else %}
        <p>Start a session to search, queue downloads, and generate a playlist from that session.</p>
      {% endif %}
    </section>
  </div>
</main>
{% if session %}
<script>
const sessionId = {{ session.id|tojson }};
const player = document.getElementById('player');
const playerMeta = document.getElementById('player-meta');
const deckToggle = document.getElementById('deck-toggle');
const deckTime = document.getElementById('deck-time');
const waveCanvas = document.getElementById('wave-canvas');
const waveWrap = document.getElementById('wave-wrap');
const wavePlayhead = document.getElementById('wave-playhead');
const waveLoading = document.getElementById('wave-loading');
const waveContext = waveCanvas.getContext('2d');
const audioContext = new (window.AudioContext || window.webkitAudioContext)();
const waveformState = {
  bars: [],
  currentTrackUrl: '',
  currentLabel: '',
  dragging: false,
  ready: false,
};
function formatClock(seconds) {
  const total = Math.max(0, Math.floor(seconds || 0));
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
}
function setLoading(message) {
  waveLoading.textContent = message;
  waveLoading.style.display = 'flex';
}
function clearLoading() {
  waveLoading.style.display = 'none';
}
function resizeWaveCanvas() {
  const rect = waveWrap.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  waveCanvas.width = Math.max(640, Math.floor(rect.width * ratio));
  waveCanvas.height = Math.floor(180 * ratio);
  waveCanvas.style.height = '180px';
  drawWaveform();
}
function buildWaveBars(audioBuffer) {
  const samples = audioBuffer.getChannelData(0);
  const barCount = Math.max(180, Math.floor(waveCanvas.width / ((window.devicePixelRatio || 1) * 3)));
  const step = Math.max(1, Math.floor(samples.length / barCount));
  const bars = [];
  for (let index = 0; index < barCount; index += 1) {
    const start = index * step;
    const end = Math.min(samples.length, start + step);
    let low = 0;
    let mid = 0;
    let high = 0;
    let count = 0;
    let prev = 0;
    let prevPrev = 0;
    for (let sampleIndex = start; sampleIndex < end; sampleIndex += 1) {
      const sample = samples[sampleIndex];
      low += Math.abs(sample);
      mid += Math.abs(sample - prev);
      high += Math.abs(sample - (2 * prev) + prevPrev);
      prevPrev = prev;
      prev = sample;
      count += 1;
    }
    bars.push({
      low: low / Math.max(1, count),
      mid: mid / Math.max(1, count),
      high: high / Math.max(1, count),
    });
  }
  const maxLow = Math.max(...bars.map(bar => bar.low), 0.001);
  const maxMid = Math.max(...bars.map(bar => bar.mid), 0.001);
  const maxHigh = Math.max(...bars.map(bar => bar.high), 0.001);
  return bars.map(bar => ({
    low: Math.min(1, bar.low / maxLow),
    mid: Math.min(1, bar.mid / maxMid),
    high: Math.min(1, bar.high / maxHigh),
  }));
}
function drawWaveform() {
  const width = waveCanvas.width;
  const height = waveCanvas.height;
  waveContext.clearRect(0, 0, width, height);
  const background = waveContext.createLinearGradient(0, 0, 0, height);
  background.addColorStop(0, '#10181b');
  background.addColorStop(1, '#050809');
  waveContext.fillStyle = background;
  waveContext.fillRect(0, 0, width, height);

  const center = height / 2;
  const bars = waveformState.bars;
  if (!bars.length) {
    waveContext.strokeStyle = 'rgba(180, 210, 195, 0.25)';
    waveContext.beginPath();
    waveContext.moveTo(0, center);
    waveContext.lineTo(width, center);
    waveContext.stroke();
    updatePlayhead();
    return;
  }

  const barWidth = width / bars.length;
  bars.forEach((bar, index) => {
    const x = index * barWidth;
    const lowHeight = Math.max(2, bar.low * height * 0.42);
    const midHeight = Math.max(2, bar.mid * height * 0.34);
    const highHeight = Math.max(2, bar.high * height * 0.26);
    const energy = Math.min(1, (bar.low * 0.7) + (bar.mid * 0.22) + (bar.high * 0.08));
    const quietMix = Math.max(0, 1 - (energy * 1.25));
    const red = Math.floor((40 * quietMix) + (255 * energy));
    const green = Math.floor((220 * quietMix) + (70 * energy));
    const blue = Math.floor((70 * quietMix) + (55 * energy));
    waveContext.fillStyle = `rgba(${red}, ${green}, ${blue}, 0.96)`;
    waveContext.fillRect(x, center - lowHeight, Math.max(1, barWidth - 1), lowHeight * 2);
    waveContext.fillStyle = `rgba(255, 90, 50, ${0.16 + (bar.low * 0.52)})`;
    waveContext.fillRect(x, center - lowHeight, Math.max(1, barWidth - 1), lowHeight * 2);
    waveContext.fillStyle = `rgba(90, 130, 255, ${0.18 + (bar.mid * 0.46)})`;
    waveContext.fillRect(x, center - midHeight, Math.max(1, barWidth - 1), midHeight * 2);
    waveContext.fillStyle = `rgba(255, 235, 150, ${0.06 + (bar.high * 0.18)})`;
    waveContext.fillRect(x, center - highHeight, Math.max(1, barWidth - 1), highHeight * 2);
  });

  const gridColor = 'rgba(255,255,255,0.06)';
  waveContext.strokeStyle = gridColor;
  waveContext.lineWidth = 1;
  for (let i = 1; i < 8; i += 1) {
    const x = (width / 8) * i;
    waveContext.beginPath();
    waveContext.moveTo(x, 0);
    waveContext.lineTo(x, height);
    waveContext.stroke();
  }
  updatePlayhead();
}
function updatePlayhead() {
  const duration = player.duration || 0;
  const progress = duration ? Math.min(1, player.currentTime / duration) : 0;
  wavePlayhead.style.left = `${progress * 100}%`;
  deckTime.textContent = `${formatClock(player.currentTime)} / ${formatClock(duration)}`;
  deckToggle.textContent = player.paused ? 'Play' : 'Pause';
}
function seekFromClientX(clientX) {
  const rect = waveWrap.getBoundingClientRect();
  const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
  if (player.duration) {
    player.currentTime = player.duration * ratio;
    updatePlayhead();
  }
}
async function loadWaveform(url, label) {
  waveformState.currentTrackUrl = url;
  waveformState.currentLabel = label;
  waveformState.ready = false;
  playerMeta.textContent = `Loading: ${label}`;
  setLoading('Analyzing waveform...');
  const response = await fetch(url);
  const arrayBuffer = await response.arrayBuffer();
  const audioBuffer = await audioContext.decodeAudioData(arrayBuffer.slice(0));
  if (waveformState.currentTrackUrl !== url) {
    return;
  }
  waveformState.bars = buildWaveBars(audioBuffer);
  waveformState.ready = true;
  drawWaveform();
  clearLoading();
  playerMeta.textContent = `Loaded: ${label}`;
}
async function playTrack(index, label) {
  const url = `/sessions/${sessionId}/items/${index}/audio?t=${Date.now()}`;
  player.src = url;
  playerMeta.textContent = `Now playing: ${label}`;
  setLoading('Loading audio...');
  try {
    await loadWaveform(url, label);
  } catch (error) {
    setLoading('Waveform unavailable');
    playerMeta.textContent = `Playback error: ${label}`;
  }
  player.play().catch(() => {});
}
function escapeHtml(value) {
  return String(value || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function render(data) {
  document.getElementById('status').textContent = data.status;
  const queued = data.items.filter(x => x.status === 'queued').length;
  const complete = data.items.filter(x => x.status === 'complete').length;
  const missing = data.items.filter(x => x.status === 'missing').length;
  document.getElementById('counts').textContent = `${complete} complete, ${queued} queued, ${missing} missing, ${data.items.length} total`;
  document.getElementById('items').innerHTML = data.items.map((item, i) => `
    <tr>
      <td>${i + 1}</td>
      <td>${escapeHtml(item.query)}</td>
      <td class="status-${escapeHtml(item.status)}">${escapeHtml(item.status)}</td>
      <td>
        ${escapeHtml(item.basename || '')}
        <div class="filename">${escapeHtml(item.filename || item.error || '')}</div>
        <div class="progress">${escapeHtml(item.activeQuery ? 'search: ' + item.activeQuery : '')}</div>
        <div class="progress">${escapeHtml(item.transferState ? item.transferState + ' ' + Number(item.percentComplete || 0).toFixed(1) + '%' : '')}</div>
        <div class="progress">${escapeHtml(item.retryCount ? 'retries: ' + item.retryCount : '')}</div>
        <div class="mini-actions">
          ${item.status === 'complete' ? `<button type="button" class="secondary play-button" data-index="${i}" data-label="${escapeHtml(item.basename || item.query)}">Play</button>` : ''}
        </div>
      </td>
    </tr>`).join('');
  document.querySelectorAll('.play-button').forEach(button => {
    button.addEventListener('click', () => {
      playTrack(Number(button.dataset.index), button.dataset.label || '');
    });
  });
  document.getElementById('activity').innerHTML = (data.events || []).slice(-60).reverse().map(event =>
    `<div><strong>${escapeHtml(event.time)}</strong> ${escapeHtml(event.message)}</div>`
  ).join('');
}
async function poll() {
  const response = await fetch(`/api/session/${sessionId}`);
  render(await response.json());
}
poll();
setInterval(poll, 2500);
deckToggle.addEventListener('click', async () => {
  if (!player.src) {
    return;
  }
  if (audioContext.state === 'suspended') {
    await audioContext.resume();
  }
  if (player.paused) {
    player.play().catch(() => {});
  } else {
    player.pause();
  }
});
player.addEventListener('loadedmetadata', () => {
  clearLoading();
  updatePlayhead();
});
player.addEventListener('timeupdate', updatePlayhead);
player.addEventListener('play', updatePlayhead);
player.addEventListener('pause', updatePlayhead);
player.addEventListener('ended', updatePlayhead);
waveWrap.addEventListener('click', event => seekFromClientX(event.clientX));
waveWrap.addEventListener('mousedown', event => {
  waveformState.dragging = true;
  seekFromClientX(event.clientX);
});
window.addEventListener('mousemove', event => {
  if (waveformState.dragging) {
    seekFromClientX(event.clientX);
  }
});
window.addEventListener('mouseup', () => {
  waveformState.dragging = false;
});
window.addEventListener('resize', resizeWaveCanvas);
resizeWaveCanvas();
setLoading('No track loaded');
</script>
{% endif %}
</body>
</html>
"""


@app.get("/")
def index():
    latest = None
    if SESSIONS_DIR.exists():
        sessions = sorted(SESSIONS_DIR.glob("*/session.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        if sessions:
            latest = json.loads(sessions[0].read_text(encoding="utf-8"))
    return render_template_string(HTML, session=latest)


@app.post("/sessions")
def create_session():
    load_local_env()
    tracks = parse_track_list(request.form.get("tracks", ""))
    if not tracks:
        return redirect(url_for("index"))

    name = request.form.get("name", "download-session").strip() or "download-session"
    session_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slugify(name)}"
    session = {
        "id": session_id,
        "name": name,
        "status": "queued",
        "startedAt": time.time(),
        "currentIndex": 0,
        "cancelRequested": False,
        "tracks": tracks,
        "items": [{"query": track, "status": "pending"} for track in tracks],
    }
    write_session(session)
    threading.Thread(target=run_session, args=(session_id,), daemon=True).start()
    return redirect(url_for("show_session", session_id=session_id))


@app.get("/sessions/<session_id>")
def show_session(session_id: str):
    return render_template_string(HTML, session=read_session(session_id))


@app.get("/api/session/<session_id>")
def api_session(session_id: str):
    sync_download_states(session_id)
    return jsonify(read_session(session_id))


@app.post("/sessions/<session_id>/playlist")
def make_playlist(session_id: str):
    create_playlist(read_session(session_id))
    return redirect(url_for("show_session", session_id=session_id))


@app.post("/sessions/<session_id>/rekordbox-export")
def make_rekordbox_export(session_id: str):
    create_rekordbox_export(read_session(session_id))
    return redirect(url_for("show_session", session_id=session_id))


@app.post("/sessions/<session_id>/cancel")
def cancel_session(session_id: str):
    def mutate(session: dict[str, Any]) -> None:
        session["cancelRequested"] = True
        if session.get("status") not in {"complete", "cancelled"}:
            session["status"] = "cancel_requested"
        session["endedAt"] = time.time()

    update_session(session_id, mutate)
    add_event(session_id, "Cancel requested.")
    return redirect(url_for("show_session", session_id=session_id))


@app.get("/sessions/<session_id>/playlist")
def download_playlist(session_id: str):
    session = read_session(session_id)
    playlist_path = Path(session["playlistPath"])
    return send_file(playlist_path, as_attachment=True, download_name=playlist_path.name)


@app.get("/sessions/<session_id>/playlist-simple")
def download_simple_playlist(session_id: str):
    session = read_session(session_id)
    playlist_path = Path(session["simplePlaylistPath"])
    return send_file(playlist_path, as_attachment=True, download_name=playlist_path.name)


@app.get("/sessions/<session_id>/rekordbox-export")
def download_rekordbox_export(session_id: str):
    session = read_session(session_id)
    playlist_path = Path(session["rekordboxPlaylistPath"])
    return send_file(playlist_path, as_attachment=True, download_name=playlist_path.name)


@app.get("/sessions/<session_id>/items/<int:index>/audio")
def stream_session_audio(session_id: str, index: int):
    session = read_session(session_id)
    path = item_audio_path(session, index)
    if not path or not path.exists():
        return ("Not found", 404)
    return send_file(path, conditional=True)


if __name__ == "__main__":
    load_local_env()
    SESSIONS_DIR.mkdir(exist_ok=True)
    app.run(host="127.0.0.1", port=5055, debug=False)
