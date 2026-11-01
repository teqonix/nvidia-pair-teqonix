#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
#
# deploy_stack.sh — stage the GUFO masquerade stack on the target.
#
#   --stage-only   write files, fix the shim bind, install the manifest override.
#                  Does NOT start anything. Reversible with rollback_all.sh.
#   --start        additionally bring up the GUFO pod + shim (Halogen untouched).
#
# Deliberately separate from cutover: staging is safe while Halogen serves;
# stopping Halogen is not, and lives in cutover_halogen.sh.
#
# Exit: 0 ok | 1 failure | 2 usage

set -uo pipefail

HOST="${HOST:-amd-ai-core-two.lan}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_ROOT="/opt/gufo-pair"
ENV_PATH="/etc/gufo-pair/env"
PAIR_ENGINES_REL=".config/Nvidia Corporation/Personal AI Router/engines"
RUN_USER="turnstone"
# Model source on the target. Halogen's .hgn weights are NOT usable by GUFO
# (`gufo serve llm` takes GGUF), so point at a GGUF set on the host.
#
# Default is GUFO's QUALIFIED target (docs/models/qwen3.8-flash-next/README.md):
#   unsloth/Qwen3.8-Flash-Next-GGUF @ 38bb39ee97821de2c9009abb7e93950eec396e66
#   UD-Q4_K_XL/  (4 shards, ~111 GB)
# The repo qualifies ONLY this quant; "Original unquantized-model and
# GGUF-conversion parity remain unqualified." The IQ4_NL-PROJFIX set that was
# the previous default is NOT on the qualified list -- override deliberately
# with GUFO_MODEL_FILE if you accept that.
MODELS_DIR="${GUFO_MODELS_DIR:-/home/turnstone/models/Qwen3.8-Flash-Next-GGUF/UD-Q4_K_XL}"
MODEL_FILE="${GUFO_MODEL_FILE:-Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf}"
SERVED_NAME="${SERVED_MODEL_NAME:-gufo-qwen38-flash-next}"

# GUFO admits by reserving the FULL configured context capacity up front.
# --context 0 = native from model metadata = 262144 tokens/session for this
# model, which is not what the 88.78 GiB qualified figure was measured at
# (that was capacity 133121). Cap it explicitly; see the memory guard below.
GUFO_CTX="${GUFO_CONTEXT:-32768}"
GUFO_SESS="${GUFO_SESSIONS:-1}"
# Refuse to start unless MemAvailable exceeds this many GiB. Sized for the
# qualified 88.78 GiB peak plus headroom; raise only with GUFO_ALLOW_COEXIST.
MIN_AVAIL_GIB="${GUFO_MIN_AVAIL_GIB:-95}"

MODE="stage"
say()  { printf '%s\n' "$*"; }
log()  { printf '[deploy] %s\n' "$*"; }
err()  { printf '[deploy] ERROR: %s\n' "$*" >&2; }
warn() { printf '\033[33m[deploy] warn:\033[0m %s\n' "$*" >&2; }
hdr()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
rssh()  { ssh -o ConnectTimeout=15 -o BatchMode=yes "$HOST" "$1" 2>/dev/null; }
rscp()  { scp -o ConnectTimeout=15 -o BatchMode=yes -q "$@"; }

usage() {
  cat <<USAGE
usage: deploy_stack.sh [--host HOST] (--stage-only | --start)

  --stage-only   install compose/env/ctl/override files; start nothing
  --start        stage, then start the GUFO pod + shim (Halogen left running)

  env overrides: GUFO_MODELS_DIR, GUFO_MODEL_FILE, SERVED_MODEL_NAME
                 SHIM_PORT (8081), GUFO_PORT (18080)

Nothing here stops Halogen. Use cutover_halogen.sh for that.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --stage-only) MODE="stage"; shift ;;
    --start) MODE="start"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown arg: $1"; usage; exit 2 ;;
  esac
done

[[ -d "$REPO_DIR/ops" ]] || { err "repo layout not found at $REPO_DIR"; exit 2; }

hdr "0. Preflight"
if ! rssh 'echo UP' | grep -q UP; then err "cannot reach $HOST"; exit 1; fi
log "target reachable: $HOST"

# ------------------------------------------------------- 1. remote directories
hdr "1. Remote directories"
rssh "sudo mkdir -p $REMOTE_ROOT /var/lib/gufo-masquerade $ENV_PATH.d && \
      sudo chown -R ${RUN_USER}:${RUN_USER} $REMOTE_ROOT /var/lib/gufo-masquerade" \
  || { err "could not create remote dirs"; exit 1; }
