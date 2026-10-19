#!/usr/bin/env bash
# 50_provision_gguf.sh — download GGUF equivalents into ~/.llamacpp on mbp-ai-core.
#
# RUN AS turnstone ON THE NODE (run it there, not through a pipe, so it survives
# an SSH drop):
#   ssh turnstone@mbp-ai-core.lan
#   export HF_TOKEN=hf_xxx
#   bash 50_provision_gguf.sh
#
# WHY NOT pull_model:
#   `pull_model` is a JSON-RPC *action* on the broker/engine-manager IPC
#   (manager.go:406, pull.go:38) that maps to POST /models on an ALREADY RUNNING
#   llama-server. It is driven from the TUI, and it is for pulling into a live
#   engine — a chicken-and-egg for cold cache warm-up, and unreachable from a
#   plain shell. Direct HTTPS from HF is the honest tool here.
#
# Set HF_TOKEN: google/* and some unsloth repos are gated and 401 without a
# read token. (Verified 2026-10-17: the repos listed below report gated=None and
# work anonymously, but a token avoids rate limits.)
set -uo pipefail

log()  { printf '\033[1;32m[provision]\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$1"; }
err()  { printf '\033[1;31m[error]\033[0m %s\n' "$1"; }

CACHE="/Users/turnstone/.llamacpp"
TOKEN="${HF_TOKEN:-}"
[ -d "$CACHE" ] || { err "cache dir missing: $CACHE (run 30_install_turnstone.sh)"; exit 1; }
[ "$USER" = "turnstone" ] || warn "running as $USER, expected turnstone"

# ---------------------------------------------------------------------------
# MIGRATION MAP: LM Studio alias -> GGUF equivalent.
#
# The left column is what clients call today. The right column is a NEW download;
# nothing is converted. MLX .safetensors cannot be read by llama.cpp.
#
# Quants chosen to match what LM Studio was serving:
#   gemma-4-31b            was Q4_0  -> Q4_K_M (better quality/size; see note)
#   gemma-4-26b-a4b        was Q4_0  -> UD-Q4_K_XL
#   qwen-3.8-27b           was Q4_K_M-> Q4_K_M (exact)
#   nemotron lightning     was Q4_K_S-> Q4_K_M
#   nemotron-3-nano-4b     was Q4_K_M-> ALREADY GGUF on disk, copy not download
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# HUGGING FACE HUB CACHE LAYOUT — REQUIRED, NOT OPTIONAL
#
# llama-server discovers models by scanning $LLAMA_CACHE for the HF hub layout:
#
#   $LLAMA_CACHE/models--<org>--<repo>/
#       refs/main                      -> contains the commit hash (no newline)
#       blobs/<sha256-of-file>         -> the actual bytes
#       snapshots/<commit>/<filename>  -> symlink -> ../../blobs/<sha256>
#
# A flat directory of .gguf files is INVISIBLE to the router: `--cache-list`
# reports "number of models in cache: 0" and GET /models returns an empty list,
# even though the files are on disk. (Cost us a long detour.) Model ids are
# derived from <org>/<repo> plus the quant parsed out of <filename>, so
# "Qwen3.8-27B-UD-Q4_K_M.gguf" in unsloth/Qwen3.8-27B-GGUF becomes
# "unsloth/Qwen3.8-27B-GGUF:Q4_K_M".
#
# Downloads land in a staging dir and are hardlinked into the hub tree, so the
# staging copy is free (same volume) and can be deleted without losing anything.
# ---------------------------------------------------------------------------
STAGE="$CACHE/.staging"

hub_install() {  # repo  filename  staged_path
  local repo="$1" file="$2" staged="$3"
  local d="$CACHE/models--${repo//\//--}"
  local fhash commit
  fhash=$(shasum -a 256 "$staged" | cut -d' ' -f1)
  # Commit hash is synthesised from the blob hash: stable across re-runs so the
  # snapshot dir is idempotent. We never talk to the hub API for a real SHA.
  commit=$(printf 'local-%s' "$fhash" | shasum -a 256 | cut -c1-40)

  if [ -e "$d/snapshots/$commit/$file" ]; then
    log "  hub entry already present: $repo ($file)"
    return 0
  fi

  rm -rf "$d"
  mkdir -p "$d/refs" "$d/blobs" "$d/snapshots/$commit"
  # Hardlink the bytes in, then symlink the snapshot entry per hub convention.
  ln "$staged" "$d/blobs/$fhash" 2>/dev/null || cp "$staged" "$d/blobs/$fhash"
  ln -s "../../blobs/$fhash" "$d/snapshots/$commit/$file"
  printf '%s' "$commit" > "$d/refs/main"
  log "  hub entry built: $d -> ${repo}"
}

download() {  # repo  filename  local_alias
  local repo="$1" file="$2" alias="${3:-}"
  local dest="$STAGE${alias:+/$alias}"
  mkdir -p "$dest"
  local out="$dest/$(basename "$file")"

  # Already installed into the hub? Nothing to do.
  if [ -d "$CACHE/models--${repo//\//--}/snapshots" ] \
     && find "$CACHE/models--${repo//\//--}/snapshots" -name "$(basename "$file")" -print -quit 2>/dev/null | grep -q .; then
    log "  already provisioned: $repo/$file"
    return 0
  fi

  local url="https://huggingface.co/${repo}/resolve/main/${file}"
  log "  GET ${repo}/${file}"
  local auth=()
  [ -n "$TOKEN" ] && auth=(-H "Authorization: Bearer ${TOKEN}")
  # -C - : resume. --fail: HTTP errors are failures, not saved HTML.
  # ${auth[@]+...} guards bash 3.2 (macOS /bin/bash), where expanding an EMPTY
  # array with "${arr[@]}" under `set -u` dies with "unbound variable".
  if curl -L --fail --retry 5 --retry-delay 5 -C - ${auth[@]+"${auth[@]}"} -o "$out" "$url"; then
    log "  OK  $(du -h "$out" | cut -f1)  -> $out"
    hub_install "$repo" "$(basename "$file")" "$out"
  else
    err "  FAILED: $url  (check repo name / token / disk)"
    rm -f "$out"
    return 1
  fi
}

# ---------------------------------------------------------------------------
log "Models to provision (this is a RE-DOWNLOAD, not a conversion):"
cat <<'MAP'

  LM Studio alias                     GGUF source repo                                  file
  -------------------------------     ----------------------------------------------      ---------------------------
  gemma-4-31b                 ->     unsloth/gemma-4-31B-it-GGUF                       Q4_K_M
  gemma-4-26b-a4b             ->     unsloth/gemma-4-26B-A4B-it-GGUF                   UD-Q4_K_XL
  google/gemma-4-26b-a4b-qat  ->     google/gemma-4-26B-A4B-it-qat-q4_0-gguf            (gated: token required)
  qwen-3.8-27b                ->     unsloth/Qwen3.8-27B-GGUF                          Q4_K_M
  nvidia-nemotron-3.5-light   ->     lmstudio-community/...Lightning-30B-A3B-GGUF      Q4_K_M
  nemotron-3-nano-4b          ->     (already GGUF on this node - copied, not downloaded)
  gpt-oss-120b                ->     ggml-org/gpt-oss-120b-GGUF                        Q4_K_M
  ministral-3-3b / e2b / etc  ->     NOT AUTO-MAPPED - confirm you actually use these

  Rough total: ~180-260 GB depending on quants. Check: df -h /System/Volumes/Data

MAP

if [ "${1:-}" != "--yes" ]; then
  echo "Re-run with --yes to actually download:  bash 50_provision_gguf.sh --yes"
  exit 0
fi

FAIL=0

log "1/7 gemma-4-31b"
download "unsloth/gemma-4-31B-it-GGUF" "gemma-4-31B-it-Q4_K_M.gguf" "gemma-4-31b" || FAIL=1

log "2/7 gemma-4-26b-a4b"
download "unsloth/gemma-4-26B-A4B-it-GGUF" "gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf" "gemma-4-26b-a4b" || FAIL=1

log "3/7 qwen-3.8-27b"
download "unsloth/Qwen3.8-27B-GGUF" "Qwen3.8-27B-UD-Q4_K_M.gguf" "qwen-3.8-27b" || FAIL=1

log "4/7 nemotron-3.5-lightning-30b-a3b"
download "lmstudio-community/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF" \
         "NVIDIA-Nemotron-3.5-Lightning-30B-A3B-Q4_K_M.gguf" "nemotron-lightning" || FAIL=1

log "5/7 gpt-oss-120b  (native MXFP4 - the repo has no Q4_K_M; 63.4 GB)"
download "ggml-org/gpt-oss-120b-GGUF" "gpt-oss-120b-MXFP4.gguf" "gpt-oss-120b" || FAIL=1

log "6/7 reuse the one real GGUF already on disk (nemotron-3-nano-4b)"
SRC_GGUF="/Users/teqonix/.lmstudio/models/lmstudio-community/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf"
NANO_STAGED="$STAGE/nemotron-3-nano-4b/NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf"
if [ -r "$SRC_GGUF" ]; then
  mkdir -p "$STAGE/nemotron-3-nano-4b"
  if [ ! -f "$NANO_STAGED" ]; then
    log "  copying (may take a while)..."
    cp "$SRC_GGUF" "$NANO_STAGED" && log "  OK" || { warn "copy failed"; FAIL=1; }
  fi
  [ -f "$NANO_STAGED" ] && hub_install "lmstudio-community/NVIDIA-Nemotron-3-Nano-4B-GGUF" \
    "NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf" "$NANO_STAGED" || FAIL=1
else
  log "  source not readable; downloading instead"
  download "lmstudio-community/NVIDIA-Nemotron-3-Nano-4B-GGUF" \
           "NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf" "nemotron-3-nano-4b" || FAIL=1
fi

log "7/7 gemma-4-26b-a4b-qat  (SKIPPED by default - needs a gated token)"
if [ -n "$TOKEN" ]; then
  log "  token present; uncomment the next line in this script to pull the QAT variant"
  # download "google/gemma-4-26B-A4B-it-qat-q4_0-gguf" "<verify filename first>" "gemma-4-26b-a4b-qat"
else
  log "  no HF_TOKEN; skipping gated repo"
fi

echo
log "hub cache (what llama-server sees):"
LLAMA_CACHE="$CACHE" ls -d "$CACHE"/models--*/ 2>/dev/null | sed 's/^/  /' || warn "no models-- dirs found"
log "cache contents:"
find "$CACHE"/models--*/snapshots -type l 2>/dev/null | sed 's/^/  /'
echo
log "total (staging shares blocks with hub, so count once): $(du -sh "$CACHE" 2>/dev/null | cut -f1)"
echo
[ "$FAIL" = 0 ] && log "provisioning complete" || warn "one or more downloads failed - see above"

cat <<'NOTE'

IMPORTANT — model naming after provisioning:
  llama-server exposes models by the id embedded in the GGUF metadata, NOT by
  the directory name. If clients must keep calling "gemma-4-31b", either
    (a) use the id llama-server reports (check GET :8080/.../props after load), or
    (b) set PAIR aliases in the TUI.
  Do not assume the friendly LM Studio alias carries over automatically.
NOTE
