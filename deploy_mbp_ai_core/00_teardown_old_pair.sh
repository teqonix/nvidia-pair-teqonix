#!/usr/bin/env bash
# 00_teardown_old_pair.sh
#
# STOP + TEARDOWN of the existing PAIR 0.91.7 stack on mbp-ai-core.
#
# MUST RUN AS teqonix (or root). As `turnstone` every kill below fails with
# "operation not permitted" - the live nvpair-* processes are teqonix-owned.
#
#   as teqonix :  ssh teqonix@mbp-ai-core.lan 'bash -s' < 00_teardown_old_pair.sh
#   via sudo   :  ssh turnstone@mbp-ai-core.lan 'sudo bash -s' < 00_teardown_old_pair.sh
#
# Order matters: stop the supervisor before the workers, or the supervisor just
# respawns them.
set -uo pipefail

log() { printf '\033[1;34m[teardown]\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$1"; }

WHO=$(whoami)
if [ "$WHO" != "teqonix" ] && [ "$WHO" != "root" ]; then
  echo "REFUSING to run as '$WHO'. This script must run as teqonix or root."
  echo "As turnstone, kill -0 on teqonix pids returns 'operation not permitted'."
  exit 1
fi

APP="/Applications/PAIR.app"
TDIR="/Users/teqonix/Library/Application Support/Nvidia Corporation/Personal AI Router"

# ---------------------------------------------------------------------------
# 0. Backups (only what THIS user can read; teqonix config needs teqonix)
# ---------------------------------------------------------------------------
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BK="/Users/teqonix/pair_backup_pre_1_0_0_${STAMP}"
mkdir -p "$BK" 2>/dev/null || BK="/tmp/pair_backup_pre_1_0_0_${STAMP}"
log "backup dir: $BK"

if [ -d "$TDIR" ]; then
  if cp -a "$TDIR" "$BK/config-teqonix" 2>/dev/null; then
    log "backed up live config -> $BK/config-teqonix"
  else
    warn "could not copy teqonix config (permissions). Continuing."
  fi
fi
if [ -d "$APP/Contents/Resources/cli-bin" ]; then
  cp -a "$APP/Contents/Resources/cli-bin" "$BK/cli-bin-0.91.7" 2>/dev/null \
    && log "backed up old binaries -> $BK/cli-bin-0.91.7"
  cp -a "$APP/Contents/Resources/cli-bin/manifest.json" "$BK/manifest-0.91.7.json" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 1. Stop the supervisor first: the TUI owns nvpair-ui-broker, which owns everything.
#    Killing workers without killing the TUI just gets them respawned.
# ---------------------------------------------------------------------------
log "stopping TUI / tmux session 'pair'"
tmux kill-session -t pair 2>/dev/null && log "  tmux session 'pair' killed" || true
# the GUI app itself, if it is what is supervising
if pgrep -f "PAIR.app/Contents/MacOS" >/dev/null 2>&1; then
  log "quitting PAIR.app (GUI supervisor)"
  osascript -e 'quit app "PAIR"' 2>/dev/null || pkill -f "PAIR.app/Contents/MacOS" 2>/dev/null || true
  sleep 3
fi

# ---------------------------------------------------------------------------
# 2. Belt-and-braces: stop the broker, then the workers it supervises.
# ---------------------------------------------------------------------------
log "stopping nvpair-ui-broker"
pkill -TERM -f 'cli-bin/nvpair-ui-broker' 2>/dev/null && log "  sent SIGTERM" || log "  (not running)"
sleep 4

log "stopping remaining nvpair-* workers"
pkill -TERM -f 'cli-bin/nvpair-' 2>/dev/null && log "  sent SIGTERM to workers" || log "  (none left)"
pkill -TERM -f 'cli-bin/ollama-proxy'   2>/dev/null || true
pkill -TERM -f 'cli-bin/lmstudio-proxy' 2>/dev/null || true
sleep 5

# force anything still alive
LEFT=$(pgrep -f 'cli-bin/(nvpair-|ollama-proxy|lmstudio-proxy)' 2>/dev/null | tr '\n' ' ')
if [ -n "${LEFT:-}" ]; then
  warn "still alive after SIGTERM: $LEFT - sending SIGKILL"
  pkill -KILL -f 'cli-bin/(nvpair-|ollama-proxy|lmstudio-proxy)' 2>/dev/null || true
  sleep 2
fi

# ---------------------------------------------------------------------------
# 3. LM Studio: it holds the real model server behind :1234 today.
#    Freeing :1234 requires stopping LM Studio's server, or the new proxy will
#    bind a fallback port and clients will keep hitting MLX.
# ---------------------------------------------------------------------------
log "stopping LM Studio"
osascript -e 'quit app "LM Studio"' 2>/dev/null || true
pkill -TERM -f 'LM Studio' 2>/dev/null || true
pkill -TERM -f '\.lmstudio/\.internal' 2>/dev/null || true
sleep 5
pgrep -f 'LM Studio' >/dev/null 2>&1 && { warn "LM Studio still up - SIGKILL"; pkill -KILL -f 'LM Studio' 2>/dev/null || true; }

# ---------------------------------------------------------------------------
# 4. Confirm the ports we need are actually free.
# ---------------------------------------------------------------------------
log "port state after teardown:"
FAIL=0
for p in 1234 11434 8080 8081; do
  if lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then
    printf '  \033[1;31m%-6s STILL BOUND\033[0m  %s\n' "$p" \
      "$(lsof -nP -iTCP:"$p" -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $1" pid"$2}' | sort -u | tr '\n' ' ')"
    FAIL=1
  else
    printf '  %-6s free\n' "$p"
  fi
done

echo
if [ "$FAIL" -eq 1 ]; then
  warn "Some ports are still held. If held by a process you cannot kill, you are"
  warn "not running as the owning user - re-run as teqonix or root."
  exit 1
fi
log "teardown complete. Ports 1234/11434/8080/8081 are free."
echo
echo "Next: run 30_install_turnstone.sh as turnstone, then 40_install_launchd.sh with sudo."
echo "Rollback later: as teqonix, 'open -a PAIR' restores the 0.91.7 GUI-supervised router."
