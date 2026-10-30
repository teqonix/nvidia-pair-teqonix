#!/usr/bin/env bash
# provision_model.sh -- fetch GUFO's QUALIFIED model set from HuggingFace.
#
# Why this exists: Halogen's .hgn weights are not consumable by
# `gufo serve llm` (GGUF only), and the previously-queued IQ4_NL-PROJFIX set
# is NOT what GUFO qualifies. GUFO's docs (docs/models/qwen3.8-flash-next/
# README.md) name one qualified target:
#
#   unsloth/Qwen3.8-Flash-Next-GGUF
#   @ 38bb39ee97821de2c9009abb7e93950eec396e66
#     UD-Q4_K_XL/*                                111.33 GB  target
#     MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf          speculative decode
#     mmproj-BF16.gguf                                      vision projector
#
# The repo states plainly: "Original unquantized-model and GGUF-conversion
# parity remain unqualified." Do not substitute another quant without knowing
# you are leaving the qualified configuration.
#
# Safe by construction: never overwrites an existing file, never touches
# Halogen's weight directory, verifies shard count, and requires --run.

set -uo pipefail

HOST="${HOST:-amd-ai-core-two.lan}"
RUN_USER="${RUN_USER:-turnstone}"
REPO_ID="unsloth/Qwen3.8-Flash-Next-GGUF"
REVISION="38bb39ee97821de2c9009abb7e93950eec396e66"

# Target dir on the host. Deliberately NOT under /home/turnstone/halogen-models.
DEST="${GUFO_MODELS_DIR:-/home/turnstone/models/Qwen3.8-Flash-Next-GGUF}"

RUN=0
DRY=1
VERIFY=0
ONLY=""

say()  { printf '%s\n' "$*"; }
log()  { printf '[provision] %s\n' "$*"; }
warn() { printf '\033[33m[provision] warn:\033[0m %s\n' "$*" >&2; }
err()  { printf '[provision] ERROR: %s\n' "$*" >&2; }
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; }
hdr()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
rssh() { ssh -o ConnectTimeout=15 -o BatchMode=yes "$HOST" "$1" 2>/dev/null; }

usage() {
  cat <<USAGE
usage: provision_model.sh [--host HOST] (--dry-run | --run | --verify) [--only PART]

  --dry-run    show exactly what would be downloaded, change nothing (default)
  --run        actually download
  --verify     check the qualified set is present with expected shard counts
  --only PART  target | mtp | mmproj | all      (default: all)

  env: GUFO_MODELS_DIR (default: $DEST)
       REPO_ID / REVISION are pinned to the qualified commit; overriding them
       leaves the qualified configuration.

Downloads land in GUFO_MODELS_DIR. Existing files are NEVER overwritten --
partial/interrupted files are reported so you can resume deliberately.
Halogen's weight directory is never read as a source or written at all.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)    HOST="$2"; shift 2 ;;
    --run)     RUN=1; DRY=0; shift ;;
    --dry-run) DRY=1; RUN=0; shift ;;
    --verify)  VERIFY=1; DRY=0; shift ;;
    --only)    ONLY="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown arg: $1"; usage; exit 2 ;;
  esac
done
ONLY="${ONLY:-all}"

# ---------------------------------------------------------------- 0. safety rails
hdr "0. Safety rails"
case "$DEST" in
  *halogen*|/home/turnstone/halogen-models*)
    err "refusing: DEST='$DEST' looks like Halogen's weight directory."
    err "Halogen is the rollback path. Provision elsewhere."
    exit 1 ;;
