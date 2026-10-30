#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
#
# rollback_all.sh — return amd-ai-core-two to the pre-adoption state:
# Halogen serving on :8731, PAIR with its original manifests.
#
# Ordering is deliberate:
#   1. Bring Halogen back FIRST (restores inference for this conversation).
#   2. Verify it actually serves — a masked-but-stopped Halogen is not a rollback.
#   3. Only then tear the GUFO stack down and restore PAIR manifests.
#
# --halogen-only stops at step 2 (use when you just want inference back fast and
# will deal with PAIR/manifests later).
#
# Exit: 0 rolled back | 1 FAILED TO RESTORE INFERENCE (manual action required) | 2 usage

set -uo pipefail

HOST="${HOST:-amd-ai-core-two.lan}"
HAL_UNIT="${HALOGEN_UNIT:-halogen-flash-server.service}"
ENV_PATH="/etc/gufo-pair/env"
PAIR_ENGINES_REL=".config/Nvidia Corporation/Personal AI Router/engines"
RUN_USER="turnstone"
HALOGEN_ONLY=0
DRAIN_WAIT="${DRAIN_WAIT:-25}"

say()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$*"; }
hdr()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
die()  { printf '\n\033[41m ROLLBACK FAILED \033[0m %s\n' "$*"; exit 1; }
err()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; }
rssh() { ssh -o ConnectTimeout=15 -o BatchMode=yes "$HOST" "$1" 2>/dev/null; }
rsudo(){ rssh "sudo -n bash -lc '$1'"; }

HALOGEN_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --halogen-only) HALOGEN_ONLY=1; shift ;;
    --drain-wait) DRAIN_WAIT="$2"; shift 2 ;;
    -h|--help)
      say "usage: rollback_all.sh [--host HOST] [--halogen-only] [--drain-wait N]"
      say "  --halogen-only   restore Halogen inference and stop there"
      exit 0 ;;
    *) err "unknown arg: $1"; exit 2 ;;
  esac
done

[[ "$DRAIN_WAIT" =~ ^[0-9]+$ ]] || die "--drain-wait must be an integer"
PASS=0; FAIL=0

say "=============================================================="
say " ROLLBACK  host=$HOST  unit=$HAL_UNIT"
say " scope=$([[ $HALOGEN_ONLY -eq 1 ]] && echo 'halogen-only' || echo 'full')"
say "=============================================================="

# ---------------------------------------------------- 1. Halogen back up
hdr "1. Restoring Halogen"
rsudo "systemctl unmask $HAL_UNIT" >/dev/null
enabled=$(rssh "systemctl is-enabled $HAL_UNIT 2>/dev/null")
if [[ "$enabled" == "enabled" ]]; then
  ok "unmasked $HAL_UNIT"
else
  die "could not unmask $HAL_UNIT (state: ${enabled:-unknown}). Manual: ssh $HOST sudo systemctl unmask $HAL_UNIT"
fi

rsudo "systemctl restart $HAL_UNIT" >/dev/null
say "  waiting ${DRAIN_WAIT}s for the container to bind :8731 ..."
sleep "$DRAIN_WAIT"

hal_port=$(rssh "ss -tln '( sport = :8731 )' 2>/dev/null | tail -n +2 | wc -l")
if [[ "${hal_port:-0}" -gt 0 ]]; then
  ok ":8731 bound again"
else
  die "Halogen did not rebind :8731. INFERENCE IS NOT RESTORED.
  Investigate: ssh $HOST 'sudo journalctl -u $HAL_UNIT -n 50'
  Do NOT proceed with the rest of the rollback until inference is back."
fi

hal_models=$(rssh "curl -fsS --max-time 15 http://127.0.0.1:8731/v1/models 2>/dev/null")
if printf '%s' "$hal_models" | grep -q '"data"'; then
  ok "Halogen serving models"
  printf '%s' "$hal_models" | tr ',' '\n' | grep '"id"' | head -2 | sed 's/^/        /'
else
  die "Halogen bound :8731 but /v1/models is not serving. INFERENCE NOT RESTORED."
fi

if [[ "$HALOGEN_ONLY" -eq 1 ]]; then
  hdr "Halogen-only rollback complete — inference restored."
  say "The GUFO stack and PAIR override are still in place (paused, not torn down)."
  say "Run without --halogen-only for the full rollback."
  exit 0
fi

