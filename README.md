# Bookplate — self-hosted ebook library

Self-hosted multi-user ebook library: upload with auto-metadata, hash dedup,
in-browser reading, z-library search/download (optional), user-to-user sharing,
OPDS for iOS reader apps, AI metadata fallback (optional).

## Deploy with Docker

Multi-arch image (`linux/amd64` + `linux/arm64`) published to GHCR by CI on every push to
`main` and every `v*` tag. Docker picks the right variant automatically — x86-64 servers,
Apple Silicon, Raspberry Pi 4/5 and ARM NAS all just `docker pull`.

```bash
# quick start
docker run -d --name bookplate -p 8480:8480 -v bookplate-data:/app/data \
  ghcr.io/bacnh85/bookplate:latest

# or with compose (recommended)
docker compose up -d
# then open http://localhost:8480
```

All state lives in the mounted volume (`/app/data` inside the container): SQLite DB,
book files, covers. Back up = copy that volume. To move an existing local-dev install,
stop the server and copy the whole local `data/` dir into the volume — with compose
(named volume `bookplate-data`) one way:

```bash
docker run --rm -v bookplate-data:/dest -v "$PWD/data":/src:ro alpine sh -c 'cp -a /src/. /dest/ && chown -R 1000:1000 /dest'
```

The `chown` matters: `cp -a` keeps the source uid (501 on macOS, your uid on Linux) but
the container runs as uid 1000 — without it the migrated DB is unwritable and the
container crash-loops.

### Image tags

| Tag | What |
|---|---|
| `latest` | newest build from `main` |
| `main` | current `main` (same as `latest`) |
| `1.2.3`, `1.2` | release builds (`git tag v1.2.3 && git push --tags`) |
| `sha-<commit>` | exact commit a running container came from |

### Configuration

Optional keys are passed as env (`-e` / compose `environment:`) — same vars as the table
below (`.env.local` is only a local-dev convenience, never used in Docker):

- `ZAI_API_KEY` (+ optional `ZAI_BASE_URL` / `ZAI_MODEL`) — AI metadata fallback.
- `ZLIB_EMAIL` / `ZLIB_PASSWORD` — Z-Library search/download. The `zlib` CLI is baked
  into the image; with credentials set it auto-logs-in on demand (the session is
  per-container and re-establishes after restarts).

### Notes

- The server is plain HTTP — put a TLS reverse proxy (e.g. Caddy) in front for anything
  beyond a trusted LAN (OPDS/reader apps use basic auth).
- Healthcheck is built into the image; `docker ps` shows `healthy` once up.
- Prefer the named volume (compose default). Bind-mounting a host dir instead? On
  Linux, pre-create it with the right owner or the container (uid 1000) can't write:
  `mkdir -p data && sudo chown 1000:1000 data`
- Pulling from a private package? `docker login ghcr.io` with a GitHub PAT (read:packages),
  or flip the package to public in the GitHub UI (Package settings → visibility).

## Local development (macOS)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
scripts/serve.sh &                      # binds 127.0.0.1:8480 (keepalive, auto-restarts)
HOST=0.0.0.0 scripts/serve.sh &         # or LAN-exposed for iPhone/iPad testing
# open http://localhost:8480
# stop:  kill $(cat data/serve.pid)
```

`scripts/serve.sh` must be started from a Terminal context: macOS TCC denies
launchd-spawned processes access to `/Volumes`, so a LaunchAgent cannot run this
repo (verified: launchd children get `getcwd: Operation not permitted`). After a
reboot, start it again — or ask the agent to.

⚠️ The server is plain HTTP. On `HOST=0.0.0.0` — including OPDS basic auth —
credentials cross the LAN in cleartext; fine for a trusted home LAN, use a TLS
reverse proxy (e.g. Caddy) for anything wider.

## Data (migration-ready)

All state lives in `data/` (gitignored): `ebook.db` (+WAL), content-addressed
`books/`, `covers/`, `tmp/`. To migrate to a remote server later, Docker-mount
this one folder as a volume — nothing else needs copying. `.env.local` is
gitignored too; recreate provider keys there on the target host.

## Optional env (`.env.local` — loaded by `serve.sh` at startup)

| Var | Purpose |
|---|---|
| `ZLIB_EMAIL` / `ZLIB_PASSWORD` | Your Z-Library account — used to auto-login the `zlib` CLI if no session exists |
| `ZAI_API_KEY` | AI metadata fallback for garbage files (any OpenAI-compatible provider) |
| `ZAI_BASE_URL` / `ZAI_MODEL` | Defaults: `https://api.z.ai/api/openai/v1`, `glm-5.3-flash` |

Z-Library uses the `zlib` CLI (EAPI mobile-app API, auto-solves the DiamWall
proof-of-work that walls raw HTTP clients). One-time setup:

```bash
brew install heartleo/tap/zlib
zlib doctor --eapi                        # pick a 'healthy' domain (e.g. z-lib.gd)
zlib login --eapi --email you@x --password ... --domain https://z-lib.gd
```

Add keys to `.env.local`, then restart: `kill $(cat data/serve.pid)` and start
again. No web settings UI — secrets stay out of the database and out of git.

## Test

```bash
.venv/bin/python scripts/selftest.py   # e2e checks against a running server
```

## iPhone / iPad

- Browser: works in Safari (reader, upload, search).
- Native reader apps (KyBook 3, MapleRead, Yomu): add OPDS catalog
  `http://<host>:8480/opds` with your email + password (HTTP basic auth —
  cleartext over plain HTTP, see the warning above).

## Layout

- `app/` — FastAPI: `db.py` (SQLite+FTS5), `auth.py`, `storage.py` (SHA-256 store),
  `metadata.py` (extraction chain), `ai.py`, `zlib_client.py`, `opds.py`, `main.py`
- `web/` — vanilla JS SPA + vendored `foliate-js` (no build step)
- `data/` — SQLite DB, content-addressed book files, covers (gitignored)
