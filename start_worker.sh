#!/bin/bash
# NewsBrief — Celery Worker + Beat
# Runs on Back4App. Starts a tiny health HTTP server so Back4App
# health checks pass, then starts beat + worker.

set -e

echo "Starting NewsBrief Celery services..."
echo "AI Provider: ${AI_PROVIDER:-claude}"
echo "Broker: ${CELERY_BROKER_URL:-not set}"

# ── Health check server ───────────────────────────────────────
# Back4App checks port 8000 via HTTP. The worker doesn't serve HTTP
# so we run a one-line Python HTTP server in the background to
# answer those health pings and keep the container alive.
python3 -c "
import http.server, threading, os
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'ok')
    def log_message(self, *a): pass
port = int(os.environ.get('PORT', 8000))
t = threading.Thread(target=http.server.HTTPServer(('0.0.0.0', port), H).serve_forever, daemon=True)
t.start()
print(f'[health] Listening on :{port}')
" &

sleep 1

# ── Beat scheduler ────────────────────────────────────────────
echo "[beat] Starting scheduler..."
celery -A app.workers.tasks.celery_app beat \
  --loglevel=info \
  --scheduler redbeat.RedBeatScheduler \
  --pidfile=/tmp/celerybeat.pid &

BEAT_PID=$!
echo "[beat] Started with PID $BEAT_PID"

sleep 3

# ── Worker (foreground — keeps container alive) ───────────────
echo "[worker] Starting worker..."
celery -A app.workers.tasks.celery_app worker \
  --loglevel=info \
  --pool=solo

kill $BEAT_PID 2>/dev/null || true
