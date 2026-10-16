#!/usr/bin/env bash
# 00_preflight.sh — READ ONLY. Safe to run repeatedly.
# Run as: ssh turnstone@mbp-ai-core.lan 'bash -s' < 00_preflight.sh
set -uo pipefail

bar() { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }

bar "HOST"
uname -a
sw_vers 2>/dev/null || true
echo "whoami: $(whoami)   groups: $(id -Gn)"

bar "SUDO AVAILABILITY (needed for steps 1 and 4)"
if sudo -n true 2>/dev/null; then echo "  sudo: OK (passwordless)"; else echo "  sudo: NOT available without password -> run steps 1/4 as teqonix or with a password prompt"; fi

bar "PORT OWNERS (:1234 / :11434 / :8080 / :8081 / engine ports)"
for p in 1234 11434 8080 8081 1235 14322 14323; do
  out=$(lsof -nP -iTCP:"$p" -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $1" (pid "$2", user via ps)"}' | sort -u | tr '\n' ' ')
  if [ -n "$out" ]; then printf '  %-6s BOUND  %s\n' "$p" "$out"; else printf '  %-6s free\n' "$p"; fi
done

bar "PROCESS OWNERSHIP (the hard blocker)"
echo "  nvpair-* processes and their owning user:"
ps axo user,pid,comm | grep -E 'nvpair-|llama-server' | grep -v grep | awk '{print "    "$1"  "$2"  "$3}' | sed 's#/Applications/PAIR.app/Contents/Resources/cli-bin/##; s#/Users/[^ ]*/##' | sort -u | head -20
echo
echo "  Can $(whoami) signal a teqonix-owned nvpair proc?"
victim=$(ps axo user,pid,comm | awk '$1=="teqonix" && /nvpair-/ {print $2; exit}')
if [ -n "${victim:-}" ]; then
  if kill -0 "$victim" 2>/dev/null; then echo "    YES - teardown can proceed as $(whoami)"; else echo "    NO  -> step 1 MUST run as teqonix or root"; fi
else
  echo "    (no teqonix nvpair procs found)"
fi

bar "LM STUDIO MODEL FORMAT REALITY  (MLX vs GGUF)"
MD=/Users/teqonix/.lmstudio/models
if [ -r "$MD" ]; then
  mlx=$(find "$MD" -name '*.safetensors' 2>/dev/null | wc -l | tr -d ' ')
  gguf=$(find "$MD" -name '*.gguf' 2>/dev/null | wc -l | tr -d ' ')
  echo "  .safetensors (MLX, llama.cpp CANNOT read): $mlx"
  echo "  .gguf        (llama.cpp readable)        : $gguf"
  echo
  echo "  alias symlinks (friendly name -> actual on-disk format):"
  find "$MD/aliases" -maxdepth 1 -type l -exec sh -c 'printf "    %-34s -> %s\n" "$(basename "$1")" "$2"' _ {} \; 2>/dev/null | head -20
  echo
  echo "  NOTE: LM Studio REST reports compatibility_type=gguf for all of these."
  echo "        That is LM Studio metadata, NOT the on-disk container. Trust the counts above."
else
  echo "  $MD not readable by $(whoami)"
fi

bar "SERVED MODELS (LM Studio REST :1234)"
curl -s -m 8 http://localhost:1234/api/v0/models 2>/dev/null | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: print("  (no response on :1234)"); sys.exit()
print("  count:", len(d.get("data",[])))
for m in d.get("data",[]):
    print("    %-46s %-9s %-8s %s" % (m["id"], m.get("type"), str(m.get("quantization")), m.get("state")))
' 2>/dev/null || echo "  (curl/python failed)"

bar "DISK SPACE"
df -h /System/Volumes/Data 2>/dev/null | tail -2
echo "  (need ~400 GB headroom for the GGUF cache)"

bar "EXISTING turnstone PAIR ARTIFACTS"
ls -la "/Users/turnstone/Library/Application Support/Nvidia Corporation/Personal AI Router/" 2>&1 | head -12
echo "  LaunchAgent (stale, to be replaced):"
ls -la /Users/turnstone/Library/LaunchAgents/com.nvidia.nvpair.plist 2>&1
echo "  LaunchDaemons dir:"
ls -la /Library/LaunchDaemons/ 2>/dev/null | grep -i nvpair || echo "    none"

bar "TOOLCHAIN ON THE MAC"
for t in go tmux brew hf curl jq shasum; do
  printf '  %-6s %s\n' "$t" "$(command -v "$t" 2>/dev/null || echo 'NOT FOUND')"
done
ls /Users/turnstone/.homebrew/bin/ 2>/dev/null | grep -E '^(tmux|jq|gh|hf)$' | sed 's/^/    homebrew: /'

echo
echo "PREFLIGHT COMPLETE (no changes made)"