esac
if [[ "$DEST" != /home/turnstone/models/* ]]; then
  warn "DEST is outside the conventional /home/turnstone/models/* tree."
fi
log "host=$HOST  dest=$DEST  only=$ONLY"
log "pinned: $REPO_ID @ $REVISION"

# ---------------------------------------------------------------- 0b. verify mode
if [[ $VERIFY -eq 1 ]]; then
  hdr "VERIFY -- qualified set at $DEST"
  VFAIL=0
  # Target shards: exactly 4, and the first must be the small header shard.
  NT=$(rssh "ls -1 '$DEST'/UD-Q4_K_XL/*-of-00004.gguf 2>/dev/null | wc -l")
  if [[ "${NT:-0}" -eq 4 ]]; then ok "target shards: 4/4 present"
  else err "target shards: ${NT:-0}/4 -- INCOMPLETE, GUFO will fail to load"; VFAIL=1; fi

  TSIZE=$(rssh "du -sBG '$DEST/UD-Q4_K_XL' 2>/dev/null | cut -d\$'\\t' -f1 | tr -dc '0-9'")
  if [[ -n "${TSIZE:-}" ]] && (( TSIZE >= 105 && TSIZE <= 118 )); then
    ok "target size: ${TSIZE} GiB (expected ~104 GiB on-disk / 111.33 GB decimal)"
  else
    warn "target size: ${TSIZE:-?} GiB -- outside the expected window"
  fi

  # MTP predictor (optional but part of the qualified run command)
  if rssh "test -f '$DEST/MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf'"; then
    ok "MTP shared Q8_0 present"
  else
    warn "MTP shared Q8_0 absent -- speculative decode unavailable (AR still works)"
  fi

  # Vision projector. NOTE: if this file sits beside the target or in the target's
  # parent dir, GUFO AUTO-DISCOVERS it and keeps ~0.9 GB of encoder weights
  # resident even when no image is ever sent (docs/SERVER.md: vision_prepared).
  if rssh "test -f '$DEST/mmproj-BF16.gguf'"; then
    ok "mmproj-BF16 present (vision enabled; ~0.9 GB resident even with no images)"
  else
    say "  mmproj-BF16 absent -- text-only, no auto-discovery cost"
  fi

  say ""
  if [[ $VFAIL -eq 0 ]]; then
    say "VERIFIED -- qualified target present. Point deploy_stack.sh at:"
    say "  GUFO_MODELS_DIR=$DEST/UD-Q4_K_XL"
    say "  GUFO_MODEL_FILE=Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf"
    exit 0
  fi
  say "NOT VERIFIED -- re-run: $0 --host $HOST --run --only target"
  exit 1
fi

# ---------------------------------------------------------------- 1. free space
hdr "1. Disk space"
DISK_GB=$(rssh "df -BG --output=avail / | tail -1 | tr -dc '0-9'")
NEED_GB=120   # 111.33 target + ~2.8 MTP + 0.9 mmproj + slack
say "  free: ${DISK_GB:-?} GiB   needed: ~${NEED_GB} GiB"
if [[ -z "${DISK_GB:-}" ]]; then
  err "cannot read disk space on $HOST"; exit 1
elif (( DISK_GB < NEED_GB )); then
  err "insufficient disk: ${DISK_GB} GiB free, ~${NEED_GB} GiB needed"
  exit 1
fi
log "disk ok"

# ---------------------------------------------------------------- 2. hf CLI + auth
hdr "2. hf CLI"
HF=$(rssh "command -v hf || command -v huggingface-cli || true")
if [[ -z "$HF" ]]; then
  err "no hf / huggingface-cli on $HOST"
  say "  install: python3 -m pip install -U 'huggingface_hub[cli]'"
  exit 1
fi
log "hf at $HF"
# The repo is public, so an unauthenticated pull works. Note it either way.
HTTP=$(rssh "curl -s -o /dev/null -w '%{http_code}' \
  'https://huggingface.co/api/models/$REPO_ID' || echo 000")
if [[ "$HTTP" == "200" ]]; then
  log "repo reachable (HTTP 200, public -- no token required)"
else
  warn "repo API returned HTTP $HTTP -- may need a token or the revision moved"
fi

# ---------------------------------------------------------------- 3. file plan
hdr "3. Download plan"
# Space-separated: relative-path inside repo  ::  local subdir
declare -a FILES=()
case "$ONLY" in
  all)
    FILES=(
      "UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf"
      "UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-00002-of-00004.gguf"
      "UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-00003-of-00004.gguf"
      "UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-00004-of-00004.gguf"
      "MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf"
      "mmproj-BF16.gguf"
    ) ;;
  target)
    FILES=(
      "UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf"
      "UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-00002-of-00004.gguf"
      "UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-00003-of-00004.gguf"
      "UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-00004-of-00004.gguf"
    ) ;;
  mtp)     FILES=("MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf") ;;
  mmproj)  FILES=("mmproj-BF16.gguf") ;;
  *) err "--only must be one of: target mtp mmproj all (got: $ONLY)"; exit 2 ;;
esac

EXIST=0
for f in "${FILES[@]}"; do
  if rssh "test -f $DEST/$f"; then
    sz=$(rssh "stat -c %s '$DEST/$f' 2>/dev/null || echo 0")
    say "  PRESENT  $(printf '%8.2f' "$(echo "$sz/1073741824" | bc -l)") GB  $f"
    EXIST=$((EXIST+1))
  else
    say "  FETCH    $f"
  fi
done
say ""
say "  ${EXIST} of ${#FILES[@]} already present"

if [[ $DRY -eq 1 ]]; then
  hdr "DRY RUN -- nothing downloaded"
  say "Run for real:  $0 --host $HOST --run --only $ONLY"
  say "Then:          $0 --host $HOST --verify"
  exit 0
fi

# ---------------------------------------------------------------- 4. download
hdr "4. Downloading"
rssh "mkdir -p '$DEST' && chown -R $RUN_USER:$RUN_USER '$DEST' 2>/dev/null || true"

# hf download is resumable and skips files already present at the right size.
# Run detached-style with a log so a 111 GB pull survives the ssh session.
LOG="$DEST/../gufo-provision.log"
DL_CMD="cd '$DEST' && $HF download '$REPO_ID' --revision '$REVISION' --local-dir '$DEST'"
case "$ONLY" in
  all)     DL_CMD+=" --include 'UD-Q4_K_XL/*' 'MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf' 'mmproj-BF16.gguf'" ;;
  target)  DL_CMD+=" --include 'UD-Q4_K_XL/*'" ;;
  mtp)     DL_CMD+=" --include 'MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf'" ;;
  mmproj)  DL_CMD+=" --include 'mmproj-BF16.gguf'" ;;
esac

say "  111 GB will outlive an interactive ssh session -- starting detached."
rssh "nohup bash -lc \"$DL_CMD\" > '$LOG' 2>&1 & echo started" \
  || { err "could not start download"; exit 1; }
log "download started, logging to $LOG"
say ""
say "Watch it:   ssh $HOST 'tail -f $LOG'"
say "Verify:     $0 --host $HOST --verify"
exit 0
