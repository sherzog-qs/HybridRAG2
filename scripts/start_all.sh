#!/usr/bin/env bash
set -euo pipefail

# Start local embedding server (jina-embeddings-v2-base-de) and Streamlit GUI with one command.
# Usage:
#   EMBED_PORT=8000 UI_PORT=8501 GRAPHRAG_DEFAULT_ROOT="$(pwd)/christmas" bash scripts/start_all.sh
#
# Notes:
# - Requires `uv`. Dependencies are provided at runtime with `uv run`.
# - Writes logs to logs/embedding_server.log

EMBED_PORT=${EMBED_PORT:-8000}
UI_PORT=${UI_PORT:-8501}
HOST=127.0.0.1
LOG_DIR="logs"
mkdir -p "$LOG_DIR"

# Pre-check: is port free?
if lsof -i ":$EMBED_PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "[error] port $EMBED_PORT already in use" >&2
  exit 1
fi

# Start local embeddings server in background
echo "[start] embedding server on http://$HOST:$EMBED_PORT"
export EMBED_DEVICE=${EMBED_DEVICE:-cpu}
uv run \
  --with fastapi \
  --with uvicorn \
  --with "sentence-transformers>=2.6" \
  --with "torch>=2.2" \
  uvicorn apps.local_embedding_server.server:app \
    --host "$HOST" --port "$EMBED_PORT" \
    > "$LOG_DIR/embedding_server.log" 2>&1 &
EMBED_PID=$!

cleanup() {
  echo "[stop] embedding server (pid=$EMBED_PID)"
  kill $EMBED_PID >/dev/null 2>&1 || true
}
trap cleanup INT TERM EXIT

# Wait for health (allow longer on first run due to dependency resolution)
WAIT_MAX=${WAIT_MAX:-240}  # attempts
WAIT_SLEEP=${WAIT_SLEEP:-0.5}

echo -n "[wait] embedding server ready"
for i in $(seq 1 "$WAIT_MAX"); do
  if curl -s "http://$HOST:$EMBED_PORT/" >/dev/null; then
    echo " - ok"
    break
  fi
  # If process died, abort early and show logs
  if ! kill -0 "$EMBED_PID" >/dev/null 2>&1; then
    echo "\n[error] embedding server process exited early" >&2
    echo "----- logs/embedding_server.log (last 200 lines) -----" >&2
    tail -n 200 "$LOG_DIR/embedding_server.log" >&2 || true
    exit 1
  fi
  echo -n "."
  sleep "$WAIT_SLEEP"
  if [ "$i" -eq "$WAIT_MAX" ]; then
    echo "\n[error] embedding server did not start in time" >&2
    echo "----- logs/embedding_server.log (last 200 lines) -----" >&2
    tail -n 200 "$LOG_DIR/embedding_server.log" >&2 || true
    exit 1
  fi
done

# Start Streamlit UI (foreground)
echo "[start] streamlit on http://$HOST:$UI_PORT"
uv run --with streamlit \
  streamlit run apps/streamlit_graphrag/app.py \
  --server.headless true \
  --server.port "$UI_PORT"
