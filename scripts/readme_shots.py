"""Capture the screenshots used in README.md into docs/images/.

Needs the app running on http://127.0.0.1:8000.  python scripts/readme_shots.py
"""

import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
OUT.mkdir(parents=True, exist_ok=True)
PASSWORD = next(l.split("=", 1)[1].strip() for l in (ROOT / ".env").read_text().splitlines() if l.startswith("REVIEWER_PASSWORD="))

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)
    ctx = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1.5, color_scheme="light")
    pg = ctx.new_page()
    pg.goto(BASE + "/#/intro")
    pg.evaluate("localStorage.setItem('af.seenIntro','true'); localStorage.setItem('af.agent', JSON.stringify('simulated')); localStorage.setItem('af.view', JSON.stringify('xray'))")

    # 1. intro hero, caught on the "held" frame of the animation
    pg.goto(BASE + "/#/intro"); time.sleep(8.6)
    pg.locator(".hero").screenshot(path=OUT / "intro.png")

    # 2. console with an action waiting in the holding bay
    pg.goto(BASE + "/#/"); pg.wait_for_selector(".stage")
    pg.fill("#task", "Go through my inbox and take care of anything that needs doing")
    pg.click("text=Run task")
    pg.wait_for_selector(".luggage", timeout=60000); time.sleep(2.5)
    pg.evaluate("window.scrollTo(0, 70)"); time.sleep(0.4)
    pg.screenshot(path=OUT / "console.png")

    # 3. the review screen: score and the trace from the hidden sentence to the action
    pg.click(".luggage"); pg.wait_for_selector(".verdict")
    time.sleep(1.5)
    pg.locator(".review-top").scroll_into_view_if_needed()
    pg.evaluate("window.scrollTo(0, 0)"); time.sleep(2.6)
    pg.screenshot(path=OUT / "review.png", full_page=False)
    trace = pg.query_selector(".trace")
    if trace:
        pg.evaluate("document.querySelector('.scorebar').scrollIntoView({block:'start'}); window.scrollBy(0,-20)")
        time.sleep(2.6)
        pg.screenshot(path=OUT / "trace.png")

    # sign in and block everything still waiting so the workspace stays clean
    pg.request.post(BASE + "/api/auth/login", data={"username": "reviewer", "password": PASSWORD})
    for _ in range(40):
        for c in pg.request.get(BASE + "/api/review").json():
            pg.request.post(BASE + f"/api/calls/{c['id']}/decision", data={"decision": "block"})
        s = pg.request.get(BASE + "/api/sessions?limit=1").json()[0]
        if s["status"] != "running":
            break
        time.sleep(0.5)

    # 4. inbox x-ray
    pg.goto(BASE + "/#/inbox/em_05"); pg.wait_for_selector(".reader .scanner"); time.sleep(1.6)
    pg.screenshot(path=OUT / "inbox-xray.png")

    # 5. benchmark results
    pg.goto(BASE + "/#/bench"); pg.wait_for_selector(".lanes"); time.sleep(1.2)
    if not pg.query_selector(".score"):
        pg.click("text=Run the benchmark"); pg.wait_for_selector(".score", timeout=120000); time.sleep(1)
    pg.screenshot(path=OUT / "benchmark.png")
    b.close()

for f in sorted(OUT.iterdir()):
    print(f.name, f.stat().st_size // 1024, "KB")
