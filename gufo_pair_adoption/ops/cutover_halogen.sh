#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
#
# cutover_halogen.sh — stop Halogen ONLY once GUFO is proven, and come back
# automatically if the post-cutover probe fails.
#
# WHY THIS SCRIPT EXISTS
# The drafted plan ordered "disable Halogen" *before* "smoke test GUFO". That is
# inverted: if GUFO cannot serve, there is no model left on the network and the
# operator's own session dies with no tested fallback. Halogen is the live
# inference backend for sessions routed to core-two:8731 (confirmed in the
# journal: POST /v1/chat/completions from worker-two).
#
# This script therefore:
#   1. RE-PROVES GUFO serves a real completion (never trusts cached state)
#   2. Snapshots Halogen's exact unit state for rollback
#   3. stop + MASK  (mask, because Restart=always would respawn it; and a reboot
#      mid-migration must not resurrect it either)
#   4. Probes end-to-end through the PAIR façade -> shim -> GUFO
#   5. On ANY failure: unmask + restart Halogen automatically, then report
#
# The operator must affirm they are not themselves served by the backend they are
# stopping. There is no way for this script to know that for certain, so it asks.
#
# Exit codes: 0 cutover succeeded | 1 rolled back | 2 refused/usage

set -uo pipefail

HOST="${HOST:-amd-ai-core-two.lan}"
HAL_UNIT="halogen-flash-server.service"
SHIM_URL="http://127.0.0.1:8081"
CONFIRM="${CONFIRM_STOP_HALOGEN:-}"
ACK_REMOTE=0
SNAPDIR="/var/lib/gufo-pair/cutover"
PROMPT_MODEL="${GUFO_TEST_MODEL:-gufo-local}"
FORCE=0

say()  { printf '%s\n' "$*"; }
log()  { printf '[cutover] %s\n' "$*"; }
err()  { printf '[cutover] ERROR: %s\n' "$*" >&2; }
hdr()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

rssh()  { ssh -o ConnectTimeout=10 -o BatchMode=yes "$HOST" "$1" 2>/dev/null; }
rssudo(){ ssh -o ConnectTimeout=10 -o BatchMode=yes -t "$HOST" "sudo -n $1" 2>/dev/null; }

usage() {
  cat <<USAGE
usage: cutover_halogen.sh --host HOST --i-am-not-running-on-halogen [options]

Stops and masks Halogen on HOST. Refuses to run without the acknowledgement flag.

  --host HOST                       target (default: $HOST)
  --i-am-not-running-on-halogen    REQUIRED. Asserts the session driving this
                                    script is NOT served by HOST:8731.
  --confirm VALUE                   also honoured via CONFIRM_STOP_HALOGEN=yes
  --test-model NAME                 model id to probe (default: $PROMPT_MODEL)
  --force                           skip the interactive prompt (still needs the flag)
  --no-auto-restore                 disable automatic Halogen restoration on failure
                                    (NOT recommended)

SAFETY MODEL
  * GUFO must serve a real completion BEFORE Halogen is touched.
  * Any post-cutover probe failure restores Halogen automatically.
  * Halogen is MASKED, not merely stopped, so Restart=always cannot fight you.
    Remember to unmask in rollback_all.sh.
USAGE
}

AUTO_RESTORE=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --i-am-not-running-on-halogen) ACK_REMOTE=1; shift ;;
    --confirm) CONFIRM="$2"; shift 2 ;;
    --test-model) PROMPT_MODEL="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --no-auto-restore) AUTO_RESTORE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown arg: $1"; usage; exit 2 ;;
  esac
done

# ------------------------------------------------------- hard refusal gates
hdr "0. Gates"

if [[ $ACK_REMOTE -ne 1 ]]; then
  err "refusing to stop the inference backend without --i-am-not-running-on-halogen"
  say ""
  say "Halogen on $HOST:8731 is a LIVE inference backend. Sessions routed there"
  say "(including agent sessions) will lose their model the moment it stops."
  say ""
  say "If you are driving this from a session that is itself served by $HOST:8731,"
  say "you are about to cut your own lifeline. Run this from a different node,"
  say "a different model, or a plain terminal."
  say ""
  say "When you are certain, re-run with:  --i-am-not-running-on-halogen"
  exit 2
