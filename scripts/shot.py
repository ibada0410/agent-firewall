"""Quick screenshots: python scripts/shot.py out_dir route[,route...] [width] [dark]"""

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
routes = sys.argv[2].split(",")
width = int(sys.argv[3]) if len(sys.argv) > 3 else 1440
scheme = "dark" if "dark" in sys.argv else "light"
with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)
    pg = b.new_page(viewport={"width": width, "height": 900}, color_scheme=scheme)
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    for r in routes:
        pg.goto("http://127.0.0.1:8000/#/" + ("" if r == "home" else r))
        time.sleep(1.6)
        name = r.replace("/", "-") + f"-{width}-{scheme}.png"
        pg.screenshot(path=out / name, full_page=True)
    b.close()
print("errors:", errs or "none")
