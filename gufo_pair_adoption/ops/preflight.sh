#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
#
# preflight.sh — re-verify every assumption the GUFO deployment rests on.
#
# Run from the control node (worker-two). Read-only against the target: this
# script never mutates anything. It exists so that a plan written hours ago is
# re-checked against live state before anything destructive is attempted.
#
# Exit codes:
#   0  all checks passed (WARN lines may remain)
#   1  one or more BLOCKING failures — do not proceed
#   2  script / connectivity error

set -uo pipefail

HOST="${HOST:-amd-ai-core-two.lan}"
DRY=1   # this script is read-only by construction; flag exists for symmetry
FAIL=0
WARN=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$*"; WARN=$((WARN+1)); }
hdr()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    -h|--help)
      cat <<USAGE
usage: preflight.sh [--host HOST]

Read-only verification of GUFO deployment preconditions on HOST.
Default host: $HOST
USAGE
      exit 0 ;;
    *) say "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# rssh: run a probe remotely, never abort the whole run on a non-zero exit.
rssh() { ssh -o ConnectTimeout=10 -o BatchMode=yes "$HOST" "$1" 2>/dev/null; }

say "GUFO -> PAIR deployment preflight"
say "target: $HOST"
say "control: $(hostname)  $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ---------------------------------------------------------------- 0. reachability
hdr "0. Connectivity"
if rssh 'echo UP' | grep -q UP; then
  ok "ssh to $HOST works"
else
  bad "cannot ssh to $HOST — nothing below is meaningful"
  say "\nRESULT: BLOCKED (unreachable)"; exit 1
fi

# ---------------------------------------------------------------- 1. ROCm / GPU
hdr "1. ROCm / GPU"
# rocminfo is NOT on PATH in a non-login shell; read the version file directly.
ROCM_VER=$(rssh 'cat /opt/rocm/.info/version 2>/dev/null')
if [[ -n "$ROCM_VER" ]]; then
  ok "ROCm installed: $ROCM_VER"
else
  bad "no /opt/rocm/.info/version — ROCm absent"
fi

ISA=$(rssh 'bash -lc "rocminfo 2>/dev/null | grep -oE \"gfx[0-9]+\" | sort -u | tr \"\\n\" \" \""')
if printf '%s' "$ISA" | grep -q gfx1151; then
  ok "gfx1151 target present (Strix Halo)"
else
  bad "gfx1151 not reported (got: '${ISA:-none}') — GUFO HIP backend will not match"
fi

# GUFO needs the HIP toolchain, not just the runtime.
if rssh 'test -x /opt/rocm/bin/hipcc' ; then
  ok "hipcc present at /opt/rocm/bin/hipcc"
else
  warn "hipcc missing — build must happen in a container with the HIP compiler"
fi

KFD=$(rssh 'test -c /dev/kfd && echo yes')
REND=$(rssh 'test -c /dev/dri/renderD128 && echo yes')
[[ -n "$KFD"  ]] && ok "/dev/kfd present"  || bad "/dev/kfd missing"
[[ -n "$REND" ]] && ok "/dev/dri/renderD128 present" || bad "/dev/dri/renderD128 missing"

# ---------------------------------------------------------------- 2. port map
hdr "2. Port map (8080 façade / 8081 shim / 8901 GUFO / 8731 Halogen)"
for p in 8080 8081 8901 8731; do
  owner=$(rssh "ss -tlnp 2>/dev/null | awk -v p=':$p' '\$4 ~ p\"$\" {print \$0}' | head -1")
  case "$p" in
    8081|8901)
      [[ -z "$owner" ]] && ok "$p free (required)" \
        || bad "$p BUSY but required free -> ${owner}"
      ;;
    8080)
      [[ -n "$owner" ]] && ok "$p held by PAIR façade (expected)" \
        || warn "$p not bound — is PAIR down? GUFO must still use 8901, not 8080"
      ;;
    8731)
      [[ -n "$owner" ]] && ok "$p Halogen live (expected; becomes fallback)" \
        || warn "$p free — Halogen already down"
      ;;
  esac
done

# ---------------------------------------------------------------- 3. podman
hdr "3. Podman"
PV=$(rssh 'podman version --format "{{.Version}}" 2>/dev/null')
if [[ -n "$PV" ]]; then ok "podman $PV"; else bad "podman unusable"; fi

# Rootless + lingering, else PAIR (running as turnstone) cannot manage the pod.
LING=$(rssh 'loginctl show-user "$(whoami)" 2>/dev/null | grep -c "^Linger=yes"')
[[ "$LING" == "1" ]] && ok "linger enabled (pod survives logout)" \
  || bad "linger NOT enabled — pod dies with the session"

