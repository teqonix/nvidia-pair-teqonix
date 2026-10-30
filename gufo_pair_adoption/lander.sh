#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
#
# lander.sh — copy the staged GUFO/PAIR masquerade tree from this staging node
# onto /workspace/nvidia-pair-teqonix/gufo_pair_adoption and capture it as one
# atomic jj change on the gufo-pair-masquerade-impl bookmark.
#
# Staging lives on persistent ext4 (/var/lib/turnstone) because on this node
# /workspace is read-only NFS and /tmp is tmpfs. Run this from a node where
# /workspace is writable.
#
#   ./lander.sh              copy + jj new
#   ./lander.sh --dry-run    show what would be copied, change nothing
#
# Requires: jj (jujutsu). Does NOT require jq.

set -euo pipefail

SRC="${SRC:-/var/lib/turnstone/gufo_pair_adoption}"
# Operator-confirmed 2026-10-30: the gufo-pair-masquerade-impl bookmark lives in
# the nvidia-pair-teqonix repo (origin: teqonix/nvidia-pair-teqonix.git).
# NOT misc_tasks - that is a separate jj repo with unrelated history.
DEST="${DEST:-/workspace/nvidia-pair-teqonix/gufo_pair_adoption}"
BRANCH="${BRANCH:-gufo-pair-masquerade-impl}"
CHANGE_MSG="${CHANGE_MSG:-Add GUFO->llama.cpp masquerade shim for PAIR engine adoption

Shim presents GUFO as a role=router llama.cpp-compatible listener on
127.0.0.1:8081 so PAIR adopts it in command runtime mode without
downloading or spawning real llama-server. Manifest neutralises the
bundled installer (artifacts+run emptied) and clears editable_launch
(explicit null). Includes host control surface, container topology,
contract test suite (33/33) and Halogen rollback.}"

DRY=0
[[ "${1:-}" == "--dry-run" || "${1:-}" == "-n" ]] && DRY=1

log() { printf 'lander: %s\n' "$*" >&2; }
die() { printf 'lander: ERROR: %s\n' "$*" >&2; exit 1; }

run() {
  if [[ "$DRY" == "1" ]]; then printf '  [dry-run] %s\n' "$*" >&2
  else "$@"; fi
}

# --- sanity ---------------------------------------------------------------
[[ -d "$SRC" ]] || die "staging dir $SRC does not exist"
[[ -f "$SRC/manifest/llamacpp.json" ]] || die "$SRC/manifest/llamacpp.json missing — refusing to land a partial tree"
if [[ "$DRY" != "1" ]]; then
  command -v jj >/dev/null 2>&1 || die "jj (jujutsu) not found on PATH"
fi
[[ -d "$DEST" || -w "$(dirname "$DEST")" ]] || die "cannot write to $(dirname "$DEST") — is /workspace writable on this node?"

# refusable: never copy the staging venv or caches into the repo
# refusable: never copy the staging venv, caches, or the Go verification harness
# (manifest_check/ is a writable COPY of upstream PAIR's engine-manager package -
# scaffolding used to validate our override, not a deliverable for this repo).
EXCLUDES=(.venv __pycache__ '*.pyc' manifest_check .gocache .gomodcache .git go.sum)

log "source:      $SRC"
log "destination: $DEST"
log "branch:      $BRANCH"
[[ "$DRY" == "1" ]] && log "*** DRY RUN — nothing will be written ***"

# --- copy -----------------------------------------------------------------
run mkdir -p "$DEST"
# rsync preferred; fall back to cp if absent.
if command -v rsync >/dev/null 2>&1; then
  rs_args=(-a --delete)
  for e in "${EXCLUDES[@]}"; do rs_args+=(--exclude "$e"); done
  run rsync "${rs_args[@]}" "$SRC/" "$DEST/"
else
  log "rsync not found — using cp (no --delete semantics)"
  # copy explicitly-named artefacts so we never drag the venv along
  for item in PLAN.md shim manifest container ops; do
    [[ -e "$SRC/$item" ]] || { log "  skip missing $item"; continue; }
    run cp -a "$SRC/$item" "$DEST/"
  done
fi

# --- hygiene: no exec bit on non-script files -----------------------------
# (global lesson: chmod -x on a glob over a dir breaks directories. Per-file only.)
if [[ "$DRY" != "1" ]]; then
  find "$DEST" -type f \( -name '*.py' -o -name '*.json' -o -name '*.md' \
       -o -name '*.yaml' -o -name '*.yml' -o -name '*.txt' \
       -o -name 'env.sample' \) -perm -u+x -print -exec chmod -x {} + 2>/dev/null || true
  # scripts and control surfaces keep +x
  for f in "$DEST"/ops/gufo-pairctl "$DEST"/ops/install_pair_override.sh \
           "$DEST"/ops/rollback_halogen.sh "$DEST"/lander.sh; do
    [[ -f "$f" ]] && chmod +x "$f"
  done
fi

# --- capture --------------------------------------------------------------
if [[ "$DRY" == "1" ]]; then
  log "dry run complete"
  exit 0
fi

log "capturing jj change on $BRANCH"
cd "$(dirname "$DEST")"
# Keep the feature branch stacked on the latest bookmark head without clobbering
# the parent's description (jj 0.44 non-TTY gotcha: never `jj describe` the parent).
if jj bookmark list "$BRANCH" 2>/dev/null | grep -q .; then
  run jj new "$BRANCH" -m "$CHANGE_MSG"
else
  run jj new -m "$CHANGE_MSG"
fi
run jj file status
log "change created. Review with:  jj diff --summary"
log "Then:  jj bookmark set $BRANCH -r @  &&  jj git push --bookmark $BRANCH"
