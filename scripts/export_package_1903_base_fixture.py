#!/usr/bin/env python3
"""Export the immutable pre-registered 2026-09-21 Rule-C base fixture."""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

HOST = "ozand@eeepc-lan"
REMOTE = r'''
import datetime, gzip, json, pathlib, subprocess
root=pathlib.Path("/var/lib/eeepc-agent/self-evolving-agent")
state=root/"state"; repo=root/"eeebot-self-evolving"
cut=datetime.datetime.fromisoformat("2026-09-24T23:37:48+00:00")
rows=[]
for lp in sorted((state/"ledger").glob("cycles-*.jsonl.gz")):
 with gzip.open(lp, "rt") as f:
  for line in f:
   try: rows.append(json.loads(line))
   except: pass
active=state/"ledger/cycles.jsonl"
if active.exists():
 for line in active.read_text().splitlines():
  try: rows.append(json.loads(line))
  except: pass
starts={}; outcomes={}
for r in rows:
 c=r.get("cycle_id"); t=r.get("ts")
 if not c or not t: continue
 if r.get("phase")=="started": starts[c]=min(starts.get(c,t),t)
 if r.get("phase")=="outcome" and (c not in outcomes or t>outcomes[c].get("ts","")): outcomes[c]=r
base=[c for c,t in starts.items() if "2026-09-21T00:00:00"<=t<"2026-09-22T00:00:00"]
first={}
for line in (state/"llm_calls/2026-09-21.jsonl").read_text().splitlines():
 try:r=json.loads(line)
 except:continue
 if r.get("component")=="executor" and r.get("cycle_id"):
  c=r["cycle_id"]; first[c]=min(first.get(c,r["ts"]),r["ts"])
first24=sorted((c for c in base if c in first),key=lambda c:first[c])[:24]
assert len(base)==42 and len(first24)==24
assert all(datetime.datetime.fromisoformat(starts[c].replace("Z","+00:00"))<cut for c in base), "window cycle in base export"
accepted={c for c in base if outcomes.get(c,{}).get("outcome")=="success" and outcomes[c].get("verdict")=="accept"}
log=subprocess.run(["git","-C",str(repo),"log","origin/main","--first-parent","--format=%H%x09%s"],capture_output=True,text=True,check=True).stdout
merges={}
for line in log.splitlines():
 sha,_,s=line.partition("	"); prefix="merge: integrate selfevo/cycle-"
 if s.startswith(prefix):
  c=s[len(prefix):].strip()
  if c in accepted and c not in merges:merges[c]=sha
files={}
for c,sha in merges.items():
 ps=subprocess.run(["git","-C",str(repo),"diff","--name-only",f"{sha}^1",f"{sha}^2"],capture_output=True,text=True,check=True).stdout.splitlines()
 assert all(not p.startswith("/") and ".." not in pathlib.PurePosixPath(p).parts for p in ps)
 files[c]=ps
order=first24+[c for c in sorted(base) if c not in first24]
print(json.dumps([{"cycle_id":c,"outcome":outcomes[c].get("outcome"),"verdict":outcomes[c].get("verdict"),"branch_files":files.get(c,[])} for c in order if c in outcomes],separators=(",",":")))
'''

def main() -> None:
    payload = base64.b64encode(REMOTE.encode()).decode()
    remote_cmd = "sudo -n -u eeepc-agent python3 -c 'import base64;exec(base64.b64decode(\"" + payload + "\"))'"
    raw = subprocess.run(["ssh", HOST, remote_cmd], check=False, text=True, capture_output=True)
    if raw.returncode:
        raise RuntimeError(f"read-only export failed ({raw.returncode}): {raw.stderr[-500:]}")
    rows = json.loads(raw.stdout)
    assert len(rows) >= 42 and all(set(r) == {"cycle_id", "outcome", "verdict", "branch_files"} for r in rows)
    (Path(__file__).parent.parent / "tests/fixtures/package_1903_base_2026-09-21.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")

if __name__ == "__main__": main()
