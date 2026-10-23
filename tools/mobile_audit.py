"""Check the published dashboard holds together at real device widths.

    .venv\\Scripts\\python.exe -m tools.mobile_audit [--shots]

Serves `docs/` on a throwaway port and drives it with Playwright across a
spread of phone, tablet and laptop widths. Three things are checked, because
only the first one is obvious and the other two are how a fix for the first
one goes wrong:

  overflow    nothing sticks out past the viewport. An element inside an
              `overflow-x: auto` box does not count - that box scrolls, the
              page does not, which is the whole point of `.tscroll`.

  reachable   every table too wide for its container can actually be scrolled
              sideways. Clipping the overflow instead of scrolling it also
              produces a clean overflow report, and loses columns.

  sticky      column headers still pin to the top above the breakpoint, and
              the first column pins to the left below it. Wrapping tables in
              a scroll container silently breaks sticky positioning, so this
              is the regression that would otherwise ship unnoticed.

Exits non-zero on any failure, so it can gate a push the way `league.verify`
gates a refresh. Run it after anything that touches the layout.
"""
from __future__ import annotations

import functools
import http.server
import socket
import socketserver
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SHOTS = ROOT / ".shots"          # gitignored: not part of the published site

# The breakpoint in styles.css: at or above this the tables fit as they are.
WIDE = 1024

VIEWPORTS = [
    ("iPhone SE (1st gen)", 320, 568),
    ("Galaxy S / common Android", 360, 800),
    ("iPhone SE (2nd/3rd gen)", 375, 667),
    ("iPhone 14/15/16", 390, 844),
    ("Pixel 8", 393, 873),
    ("iPhone 11 / XR", 414, 896),
    ("iPhone 16 Pro Max", 430, 932),
    ("iPad portrait", 768, 1024),
    ("iPad Air", 820, 1180),
    ("iPad landscape", 1024, 768),
    ("laptop", 1280, 800),
    ("laptop large", 1440, 900),
]

TABS = ["standings", "weekly", "luck", "h2h", "managers", "players", "moves",
        "records"]

OVERFLOW = """
() => {
  const vw = document.documentElement.clientWidth;
  const scrolled = (el) => {
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const ox = getComputedStyle(p).overflowX;
      if (ox === 'auto' || ox === 'scroll' || ox === 'hidden') return true;
    }
    return false;
  };
  const guilty = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('body *')) {
    const pos = getComputedStyle(el).position;
    if (!el.offsetParent && pos !== 'fixed') continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || (r.right <= vw + 1 && r.left >= -1)) continue;
    if (scrolled(el)) continue;
    const name = el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
      (el.className && typeof el.className === 'string'
        ? '.' + el.className.trim().split(/\\s+/).join('.') : '');
    if (seen.has(name)) continue;
    seen.add(name);
    guilty.push(`${name} w=${Math.round(r.width)} right=${Math.round(r.right)}`);
  }
  return {vw, over: document.scrollingElement.scrollWidth - vw,
          guilty: guilty.slice(0, 5)};
}
"""

# A table wider than its scrollport must be reachable by a finger, not just
# by script. `overflow-x: hidden` still answers a scrollLeft assignment, so
# testing that it moves proves nothing - only the computed overflow says
# whether the user can get at the far columns.
REACHABLE = """
() => {
  const bad = [];
  for (const box of document.querySelectorAll('.tscroll')) {
    if (box.scrollWidth <= box.clientWidth + 1) continue;
    const ox = getComputedStyle(box).overflowX;
    if (ox !== 'auto' && ox !== 'scroll') {
      bad.push(`${box.scrollWidth}px table is ${ox}: columns unreachable`);
      continue;
    }
    const before = box.scrollLeft;
    box.scrollLeft = 9999;
    const moved = box.scrollLeft > before;
    box.scrollLeft = before;
    if (!moved) bad.push(`${box.scrollWidth}px table will not scroll`);
  }
  return bad;
}
"""

