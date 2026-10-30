#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
#
# commit_deployment.sh — step 9: make the GUFO-backed llamacpp engine the
# persistent default across reboots.
#
# "Commit" here means persistence, not VCS. Nothing is deleted:
#   * Halogen stays installed and its images/weights remain on disk; it is left
#     masked so it cannot restart at boot or via Restart=always.
#   * GUFO pod + shim are made start-able at boot as the unprivileged user,
#     via a systemd user unit with lingering (already enabled on this host).
#   * PAIR (nvpair.service) stays enabled with the override manifest in place.
#
# Halogen is NOT removed. Un-masking it returns the old stack; the machine has
# 582 GB free and the weights are the rollback path.
#
# Run only after smoke_pair_facade.sh --post-cutover passes.
#
# Exit: 0 committed | 1 failed | 2 usage

set -uo pipefail

HOST="${HOST:-amd-ai-core-two.lan}"
RUN_USER="turnstone"
HAL_UNIT="${HALOGEN_UNIT:-halogen-flash-server.service}"
ENV_PATH="/etc/gufo-pair/env"
COMPOSE="/opt/gufo-pair/compose.yaml"
COMMITTED_FLAG="/etc/gufo-pair/.committed"
DRY_RUN=0

say()  { printf '%s\n' "$*"; }
log()  { printf '[commit] %s\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$*"; }
hdr()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
err()  { printf '[commit] ERROR: %s\n' "$*" >&2; }
rssh() { ssh -o ConnectTimeout=15 -o BatchMode=yes "$HOST" "$1" 2>/dev/null; }
rsudo(){ rssh "sudo -n bash -lc '$1'"; }

usage() {
  cat <<USAGE
usage: commit_deployment.sh [--host HOST] [--dry-run]

  --dry-run   print the actions without changing anything

PRECONDITIONS (enforced):
  * GUFO shim reports role=router
  * Halogen is already stopped+masked (post-cutover state)
  * nvpair.service active with the override installed
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown arg: $1"; usage; exit 2 ;;
  esac
done
PASS=0; FAIL=0

say "=============================================================="
say " COMMIT DEPLOYMENT  host=$HOST"
say " $( [[ $DRY_RUN -eq 1 ]] && echo '(DRY RUN — nothing will change)' )"
say "=============================================================="

# ------------------------------------------------- 0. preconditions
hdr "0. Preconditions"
props=$(rssh "curl -fsS --max-time 8 http://127.0.0.1:8081/props 2>/dev/null")
if printf '%s' "$props" | grep -q '"role"[[:space:]]*:[[:space:]]*"router"'; then
  ok "shim role=router"
else
  bad "shim not ready — do NOT commit. Run smoke_pair_facade.sh --post-cutover first."
  [[ $DRY_RUN -eq 0 ]] && { err "refusing to commit an unproven stack"; exit 1; }
fi

halstate=$(rssh "systemctl is-enabled $HAL_UNIT 2>/dev/null")
if [[ "$halstate" == "masked" ]]; then
  ok "Halogen masked (cutover is done)"
else
  bad "Halogen is '$halstate', not masked — cutover incomplete; committing would leave"
  bad "  two engines racing for :8731 / unified memory at boot."
  [[ $DRY_RUN -eq 0 ]] && { err "refusing to commit"; exit 1; }
fi

pair=$(rssh 'systemctl is-active nvpair.service 2>/dev/null')
[[ "$pair" == "active" ]] && ok "nvpair.service active" || bad "nvpair.service '$pair'"
pair_en=$(rssh 'systemctl is-enabled nvpair.service 2>/dev/null')
[[ "$pair_en" == "enabled" ]] && ok "nvpair.service enabled at boot" || warn "nvpair.service '$pair_en'"

# ------------------------------------------------- 1. linger
hdr "1. User lingering for $RUN_USER (required for boot-time rootless podman)"
ling=$(rssh "loginctl show-user $RUN_USER -p Linger --value 2>/dev/null")
if [[ "$ling" == "yes" ]]; then
  ok "Linger=yes already"
else
  log "enabling linger for $RUN_USER"
  if [[ $DRY_RUN -eq 0 ]]; then
    rsudo "loginctl enable-linger $RUN_USER" >/dev/null
    ling=$(rssh "loginctl show-user $RUN_USER -p Linger --value 2>/dev/null")
    [[ "$ling" == "yes" ]] && ok "linger enabled" || bad "linger still '$ling'"
  else
    say "  would run: sudo loginctl enable-linger $RUN_USER"
  fi
fi

# ------------------------------------------------- 2. user unit
hdr "2. systemd user unit: gufo-pair.service"
UNIT_BODY='[Unit]
Description=GUFO masquerade stack (GUFO + llama.cpp shim) for PAIR
Documentation=file:///opt/gufo-pair/README
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=GUFO_PAIR_COMPOSE=/opt/gufo-pair/compose.yaml
Environment=GUFO_PAIR_ENV=/etc/gufo-pair/env
Environment=PATH=/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/local/bin/gufo-pairctl start
ExecStop=/usr/local/bin/gufo-pairctl stop
TimeoutStartSec=240
TimeoutStopSec=60

[Install]
WantedBy=default.target'

UUNIT_PATH="/home/$RUN_USER/.config/systemd/user/gufo-pair.service"

# systemctl --user needs the user's runtime bus. Resolve the uid rather than
# hard-coding it, and prefer --machine=user@ (systemd >= 248) which works from
# root without fiddling with DBus env vars.
UID_N=$(rssh "id -u $RUN_USER 2>/dev/null")

if [[ $DRY_RUN -eq 0 ]]; then
  # ssh inherits stdin, so a single pipe writes the file as the login user.
  printf '%s\n' "$UNIT_BODY" \
    | ssh -o ConnectTimeout=15 -o BatchMode=yes "$HOST" \
        "mkdir -p ~/.config/systemd/user && cat > ~/.config/systemd/user/gufo-pair.service" 2>/dev/null
  written=$(rssh "test -f $UUNIT_PATH && echo yes || echo no")
  [[ "$written" == "yes" ]] && ok "wrote $UUNIT_PATH" || { bad "could not write $UUNIT_PATH"; }

  # Backup ordering: reload, then enable. Use --machine= if available.
  if rssh "systemctl --user --machine=$RUN_USER@ daemon-reload" >/dev/null 2>&1; then
    ok "daemon-reload via --machine=$RUN_USER@"
    rssh "systemctl --user --machine=$RUN_USER@ enable gufo-pair.service" >/dev/null 2>&1 \
      && ok "enabled gufo-pair.service (user)" \
      || bad "enabled failed via --machine"
  elif [[ -n "$UID_N" ]]; then
    warn "--machine= unsupported; falling back to explicit bus env"
    BUS="unix:path=/run/user/$UID_N/bus"
    rssh "sudo -u $RUN_USER XDG_RUNTIME_DIR=/run/user/$UID_N DBUS_SESSION_BUS_ADDRESS=$BUS \
          systemctl --user daemon-reload" >/dev/null 2>&1 \
      && ok "daemon-reload via env" || bad "daemon-reload via env failed"
    rssh "sudo -u $RUN_USER XDG_RUNTIME_DIR=/run/user/$UID_N DBUS_SESSION_BUS_ADDRESS=$BUS \
          systemctl --user enable gufo-pair.service" >/dev/null 2>&1 \
      && ok "enabled gufo-pair.service (user)" || bad "enable via env failed"
  else
    bad "cannot resolve uid for $RUN_USER; user unit not enabled"
  fi
else
  say "  would write $UUNIT_PATH:"
  printf '%s\n' "$UNIT_BODY" | sed 's/^/      /'
  say "  would run: systemctl --user --machine=$RUN_USER@ enable gufo-pair.service"
fi

# ------------------------------------------------- 3. keep Halogen down
hdr "3. Halogen: keep masked, do NOT remove"
if [[ $DRY_RUN -eq 0 ]]; then
  rsudo "systemctl mask $HAL_UNIT" >/dev/null 2>&1 || true
  st=$(rssh "systemctl is-enabled $HAL_UNIT 2>/dev/null")
  [[ "$st" == "masked" ]] && ok "confirmed masked" || bad "state: $st"
else
  say "  would ensure: systemctl mask $HAL_UNIT"
fi
say "  Halogen image + weights are retained on disk as the rollback path."
say "  Restore with: $0 --host $HOST  ->  no; use ops/rollback_all.sh"

# ------------------------------------------------- 4. record commitment
hdr "4. Record deployment marker"
MARKER="committed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
engine=gufo-masquerade
pair_manifest=llamacpp.json
shim=127.0.0.1:8081
gufo=127.0.0.1:8901
halogen=masked"
if [[ $DRY_RUN -eq 0 ]]; then
  rssh "sudo mkdir -p /etc/gufo-pair && sudo tee $COMMITTED_FLAG >/dev/null" <<<"$MARKER" 2>/dev/null \
    && ok "wrote $COMMITTED_FLAG" || warn "could not write marker"
else
  say "  would write $COMMITTED_FLAG"
fi

# ------------------------------------------------- 5. final state
hdr "Final state"
gpf=$(rssh "sudo -u $RUN_USER systemctl --user is-enabled gufo-pair.service 2>/dev/null")
gpa=$(rssh "sudo -u $RUN_USER systemctl --user is-active gufo-pair.service 2>/dev/null")
npfe=$(rssh "systemctl is-enabled nvpair.service 2>/dev/null")
npfa=$(rssh "systemctl is-active nvpair.service 2>/dev/null")
hale=$(rssh "systemctl is-enabled $HAL_UNIT 2>/dev/null")
hala=$(rssh "systemctl is-active $HAL_UNIT 2>/dev/null")
lng=$(rssh "loginctl show-user $RUN_USER -p Linger --value 2>/dev/null")

printf '  %-22s %s\n' "gufo-pair (user)" "${gpf:-?}/${gpa:-?}"
printf '  %-22s %s\n' "nvpair.service" "${npfe:-?}/${npfa:-?}"
printf '  %-22s %s\n' "$HAL_UNIT" "${hale:-?}/${hala:-?}"
printf '  %-22s %s\n' "linger $RUN_USER" "${lng:-?}"

hdr "Commit complete  pass=$PASS fail=$FAIL"
if [[ $FAIL -gt 0 ]]; then
  say "Review FAIL items — the stack is running now but may not survive reboot."
else
  say "GUFO-backed llamacpp engine is now the persistent default."
  say "Reboot test recommended when a maintenance window allows:"
  say "  ssh $HOST 'sudo reboot'   then verify :8081 role=router and PAIR cluster join."
fi
exit $(( FAIL > 0 ? 1 : 0 ))
