#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
#
# install_pair_override.sh — install the GUFO masquerade manifest override for
# PAIR's llamacpp engine, with a mandatory timestamped backup so
# rollback_halogen.sh can restore the exact previous state.
#
# This script ONLY touches the manifest file. It does not start pods, does not
# install binaries, and does not touch model weights.
#
# The override being installed has been verified against the REAL
# nvpair-engine-manager loader (see ../manifest_check/):
#   - accepted by Validate()
#   - install neutralised: effective install == {"mode":"user"}  (no llama.cpp download)
#   - editable_launch nulled so PAIR does not append/validate llama.cpp flags
#     against `gufo-pairctl start`
#
# Prereqs (checked below, not installed by this script):
#   - /usr/local/bin/gufo-pairctl exists (manifest detect path)
#   - the gufo-pair pod answers role=router on 127.0.0.1:8081 (else adopt fails)

set -euo pipefail

PAIR_ENGINES_DIR="${PAIR_ENGINES_DIR:-$HOME/.config/Nvidia Corporation/Personal AI Router/engines}"
SRC_OVERRIDE="${SRC_OVERRIDE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/manifest/llamacpp.json}"
TARGET="$PAIR_ENGINES_DIR/llamacpp.json"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$TARGET.pre-gufo.$STAMP"

log() { printf 'install-override: %s\n' "$*" >&2; }
die() { printf 'install-override: ERROR: %s\n' "$*" >&2; exit 1; }

[[ -f "$SRC_OVERRIDE" ]] || die "override source not found: $SRC_OVERRIDE"
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$SRC_OVERRIDE" \
  || die "override source is not valid JSON: $SRC_OVERRIDE"

if [[ ! -d "$PAIR_ENGINES_DIR" ]]; then
  log "creating $PAIR_ENGINES_DIR"
  mkdir -p "$PAIR_ENGINES_DIR" || die "cannot create PAIR engines dir; is PAIR installed?"
fi

# --- backup ---------------------------------------------------------------
if [[ -f "$TARGET" ]]; then
  cp -p "$TARGET" "$BACKUP"
  log "backed up existing override -> $BACKUP"
else
  log "no existing llamacpp.json override (bundled manifest was in force)"
  # Record that fact so rollback removes rather than restores.
  printf 'no_prior_override\n' > "$TARGET.absent.$STAMP"
  log "recorded absence marker -> $TARGET.absent.$STAMP"
fi

# --- install --------------------------------------------------------------
install -m 0644 "$SRC_OVERRIDE" "$TARGET"
log "installed override -> $TARGET"

# --- sanity: what PAIR will see --------------------------------------------
python3 - "$TARGET" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
p = m["platforms"]["linux/amd64"]
rt = p["runtime"]
print("  display_name :", m.get("display_name"))
print("  detect       :", p.get("detect"))
print("  engine mode  :", rt.get("mode"), "| port", rt.get("port"), "| bind", rt.get("bind"))
print("  editable_launch:", rt.get("editable_launch", "<ABSENT — WOULD BE BLOCKING>"))
print("  install      :", p.get("install"))
assert rt.get("editable_launch") is None, "editable_launch must be explicit null"
assert p.get("install", {}).get("artifacts") == [], "install.artifacts must be emptied"
print("  sanity: OK (no llama.cpp download path, no editable-launch conflict)")
PY

cat >&2 <<'NEXT'

Next steps (NOT performed by this script):
  1. Ensure the stack is up:   sudo gufo-pairctl start     (as the podman user)
  2. Restart PAIR so it re-reads manifests:
       systemctl --user restart nvpair    # or restart the PAIR app
  3. In PAIR, the llamacpp engine should appear as GUFO (llama.cpp-compatible)
     and be ADOPTED without any llama.cpp download. Verify:
       ls ~/.local/share/*pair*/engine-bin 2>/dev/null   # must NOT contain llama-server
  4. Rollback any time with:   ops/rollback_halogen.sh
NEXT
