#!/bin/bash
# Keepalive dev server.  Start: HOST=0.0.0.0 scripts/serve.sh &   Stop: kill $(cat data/serve.pid)
#
# Binds 127.0.0.1 by default. HOST=0.0.0.0 exposes plain HTTP to the LAN — OPDS
# uses HTTP basic auth, so credentials cross the network in cleartext; fine for
# a trusted home LAN, use a TLS reverse proxy for anything wider.
cd "$(dirname "$0")/.." || exit 1
# load optional provider keys (ZLIB_*, ZAI_*) from the gitignored .env.local
[ -f .env.local ] && { set -a; . ./.env.local; set +a; }
UV=".venv/bin/uvicorn"
HOST="${HOST:-127.0.0.1}"
LOG="data/server.log"
if [ ! -x "$UV" ]; then
  echo "serve.sh: $UV not found — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi
mkdir -p data
if [ -f data/serve.pid ] && kill -0 "$(cat data/serve.pid)" 2>/dev/null; then
  echo "serve.sh: already running (pid $(cat data/serve.pid)) — stop it first: kill $(cat data/serve.pid)" >&2
  exit 1
fi
echo $$ > data/serve.pid
fails=0
while true; do
  # rotate at ~5 MB so persistent failures can't grow the log unbounded (GNU+BSD safe)
  [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 5242880 ] && mv "$LOG" "$LOG.1"
  t0=$(date +%s)
  "$UV" app.main:app --host "$HOST" --port 8480 >> "$LOG" 2>&1 &
  child=$!
  trap 'kill "$child" 2>/dev/null; rm -f data/serve.pid; exit 0' TERM INT
  wait "$child"; rc=$?
  trap - TERM INT
  echo "[$(date '+%F %T')] server exited (rc=$rc, uptime=$(( $(date +%s) - t0 ))s)" >> "$LOG"
  # give up after 5 consecutive fast failures (bad venv, port conflict, …)
  if [ $(( $(date +%s) - t0 )) -lt 5 ]; then
    fails=$((fails + 1))
    if [ "$fails" -ge 5 ]; then
      echo "serve.sh: 5 consecutive fast failures, giving up" >&2
      rm -f data/serve.pid
      exit 1
    fi
  else
    fails=0
  fi
  sleep 2
done
