#!/usr/bin/env bash
# 60_verify.sh — post-upgrade validation for PAIR 1.0.0 on mbp-ai-core.
# Run as: ssh turnstone@mbp-ai-core.lan 'bash -s' < 60_verify.sh
set -uo pipefail

PASS=0; FAIL=0; WARN=0
ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[1;31m✗\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
wrn()  { printf '  \033[1;33m!\033[0m %s\n' "$1"; WARN=$((WARN+1)); }
hdr()  { printf '\n\033[1m%s\033[0m\n' "$1"; }
LABEL="com.nvidia.nvpair.turnstone"

# ---------------------------------------------------------------------------
hdr "1. Process / daemon status"
if sudo -n launchctl print "system/${LABEL}" >/dev/null 2>&1; then
  ST=$(sudo -n launchctl print "system/${LABEL}" 2>/dev/null | awk '/state =/{print $3; exit}')
  [ "$ST" = "running" ] && ok "launchd daemon state: running" || bad "launchd state: ${ST:-unknown}"
  NRE=$(sudo -n launchctl print "system/${LABEL}" 2>/dev/null | grep -c 'last exit code' || true)
  LE=$(sudo -n launchctl print "system/${LABEL}" 2>/dev/null | awk '/last exit code/{print $NF; exit}')
  if [ -n "${LE:-}" ]; then wrn "last exit code: $LE (a nonzero here means a crash-restart happened)"; else ok "no recorded crash exit"; fi
else
  wrn "cannot read launchd state without passwordless sudo; check manually: sudo launchctl print system/${LABEL}"
fi

BROKER=$(pgrep -f 'pair/1.0.0/cli-bin/nvpair-ui-broker' | head -1)
[ -n "$BROKER" ] && ok "nvpair-ui-broker running (pid $BROKER)" || bad "nvpair-ui-broker NOT running"
PROXY=$(pgrep -f 'pair/1.0.0/cli-bin/nvpair-proxy' | head -1)
[ -n "$PROXY" ] && ok "nvpair-proxy running (pid $PROXY)" || bad "nvpair-proxy NOT running"
EMGR=$(pgrep -f 'pair/1.0.0/cli-bin/nvpair-engine-manager' | head -1)
[ -n "$EMGR" ] && ok "nvpair-engine-manager running" || bad "engine-manager NOT running"

# the OLD stack must be gone
if pgrep -f '/Applications/PAIR.app/Contents/Resources/cli-bin' >/dev/null 2>&1; then
  bad "old 0.91.7 binaries STILL running - teardown incomplete, ports may be stolen"
else
  ok "no old /Applications/PAIR.app processes remain"
fi

# restart-loop check
if [ -n "$BROKER" ]; then
  C1=$(pgrep -f 'pair/1.0.0/cli-bin/nvpair-ui-broker' | head -1)
  sleep 5
  C2=$(pgrep -f 'pair/1.0.0/cli-bin/nvpair-ui-broker' | head -1)
  [ "$C1" = "$C2" ] && ok "broker pid stable across 5s (no restart loop)" || bad "broker pid changed $C1 -> $C2 (RESTART LOOP)"
fi

# ---------------------------------------------------------------------------
hdr "2. Port bindings"
for p in 1234 8080 11434 1235; do
  if lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then
    PROC=$(lsof -nP -iTCP:"$p" -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $1}' | sort -u | tr '\n' ' ')
    case "$p" in
      1234) case "$PROC" in *nvpair-proxy*) ok ":$p held by nvpair-proxy (llama.cpp facade)";; *) bad ":$p held by $PROC, expected nvpair-proxy";; esac;;
      *) ok ":$p LISTENING ($PROC)";;
    esac
  else
    [ "$p" = 8080 ] && wrn ":$p not listening (llama.cpp facade may have fallen back — check llamacpp-proxy-port.json)" || wrn ":$p not listening"
  fi
