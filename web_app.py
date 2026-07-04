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
)


ROOT = Path(__file__).resolve().parent
SESSIONS_DIR = ROOT / "sessions"
DOWNLOADS_DIR = ROOT / "slskd-data" / "downloads"
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

    update_session(session_id, mutate)


def search_args() -> SimpleNamespace:
    return SimpleNamespace(
        responses=50,
        file_limit=10000,
        timeout=20,
        min_speed=0,
        max_queue=1000000,
        ext=SUPPORTED_EXTENSIONS,
        no_broad=False,
        pause=1.0,
        limit=10,
    )


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
            best, used_query, _results = find_best_result(client, args, track)
            if not best:
                update_session(
                    session_id,
                    lambda s, i=index, q=used_query: s["items"][i].update(
                        {"status": "missing", "usedQuery": q, "activeQuery": q}
                    ),
                )
                add_event(session_id, f"No result found: {track}")
                continue

            ok = client.transfers.enqueue(best["username"], [best["file"]])
            status = "queued" if ok else "queue_failed"
            update_session(
                session_id,
                lambda s, i=index, q=used_query, b=best, st=status: s["items"][i].update(
                    {
                        "status": st,
                        "usedQuery": q,
                        "activeQuery": q,
                        "username": b["username"],
                        "filename": b["filename"],
                        "basename": re.split(r"[\\/]", b["filename"])[-1],
                        "bitRate": b.get("bitRate"),
                        "length": b.get("length"),
                        "size": b.get("size"),
                        "qualityRank": quality_rank(b),
                    }
                ),
            )
            if ok:
                queued_basename = re.split(r"[\\/]", best["filename"])[-1]
                add_event(session_id, f"Queued: {queued_basename}")
            else:
                add_event(session_id, f"Queue failed: {track}")
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


def audio_files_since(start_epoch: float) -> list[Path]:
    extensions = {f".{ext.lower().lstrip('.')}" for ext in SUPPORTED_EXTENSIONS}
    if not DOWNLOADS_DIR.exists():
        return []
    return [
        path
        for path in DOWNLOADS_DIR.rglob("*")
        if path.is_file() and path.suffix.lower() in extensions and path.stat().st_mtime >= start_epoch
    ]


def create_playlist(session: dict[str, Any]) -> Path:
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
    files = sorted(files, key=lambda path: path.stat().st_mtime)

    playlist_path = SESSIONS_DIR / session["id"] / f"{session['id']}.m3u8"
    lines = ["#EXTM3U"]
    for path in files:
        lines.append(f"#EXTINF:-1,{path.stem}")
        lines.append(str(path.resolve()))
    playlist_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    update_session(
        session["id"],
        lambda s: s.update({"playlistPath": str(playlist_path), "playlistTrackCount": len(files)}),
    )
    add_event(session["id"], f"Created playlist with {len(files)} tracks.")
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
    .status-missing, .status-error, .status-queue_failed { color:var(--bad); font-weight:700; }
    .status-queued, .status-complete { color:var(--ok); font-weight:700; }
    .status-searching { color:#856000; font-weight:700; }
    .filename { color:var(--muted); font-size:12px; margin-top:3px; word-break:break-word; }
    .activity { max-height:220px; overflow:auto; background:#fffdf8; border:1px solid var(--line); border-radius:6px; padding:10px; margin:12px 0; font:13px Consolas, monospace; }
    .activity div { padding:3px 0; border-bottom:1px solid #eee4d3; }
    .progress { color:var(--muted); font-size:12px; margin-top:3px; }
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
          {% if session.playlistPath %}<a class="button secondary" href="{{ url_for('download_playlist', session_id=session.id) }}">Download M3U8</a>{% endif %}
        </div>
        <div class="activity" id="activity"></div>
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
function escapeHtml(value) {
  return String(value || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function render(data) {
  document.getElementById('status').textContent = data.status;
  const queued = data.items.filter(x => x.status === 'queued').length;
  const missing = data.items.filter(x => x.status === 'missing').length;
  document.getElementById('counts').textContent = `${queued} queued, ${missing} missing, ${data.items.length} total`;
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
      </td>
    </tr>`).join('');
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


@app.get("/sessions/<session_id>/playlist")
def download_playlist(session_id: str):
    session = read_session(session_id)
    playlist_path = Path(session["playlistPath"])
    return send_file(playlist_path, as_attachment=True, download_name=playlist_path.name)


if __name__ == "__main__":
    load_local_env()
    SESSIONS_DIR.mkdir(exist_ok=True)
    app.run(host="127.0.0.1", port=5055, debug=False)
