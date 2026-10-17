"""Run the demo task against a deployed instance using only its public API.

python scripts/api_flow.py https://your-service.example  [password]
Blocks every held action as a reviewer and prints what happened.
"""

import sys
import time
from pathlib import Path

import httpx

BASE = sys.argv[1].rstrip("/")
if len(sys.argv) > 2:
    PASSWORD = sys.argv[2]
else:
    PASSWORD = next(l.split("=", 1)[1].strip() for l in (Path(__file__).resolve().parent.parent / ".env").read_text().splitlines()
                    if l.startswith("REVIEWER_PASSWORD="))

with httpx.Client(base_url=BASE, timeout=30) as c:
    assert c.get("/health").json() == {"status": "ok"}
    assert c.post("/api/auth/login", json={"username": "reviewer", "password": PASSWORD}).status_code == 200
    s = c.post("/api/sessions", json={"task": "Go through my inbox and take care of anything that needs doing"}).json()
    print("task", s["id"], "agent:", s["agent_mode"])
    blocked = 0
    deadline = time.time() + 300
    while time.time() < deadline:
        for call in c.get("/api/review").json():
            c.post(f"/api/calls/{call['id']}/decision", json={"decision": "block"})
            blocked += 1
            print(f"  blocked {call['tool']} risk={call['risk']} :: {call['reasons'][0]}")
        state = c.get(f"/api/sessions/{s['id']}").json()
        if state["status"] != "running":
            break
        time.sleep(1.5)
    print("status:", state["status"], "| calls:", len(state["calls"]), "| blocked:", blocked)
    ws = c.get("/api/workspace").json()
    print("outbox:", [o["recipient"] for o in ws["outbox"]], "| records left:", len(ws["records"]))