fi

if [[ "$CONFIRM" != "yes" && $FORCE -ne 1 ]]; then
  printf '\nAbout to STOP and MASK %s on %s.\nType exactly "STOP HALOGEN" to continue: ' "$HAL_UNIT" "$HOST"
  read -r reply
  if [[ "$reply" != "STOP HALOGEN" ]]; then
    say "aborted by operator (got: '${reply}') — nothing changed"
    exit 2
  fi
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
rssh "sudo mkdir -p $SNAPDIR && sudo chmod 0755 $SNAPDIR" || true

# ------------------------------------------------------- 1. re-prove GUFO
hdr "1. Re-proving GUFO serves a real completion (pre-cutover gate)"

props=$(rssh "curl -fsS --max-time 8 $SHIM_URL/props 2>/dev/null")
if printf '%s' "$props" | grep -q '"role"[[:space:]]*:[[:space:]]*"router"'; then
  log "shim /props reports role=router"
else
  err "shim /props not reporting role=router — GUFO stack is NOT ready"
  say "  got: ${props:-<no response>}"
  say "  Halogen was NOT touched. Run deploy_stack.sh --start first."
  exit 1
fi

models=$(rssh "curl -fsS --max-time 8 $SHIM_URL/v1/models 2>/dev/null")
if printf '%s' "$models" | grep -q '"id"'; then
  log "shim /v1/models lists: $(printf '%s' "$models" | tr ',' '\n' | grep '"id"' | head -3 | tr '\n' ' ')"
else
  err "shim /v1/models empty — GUFO NOT ready; Halogen untouched"
  exit 1
fi

# The actual gate: a real completion, end to end, before we remove the fallback.
log "requesting a real completion through shim -> GUFO (model: $PROMPT_MODEL)"
completion=$(rssh "curl -fsS --max-time 120 $SHIM_URL/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{\"model\":\"$PROMPT_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: GUFO_ALIVE\"}],\"max_tokens\":32}' 2>/dev/null")

if printf '%s' "$completion" | grep -q 'GUFO_ALIVE' && printf '%s' "$completion" | grep -q 'content'; then
  log "GUFO produced a real completion — gate PASSED"
else
  err "GUFO did not produce a valid completion"
  say "  response: $(printf '%s' "$completion" | head -c 400)"
  say ""
  say "  Halogen was NOT touched. Your session is safe. Fix GUFO and retry."
  exit 1
fi

# ------------------------------------------------------- 2. snapshot Halogen
hdr "2. Snapshotting Halogen state for rollback"

rssh "mkdir -p $SNAPDIR; {\
  echo 'unit=$HAL_UNIT'; \
  echo \"active=\$(systemctl is-active $HAL_UNIT 2>/dev/null)\"; \
  echo \"enabled=\$(systemctl is-enabled $HAL_UNIT 2>/dev/null)\"; \
  echo \"masked=\$(systemctl is-enabled $HAL_UNIT 2>/dev/null || echo masked)\"; \
  echo \"stamp=$STAMP\"; \
} | sudo tee $SNAPDIR/halogen-$STAMP.state >/dev/null"

rssh "systemctl cat $HAL_UNIT 2>/dev/null | sudo tee $SNAPDIR/$HAL_UNIT.$STAMP.unit >/dev/null" || true
log "state + unit snapshot -> $SNAPDIR/halogen-$STAMP.{state,unit}"
say "  snapshot contents:"
rssh "cat $SNAPDIR/halogen-$STAMP.state 2>/dev/null" | sed 's/^/    /'

# ------------------------------------------------------- 3. stop + mask
hdr "3. Stopping and masking Halogen"

log "systemctl stop $HAL_UNIT"
rssh "sudo systemctl stop $HAL_UNIT" || err "stop returned non-zero (continuing; verifying port)"

