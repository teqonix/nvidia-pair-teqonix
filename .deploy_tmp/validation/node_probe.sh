#!/bin/bash
# PAIR 1.0.0 cluster-wide validation probe.
# Runs ON a target node (locally or via ssh). Emits TAB-separated KEY<TAB>VALUE lines.
# Read-only: performs no mutations.
CLI=/opt/PAIR/resources/cli-bin
CFG_TURNSTONE="/home/turnstone/.config/Nvidia Corporation/Personal AI Router"
CFG_TEQONIX="/home/teqonix/.config/Nvidia Corporation/Personal AI Router"
CFG="${PAIR_CFG:-}"
if [ -z "$CFG" ]; then CFG="$CFG_TURNSTONE"; [ -d "$CFG" ] || CFG="$CFG_TEQONIX"; fi
MAN="$CLI/manifest.json"
TESTSTRING='PAIR-1.0.0-OK'
INFER_MODEL="${INFER_MODEL:-nvidia-nemotron-3.5-lightning-30b-a3b-optiq}"

p(){ printf '%s\t%s\n' "$1" "${2:-}" ; }

p NODE_HOST "$(hostname -s)"
p NODE_USER "$(whoami)"
p CONFIG_DIR "$CFG"

# ---------- 1. VERSION CONSISTENCY ----------
if [ -f "$MAN" ]; then
  p MANIFEST_PRESENT yes
  python3 - "$MAN" <<'PY' 2>/dev/null | sed 's/^/MAN_\t/'
import json,sys
m=json.load(open(sys.argv[1]))
print("product=%s" % m.get("product"))
print("platform=%s/%s" % (m.get("platform"), m.get("arch")))
c=m.get("components",{})
for k in ("nvpair-ui-broker","nvpair-tui","nvpair-proxy","nvpair-cluster-manager","nvpair-engine-manager"):
    print("%s=%s" % (k, c.get(k,"MISSING")))
PY
else
  p MANIFEST_PRESENT "no ($MAN)"
fi

# binary-reported versions (ground truth vs manifest drift)
for b in nvpair-tui nvpair-proxy nvpair-ui-broker; do
  if [ -x "$CLI/$b" ]; then p "BIN_VER_$b" "$("$CLI/$b" --version 2>&1 | head -1)"; else p "BIN_VER_$b" "MISSING"; fi
done

# sha256 integrity of manifest entries
python3 - "$MAN" "$CLI" <<'PY' 2>/dev/null | sed 's/^/SHA_\t/'
import json,sys,hashlib,os
man,cli=sys.argv[1],sys.argv[2]
if not os.path.isfile(man): print("skipped=no_manifest"); sys.exit()
m=json.load(open(man)); ok=bad=0; badlist=[]
for f in m.get("files",[]):
    n=f["fileName"]; p=os.path.join(cli,n)
    if not os.path.isfile(p): bad+=1; badlist.append(n+":absent"); continue
    h=hashlib.sha256(open(p,'rb').read()).hexdigest()
    if h==f["sha256"] and os.path.getsize(p)==f["size"]: ok+=1
    else: bad+=1; badlist.append(n)
present={f["fileName"] for f in m.get("files",[])}
extra=[e for e in os.listdir(cli) if e not in present and e!="manifest.json" and os.path.isfile(os.path.join(cli,e))]
print("ok=%d bad=%d extra=%s" % (ok,bad,(",".join(extra) if extra else "none")))
if badlist: print("badfiles=%s" % ",".join(badlist))
PY

# old (pre-1.0.0) proxy binaries must be GONE
OLD=$(ls "$CLI" 2>/dev/null | grep -E '^(ollama-proxy|lmstudio-proxy)$' | tr '\n' ',')
p OLD_PROXIES "${OLD:-none}"

# ---------- 2. UNIFIED PROXY ----------
PIDS=$(pgrep -x nvpair-proxy 2>/dev/null | tr '\n' ' ')
p PROXY_PIDS "${PIDS:-none}"
p PROXY_PID_COUNT "$(pgrep -cx nvpair-proxy 2>/dev/null || echo 0)"
for port in 1234 11434; do
  owner=$(ss -tlnpH "sport = :$port" 2>/dev/null | grep -oE '"[^"]+"' | head -1 | tr -d '"')
  p "PORT_${port}_PROCESS" "${owner:-nobody}"
