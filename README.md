# Bookplate — self-hosted ebook library

Self-hosted multi-user ebook library: upload with auto-metadata, hash dedup,
in-browser reading, z-library search/download (optional), user-to-user sharing,
OPDS for iOS reader apps, AI metadata fallback (optional).

## Run

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
.venv/bin/python scripts/selftest.py   # 14 end-to-end checks against a running server
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