# ---------------------------------------------------- 2. GUFO stack down
hdr "2. Stopping GUFO stack"
say "  (after inference is safely on Halogen, so no request path is orphaned)"
rssh "sudo -u $RUN_USER GUFO_PAIR_COMPOSE=/opt/gufo-pair/compose.yaml GUFO_PAIR_ENV=$ENV_PATH \
      /usr/local/bin/gufo-pairctl stop" 2>/dev/null \
  && ok "gufo-pairctl stop issued" || warn "gufo-pairctl stop returned non-zero (may already be down)"

sleep 3
for p in 8081 8901; do
  n=$(rssh "ss -tln '( sport = :$p )' 2>/dev/null | tail -n +2 | wc -l")
  [[ "${n:-0}" -eq 0 ]] && ok ":$p released" || warn ":$p still bound"
done

# ---------------------------------------------------- 3. PAIR manifests
hdr "3. Restoring PAIR manifests"
# install_pair_override.sh writes backups as:
#   <target>/llamacpp.json.pre-gufo.<STAMP>      (prior override existed)
#   <target>/llamacpp.json.absent.<STAMP>        (no prior override; bundled was in force)
# The engines dir contains spaces ("Nvidia Corporation"), so every path is quoted.
ovr_dir="/home/turnstone/$PAIR_ENGINES_REL"
target="$ovr_dir/llamacpp.json"

bk=$(rssh "ls -1t '$target'.pre-gufo.* 2>/dev/null | head -1")
marker=$(rssh "ls -1t '$target'.absent.* 2>/dev/null | head -1")

if [[ -n "$bk" ]]; then
  rssh "cp -p '$bk' '$target'" 2>/dev/null
  has_gufo=$(rssh "grep -c 'gufo-pairctl' '$target' 2>/dev/null || echo 0")
  if [[ "${has_gufo:-0}" -eq 0 ]]; then
    ok "restored prior override from $(basename "$bk")"
  else
    warn "restored file still mentions gufo-pairctl — verify: $bk"
  fi
elif [[ -n "$marker" ]]; then
  # Installer recorded that NO override existed before us: remove ours so the
  # bundled manifest applies again. Restoring nothing is the correct action.
  rssh "rm -f '$target'" 2>/dev/null \
    && ok "override removed per absence marker $(basename "$marker") (bundled manifest applies)" \
    || bad "could not remove $target"
else
  warn "neither a .pre-gufo backup nor an .absent marker found for $target"
  warn "Refusing to guess the prior state. Options:"
  warn "  a) If there was no prior override:  rm -f '$target'"
  warn "  b) If there was: restore it by hand from your own backup"
  warn "  Leaving the GUFO override in place for now — inference is on Halogen regardless."
fi

# ---------------------------------------------------- 4. PAIR service
hdr "4. PAIR service"
pair=$(rssh 'systemctl is-active nvpair.service 2>/dev/null')
if [[ "$pair" == "active" ]]; then
  ok "nvpair.service already active"
else
  say "  starting nvpair.service (PAIR node rejoins the cluster)"
  rsudo "systemctl start nvpair.service" >/dev/null
  sleep 8
  pair=$(rssh 'systemctl is-active nvpair.service 2>/dev/null')
  [[ "$pair" == "active" ]] && ok "nvpair.service started" || bad "nvpair.service failed to start ($pair)"
fi

# ---------------------------------------------------- 5. Final state
hdr "Final state"
printf '  %-12s %s\n' "Halogen" "$(rssh "systemctl is-active $HAL_UNIT 2>/dev/null")/$(rssh "systemctl is-enabled $HAL_UNIT 2>/dev/null")"
printf '  %-12s %s\n' "PAIR" "$(rssh 'systemctl is-active nvpair.service 2>/dev/null')"
printf '  %-12s %s\n' ":8731" "$(rssh "ss -tln '( sport = :8731 )' 2>/dev/null | tail -n +2 | wc -l" | xargs -I{} echo '{} listener(s)')"
printf '  %-12s %s\n' ":8081" "$(rssh "ss -tln '( sport = :8081 )' 2>/dev/null | tail -n +2 | wc -l" | xargs -I{} echo '{} listener(s)')"
printf '  %-12s %s\n' ":8901" "$(rssh "ss -tln '( sport = :8901 )' 2>/dev/null | tail -n +2 | wc -l" | xargs -I{} echo '{} listener(s)')"

hdr "Rollback complete  passed=$PASS warnings=$FAIL"
say "Next: confirm this conversation still responds. If it does, inference is on Halogen."
say "GUFO artifacts remain on disk for a later retry; 'gufo-pairctl destroy' removes pod+state"
say "but never model weights."
[[ $FAIL -gt 0 ]] && say "Review WARN items above — none blocked inference." || true
exit 0
