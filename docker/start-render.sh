#!/usr/bin/env sh
set -eu

PORT_VALUE="${PORT:-10000}"
HOST_VALUE="${SPOTDL_HOST:-0.0.0.0}"

mkdir -p "${SPOTDL_DATA_DIR:-/var/data/songzip}"

exec python -m uvicorn spotdl.render_app:app --host "${HOST_VALUE}" --port "${PORT_VALUE}"
