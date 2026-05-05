#!/bin/bash
# NewsBrief — Celery Worker + Beat (Back4App)

set -e

echo "Starting NewsBrief Celery services..."
echo "AI Provider: ${AI_PROVIDER:-claude}"
echo "Broker: ${CELERY_BROKER_URL:-not set}"

# ── Health check HTTP server ──────────────────────────────────
# Back4App requires a process listening on PORT via HTTP.
# serve_forever() blocks the main thread — the & puts the entire
# Python process in the background so it stays alive permanently.
python3 -c "
import http.server, os, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'worker ok')
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()
    def log_message(self, *a): pass
port = int(os.environ.get('PORT', 8000))
sys.stdout.write(f'[health] Listening on :{port}\n')
sys.stdout.flush()
http.server.HTTPServer(('0.0.0.0', port), H).serve_forever()
" &

HEALTH_PID=$!
echo "[health] PID $HEALTH_PID"

# Give the health server a moment to bind the port
sleep 2

# ── Beat scheduler ────────────────────────────────────────────
echo "[beat] Starting scheduler..."
celery -A app.workers.tasks.celery_app beat \
  --loglevel=info \
  --scheduler redbeat.RedBeatScheduler \
  --pidfile=/tmp/celerybeat.pid &

BEAT_PID=$!
echo "[beat] PID $BEAT_PID"

sleep 3

# ── Worker (foreground — keeps container alive) ───────────────
echo "[worker] Starting worker..."
celery -A app.workers.tasks.celery_app worker \
  --loglevel=info \
  --pool=solo

# Cleanup if worker exits
kill $BEAT_PID 2>/dev/null || true
kill $HEALTH_PID 2>/dev/null || true
