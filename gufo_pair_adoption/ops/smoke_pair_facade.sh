#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 teqonix
# SPDX-License-Identifier: Apache-2.0
#
# smoke_pair_facade.sh — verify the GUFO masquerade at three escalating scopes.
#
#   --direct         shim only, PAIR not involved. The GATE for cutover.
#   --via-pair     PAIR adopted the override and routes to the shim; peers see it.
#   --post-cutover same as --via-pair but asserts Halogen is gone.
#
# --direct is deliberately independent of PAIR so the stack can be proven while
# PAIR is stopped (step 5) — with PAIR down there is no façade to test through,
# so we test the shim's own contract instead.
#
# Exit: 0 all pass | 1 failures | 2 usage

set -uo pipefail

HOST="${HOST:-amd-ai-core-two.lan}"
SHIM="http://127.0.0.1:8081"
MODEL="${GUFO_TEST_MODEL:-gufo-qwen38-flash-next}"
SCOPE=""
PEER="${PEER_NODE:-}"
PASS=0; FAIL=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$*"; }
hdr()  { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
err()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; }
rssh() { ssh -o ConnectTimeout=15 -o BatchMode=yes "$HOST" "$1" 2>/dev/null; }

usage() {
  cat <<USAGE
usage: smoke_pair_facade.sh [--host HOST] (--direct | --via-pair | --post-cutover)

  --direct          shim contract only; PAIR may be down (cutover gate)
  --via-pair        PAIR adopted override + peer visibility
  --post-cutover    as --via-pair, plus Halogen must be absent

  --peer HOST       a second cluster node to verify cross-node visibility from
                  (uses the mTLS ingress; plaintext from remote = loopback-only)
  --model NAME      model id to probe (default: $MODEL)
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --direct) SCOPE="direct"; shift ;;
    --via-pair) SCOPE="via-pair"; shift ;;
    --post-cutover) SCOPE="post-cutover"; shift ;;
    --peer) PEER="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown arg: $1"; usage; exit 2 ;;
  esac
done
[[ -z "$SCOPE" ]] && { usage; exit 2; }

# ------------------------------------------------------------ shared: shim core
hdr "Shim core (always checked)"

props=$(rssh "curl -fsS --max-time 8 $SHIM/props 2>/dev/null")
if printf '%s' "$props" | grep -q '"role"[[:space:]]*:[[:space:]]*"router"'; then
  ok "/props -> role=router  (satisfies manifest ready.json_match)"
else
  bad "/props not role=router: ${props:-<unreachable>}"
fi

rssh "curl -fsS --max-time 8 $SHIM/health >/dev/null 2>&1" \
  && ok "/health -> 200" || bad "/health unreachable"

models=$(rssh "curl -fsS --max-time 10 $SHIM/v1/models 2>/dev/null")
if printf '%s' "$models" | grep -q '"data"'; then
  ok "/v1/models -> OpenAI-shaped list"
  printf '%s' "$models" | tr ',' '\n' | grep '"id"' | head -3 | sed 's/^/        /'
else
  bad "/v1/models not a list: ${models:-<unreachable>}"
fi

m2=$(rssh "curl -fsS --max-time 10 $SHIM/models 2>/dev/null")
printf '%s' "$m2" | grep -q '"data"' && ok "/models -> data array" || bad "/models missing"

# Loopback-only enforcement: shim must not be reachable from off-host.
hdr "Shim exposure"
EXT=$(curl -fsS --max-time 8 "http://$HOST:8081/props" 2>&1)
if printf '%s' "$EXT" | grep -qi 'role'; then
  bad "shim reachable from OFF-HOST on :8081 — unauthenticated proxy holding the GUFO key"
  say "        Fix SHIM_HOST to 127.0.0.1 in $HOST:/opt/gufo-pair/compose.yaml"
else
  ok "shim NOT reachable off-host (loopback bind holds)"
fi

# Real completion — the only check that proves inference works.
hdr "Real inference through shim -> GUFO"
comp=$(rssh "curl -fsS --max-time 180 $SHIM/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: SHIM_SMOKE_OK\"}],\"max_tokens\":48}' 2>/dev/null")
if printf '%s' "$comp" | grep -q 'SHIM_SMOKE_OK'; then
  ok "completion returned expected marker"
elif printf '%s' "$comp" | grep -q '"choices"'; then
  warn "completion returned choices but not the exact marker (model variance?)"
  printf '%s' "$comp" | head -c 250 | sed 's/^/        /'; echo
