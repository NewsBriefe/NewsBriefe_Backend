#!/bin/bash
# ─────────────────────────────────────────────────────────────
# NewsBrief — Celery Worker + Beat startup script
# Used by Back4App Container deployment.
#
# Runs both worker and beat in the same container using a
# simple background process approach. Beat schedules tasks,
# worker executes them. Both connect to Upstash Redis.
#
# Back4App start command: /bin/bash start_worker.sh
# ─────────────────────────────────────────────────────────────

set -e

echo "Starting NewsBrief Celery services..."
echo "AI Provider: ${AI_PROVIDER:-claude}"
echo "Broker: ${CELERY_BROKER_URL:-not set}"

# Start beat scheduler in background
echo "[beat] Starting scheduler..."
celery -A app.workers.tasks.celery_app beat \
  --loglevel=info \
  --scheduler redbeat.RedBeatScheduler \
  --pidfile=/tmp/celerybeat.pid &

BEAT_PID=$!
echo "[beat] Started with PID $BEAT_PID"

# Small delay so beat registers schedules before worker starts
sleep 3

# Start worker in foreground (keeps container alive)
echo "[worker] Starting worker..."
celery -A app.workers.tasks.celery_app worker \
  --loglevel=info \
  --pool=solo

# If worker exits, kill beat too
kill $BEAT_PID 2>/dev/null || true
