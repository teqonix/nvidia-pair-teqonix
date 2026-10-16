#!/usr/bin/env bash
# 40_install_launchd.sh — system daemon for PAIR 1.0.0 as turnstone.  RUN WITH SUDO.
#
#   ssh turnstone@mbp-ai-core.lan 'sudo bash -s' < 40_install_launchd.sh
#
# Installs a LaunchDAEMON (boot-time, no login required) and removes the stale
# LaunchAgent left behind by the previous setup.
#
# Why a Daemon and not an Agent: a LaunchAgent only starts after that user logs in,
# so an unattended Mac Studio would boot with no router until someone signed in as
# turnstone. LaunchDaemon + UserName=turnstone runs at boot with no console session.
set -euo pipefail

log()  { printf '\033[1;32m[launchd]\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$1"; exit 1; }

[ "$(whoami)" = "root" ] || die "must run as root:  sudo bash 40_install_launchd.sh"

LABEL="com.nvidia.nvpair.turnstone"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"
WRAPPER="/Users/turnstone/pair/run_pair_daemon.sh"
RUN_USER="turnstone"
RUN_GROUP="staff"

[ -x "$WRAPPER" ] || die "wrapper missing: $WRAPPER (run 30_install_turnstone.sh first)"

# ---------------------------------------------------------------------------
log "0. remove the stale LaunchAgent"
# The old ~/Library/LaunchAgents/com.nvidia.nvpair.plist ran run_pair_tmux.sh,
# which fell back to /Applications/PAIR.app/.../nvpair-tui — i.e. a turnstone
# LaunchAgent launching teqonix's binaries. Cross-user config mixing. Remove it.
OLD_AGENT="/Users/turnstone/Library/LaunchAgents/com.nvidia.nvpair.plist"
if [ -f "$OLD_AGENT" ]; then
  launchctl bootout "gui/$(id -u $RUN_USER)/com.nvidia.nvpair" 2>/dev/null || true
  # also try the legacy form
  launchctl remove com.nvidia.nvpair 2>/dev/null || true
  mv "$OLD_AGENT" "/Users/turnstone/pair_backup_removed_${LABEL}.agent.plist" 2>/dev/null \
    && log "   moved stale agent -> /Users/turnstone/pair_backup_removed_${LABEL}.agent.plist"
  # kill any tmux 'pair' session the agent may have left, as the right user
  sudo -u "$RUN_USER" tmux kill-session -t pair 2>/dev/null || true
else
  log "   no stale agent present"
fi

# ---------------------------------------------------------------------------
log "1. write $PLIST"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${WRAPPER}</string>
    </array>

    <!-- Boot-time, no console session required -->
    <key>RunAtLoad</key>
    <true/>

    <!-- Restart if it dies -->
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>15</integer>

    <!-- Run as turnstone, not root -->
    <key>UserName</key>
    <string>${RUN_USER}</string>
    <key>GroupName</key>
    <string>${RUN_GROUP}</string>

    <key>WorkingDirectory</key>
    <string>/Users/turnstone/pair</string>

    <!-- PATH must include whatever llama-server needs at exec time.
         ~/.homebrew is this node's brew prefix (NOT /opt/homebrew). -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/turnstone/.homebrew/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>/Users/turnstone</string>
        <key>PAIR_PROXY_ENGINES</key>
        <string>llamacpp</string>
    </dict>

    <key>StandardOutPath</key>
    <string>/Users/turnstone/pair/logs/launchd-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/turnstone/pair/logs/launchd-stderr.log</string>

    <!-- Generous stop window: broker SIGTERMs every worker with grace;
         30s keeps launchd from SIGKILLing mid-model-unload. -->
    <key>TimeoutStop</key>
    <integer>30</integer>

    <key>SoftResourceLimits</key>
    <dict>
        <key>NumberOfFiles</key>
        <integer>65536</integer>
    </dict>
    <key>HardResourceLimits</key>
    <dict>
        <key>NumberOfFiles</key>
        <integer>65536</integer>
    </dict>
</dict>
</plist>
PLIST

chown root:wheel "$PLIST"
chmod 0644 "$PLIST"
plutil -lint "$PLIST" || die "plist failed validation"
log "   plist written and lint-clean"

# logs dir owned by turnstone before launchd opens files in it
mkdir -p /Users/turnstone/pair/logs
chown -R "$RUN_USER:$RUN_GROUP" /Users/turnstone/pair/logs

# ---------------------------------------------------------------------------
log "2. bootstrap into the system domain"
# bootout first if already loaded, so this is idempotent
launchctl bootout system "$PLIST" 2>/dev/null || true
launchctl bootstrap system "$PLIST"
log "   bootstrapped"

sleep 8

log "3. status"
launchctl print "system/${LABEL}" 2>&1 | grep -E "state|pid|last exit code|runs|spawn reason" | sed 's/^ */    /' || \
  launchctl list | grep nvpair | sed 's/^/    /'

# ---------------------------------------------------------------------------
log "4. port check"
sleep 3
for p in 1234 8080 11434; do
  if lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then
    printf '  \033[1;32m%-6s LISTENING\033[0m  %s\n' "$p" \
      "$(lsof -nP -iTCP:"$p" -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $1}' | sort -u | tr '\n' ' ')"
  else
    printf '  %-6s not listening\n' "$p"
  fi
done

echo
log "launchd daemon installed."
echo
echo "  Start now : sudo launchctl kickstart -k system/${LABEL}"
echo "  Stop      : sudo launchctl bootout system/${LABEL}"
echo "  Logs      : /Users/turnstone/pair/logs/{launchd,broker}-{stdout,stderr}.log"
echo "  Follow    : tail -f /Users/turnstone/pair/logs/broker-stderr.log"
echo
echo "  If it restarts in a loop, the FIFO or codesigning is the usual cause:"
echo "    sudo launchctl print system/${LABEL} | grep -A3 'last exit code'"
echo
warn "Model cache is still EMPTY. Run 50_provision_gguf.sh before expecting inference."
