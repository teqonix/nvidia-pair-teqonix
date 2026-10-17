#!/bin/bash
# Cohesion matrix using the node's OWN cluster roster (what node-scanner/TUI consults),
# not local /etc/hosts. For each member other than self, probe :14318/v1/node-info.
CFG="${PAIR_CFG:-/home/turnstone/.config/Nvidia Corporation/Personal AI Router}"
python3 - "$CFG" <<'PY'
import json,sys,socket,urllib.request
cfg=sys.argv[1]
try:
    self_u=json.load(open(cfg+"/node-id.json")).get("node_uuid","")
except Exception: self_u=""
try:
    members=json.load(open(cfg+"/cluster/members.json"))
except Exception as e:
    print("MEMBERS_READ_FAIL",e); sys.exit()
print("FROM_HOST %s  SELF_UUID %s  CLUSTER_ID %s" % (
    socket.gethostname().split('.')[0], self_u[:8],
    json.load(open(cfg+"/settings.json")).get("cluster_id","?")))
WATCH=["amd-ai-core-one","turnstone-worker-one","turnstone-worker-two","turnstone-coordinator-nerd-projects","mbp-ai-core.lan"]
for m in members:
    n=m.get("name")
    if n not in WATCH: continue
    ip=m.get("ipAddress")
    if (m.get("nodeUuid") or "")==self_u:
        print("  %-38s SELF (roster ip=%s)" % (n,ip)); continue
    url="http://%s:14318/v1/node-info" % ip
    try:
        r=urllib.request.urlopen(url,timeout=4); d=json.load(r)
        gpu=(d.get("GPUs") or [{}])[0].get("name","?")
        hu=(d.get("hostUuid") or "?")[:8]
        expect=(m.get("nodeUuid") or "?")[:8]
        flag="" if hu==expect else "  <-- MISMATCH roster=%s answered=%s" % (expect,hu)
        print("  %-38s ONLINE via %-15s gpu=%s uuid=%s%s" % (n,ip,gpu,hu,flag))
    except Exception as e:
        print("  %-38s OFFLINE via %-15s (%s)" % (n,ip,type(e).__name__))
PY
