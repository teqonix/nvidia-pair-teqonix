#!/bin/bash
# Cluster cohesion matrix: from THIS node, probe each member's /v1/node-info:14318
# (the exact endpoint nvpair-tui uses to mark a node Online) and report cluster id.
# Read-only.
NODES="turnstone-worker-one.lan turnstone-worker-two.lan turnstone-coordinator-nerd-projects.lan amd-ai-core-one.lan mbp-ai-core.lan"
CFG="${PAIR_CFG:-/home/turnstone/.config/Nvidia Corporation/Personal AI Router}"

echo "FROM_HOST $(hostname -s)"
echo "CLUSTER_ID $(python3 -c "import json;print(json.load(open('$CFG/settings.json')).get('cluster_id'))" 2>/dev/null)"
echo "SELF_NODE_ID $(python3 -c "import json;print(json.load(open('$CFG/node-id.json')).get('nodeId','?'))" 2>/dev/null)"
echo "MEMBER_COUNT $(python3 -c "import json;print(len(json.load(open('$CFG/cluster/members.json'))))" 2>/dev/null)"
for h in $NODES; do
  code=$(curl -s -m 4 -o /tmp/ni_$$.json -w '%{http_code}' "http://$h:14318/v1/node-info" 2>/dev/null)
  if [ "$code" = "200" ]; then
    gpu=$(python3 -c "import json;d=json.load(open('/tmp/ni_$$.json'));g=d.get('GPUs') or [{}];print(g[0].get('name','?'))" 2>/dev/null)
    hu=$(python3 -c "import json;print(json.load(open('/tmp/ni_$$.json')).get('hostUuid','?')[:8])" 2>/dev/null)
    printf 'PROBE\t%s\t%s\tgpu=%s\tuuid=%s\n' "$h" "ONLINE" "$gpu" "$hu"
  else
    printf 'PROBE\t%s\tOFFLINE\thttp=%s\n' "$h" "${code:-000}"
  fi
done
rm -f /tmp/ni_$$.json
