# Soulseek CLI

This is a basic non-GUI Python client for a local `slskd` server. It does not connect to Soulseek directly; `slskd` handles the Soulseek protocol and this script controls it through the API.

## Requirements

- Python dependencies from `requirements.txt`
- A running `slskd` server
- A `slskd` API key

## Installed Local slskd

This project includes a Docker Compose install:

```powershell
docker compose -f docker-compose.slskd.yml up -d
```

The local web UI is:

```text
http://localhost:5030
```

Default web login:

```text
slskd / slskd
```

Create a local `.env` file from `.env.example` and set a long local API key:

```powershell
Copy-Item .env.example .env
notepad .env
```

You still need to configure your Soulseek network username/password in the slskd web UI before searches can work.

## Environment

```powershell
$env:SLSKD_URL="http://localhost:5030"
$env:SLSKD_API_KEY="your-slskd-api-key"
```

## Commands

```powershell
py soulseek_cli.py status
py soulseek_cli.py connect
py soulseek_cli.py search "Adam Ten I Never Knew"
py soulseek_cli.py search "Adam Ten I Never Knew" --limit 10 --ext flac mp3
py soulseek_cli.py best-download "Francis Mercier & Magic System Premier Gaou (Nitefreak Remix)" --dry-run
py soulseek_cli.py best-download "Francis Mercier & Magic System Premier Gaou (Nitefreak Remix)"
py soulseek_cli.py download 1
py soulseek_cli.py downloads
```

## Local Web App

Start the browser app:

```powershell
powershell -ExecutionPolicy Bypass -File .\start_web_app.ps1
```

Open:

```text
http://127.0.0.1:5055
```

The app lets you paste a plain text track list, start a session, monitor queued/missing tracks, and create a session `.m3u8` playlist. Session files are written under `sessions/`, which is ignored by Git.

After the search phase completes, the web app keeps monitoring queued downloads. If a track is still remotely queued or ends in an error/rejected/cancelled state after the retry window, it searches again and queues a different user/file candidate. Defaults:

```text
DJSET_RETRY_AFTER_SECONDS=300
DJSET_MAX_RETRIES=3
```

Search results are cached to `.last_soulseek_search.json`, so `download 1` queues the first result from the most recent search.

`best-download` ranks results with these rules:

- If the query includes a remix name, prefer filenames containing that remixer.
- Prefer `320 kbps MP3`.
- Use `FLAC` if no strong `320 kbps MP3` candidate is available.
- Prefer longer versions after quality/remix matching.
- Use open upload slots, shorter queues, and faster peers as tie-breakers.

For remix searches, `best-download` also tries broader query variants. For example, `Francis Mercier & Magic System Premier Gaou (Nitefreak Remix)` can fall back to searches like `premier gaou nitefreak remix`.

Only download material you have the rights or permission to download.
