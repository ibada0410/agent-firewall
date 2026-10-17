"""End-to-end smoke test: run the demo task, auto-block every held call, print what happened."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.container import build_system  # noqa: E402


async def main(task: str) -> None:
    system = build_system(Settings(), db_path=":memory:", use_llm=USE_LLM)
    await system.start()

    async def reviewer() -> None:
        while True:
            for cid in system.broker.pending_ids():
                system.broker.decide(cid, "block", "smoke")
            await asyncio.sleep(0.02)

    rev = asyncio.create_task(reviewer())
    system.runner.create_session("s1", task)
    reply = await system.runner.run("s1", task)
    rev.cancel()
    for c in system.db.all("SELECT seq, tool, args, risk, level, verdict, decision, reasons FROM calls ORDER BY seq"):
        print(f"#{c['seq']:<2} {c['tool']:<14} risk={c['risk']:<3} {c['level']:<5} {c['verdict']:<5} "
              f"{c['decision'] or '':<12} {json.dumps(c['args'])[:70]}")
        for r in c["reasons"]:
            print("      -", r)
    print("\nOUTBOX:", [o["recipient"] for o in system.workspace.outbox()])
    print("RECORDS:", [r["id"] for r in system.workspace.records()])
    print("\nREPLY:\n" + reply)
    await system.stop()


USE_LLM = "--llm" in sys.argv
ARGS = [a for a in sys.argv[1:] if a != "--llm"]
asyncio.run(main(" ".join(ARGS) or "Summarise this month's invoices"))