log "$REMOTE_ROOT and /var/lib/gufo-masquerade owned by $RUN_USER"

# ------------------------------------------------------- 2. copy artifacts
hdr "2. Copying artifacts"
rscp "$REPO_DIR/container/compose.yaml" "$HOST:$REMOTE_ROOT/compose.yaml" || { err "compose copy failed"; exit 1; }
rscp "$REPO_DIR/container/Containerfile.shim" "$HOST:$REMOTE_ROOT/Containerfile.shim" || true
rscp "$REPO_DIR/shim/gufo_masquerade_shim.py" "$HOST:$REMOTE_ROOT/" || { err "shim copy failed"; exit 1; }
rscp "$REPO_DIR/shim/requirements.txt" "$HOST:$REMOTE_ROOT/" || true
rscp "$REPO_DIR/ops/gufo-pairctl" "$HOST:$REMOTE_ROOT/gufo-pairctl" || { err "ctl copy failed"; exit 1; }
rssh "sudo install -m 0755 $REMOTE_ROOT/gufo-pairctl /usr/local/bin/gufo-pairctl" \
  || { err "could not install /usr/local/bin/gufo-pairctl"; exit 1; }
log "compose + shim + gufo-pairctl staged; /usr/local/bin/gufo-pairctl installed"

# ------------------------------------------------------- 3. VERIFY THE SHIM BIND
# PAIR authenticates to the shim with NOTHING; the shim holds the GUFO key.
# A 0.0.0.0 bind therefore = unauthenticated LAN proxy with a live credential.
# The shipped template now defaults SHIM_HOST to loopback via ${SHIM_BIND:-...}.
# The env file is not written until step 4, so `compose config` cannot resolve
# here; verify statically now and re-verify the live socket in step 7.
hdr "3. Verifying shim bind defaults to loopback"
if rssh "grep -q 'SHIM_HOST: \"0.0.0.0\"' $REMOTE_ROOT/compose.yaml"; then
  rssh "sed -i 's/SHIM_HOST: \"0.0.0.0\"/SHIM_HOST: \"127.0.0.1\"/' $REMOTE_ROOT/compose.yaml"
  log "rewrote legacy 0.0.0.0 literal to 127.0.0.1"
fi

# Accept either a literal loopback or a variable whose default is loopback.
if rssh "grep -E 'SHIM_HOST:' $REMOTE_ROOT/compose.yaml | grep -qvE '127\\.0\\.0\\.1'"; then
  err "shim bind default is not loopback — refusing to continue"
  rssh "grep -n SHIM_HOST $REMOTE_ROOT/compose.yaml" | sed 's/^/    /'
  exit 1
fi
log "confirmed: template default is loopback (live socket re-checked in step 7)"

# ------------------------------------------------------- 4. env file
hdr "4. Writing $ENV_PATH (mode 0600)"
# Generate a fresh key. Never reuse a committed value; never echo the key.
NEWKEY=$(rssh "openssl rand -hex 24 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \\n'")
if [[ -z "$NEWKEY" ]]; then err "could not generate API key"; exit 1; fi

if rssh "test -f $ENV_PATH"; then
  cp_bak="$ENV_PATH.pre-deploy.$(date +%Y%m%d-%H%M%S)"
  rssh "sudo cp -p $ENV_PATH $cp_bak" && log "existing env backed up -> $cp_bak"
fi

# Written remotely so the key never round-trips into this repo.
rssh "sudo tee $ENV_PATH >/dev/null <<EOF
# Generated by deploy_stack.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
GUFO_API_KEY=$NEWKEY
SHIM_BIND=127.0.0.1
SHIM_PORT=8081
GUFO_BASE_URL=http://127.0.0.1:18080
POD_NAME=gufo-pair
GUFO_PAIR_POD=gufo-pair
GUFO_PAIR_COMPOSE=$REMOTE_ROOT/compose.yaml
GUFO_PAIR_ENV=$ENV_PATH
GUFO_IMAGE=localhost/gufo:latest
SHIM_IMAGE=localhost/gufo-masquerade:1.0.0
GUFO_MODELS_DIR=$MODELS_DIR
GUFO_MODEL_FILE=$MODEL_FILE
SERVED_MODEL_NAME=$SERVED_NAME
SHIM_STATE_PATH=/var/lib/gufo-masquerade/state.json
SHIM_LOG_LEVEL=info
SHIM_ACCESS_LOG=0
SHIM_REFUSE_UNLOAD_SERVING=1
SHIM_PULL_POLICY=present-ok
GUFO_READY_TIMEOUT=300
EOF
sudo chown ${RUN_USER}:${RUN_USER} $ENV_PATH && sudo chmod 0600 $ENV_PATH" \
  || { err "could not write $ENV_PATH"; exit 1; }
