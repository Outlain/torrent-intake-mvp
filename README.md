# Torrent Intake MVP

A privacy-focused intake controller for qBittorrent.

This service accepts new torrent jobs, stages downloads in a controlled location, scans completed content for malware, deletes infected content, and only promotes clean files to a final destination under `/downloads`.

## What This Project Does

Given a magnet link and a final destination path, the app:

1. Creates a tracked intake job.
2. Adds the torrent to qBittorrent with controlled category/tags.
3. Stages download data in either local staging or NAS staging.
4. Scans completed content with `clamdscan`.
5. Deletes infected torrents and files.
6. Promotes clean torrents to the requested final destination.
7. Sends Telegram notifications only for malware detection/deletion events.

## Security Model

- Default staging mode is local staging at `/staging-local`.
- Optional staging mode is NAS staging at `/downloads/torrent-intake/staging`.
- If a job was requested as `local` and the torrent size exceeds `TI_LOCAL_MAX_GIB`, the app overrides staging to NAS.
- If a job was requested as `nas`, it stays on NAS (no move back to local).
- Only final paths under `/downloads` are accepted (`TI_FINAL_PARENT_PREFIX`).
- Malware scan runs before any promotion step.
- On infection, torrent and files are deleted; no promotion occurs.
- Telegram alerts are sent only on infection/deletion.

## Container Paths

These are the required in-container paths:

- NAS root visible to app and qBittorrent: `/downloads`
- Local staging inside app container: `/staging-local`
- App persistent data: `/app/data`
- App logs: `/app/logs`

## Required Environment Variables

Copy `.env.example` to `.env` and set values for your environment.

| Variable | Required | Purpose |
|---|---|---|
| `TI_DATABASE_URL` | Yes | SQLite DB path, default `sqlite:////app/data/torrent_intake.db` |
| `TI_QBT_HOST` | Yes | qBittorrent WebUI URL |
| `TI_QBT_USERNAME` | Yes | qBittorrent username |
| `TI_QBT_PASSWORD` | Yes | qBittorrent password |
| `TI_QBT_VERIFY_CERTIFICATE` | Yes | TLS cert verification for qBittorrent |
| `TI_LOCAL_STAGING_ROOT` | Yes | Must be `/staging-local` in container |
| `TI_NAS_STAGING_ROOT` | Yes | Usually `/downloads/torrent-intake/staging` |
| `TI_FINAL_PARENT_PREFIX` | Yes | Must be `/downloads` |
| `TI_LOCAL_MAX_GIB` | Yes | Local-to-NAS override threshold |
| `TI_CLAMDSCAN_BINARY` | Yes | Malware scanner binary |
| `TI_CLAMDSCAN_ARGS` | Yes | Scanner args |
| `TI_TELEGRAM_BOT_TOKEN` | Optional | Required only if Telegram alerts enabled |
| `TI_TELEGRAM_CHAT_ID` | Optional | Required only if Telegram alerts enabled |

Other tunables are documented in `.env.example`.

## Volume Mounts

The app container should mount:

- host runtime data -> `/app/data`
- host logs -> `/app/logs`
- host local staging -> `/staging-local`
- NAS mount -> `/downloads`

Example host paths (examples only):

- project/build folder: `/opt/docker/torrent-intake`
- runtime data folder: `/opt/docker/torrent-intake-data`
- local staging host path: `/opt/torrent-intake/staging`
- NAS host path: `/mnt/media`

## Docker Compose Example

```yaml
services:
  torrent-intake:
    image: ghcr.io/outlain/torrent-intake-mvp:latest
    container_name: torrent-intake
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - /opt/docker/torrent-intake-data/data:/app/data
      - /opt/docker/torrent-intake-data/logs:/app/logs
      - /opt/torrent-intake/staging:/staging-local
      - /mnt/media:/downloads
    ports:
      - "8095:8000"
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
```

If you prefer local builds instead of pulling from GHCR, replace `image:` with `build: .`.

## Portainer Stack Example

See `portainer-stack.example.yml` and adjust host paths, qBittorrent endpoint, and credentials.

## API Summary

- `POST /jobs` submit intake job
- `GET /jobs` list jobs
- `GET /jobs/{job_id}` job detail
- `POST /events/qbt-complete` JSON completion event
- `POST /events/qbt-complete-form` form completion event
- `GET /health` health endpoint
- `GET /ui` basic UI

Example job request:

```json
{
  "magnet_uri": "magnet:?xt=urn:btih:...",
  "final_parent": "/downloads/Movies",
  "final_category": "movies",
  "staging_preference": "local"
}
```

## Workflow Architecture

```text
Client -> Intake API -> qBittorrent (staging path)
                          |
                          v
                    Download completes
                          |
                          v
                Malware scan (clamdscan)
                  |                     |
                  | infected            | clean
                  v                     v
          Delete torrent+files     Move to final_parent
          Telegram alert sent       Optional final category
```

## What Should Not Be Committed

Never commit:

- `.env` files
- tokens/passwords/chat IDs
- runtime DB/log files (`*.db`, `*.sqlite*`, `*.log`)
- app runtime directories (`data/`, `logs/`, `/app/data` snapshots)
- local caches/build artifacts (`__pycache__/`, virtualenvs, test caches)
- machine-local files (for example `.DS_Store`)

Use the provided `.gitignore` and `.dockerignore` to keep Git history and Docker build context clean.

## Build and Run Locally

```bash
docker build -t torrent-intake-mvp:local .
docker run --rm -p 8095:8000 --env-file .env \
  -v /opt/docker/torrent-intake-data/data:/app/data \
  -v /opt/docker/torrent-intake-data/logs:/app/logs \
  -v /opt/torrent-intake/staging:/staging-local \
  -v /mnt/media:/downloads \
  torrent-intake-mvp:local
```