done
# confirm the facade really is llama.cpp on 1234, not a leftover MLX server
SRV=$(curl -s -m 5 http://localhost:1234/api/v0/models 2>/dev/null | head -c 200)
if echo "$SRV" | grep -qi 'lmstudio\|compatibility_type'; then
  wrn ":1234 still answering with LM Studio-shaped API - verify this is PAIR's facade, not ML Studio itself"
fi

# ---------------------------------------------------------------------------
hdr "3. Engine readiness (llama.cpp)"
CACHE="/Users/turnstone/.llamacpp"
NGGUF=$(find "$CACHE" -name '*.gguf' 2>/dev/null | wc -l | tr -d ' ')
if [ "$NGGUF" -gt 0 ]; then ok "$NGGUF GGUF file(s) in $CACHE"
else bad "cache empty - run 50_provision_gguf.sh; nothing can load"; fi
CACHED=$(du -sh "$CACHE" 2>/dev/null | cut -f1)
wrn "cache size: ${CACHED:-0}"

# engine-manager reports llamacpp installed?
if ls "/Users/turnstone/Library/Application Support/Nvidia Corporation/Personal AI Router/engines/" 2>/dev/null | grep -qi llamacpp; then
  ok "engine-manager has an llamacpp install record"
else
  wrn "no llamacpp engine record yet — engine may install on first use"
fi

# ---------------------------------------------------------------------------
hdr "4. Inference test"
MODEL="${VERIFY_MODEL:-}"
if [ -z "$MODEL" ]; then
  MODEL=$(find "$CACHE" -name '*.gguf' 2>/dev/null | head -1 | xargs -I{} basename {} .gguf 2>/dev/null || echo "")
  wrn "guessing model id as '$MODEL' — llama-server uses the GGUF-embedded id, not the filename."
  wrn "Set it explicitly: VERIFY_MODEL=<id> bash 60_verify.sh"
fi

# discover real ids from the facade
IDS=$(curl -s -m 10 http://localhost:1234/v1/models 2>/dev/null)
if [ -n "$IDS" ]; then
  echo "$IDS" | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: print("  (unparseable /v1/models response)"); sys.exit()
ms=d.get("data",[])
print("  facade reports %d model(s):" % len(ms))
for m in ms[:12]: print("    -", m.get("id"))
' 2>/dev/null || wrn "could not parse /v1/models"
  FIRST=$(echo "$IDS" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["data"][0]["id"])' 2>/dev/null || echo "")
  [ -n "$FIRST" ] && MODEL="$FIRST"
fi

if [ -n "$MODEL" ]; then
  echo
  log() { printf '  %s\n' "$1"; }
  log "POST /v1/chat/completions  model=$MODEL"
  RESP=$(curl -s -m 180 -X POST http://localhost:1234/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: PAIR-1.0.0-OK\"}],\"max_tokens\":32,\"temperature\":0}")
  if [ -z "$RESP" ]; then
    bad "empty response (model load may have timed out — first load can exceed 60s on a cold cache)"
  elif echo "$RESP" | grep -q 'error'; then
    bad "error response: $(echo "$RESP" | head -c 300)"
  else
    TXT=$(echo "$RESP" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["choices"][0]["message"]["content"])' 2>/dev/null || echo "")
    if echo "$TXT" | grep -q 'PAIR-1.0.0-OK'; then ok "inference OK -> '$TXT'"
    else wrn "responded but not the expected token: '$TXT'"; fi
  fi
else
  bad "no model id available to test"
fi

# ---------------------------------------------------------------------------
hdr "5. VRAM / memory + eviction"
echo "  macOS unified memory (no nvidia-smi). Occupancy proxy:"
ps axo user,rss,comm | grep -E 'llama-server|nvpair' | grep -v grep \
  | awk '{printf "    %-11s %8.1f MB  %s\n", $1, $2/1024, $3}' | head -10
echo
echo "  Eviction: llamacpp.json pins --sleep-idle-seconds 300."
echo "  To test: load a model, wait 305s, re-check the process RSS above."
echo "    watch -n 30 'ps axo rss,comm | grep llama-server'"
wrn "eviction was NOT auto-tested here (needs a 5-minute idle window)"

# ---------------------------------------------------------------------------
hdr "6. Config integrity"
CFG="/Users/turnstone/Library/Application Support/Nvidia Corporation/Personal AI Router"
for f in "$CFG"/*.json; do
  [ -e "$f" ] || continue
  if jq -e . "$f" >/dev/null 2>&1; then ok "valid JSON: $(basename "$f")"; else bad "INVALID JSON: $(basename "$f")"; fi
done
PP="$CFG/llamacpp-proxy-port.json"
[ -f "$PP" ] && { P=$(jq -r '.port' "$PP" 2>/dev/null); [ "$P" = "1234" ] && ok "llamacpp facade persisted on 1234" || wrn "facade port persisted as $P"; }

# ---------------------------------------------------------------------------
hdr "7. Logs — errors in the last 400 lines"
for L in /Users/turnstone/pair/logs/broker-stderr.log /Users/turnstone/pair/logs/launchd-stderr.log; do
  [ -f "$L" ] || continue
  E=$(tail -400 "$L" 2>/dev/null | grep -ciE '\berror\b|fatal|panic' || true)
  if [ "${E:-0}" -eq 0 ]; then ok "no errors in $(basename "$L")"
  else
    wrn "$E error-ish lines in $(basename "$L"); first 5:"
    tail -400 "$L" 2>/dev/null | grep -iE '\berror\b|fatal|panic' | head -5 | sed 's/^/      /'
  fi
done

# ---------------------------------------------------------------------------
printf '\n\033[1m================ SUMMARY ================\033[0m\n'
printf '  \033[1;32mPASS %s\033[0m   \033[1;33mWARN %s\033[0m   \033[1;31mFAIL %s\033[0m\n' "$PASS" "$WARN" "$FAIL"
if [ "$FAIL" -eq 0 ]; then
  printf '\n  \033[1;32mmbp-ai-core is migrated to PAIR 1.0.0 / llama.cpp.\033[0m\n'
else
  printf '\n  \033[1;31mNot healthy — resolve the failures above before marking the node up.\033[0m\n'
fi
exit "$FAIL"
