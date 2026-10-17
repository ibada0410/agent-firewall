"""Run the benchmark from the command line: python scripts/bench.py [--llm]"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bench.runner import run_benchmark  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402


async def main() -> None:
    out = await run_benchmark(Settings(), Database(":memory:"), None, use_llm="--llm" in sys.argv)
    for r in out["results"]:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"{mark} {r['id']:<7} {r['kind']:<6} {r['outcome']:<14} risk={r['max_risk']:<3} {r['name']}")
    print("\nmode:", out["agent_mode"], "status:", out["status"])
    for k, v in out["summary"].items():
        print(f"  {k}: {v}")


asyncio.run(main())
