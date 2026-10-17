#!/usr/bin/env python3
"""
page_smoke.py — open every page and fail if any of them is broken.

Why this exists
---------------
The Search page rendered blank on every load: a dependency array read a variable
before its `const`, so React threw "Cannot access 'lens' before initialization"
and, with no error boundary anywhere in the app, all that appeared was a white
screen. No build error. No lint error. No failing test. It was found by accident,
while verifying something else entirely.

The whole class of failure — a component that throws while drawing — is invisible
until a person opens the page and notices. This makes it visible before that.

What counts as broken
---------------------
Three things, because they fail differently:

  * an **uncaught exception** in the page — a crash React did not handle
  * the **error boundary** showing — React DID catch it; we look for the
    `data-page-error` marker it leaves behind
  * a **blank render** — a near-empty page, which is how every version of the bug
    above actually presented

A page that fails and then succeeds on reload is reported separately from one
that fails twice. The second is a real bug. The first is usually the backend
briefly being unreachable, and calling that a defect trains you to ignore this
script.

Usage
-----
    # both servers already running
    python3 scripts/page_smoke.py

    # point it somewhere else
    python3 scripts/page_smoke.py --base http://localhost:3001

    # list the routes it checks
    python3 scripts/page_smoke.py --list

It deliberately does NOT start the servers or run in the regression suite. That
suite is fast and hermetic — 309 checks, no browser, no servers — and dragging a
Next dev server into it would slow every run and make it flaky. This is a
standalone check to run after touching the frontend.
"""

import argparse
import sys

PAGES = [
    ("/", "Home"),
    ("/config", "Search"),
    ("/about", "About you"),
    ("/looking-for", "What you're looking for"),
    ("/runs", "All runs"),
    ("/verdicts", "Decisions"),
]

# Under this many characters of visible text, a page is treated as blank. The
# quietest real page here renders well over a thousand; a crashed one renders
# nothing at all, so the threshold only has to sit in the wide gap between them.
BLANK_THRESHOLD = 40


async def check_page(browser, base, path, label, retry=True):
    """Load one page. Returns (ok, detail)."""
    page = await browser.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e).splitlines()[0][:200]))
    try:
        await page.goto(f"{base}{path}", wait_until="networkidle", timeout=25_000)
        await page.wait_for_timeout(1_200)
        text = (await page.inner_text("body")).strip()
        boundary = await page.locator("[data-page-error]").count() > 0
    except Exception as exc:  # noqa: BLE001 - a load failure is a result, not a crash
        await page.close()
        return False, f"could not load: {str(exc).splitlines()[0][:120]}"
    finally:
        try:
            await page.close()
        except Exception:
            pass

    if boundary:
        return False, "showed the error boundary" + (
            f" ({errors[0][:100]})" if errors else ""
        )
    if errors:
        return False, f"uncaught exception: {errors[0][:140]}"
    if len(text) < BLANK_THRESHOLD:
        return False, f"blank ({len(text)} chars of text)"
    return True, f"{len(text):>5} chars"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://localhost:3000",
                    help="frontend base URL (default: http://localhost:3000)")
    ap.add_argument("--list", action="store_true", help="list the routes and exit")
    args = ap.parse_args()

    if args.list:
        for path, label in PAGES:
            print(f"  {path:<14} {label}")
        return 0

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Playwright is not installed. Run: pip install playwright && playwright install chromium")
        return 2

    import asyncio

    async def run() -> int:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            print(f"\n  Page smoke — {args.base}\n")
            print(f"  {'route':<14} {'result':<9} detail")
            print("  " + "-" * 62)
            failures = []
            for path, label in PAGES:
                ok, detail = await check_page(browser, args.base, path, label)
                print(f"  {path:<14} {'OK' if ok else 'BROKEN':<9} {detail}")
                if not ok:
                    # Reload once: a failure that clears is the backend blinking,
                    # not a defect. One that does not clear is real.
                    ok2, detail2 = await check_page(browser, args.base, path, label)
                    if ok2:
                        print(f"  {'':<14} {'retry':<9} cleared on reload: {detail2}")
                    else:
                        print(f"  {'':<14} {'retry':<9} still broken")
                        failures.append((path, label, detail2))
            await browser.close()
            return failures

    try:
        failures = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        print(f"\n  Could not reach {args.base} — is the frontend running?\n  {exc}\n")
        return 2

    print()
    if failures:
        print(f"  {len(failures)} page(s) broken:")
        for path, label, detail in failures:
            print(f"    {path} ({label}) — {detail}")
        print("\n  A blank page is usually a component throwing while it renders.")
        print("  The error is shown on the page itself; it is also in the `next dev`\n"
              "  terminal. Check there first — it names the file and line.\n")
        return 1
    print(f"  All {len(PAGES)} pages render.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())