# Functional GPU passthrough test, not just "podman info returned 0".
# Pick any locally-present image with a /bin/sh — avoids a network pull on the target.
BASEIMG=$(rssh 'podman images --format "{{.repository}}:{{.tag}}" 2>/dev/null | grep -vE "^<none>|podman-pause" | grep -m1 -E "strix-halo-toolboxes|ai-cluster-usage-app|halogen|nginx|alpine|debian|bash" || true')
[[ -z "$BASEIMG" ]] && BASEIMG=$(rssh 'podman images --format "{{.repository}}:{{.tag}}" 2>/dev/null | grep -vE "^<none>|podman-pause" | head -1')
if [[ -z "$BASEIMG" ]]; then
  warn "no local image available for GPU passthrough probe — skipping functional test"
else
  say "  probe image: $BASEIMG"
  PT=$(rssh "timeout 120 podman run --rm --device /dev/kfd --device /dev/dri/renderD128 --group-add keep-groups '$BASEIMG' /bin/sh -c 'test -c /dev/kfd && test -c /dev/dri/renderD128 && echo GPU_OK' 2>/dev/null")
  if printf '%s' "$PT" | grep -q GPU_OK; then
    ok "rootless GPU passthrough verified in-container"
  else
    # /bin/sh may be absent in a distroless image; retry with /bin/bash.
    PT2=$(rssh "timeout 120 podman run --rm --device /dev/kfd --device /dev/dri/renderD128 --group-add keep-groups '$BASEIMG' /bin/bash -c 'test -c /dev/kfd && test -c /dev/dri/renderD128 && echo GPU_OK' 2>/dev/null")
    printf '%s' "$PT2" | grep -q GPU_OK \
      && ok "rootless GPU passthrough verified in-container" \
      || bad "GPU passthrough test failed with '$BASEIMG' — check render/video group membership"
  fi
fi

# ---------------------------------------------------------------- 4. headroom
hdr "4. Memory / disk"
AVAIL=$(rssh "free -g | awk '/^Mem:/{print \$7}'")
DISK=$(rssh "df -BG --output=avail / | tail -1 | tr -dc '0-9'")
SWAP_KB=$(rssh "awk '/^SwapTotal:/{print \$2}' /proc/meminfo")
Mapped_KB=$(rssh "awk '/^Mapped:/{print \$2}' /proc/meminfo")
UNEV_KB=$(rssh "awk '/^Unevictable:/{print \$2}' /proc/meminfo")
say "  RAM available: ${AVAIL:-?} GiB   Disk free (/): ${DISK:-?} GiB"
say "  Mapped: $(( ${Mapped_KB:-0} / 1048576 )) GiB   Unevictable: $(( ${UNEV_KB:-0} / 1024 )) MiB"

# Swap=0 is the decisive fact for coexistence: there is no safety valve, so
# anonymous-memory pressure goes straight to the OOM killer.
if (( ${SWAP_KB:-0} == 0 )); then
  warn "SwapTotal=0 — no OOM safety valve. deploy_stack.sh 5b will enforce a hard floor."
  say "  NOTE: Halogen's ~85 GB of 'Mapped' is FILE-BACKED page cache, not pinned."
  say "  Unevictable is only $(( ${UNEV_KB:-0} / 1024 )) MiB, so Halogen's weights ARE"
  say "  reclaimable under pressure. The risk is GUFO's anon (KV/activations/registered"
  say "  HIP host buffers) pushing the kernel to drop them, with Halogen as the victim."
else
  ok "Swap present: $(( SWAP_KB / 1048576 )) GiB"
fi
[[ "${AVAIL:-0}" -ge 40 ]] && ok ">=40 GiB RAM available" \
  || warn "<40 GiB RAM free — GUFO + Halogen coexistence may thrash unified memory"
[[ "${DISK:-0}" -ge 50 ]] && ok ">=50 GiB disk free" || bad "<50 GiB disk free for build + image"

# ---------------------------------------------------------------- 5. PAIR topology
hdr "5. PAIR unit topology (see DEPLOY_RUNBOOK §0.1)"
NDAEMONS=$(rssh 'systemctl show nvpair.service -p MainPID --value 2>/dev/null; pgrep -fc nvpair- 2>/dev/null || echo 0')
PROXIES=$(rssh 'pgrep -c nvpair-proxy 2>/dev/null || echo 0')
if [[ "${PROXIES:-0}" -gt 0 ]]; then
  warn "nvpair-proxy running (${PROXIES}) inside nvpair.service — 'systemctl stop nvpair' removes ALL PAIR daemons, not just the TUI"
  warn "  => that is a full cluster-node outage on $HOST, not a cosmetic stop"
else
  ok "nvpair-proxy not running (PAIR already down)"
fi

# ---------------------------------------------------------------- 6. Halogen
hdr "6. Halogen (this conversation's inference backend)"
HALST=$(rssh 'systemctl is-active halogen-flash-server.service 2>/dev/null')
HALMASK=$(rssh 'systemctl is-enabled halogen-flash-server.service 2>/dev/null')
say "  active=$HALST enabled=$HALMASK"
if [[ "$HALST" == "active" ]]; then
  warn "Halogen ACTIVE — it is the live inference backend; stopping it kills sessions routed to :8731"
