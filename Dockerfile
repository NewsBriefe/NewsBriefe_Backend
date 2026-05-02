FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
RUN mkdir -p app && touch app/__init__.py

RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir .

COPY . .

RUN useradd -m -u 1001 appuser && chown -R appuser /app \
    && chmod +x start_worker.sh

USER appuser

EXPOSE 8000

# SERVICE env var controls what runs:
#   SERVICE=api    → FastAPI server  (Render)
#   SERVICE=worker → Celery worker + beat (Back4App)
#
# Set SERVICE=worker in Back4App environment variables.
# Render uses the default SERVICE=api.
ENV SERVICE=api

CMD ["sh", "-c", \
     "if [ \"$SERVICE\" = \"worker\" ]; then /bin/bash start_worker.sh; \
      else uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}; fi"]