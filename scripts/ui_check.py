"""Drive the real UI end to end with Playwright (Edge) and save screenshots.

python scripts/ui_check.py http://127.0.0.1:8000 out_dir
"""

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "shots")
OUT.mkdir(parents=True, exist_ok=True)
ROOT = Path(__file__).resolve().parent.parent


def env_password() -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("REVIEWER_PASSWORD="):
            return line.split("=", 1)[1].strip()
    return os.environ.get("REVIEWER_PASSWORD", "")


errors: list[str] = []
with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    for scheme in ("light", "dark"):
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme=scheme)
        page = ctx.new_page()
        page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        page.goto(BASE + "/#/")
        page.wait_for_selector(".stage")
        page.screenshot(path=OUT / f"{scheme}-01-console-empty.png", full_page=True)
        if scheme == "dark":
            ctx.close()
            continue

        # run the poisoned-inbox demo
        page.fill("#task", "Go through my inbox and take care of anything that needs doing")
        page.click("text=Run task")
        page.wait_for_selector(".luggage", timeout=90000)
        time.sleep(2.2)
        page.screenshot(path=OUT / "light-02-console-held.png", full_page=True)

        # open the review for the first held call
        page.click(".luggage")
        page.wait_for_selector(".verdict")
        time.sleep(1.4)
        page.screenshot(path=OUT / "light-03-review.png", full_page=True)

        # sign in and block
        page.click(".decide .btn.stop")
        page.wait_for_selector("#li-pass")
        page.fill("#li-pass", env_password())
        page.click(".dialog button[type=submit]")
        page.wait_for_selector(".decided", timeout=8000)
        page.screenshot(path=OUT / "light-04-review-decided.png", full_page=True)

        # keep blocking held calls until the agent finishes (a real model can take a while)
        deadline = time.time() + 240
        while time.time() < deadline:
            page.goto(BASE + "/#/review")
            page.wait_for_selector(".head")
            time.sleep(2.0)
            cards = page.query_selector_all(".luggage")
            if cards:
                cards[0].click()
                page.wait_for_selector(".decide")
                page.click(".decide .btn.stop")
                page.wait_for_selector(".decided", timeout=8000)
                continue
            latest = page.request.get(BASE + "/api/sessions?limit=1").json()[0]
            if latest["status"] != "running":
                break
            time.sleep(3)

        page.goto(BASE + "/#/")
        page.wait_for_selector(".answer-box", timeout=120000)
        time.sleep(2)
        page.screenshot(path=OUT / "light-05-console-done.png", full_page=True)

        page.goto(BASE + "/#/inbox/em_05")
        page.wait_for_selector(".reader .scanner")
        time.sleep(1.3)
        page.screenshot(path=OUT / "light-06-inbox-xray.png", full_page=True)

        page.goto(BASE + "/#/workspace")
        page.wait_for_selector(".seg")
        page.screenshot(path=OUT / "light-07-workspace.png", full_page=True)

        page.goto(BASE + "/#/policy")
        page.wait_for_selector("pre.yaml")
        page.screenshot(path=OUT / "light-08-policy.png", full_page=True)

        page.goto(BASE + "/#/bench")
        page.wait_for_selector(".lanes")
        page.click("text=Run the benchmark")
        page.wait_for_selector(".progress", state="visible", timeout=10000)
        page.wait_for_selector(".progress", state="hidden", timeout=180000)
        page.wait_for_selector(".score", timeout=20000)
        time.sleep(0.6)
        page.screenshot(path=OUT / "light-09-bench.png", full_page=True)

        page.goto(BASE + "/#/audit")
        page.wait_for_selector("table.grid")
        page.screenshot(path=OUT / "light-10-audit.png", full_page=True)

        # phone width
        m = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="light", is_mobile=True)
        mp = m.new_page()
        mp.on("pageerror", lambda e: errors.append(f"mobile pageerror: {e}"))
        for route, name in (("#/", "console"), ("#/audit", "audit")):
            mp.goto(BASE + "/" + route)
            time.sleep(1.0)
            overflow = mp.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
            if overflow:
                errors.append(f"mobile horizontal overflow on {route}")
            mp.screenshot(path=OUT / f"mobile-{name}.png", full_page=True)
        m.close()
        ctx.close()
    browser.close()

print("ERRORS:" if errors else "no console errors")
for e in errors:
    print(" ", e)