fi

# ---------------------------------------------------------------- 7. is *this* session on Halogen?
hdr "7. Caller routing check (critical for step 7)"
# If the control node currently holds an established connection to target:8731,
# the operator driving this may themselves be served by Halogen.
CONNS=$(rssh "ss -tn state established '( dport = :8731 or sport = :8731 )' 2>/dev/null | tail -n +2 | wc -l")
say "  established connections touching :8731 right now: ${CONNS:-0}"
if [[ "${CONNS:-0}" -gt 0 ]]; then
  warn "there IS live traffic on :8731 — identify it before any cutover"
  rssh "ss -tnp state established '( dport = :8731 or sport = :8731 )' 2>/dev/null | head -5" | sed 's/^/      /'
fi
# Corroborate from the Halogen journal: who has been calling it lately?
RECENT=$(rssh 'journalctl -u halogen-flash-server.service --since "-10 min" --no-pager 2>/dev/null | grep -oE "[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+ - .POST /v1/chat" | grep -oE "^[0-9.]+" | sort -u | head -5')
if [[ -n "$RECENT" ]]; then
  say "  clients seen calling Halogen in last 10 min:"
  printf '      %s\n' $RECENT
  warn "if the control node ($(hostname -I 2>/dev/null | awk '{print $1}')) is listed, THIS session dies on cutover"
fi

# ---------------------------------------------------------------- 8. GUFO artifacts
hdr "8. GUFO install / build readiness"
if rssh 'command -v gufo >/dev/null 2>&1'; then
  ok "gufo binary present"
else
  warn "gufo not installed — must be built (ops/build_gufo.sh)"
fi
IMG=$(rssh 'podman image exists localhost/gufo:latest && echo yes')
[[ "$IMG" == "yes" ]] && ok "localhost/gufo:latest present" \
  || warn "localhost/gufo:latest absent — build required"

MISS=""
for d in hipblas-dev hipblaslt-dev rocblas-dev hipcub-dev rocprim-dev rocwmma-dev \
         libicu-dev libpng-dev libjpeg-dev libwebp-dev; do
  rssh "dpkg-query -W '$d' >/dev/null 2>&1" || MISS="$MISS $d"
done
if [[ -n "$MISS" ]]; then
  warn "host build deps missing:$MISS"
  warn "  => build in a pinned ROCm container, not on the host toolchain"
else
  ok "all host build deps present"
fi
GCC=$(rssh 'gcc -dumpversion 2>/dev/null')
say "  host gcc: ${GCC:-?} (GUFO qualified against 15.3 / ROCm 7.2.3)"

# ---------------------------------------------------------------- 9. model weights
hdr "9. Model weights"
if rssh 'test -d /home/turnstone/.models' ; then
  SHARD=$(rssh 'ls /home/turnstone/.models/*.gguf 2>/dev/null | wc -l')
  [[ "$SHARD" -gt 0 ]] && ok "GGUF candidates found ($SHARD files under ~/.models)" \
    || warn "no GGUF under ~/.models"
  rssh 'ls -1 /home/turnstone/.models/*.gguf 2>/dev/null | head -3' | sed 's/^/      /'
else
  warn "no ~/.models — GUFO has nothing to serve"
fi
warn "Halogen .hgn weights are NOT consumable by 'gufo serve llm' (needs GGUF)"
rssh 'test -d /srv/models/gufo' && ok "/srv/models/gufo exists" \
  || warn "/srv/models/gufo missing — deploy_stack.sh creates it"

# ---------------------------------------------------------------- 10. known defects
hdr "10. Known-config defect checks"
# compose.yaml ships SHIM_HOST: "0.0.0.0" while the manifest declares bind 127.0.0.1.
# An unauthenticated LAN proxy that injects a live GUFO key must not be 0.0.0.0.
if rssh 'test -f /opt/gufo-pair/compose.yaml'; then
  if rssh 'grep -q "SHIM_HOST: \"0.0.0.0\"" /opt/gufo-pair/compose.yaml'; then
    bad "staged compose.yaml still binds shim to 0.0.0.0 — unauthenticated proxy exposing GUFO key"
  else
    ok "staged compose.yaml shim bind is not 0.0.0.0"
  fi
else
  say "  (compose not staged yet — deploy_stack.sh must write 127.0.0.1)"
fi

# ---------------------------------------------------------------- verdict
hdr "VERDICT"
if [[ $FAIL -gt 0 ]]; then
  say "BLOCKED: $FAIL blocking failure(s), $WARN warning(s)."
  say "Resolve FAIL lines before running deploy_stack.sh."
  exit 1
fi
say "GO: 0 blocking failures, $WARN warning(s)."
say "Warnings are informational — read DEPLOY_RUNBOOK.md §5 before proceeding."
exit 0