# Below the breakpoint the first column pins left; above it the header pins
# top. Reading computed styles rather than pixels: this is about which rule
# won, not about layout.
STICKY = """
(wide) => {
  const bad = [];
  const th = document.querySelector('.view.active table.sortable thead th');
  const td = document.querySelector('.view.active table.sortable tbody td.pin');
  if (!th || !td) return bad;
  const hp = getComputedStyle(th).position;
  const cp = getComputedStyle(td).position;
  if (wide && hp !== 'sticky') bad.push(`header not sticky (${hp})`);
  if (!wide && cp !== 'sticky') bad.push(`first column not pinned (${cp})`);
  if (!wide && getComputedStyle(td).backgroundColor === 'rgba(0, 0, 0, 0)')
    bad.push('pinned column is transparent, rows will show through');
  return bad;
}
"""


def serve(directory: Path):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):      # one line per asset per viewport
            pass

    handler = functools.partial(Handler, directory=str(directory))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    class Quiet(socketserver.TCPServer):
        allow_reuse_address = True

        def handle_error(self, request, addr):
            pass

    httpd = Quiet(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def main() -> int:
    shots = "--shots" in sys.argv
    if shots:
        SHOTS.mkdir(parents=True, exist_ok=True)
    if not (DOCS / "index.html").exists():
        print("docs/ is empty - run `python -m league.export` first")
        return 1

    httpd, port = serve(DOCS)
    url = f"http://127.0.0.1:{port}/"
    failures = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for label, w, h in VIEWPORTS:
            phone = w < 500
            ctx = browser.new_context(
                viewport={"width": w, "height": h},
                device_scale_factor=2 if phone else 1,
                is_mobile=phone, has_touch=phone)
            page = ctx.new_page()
            page.goto(url, wait_until="networkidle")
            problems = []

            for tab in TABS:
                page.click(f'.tab[data-view="{tab}"]')
                page.wait_for_timeout(250)
                if tab == "players":          # loads a 3.7 MB file once
                    page.wait_for_timeout(900)

                o = page.evaluate(OVERFLOW)
                if o["over"] > 1:
                    problems.append(
                        f"{tab}: page {o['over']}px too wide - " +
                        "; ".join(o["guilty"]))
                for msg in page.evaluate(REACHABLE):
                    problems.append(f"{tab}: {msg}")
                for msg in page.evaluate(STICKY, w >= WIDE):
                    problems.append(f"{tab}: {msg}")

                if shots:
                    page.screenshot(path=str(SHOTS / f"{w}-{tab}.png"))

            # The two overlays, which have their own width rules.
            page.click('.tab[data-view="weekly"]')
            page.wait_for_timeout(200)
            # Scoped to the active view: the hidden ones hold matching rows
            # too, and Playwright will sit and wait for one to become visible.
            side = page.query_selector(".view.active .mu-side")
            if side:
                side.click()
                page.wait_for_timeout(350)
                o = page.evaluate(OVERFLOW)
                if o["over"] > 1:
                    problems.append(
                        f"roster popup: page {o['over']}px too wide - " +
                        "; ".join(o["guilty"]))
                if shots:
                    page.screenshot(path=str(SHOTS / f"{w}-roster.png"))

            page.click('.tab[data-view="managers"]')
            page.wait_for_timeout(250)
            row = page.query_selector(".view.active tbody tr.clickable")
            if row:
                row.click()
                page.wait_for_timeout(400)
                o = page.evaluate(OVERFLOW)
                if o["over"] > 1:
                    problems.append(
                        f"manager dialog: page {o['over']}px too wide - " +
                        "; ".join(o["guilty"]))
                if shots:
                    page.screenshot(path=str(SHOTS / f"{w}-dialog.png"))

            tag = f"{w}x{h}".ljust(9)
            if problems:
                failures += 1
                print(f"{tag} {label}")
                for p in problems:
                    print(f"    {p}")
            else:
                print(f"{tag} {label:<28} ok")
            ctx.close()
        browser.close()

    httpd.shutdown()
    print(f"\n{len(VIEWPORTS) - failures}/{len(VIEWPORTS)} viewports pass")
    if shots:
        print(f"screenshots in {SHOTS}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