log "env written, 0600, owned by $RUN_USER (key generated, not logged)"

# ------------------------------------------------------- 5. model dir check
hdr "5. Model availability"
if rssh "test -f $MODELS_DIR/$MODEL_FILE"; then
  log "model present: $MODELS_DIR/$MODEL_FILE"
  rssh "ls -la $MODELS_DIR/*.gguf 2>/dev/null | head -3" | sed 's/^/    /'
  warn_shards=$(rssh "ls $MODELS_DIR/${MODEL_FILE%-00001-*}-*-of-*.gguf 2>/dev/null | wc -l")
  [[ "${warn_shards:-0}" -gt 1 ]] && \
    log "note: sharded GGUF set detected ($warn_shards parts) — GUFO reads the first shard and finds the rest in-dir"
else
  err "model file not found: $MODELS_DIR/$MODEL_FILE"
  say "  set GUFO_MODEL_FILE / GUFO_MODELS_DIR to a GGUF that exists, or stage one."
  say "  Halogen's .hgn weights are NOT consumable by 'gufo serve llm'."
  say "  Available GGUFs:"
  rssh "ls -1 $MODELS_DIR/*.gguf /home/turnstone/models/*/*.gguf 2>/dev/null | head -10" | sed 's/^/    /'
  exit 1
fi

# ------------------------------------------------------- 5b. memory guard
# The whole reason this exists: this box has SwapTotal=0.
#
#   MemTotal ~125 GiB / MemAvailable ~86 GiB / Swap 0
#   Halogen weights ~85 GB mapped  -> file-backed, EVICTABLE
#   GUFO qualified peak 88.78 GiB (context capacity 133121, idle host)
#
# Evictable page cache is not the hazard -- the kernel reclaims it under
# pressure. The hazard is GUFO's ANONYMOUS memory (KV state, activations,
# registered HIP host buffers). With no swap, anon pressure goes straight to
# the OOM killer, and Halogen's mapping is the largest, easiest victim.
#
# Thresholds derive from the one qualified data point (88.78 GiB @ 133121)
# and are deliberately rough. Estimates, not measurements.
hdr "5b. Memory guard (SwapTotal=0)"
AVAIL_KB=$(rssh "awk '/^MemAvailable:/{print \$2}' /proc/meminfo")
SWAP_KB=$(rssh "awk '/^SwapTotal:/{print \$2}' /proc/meminfo")
AVAIL_GIB=$(( ${AVAIL_KB:-0} / 1048576 ))

# Tiered requirement by context width (GiB), per session.
case "$GUFO_CTX" in
  ""|*[!0-9]*) NEED_GIB=95 ;;
  *)
    if   (( GUFO_CTX <=  8192 )); then NEED_GIB=24
    elif (( GUFO_CTX <= 32768 )); then NEED_GIB=50
    else                               NEED_GIB=95
    fi ;;
esac
NEED_GIB=$(( NEED_GIB * ${GUFO_SESS:-1} ))
# Explicit override wins over the tier table (used for the small-model smoke path).
if [[ "${GUFO_MIN_AVAIL_GIB:-}" =~ ^[0-9]+$ ]]; then
  NEED_GIB="$GUFO_MIN_AVAIL_GIB"
fi

say "  MemAvailable : ${AVAIL_GIB} GiB"
say "  SwapTotal    : ${SWAP_KB} kB  (0 = no safety valve)"
say "  GUFO context : ${GUFO_CTX} x ${GUFO_SESS} session(s) -> ~${NEED_GIB} GiB anon working set (estimated)"

if (( ${SWAP_KB:-0} == 0 )) && (( AVAIL_GIB < NEED_GIB )); then
  if [[ "${GUFO_ALLOW_COEXIST:-0}" == "1" ]]; then
    warn "memory guard BYPASSED (GUFO_ALLOW_COEXIST=1): ${AVAIL_GIB} < ${NEED_GIB} GiB."
    warn "With Swap=0 the OOM killer may take Halogen (your fallback) or GUFO mid-serve."
  else
    err "REFUSING to start: ${AVAIL_GIB} GiB available, ~${NEED_GIB} GiB needed, Swap=0."
    say ""
    say "  Halogen is holding the difference. Two safe options:"
    say ""
    say "  A) SMOKE TEST ONLY -- prove plumbing with a small model, Halogen untouched:"
    say "       GUFO_MODELS_DIR=/home/turnstone/models/Ministral-3-14B-Instruct-2512-GGUF \\"
    say "       GUFO_MODEL_FILE=Ministral-3-14B-Instruct-2512-UD-Q4_K_XL.gguf \\"
    say "       GUFO_CONTEXT=4096 GUFO_MIN_AVAIL_GIB=20 \\"
    say "       $0 --host $HOST --start"
    say "     (8.4 GB model: proves binary + ROCm + shim contract, no OOM risk)"
    say ""
    say "  B) FULL QUALIFIED MODEL -- cut Halogen over FIRST, then start GUFO:"
    say "       Run cutover_halogen.sh from a session NOT served by core-two:8731,"
    say "       then re-run this with --start. Halogen's ~85 GB becomes reclaimable."
    say ""
    say "  Do NOT just set GUFO_ALLOW_COEXIST=1 -- that is how the fallback dies."
    exit 1
  fi
