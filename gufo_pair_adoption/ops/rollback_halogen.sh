#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
#
# rollback_halogen.sh — revert PAIR's llamacpp engine slot from the GUFO
# masquerade back to the Halogen Qwen 3.8 Flash Next deployment.
#
# Rollback is deliberately NOT symmetric with install: the shim never owned the
# Halogen deployment, so rollback = stop pretending to be the router + restore
# the previous manifest. Halogen runs on :8731 under systemd
# (halogen-flash-server.service) and is a DIFFERENT port from the shim's 8081,
# so stopping GUFO does not disturb Halogen and vice versa.
#
# This script NEVER deletes model weights. Not Halogen's, not GUFO's.
# The GUFO pod is left stopped-but-present so the flip forward is cheap.

set -euo pipefail

PAIR_ENGINES_DIR="${PAIR_ENGINES_DIR:-$HOME/.config/Nvidia Corporation/Personal AI Router/engines}"
TARGET="$PAIR_ENGINES_DIR/llamacpp.json"
HALOGEN_SERVICE="${HALOGEN_SERVICE:-halogen-flash-server.service}"
HALOGEN_PORT="${HALOGEN_PORT:-8731}"
SHIM_PORT="${SHIM_PORT:-8081}"
KEEP_GUFO_POD="${KEEP_GUFO_POD:-1}"
CTL="${GUFO_PAIRCTL:-/usr/local/bin/gufo-pairctl}"

log() { printf 'rollback: %s\n' "$*" >&2; }
warn() { printf 'rollback: WARNING: %s\n' "$*" >&2; }
die() { printf 'rollback: ERROR: %s\n' "$*" >&2; exit 1; }

DRY="${DRY:-0}"
run() {
  if [[ "$DRY" == "1" ]]; then
    printf '  [dry-run] %s\n' "$*" >&2
  else
    "$@"
  fi
}

# --- 1. stop PAIR managing the llamacpp engine -----------------------------
# Freeze management first so PAIR cannot auto-restart the shim mid-rollback.
log "step 1: tell PAIR to stop the llamacpp engine"
if command -v pairctl >/dev/null 2>&1; then
  run pairctl engine stop llamacpp || warn "pairctl engine stop failed (continuing)"
else
  warn "pairctl not found; stop the llamacpp engine in the PAIR UI before continuing"
fi

# --- 2. bring down the GUFO pod -------------------------------------------
log "step 2: stop the GUFO masquerade pod"
if [[ -x "$CTL" ]]; then
  run "$CTL" stop || warn "gufo-pairctl stop failed (continuing)"
else
  warn "$CTL not executable; stopping pod directly if present"
  run podman pod stop -t 20 gufo-pair 2>/dev/null || log "no gufo-pair pod to stop"
fi

if [[ "$KEEP_GUFO_POD" == "1" ]]; then
  log "GUFO pod left present but stopped (set KEEP_GUFO_POD=0 + 'gufo-pairctl destroy' to remove)"
else
  log "KEEP_GUFO_POD=0 -> destroying pod and shim state (weights untouched)"
  run "$CTL" destroy 2>/dev/null || run podman pod rm -f gufo-pair 2>/dev/null || true
fi

# --- 3. restore the previous manifest -------------------------------------
log "step 3: restore the previous llamacpp manifest"
latest_backup="$(ls -1t "$TARGET".pre-gufo.* 2>/dev/null | head -1 || true)"
absent_marker="$(ls -1t "$TARGET".absent.* 2>/dev/null | head -1 || true)"

if [[ -n "$latest_backup" ]]; then
  log "restoring from $latest_backup"
  run cp -p "$latest_backup" "$TARGET"
elif [[ -n "$absent_marker" ]]; then
  log "install recorded no prior override -> removing override so bundled manifest applies"
  run rm -f "$TARGET"
else
  warn "no backup and no absence marker found for $TARGET"
  warn "refusing to guess. Remove $TARGET by hand if you are sure there was no prior override."
  die "cannot determine previous manifest state"
fi

# --- 4. restart PAIR so it re-reads manifests -----------------------------
log "step 4: restart PAIR's engine manager"
restarted=0
for svc in "systemctl --user restart nvpair" "sudo systemctl restart nvpair"; do
  # shellcheck disable=SC2086
  if $svc >/dev/null 2>&1; then
    log "restarted via: $svc"
    restarted=1
    break
  fi
done
[[ "$restarted" == "1" ]] || warn "could not restart nvpair automatically; restart the PAIR app manually"

# --- 5. verify ------------------------------------------------------------
log "step 5: verification"
sleep 3

fails=0
# 5a. the shim must no longer answer as a router on 8081
if curl -fsS --max-time 5 "http://127.0.0.1:$SHIM_PORT/props" 2>/dev/null | grep -q '"role"[[:space:]]*:[[:space:]]*"router"'; then
  warn "something STILL reports role=router on :$SHIM_PORT"
  warn "if this is a leftover shim, run: $CTL destroy"
  fails=$((fails+1))
else
  log "  OK  no router on :$SHIM_PORT"
fi

# 5b. the manifest must not reference gufo-pairctl
if [[ -f "$TARGET" ]] && grep -q 'gufo-pairctl' "$TARGET"; then
  warn "$TARGET still references gufo-pairctl"
  fails=$((fails+1))
else
  log "  OK  manifest has no gufo-pairctl reference"
fi

# 5c. Halogen must be alive on its own port
if curl -fsS --max-time 5 "http://127.0.0.1:$HALOGEN_PORT/v1/models" >/dev/null 2>&1; then
  log "  OK  Halogen answering on :$HALOGEN_PORT"
else
  warn "Halogen not answering on :$HALOGEN_PORT"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl status "$HALOGEN_SERVICE" --no-pager 2>/dev/null | head -12 >&2 || true
  fi
  warn "start it with: sudo systemctl start $HALOGEN_SERVICE"
  fails=$((fails+1))
fi

# 5d. no gufo pod running
if podman pod exists gufo-pair 2>/dev/null; then
  pst="$(podman pod inspect -f '{{.State}}' gufo-pair 2>/dev/null || echo unknown)"
  if [[ "$pst" == "Running" ]]; then
    warn "gufo-pair pod is still Running"
    fails=$((fails+1))
  else
    log "  OK  gufo-pair pod present but $pst"
  fi
else
  log "  OK  gufo-pair pod absent"
fi

echo
if (( fails == 0 )); then
  log "ROLLBACK COMPLETE — all checks passed"
  log "Confirm in the PAIR UI that Halogen / Qwen 3.8 Flash Next is listed and running."
else
  log "ROLLBACK FINISHED WITH $fails WARNING(S) — review above before declaring success."
  exit 1
fi
