#!/usr/bin/env sh
# Launch TrainingOS for local visual testing.
# Migrates the DB, optionally imports FIT data, refreshes metrics/docs,
# and starts the coach web UI.
#
# Overridable env vars:
#   TRAININGOS_DB_PATH        default: ./var/trainingos.sqlite3
#   TRAININGOS_RAW_DATA_DIR   default: ./var/raw
#   TRAININGOS_FIT_IMPORT_DIR default: ./var/fit-imports  (skipped if absent)
#   TRAININGOS_LOCAL_TIMEZONE default: America/Toronto
#   TRAININGOS_COACH_PORT     default: 8765

set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"

db_path=${TRAININGOS_DB_PATH:-$repo_root/var/trainingos.sqlite3}
raw_data_dir=${TRAININGOS_RAW_DATA_DIR:-$repo_root/var/raw}
fit_import_dir=${TRAININGOS_FIT_IMPORT_DIR:-$repo_root/var/fit-imports}
timezone=${TRAININGOS_LOCAL_TIMEZONE:-America/Toronto}
coach_port=${TRAININGOS_COACH_PORT:-8765}

export TRAININGOS_DB_PATH=$db_path
export TRAININGOS_RAW_DATA_DIR=$raw_data_dir
export TRAININGOS_LOCAL_TIMEZONE=$timezone

_step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
_ok()   { printf '\033[0;32m    ✓ %s\033[0m\n' "$*"; }
_warn() { printf '\033[0;33m    ! %s\033[0m\n' "$*"; }

# ── 1. Database ────────────────────────────────────────────────────────────────
_step "Initializing database"
mkdir -p "$(dirname "$db_path")" "$raw_data_dir"
PYTHONPATH=src python3 -m trainingos.storage
_ok "Database ready at $db_path"

# ── 2. FIT import (optional) ──────────────────────────────────────────────────
if [ -d "$fit_import_dir" ]; then
    fit_files=$(find "$fit_import_dir" \( -name '*.fit' -o -name '*.zip' \) 2>/dev/null | wc -l | tr -d ' ')
    if [ "$fit_files" -gt 0 ]; then
        _step "Importing FIT data from $fit_import_dir ($fit_files file(s))"
        PYTHONPATH=src python3 -m trainingos.ingestion.fit_import \
            --timezone "$timezone" \
            "$fit_import_dir"
        _ok "FIT import complete"
    else
        _ok "No FIT files in $fit_import_dir — skipping import"
    fi
fi

# ── 3. Refresh metrics and retrieval documents ────────────────────────────────
_step "Refreshing metrics and retrieval documents"
PYTHONPATH=src python3 -m trainingos.refresh --timezone "$timezone"
_ok "Metrics and retrieval documents up to date"

# ── 4. Coach web UI ────────────────────────────────────────────────────────────
_step "Starting coach web UI"
printf '\n'
printf '  Coach:    http://localhost:%s\n' "$coach_port"
printf '  Health:   http://localhost:%s/api/health\n' "$coach_port"
printf '\n  Press Ctrl-C to stop the coach.\n\n'
PYTHONPATH=src python3 -m trainingos.coach_web