else
  log "memory guard passed: ${AVAIL_GIB} GiB available >= ~${NEED_GIB} GiB needed"
fi

# ------------------------------------------------------- 6. manifest override
hdr "6. PAIR manifest override"
# install_pair_override.sh is host-side; copy and run as the PAIR user.
rscp "$REPO_DIR/manifest/llamacpp.json" "$HOST:$REMOTE_ROOT/llamacpp.json" || { err "manifest copy failed"; exit 1; }
rscp "$REPO_DIR/ops/install_pair_override.sh" "$HOST:$REMOTE_ROOT/install_pair_override.sh" || true
rssh "chmod +x $REMOTE_ROOT/install_pair_override.sh && \
      sudo -u $RUN_USER SRC_OVERRIDE=$REMOTE_ROOT/llamacpp.json \
        PAIR_ENGINES_DIR='/home/$RUN_USER/$PAIR_ENGINES_REL' \
        bash $REMOTE_ROOT/install_pair_override.sh" \
  || { err "manifest override install failed"; exit 1; }
log "override installed to /home/$RUN_USER/$PAIR_ENGINES_REL/llamacpp.json"

if [[ "$MODE" == "stage" ]]; then
  hdr "STAGED (nothing started)"
  say "Review, then run:  deploy_stack.sh --host $HOST --start"
  say "Undo:              rollback_all.sh --host $HOST"
  exit 0
fi

# ------------------------------------------------------- 7. start the stack
hdr "7. Starting GUFO pod + shim (Halogen untouched)"
say "Starting. GUFO cold start with a large GGUF can take minutes."
rssh "sudo -u $RUN_USER GUFO_PAIR_COMPOSE=$REMOTE_ROOT/compose.yaml GUFO_PAIR_ENV=$ENV_PATH \
      /usr/local/bin/gufo-pairctl start" \
  || { err "gufo-pairctl start failed"; exit 1; }

rssh "sudo -u $RUN_USER /usr/local/bin/gufo-pairctl status" | sed 's/^/    /'

# Re-verify the bind on the LIVE socket, not just the template. A SHIM_BIND=
# override in the env file would otherwise silently widen exposure. Match all
# three wildcard spellings ss uses: 0.0.0.0:P, *:P and [::]:P.
hdr "7b. Live bind check on :${SHIM_PORT:-8081}"
WILDCARD=$(rssh "ss -tln 2>/dev/null | awk '{print \$4}' | grep -cE '(0\\.0\\.0\\.0|\\*|\\[::\\]):${SHIM_PORT:-8081}\$' || true")
LOOPBACK=$(rssh "ss -tln 2>/dev/null | awk '{print \$4}' | grep -cE '^127\\.0\\.0\\.1:${SHIM_PORT:-8081}\$' || true")

if [[ "${WILDCARD:-0}" -gt 0 ]]; then
  err "shim is listening on a WILDCARD address at :${SHIM_PORT:-8081} — UNAUTHENTICATED LAN EXPOSURE"
  err "SHIM_BIND in $ENV_PATH overrode the loopback default. Stopping the stack."
  rssh "sudo -u $RUN_USER GUFO_PAIR_COMPOSE=$REMOTE_ROOT/compose.yaml GUFO_PAIR_ENV=$ENV_PATH \
        /usr/local/bin/gufo-pairctl stop" || true
  exit 1
fi
if [[ "${LOOPBACK:-0}" -gt 0 ]]; then
  log "live check: shim bound to 127.0.0.1:${SHIM_PORT:-8081} only"
else
  warn "no listener found on :${SHIM_PORT:-8081} — shim may still be starting; verify with smoke_pair_facade.sh --direct"
fi

hdr "STACK STARTED — Halogen still serving on :8731"
say "Next: smoke_pair_facade.sh --host $HOST --direct"
say "Halogen cutover is a SEPARATE, deliberate step: cutover_halogen.sh"
exit 0
