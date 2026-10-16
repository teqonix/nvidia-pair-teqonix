#!/usr/bin/env bash
# 30_install_turnstone.sh
#
# Install PAIR 1.0.0 as the `turnstone` user on mbp-ai-core, in a clean
# turnstone-owned root. Does NOT touch /Applications/PAIR.app.
#
#   scp -r dist/pair-1.0.0-darwin-arm64 turnstone@mbp-ai-core.lan:/Users/turnstone/
#   ssh turnstone@mbp-ai-core.lan 'bash -s' < 30_install_turnstone.sh
#
# Three non-obvious things this script does:
#   1. FIFO stdin holder — the broker treats stdin EOF as fatal and launchd's
#      /dev/null stdin is an instant EOF. See run_pair_daemon.sh.
#   2. Pre-seeds llamacpp-proxy-port.json = 1234, because llamacpp's stock
#      facade port is 8080 and persisted-port wins over default (portstore.go).
#   3. Runs ONLY the llamacpp engine facade, so :1234 is genuinely free for it.
set -euo pipefail

log()  { printf '\033[1;32m[install]\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$1"; exit 1; }

[ "$(whoami)" = "turnstone" ] || die "run as turnstone, not $(whoami)"

SRC="${SRC:-/Users/turnstone/pair-1.0.0-darwin-arm64}"
ROOT="/Users/turnstone/pair"
VER="1.0.0"
DEST="$ROOT/$VER"
CFG="/Users/turnstone/Library/Application Support/Nvidia Corporation/Personal AI Router"
LLAMACPP="/Users/turnstone/.llamacpp"
FACADE_PORT=1234

[ -d "$SRC/cli-bin" ] || die "no $SRC/cli-bin - copy the artifact first (see README step 3)"

# ---------------------------------------------------------------------------
log "0. verify artifact against SHA256SUMS"
( cd "$SRC/cli-bin" && shasum -a 256 -c "$SRC/SHA256SUMS" >/dev/null 2>&1 ) \
  && log "   checksums OK" \
  || warn "checksum verification failed or SHA256SUMS missing - continuing, verify manually"

# ---------------------------------------------------------------------------
log "1. install binaries -> $DEST/cli-bin (directory-inode-preserving swap)"
mkdir -p "$ROOT"
if [ -d "$DEST" ]; then
  BK="$ROOT/.backup-${VER}-$(date -u +%Y%m%dT%H%M%SZ)"
  log "   existing install found; preserving dir inode, no full backup needed (fresh 1.0.0)"
  rm -f "$DEST"/cli-bin/* 2>/dev/null || true
  mkdir -p "$DEST/cli-bin"
else
  mkdir -p "$DEST/cli-bin"
fi
cp -a "$SRC/cli-bin/." "$DEST/cli-bin/"
chmod 0755 "$DEST"/cli-bin/*
log "   $(ls -1 "$DEST/cli-bin" | wc -l | tr -d ' ') binaries installed"

# 1.0.0 marker: 13 components, single nvpair-proxy, no legacy proxies
ls "$DEST/cli-bin" | grep -q '^nvpair-proxy$' || die "nvpair-proxy missing - is this a 1.0.0 artifact?"
if ls "$DEST/cli-bin" | grep -qE '^(ollama|lmstudio)-proxy$'; then
  die "legacy ollama-proxy/lmstudio-proxy present - this is NOT the 1.0.0 artifact"
fi
cp -a "$SRC/manifest.json" "$DEST/manifest.json" 2>/dev/null || true

# ---------------------------------------------------------------------------
log "2. Apple Silicon codesigning"
# arm64 macOS refuses to exec unsigned mach-o. The Go linker ad-hoc signs
# darwin/arm64 output, but signatures do not survive every copy path.
UNSIGNED=0
for b in "$DEST"/cli-bin/*; do
  if ! codesign --verify --verbose=0 "$b" >/dev/null 2>&1; then
    log "   ad-hoc signing $(basename "$b")"
    codesign --force --sign - "$b" >/dev/null 2>&1 || { warn "could not sign $(basename "$b")"; UNSIGNED=1; }
  fi
done
[ "$UNSIGNED" = 0 ] && log "   all binaries carry a valid signature"

# strip quarantine only if present (scp normally does not set it; browsers do)
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true

# ---------------------------------------------------------------------------
log "3. config + cache directories"
mkdir -p "$CFG" "$LLAMACPP"
log "   config root : $CFG"
log "   llama cache : $LLAMACPP"

# Do NOT copy teqonix's config in: it is unreadable by design per-user, and a
# half-migrated tree is worse than a clean one. Fresh config is intentional.

# ---------------------------------------------------------------------------
log "4. pre-seed llama.cpp facade port = $FACADE_PORT"
# llamacpp's stock facade port is 8080 (services/shared/engines/engines.go).
# chooseStartupPort() lets the persisted port override the default, so seeding it
# is what puts llama.cpp on :1234 for existing clients.
printf '{"port":%d}\n' "$FACADE_PORT" > "$CFG/llamacpp-proxy-port.json"
log "   wrote $CFG/llamacpp-proxy-port.json -> {\"port\":$FACADE_PORT}"

# Make sure nothing else has claimed a persisted 1234 for the other facades.
for f in proxy-port.json lmstudio-proxy-port.json; do
  if [ -f "$CFG/$f" ]; then
    cur=$(jq -r '.port // empty' "$CFG/$f" 2>/dev/null || echo "")
    if [ "$cur" = "$FACADE_PORT" ]; then
      warn "$CFG/$f already persists port $FACADE_PORT - removing to avoid a bind clash"
      rm -f "$CFG/$f"
    fi
  fi
done

# ---------------------------------------------------------------------------
log "5. write the daemon supervisor wrapper (FIFO stdin holder)"
cat > "$ROOT/run_pair_daemon.sh" <<'WRAPPER'
#!/bin/bash
# run_pair_daemon.sh - launchd entrypoint for PAIR 1.0.0 as turnstone.
#
# WHY THIS EXISTS
# ---------------
# nvpair-ui-broker's readLoop() treats io.EOF on its stdin as terminal:
#   "EOF is terminal (stream closed)"  - broker.go
# With no --ipc flag it uses the stdio transport, reading os.Stdin. launchd
# hands a daemon stdin from /dev/null, which is an IMMEDIATE EOF. So a
# LaunchDaemon that execs the broker directly starts, reads EOF, tears down
# every worker, and exits 0 - "healthy" exit code, serving nothing.
#
# FIX: open a FIFO read-write (mode 0<>). A FIFO opened O_RDWR never sees EOF
# because the writer end is held by the same fd. The fd is inherited across
# exec, so the broker blocks in read forever and only stops on a signal.
# Verified: /dev/null -> read rc=1 instantly; FIFO 0<> -> blocks indefinitely.
#
# The TUI avoids this problem by being the supervisor itself (it owns the
# broker's lifecycle over stdin and never lets it see EOF). That pattern needs a
# terminal, so it is wrong for a boot daemon.
set -u

PAIR_HOME="/Users/turnstone/pair"
BIN="$PAIR_HOME/1.0.0/cli-bin"
FIFO="$PAIR_HOME/pair.stdin.fifo"
LOGDIR="$PAIR_HOME/logs"
CFG="/Users/turnstone/Library/Application Support/Nvidia Corporation/Personal AI Router"

# llama.cpp ONLY. Fronting lmstudio as well would put a second facade on :1234.
PROXY_ENGINES="${PAIR_PROXY_ENGINES:-llamacpp}"

mkdir -p "$LOGDIR"
umask 077

# one instance only
if [ -f "$PAIR_HOME/pair.pid" ] && kill -0 "$(cat "$PAIR_HOME/pair.pid")" 2>/dev/null; then
  echo "already running as pid $(cat "$PAIR_HOME/pair.pid")" >&2
  exit 0
fi

# (re)create the FIFO
rm -f "$FIFO"
mkfifo "$FIFO" || { echo "cannot create FIFO at $FIFO" >&2; exit 1; }

# open read-write; fd 0 survives exec and never EOFs
exec 0<>"$FIFO"

# record our own pid for the singleton check above (survives exec)
echo "$$" > "$PAIR_HOME/pair.pid"

# keep broker's stdout off a dead pipe: JSON-RPC frames go to a log, not /dev/null
exec >>"$LOGDIR/broker-stdout.log" 2>>"$LOGDIR/broker-stderr.log"

echo "$(date -u +%FT%TZ) starting nvpair-ui-broker (engines=$PROXY_ENGINES)"

cd "$BIN" || exit 1
exec "$BIN/nvpair-ui-broker" \
  --log-level info \
  --proxy-engines "$PROXY_ENGINES" \
  --scanner-path      "$BIN/nvpair-node-scanner" \
  --node-info-path    "$BIN/nvpair-node-info" \
  --proxy-path        "$BIN/nvpair-proxy" \
  --engine-manager-path "$BIN/nvpair-engine-manager" \
  --workload-manager-path "$BIN/nvpair-workload-manager" \
  --cluster-manager-path  "$BIN/nvpair-cluster-manager" \
  --errors-path       "$BIN/nvpair-errors" \
  --settings-path     "$BIN/nvpair-node-settings" \
  --manual-nodes-path "$BIN/nvpair-manual-nodes" \
  --scheduler-path    "$BIN/nvpair-job-scheduler" \
  --cluster-dir       "$CFG/cluster"
WRAPPER
chmod 0755 "$ROOT/run_pair_daemon.sh"
log "   wrote $ROOT/run_pair_daemon.sh"

# ---------------------------------------------------------------------------
log "6. smoke test the binary (version only, no daemon yet)"
"$DEST/cli-bin/nvpair-ui-broker" --version 2>&1 | sed 's/^/   broker: /' || die "broker will not exec - codesign issue?"
"$DEST/cli-bin/nvpair-proxy" --version 2>&1 | sed 's/^/   proxy : /' || warn "proxy --version failed"
"$DEST/cli-bin/nvpair-tui" --version 2>&1 | sed 's/^/   tui   : /' || warn "tui --version failed"

echo
log "install complete."
echo "  binaries : $DEST/cli-bin"
echo "  wrapper  : $ROOT/run_pair_daemon.sh"
echo "  facade   : llama.cpp pre-seeded to :$FACADE_PORT"
echo
echo "  NEXT: sudo bash 40_install_launchd.sh"
echo
echo "  Manual foreground test first (recommended, Ctrl-C to stop):"
echo "    bash $ROOT/run_pair_daemon.sh"