done
# both ports on the SAME pid?
A=$(ss -tlnpH "sport = :1234" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
B=$(ss -tlnpH "sport = :11434" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
p UNIFIED_PAIR_1234 "${A:-none}"
p UNIFIED_PAIR_11434 "${B:-none}"
if [ -n "$A" ] && [ "$A" = "$B" ]; then p UNIFIED_PROXY_SAME_PROCESS yes; else p UNIFIED_PROXY_SAME_PROCESS "NO (1234=$A 11434=$B)"; fi

# ---------- SERVICE STATE ----------
if command -v systemctl >/dev/null 2>&1; then
  p SERVICE_STATE "$(systemctl is-active nvpair.service 2>&1)"
  p SERVICE_RESTARTS "$(systemctl show -p NRestarts --value nvpair.service 2>/dev/null)"
  p SERVICE_ENABLED "$(systemctl is-enabled nvpair.service 2>&1)"
fi

# ---------- 3. INFERENCE PATH ----------
T0=$(date +%s)
RESP=$(curl -s -m 110 -X POST http://127.0.0.1:1234/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$INFER_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: $TESTSTRING\"}],\"max_tokens\":400,\"temperature\":0}" 2>&1)
T1=$(date +%s)
p INFER_MODEL_USED "$INFER_MODEL"
p INFER_SECONDS "$((T1-T0))"
p INFER_RESULT "$(printf '%s' "$RESP" | python3 -c "
import sys,json
raw=sys.stdin.read()
try:
    d=json.loads(raw)
except Exception as e:
    print('PARSE_FAIL: '+raw[:180]); sys.exit()
if d.get('error'): print('ERROR: '+json.dumps(d['error'])[:200]); sys.exit()
ch=(d.get('choices') or [{}])[0]
c=(ch.get('message') or {}).get('content')
if c and '$TESTSTRING' in c: print('PASS')
else: print('MISMATCH: '+repr(c)[:180])
" 2>&1)"

# model inventory reachable on facade
p FACADE_MODELS_COUNT "$(curl -s -m 10 http://127.0.0.1:1234/v1/models 2>/dev/null | python3 -c "import sys,json;print(len(json.load(sys.stdin).get('data',[])))" 2>&1)"
p OLLAMA_FACADE_TAGS "$(curl -s -m 10 http://127.0.0.1:11434/api/tags 2>/dev/null | head -c 120)"

# ---------- ENGINE / MESH STATE ----------
p NODE_INFO_HTTP "$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:14318/v1/node-info 2>&1)"
if [ -f "$CFG/engine-settings-operations.json" ]; then
python3 - "$CFG/engine-settings-operations.json" <<'PY' 2>/dev/null | sed 's/^/ENGINE_\t/'
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception as e: print("parse_error=%s"%e); sys.exit()
for eng,v in sorted(d.items()):
    s=v.get("snapshot",{})
    print("%s running=%s phase=%s srvPort=%s proxyPort=%s adopted=%s" % (
        eng, s.get("running"), s.get("phase"),
        s.get("effectiveServerPort"), s.get("effectiveProxyPort"), s.get("adopted")))
PY
fi
if [ -f "$CFG/cluster/members.json" ]; then
  p CLUSTER_MEMBERS_LOCAL_COUNT "$(python3 -c "import json;print(len(json.load(open('$CFG/cluster/members.json'))))" 2>&1)"
  python3 - "$CFG/cluster/members.json" <<'PY' 2>/dev/null | sed 's/^/MEMBER_\t/'
import json,sys
for m in json.load(open(sys.argv[1])):
    print("%s state=%s uuid=%s addr=%s:%s" % (m.get("name"),m.get("state"),(m.get("nodeUuid") or "")[:8],m.get("ipAddress"),m.get("port")))
PY
fi

# ---------- LIVE ENGINE PROCESSES ----------
p LIVE_ENGINES "$(ps -eo comm 2>/dev/null | grep -E '^(llama-server|ollama|lm-studio|lmstudio)$' | sort | uniq -c | tr '\n' ';' | sed 's/^ *//')"
p BACKEND_PROCS "$(ps -eo user:10,pid,comm,args --no-headers 2>/dev/null | grep -E 'llama-server' | grep -v grep | head -2 | cut -c1-220 | tr '\n' '|')"
exit 0
