"""Walk through the guided demo in a real browser and save a screenshot per step.

python scripts/tour_check.py out_dir [simulated|auto]
"""

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
AGENT = sys.argv[2] if len(sys.argv) > 2 else "simulated"
BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parent.parent
PASSWORD = next(l.split("=", 1)[1].strip() for l in (ROOT / ".env").read_text().splitlines() if l.startswith("REVIEWER_PASSWORD="))

errors = []
with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)
    ctx = b.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.goto(BASE + "/#/intro")
    pg.evaluate(f"localStorage.setItem('af.agent', JSON.stringify('{AGENT}'))")

    def btn(label):
        time.sleep(0.5)
        pg.locator(".tour-card button", has_text=label).first.click()

    # 1. skipping works: start, then Esc
    pg.click("text=Start the guided demo >> nth=0")
    pg.wait_for_selector(".tour-card")
    pg.keyboard.press("Escape")
    time.sleep(0.4)
    assert pg.query_selector(".tour-card") is None, "Esc did not close the demo"
    pg.click("text=Start the guided demo >> nth=0")
    pg.wait_for_selector(".tour-card")
    btn("Skip demo")
    time.sleep(0.4)
    assert pg.query_selector(".tour-card") is None, "Skip demo did not close the demo"

    def title():
        return pg.inner_text(".tour-card h3")

    def shot(n):
        time.sleep(0.9)
        pg.screenshot(path=OUT / f"step-{n:02d}.png")
        print(f"step {n:02d}: {title()}")

    # 2. the full walk-through
    pg.click("text=Start the guided demo >> nth=0")
    pg.wait_for_selector(".tour-card"); shot(1)
    btn("Start"); pg.wait_for_selector(".reader .scanner"); shot(2)
    btn("Next"); pg.wait_for_selector(".tour-card >> text=Run the task"); shot(3)
    btn("Run the task")
    pg.wait_for_selector(".tour-card >> text=Every action passes the checkpoint"); shot(4)
    pg.wait_for_selector(".tour-card >> text=Held for a person", timeout=180000); shot(5)
    btn("Next"); shot(6)
    btn("Next"); shot(7)
    if "came from" in title():
        btn("Next"); shot(8)
    pg.click(".decide .btn.stop")
    pg.wait_for_selector("#li-pass"); pg.fill("#li-pass", PASSWORD); pg.click(".dialog button[type=submit]")
    pg.wait_for_selector(".tour-card >> text=Nothing harmful left the company", timeout=30000); shot(9)
    btn("Next"); shot(10)
    btn("Finish")
    time.sleep(0.4)
    assert pg.query_selector(".tour-card") is None
    b.close()

print("errors:", errors or "none")