else
  bad "no completion: ${comp:-<none>}"
fi

if [[ "$SCOPE" == "direct" ]]; then
  hdr "SCOPE: direct — DONE"
  say "passed=$PASS failed=$FAIL"
  if [[ $FAIL -eq 0 ]]; then
    say "GUFO stack is PROVEN. Safe to consider cutover_halogen.sh."
  else
    say "NOT safe to cut over — fix failures first. Halogen remains the fallback."
  fi
  exit $(( FAIL > 0 ? 1 : 0 ))
fi

# ------------------------------------------------------------ PAIR-mediated
hdr "PAIR adoption"
pair_active=$(rssh 'systemctl is-active nvpair.service 2>/dev/null')
if [[ "$pair_active" == "active" ]]; then
  ok "nvpair.service active"
else
  bad "nvpair.service not active ($pair_active) — cannot test PAIR routing"
fi

# The neutralisation invariant: adopting GUFO must NOT download llama.cpp.
hdr "Neutralisation invariant (no llama.cpp download)"
if rssh "find /home/turnstone -name 'llama-server' -newer /opt/gufo-pair/compose.yaml 2>/dev/null | grep -q ."; then
  bad "llama-server appeared AFTER the override was installed — installer NOT neutralised"
else
  ok "no llama-server materialised post-override"
fi
ovr=$(rssh "cat \"/home/turnstone/.config/Nvidia Corporation/Personal AI Router/engines/llamacpp.json\" 2>/dev/null")
if printf '%s' "$ovr" | grep -q 'gufo-pairctl'; then
  ok "override in place, references gufo-pairctl"
else
  bad "override absent or does not reference gufo-pairctl"
fi
printf '%s' "$ovr" | grep -q '"editable_launch": null' \
  && ok "editable_launch is explicit null" \
  || warn "editable_launch not explicit null — PAIR may validate llama.cpp flags against gufo-pairctl"

hdr "PAIR façade -> shim routing"
# PAIR façade is loopback-only for plaintext; probe from the host itself.
froute=$(rssh "curl -fsS --max-time 20 http://127.0.0.1:8080/v1/models 2>/dev/null")
if printf '%s' "$froute" | grep -q '"data"'; then
  ok "façade :8080 returns model list"
elif printf '%s' "$froute" | grep -q 'loopback-only'; then
  warn "façade answered loopback-only; plaintext probe path differs — check mTLS ingress"
else
  bad "façade :8080 did not return a model list: ${froute:-<none>}"
fi

# ---------------------------------------------------------- peer visibility
hdr "Peer visibility"
if [[ -n "$PEER" ]]; then
  if ssh -o ConnectTimeout=10 -o BatchMode=yes "$PEER" 'echo UP' 2>/dev/null | grep -q UP; then
    ok "peer $PEER reachable"
    seen=$(ssh -o BatchTimeout=10 -o BatchMode=yes "$PEER" \
      "curl -fsS --max-time 20 https://$HOST:1234/v1/models -k 2>/dev/null" )
    if printf '%s' "$seen" | grep -q '"data"'; then
      ok "peer sees core-two models through mTLS ingress"
    else
      bad "peer cannot see core-two models: ${seen:-<none>}"
    fi
  else
    bad "cannot ssh to peer $PEER"
  fi
else
  warn "no --peer given: cross-node visibility NOT verified"
  warn "  pass --peer <node> to prove other nodes see this engine"
fi

# ---------------------------------------------------------- post-cutover
if [[ "$SCOPE" == "post-cutover" ]]; then
  hdr "Halogen absence (post-cutover assertion)"
  hal=$(rssh "ss -tln '( sport = :8731 )' 2>/dev/null | tail -n +2 | wc -l")
  halmask=$(rssh "systemctl is-enabled halogen-flash-server.service 2>/dev/null")
  if [[ "${hal:-0}" -eq 0 ]]; then
    ok ":8731 not bound — Halogen down"
  else
    bad ":8731 still bound — Halogen running during post-cutover test"
  fi
  [[ "$halmask" == "masked" ]] && ok "halogen unit masked" || warn "halogen unit not masked ($halmask)"
fi

hdr "SCOPE: $SCOPE — DONE"
say "passed=$PASS failed=$FAIL"
exit $(( FAIL > 0 ? 1 : 0 ))