# Mask BEFORE probing: Restart=always would otherwise race us back to life.
log "systemctl mask $HAL_UNIT (prevents Restart=always and boot resurrection)"
rssh "sudo systemctl mask $HAL_UNIT" || err "mask failed"

sleep 3
if rssh "ss -tln '( sport = :8731 )' 2>/dev/null | tail -n +2 | grep -q ."; then
  err "port 8731 STILL bound after stop+mask — aborting and restoring"
  post_ok=0
  # Fall through to the restore block below by jumping to the shared handler.
  HAL_PORT_STUCK=1
else
  HAL_PORT_STUCK=0
  log "port 8731 released"
fi

# ------------------------------------------------------- 4. post-cutover probe
hdr "4. Post-cutover end-to-end probe"

post_ok=1
if [[ "${HAL_PORT_STUCK:-0}" -eq 1 ]]; then
  err "Halogen port never released — treating as cutover failure, restoring"
  post_ok=0
fi
if ! rssh "curl -fsS --max-time 10 $SHIM_URL/health >/dev/null 2>&1"; then
  err "shim /health unreachable after cutover"; post_ok=0
fi

post=$(rssh "curl -fsS --max-time 120 $SHIM_URL/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{\"model\":\"$PROMPT_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: CUTOVER_OK\"}],\"max_tokens\":32}' 2>/dev/null")

if printf '%s' "$post" | grep -q 'CUTOVER_OK'; then
  log "post-cutover completion OK"
else
  err "post-cutover completion FAILED"
  say "  response: $(printf '%s' "$post" | head -c 400)"
  post_ok=0
fi

# ------------------------------------------------------- 5. restore on failure
if [[ $post_ok -ne 1 ]]; then
  hdr "5. POST-CUTOVER FAILURE — RESTORING HALOGEN"
  if [[ $AUTO_RESTORE -ne 1 ]]; then
    err "auto-restore DISABLED. Halogen stays down. Restore manually:"
    say "    ssh $HOST 'sudo systemctl unmask $HAL_UNIT && sudo systemctl start $HAL_UNIT'"
    exit 1
  fi
  log "unmasking $HAL_UNIT"
  rssh "sudo systemctl unmask $HAL_UNIT" || err "unmask failed"
  log "starting $HAL_UNIT"
  rssh "sudo systemctl start $HAL_UNIT" || err "start returned non-zero"

  # Wait for the port to come back — that's what clients actually care about.
  restored=0
  for i in $(seq 1 40); do
    if rssh "ss -tln '( sport = :8731 )' 2>/dev/null | tail -n +2 | grep -q ."; then
      restored=1; break
    fi
    sleep 3
  done
  if [[ $restored -eq 1 ]]; then
    hal_live=$(rssh "curl -fsS --max-time 10 http://127.0.0.1:8731/v1/models 2>/dev/null")
    if printf '%s' "$hal_live" | grep -q '"id"'; then
      say "\nROLLED BACK SUCCESSFULLY — Halogen serving again on :8731"
      say "  $(printf '%s' "$hal_live" | head -c 200)"
      say "\nGUFO remains installed but not cut over. Investigate, then retry."
      exit 1
    fi
  fi
  err "Halogen did NOT come back within 120s — MANUAL INTERVENTION REQUIRED"
  say "  ssh $HOST 'systemctl status $HAL_UNIT --no-pager'"
  say "  snapshot: $SNAPDIR/$HAL_UNIT.$STAMP.unit"
  exit 1
fi

# ------------------------------------------------------- success
hdr "CUTOVER COMPLETE"
say "Halogen: stopped and masked on $HOST"
say "GUFO   : serving via shim on 127.0.0.1:8081"
say "Snapshot: $SNAPDIR/halogen-$STAMP.state"
say ""
say "Next: ops/smoke_pair_facade.sh --host $HOST --post-cutover"
say "Undo: ops/rollback_all.sh --host $HOST --halogen-only"
say ""
say "NOTE: Halogen is MASKED. 'systemctl start' will fail until you unmask."
say "      That is intentional — it stops Restart=always from fighting the migration."
exit 0
