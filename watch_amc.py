#!/usr/bin/env python3
"""
AMC Odyssey Watcher
-------------------
Fires an ntfy push the moment IMAX 70MM showtimes for AMC Lincoln Square 13
appear for a target date (default 2026-08-13).

What it DOES:      reads the PUBLIC showtimes page and checks whether the target
                   theatre + format now has any showtimes listed.
What it does NOT:  sign in, select seats, enter payment, or buy anything.
                   The purchase stays 100% manual — the push just gets you there fast.

NOTE: runs a FULL (non-headless) Chromium under Xvfb to avoid headless-browser
detection. The workflow must launch it via `xvfb-run -a python watch_amc.py`.
"""

import os
import re
import sys
import urllib.request

from playwright.sync_api import sync_playwright

# ---------------- Config (override via env) ----------------
TARGET_DATE     = os.environ.get("TARGET_DATE", "2026-08-13")
MOVIE_PATH      = os.environ.get("MOVIE_PATH", "the-odyssey-76238")
THEATRE_NAME    = os.environ.get("THEATRE_NAME", "AMC Lincoln Square 13")
THEATRE_ADDRESS = os.environ.get("THEATRE_ADDRESS", "1998 Broadway")   # disambiguates from "Lincoln, NE", etc.
FORMAT_LABEL    = os.environ.get("FORMAT_LABEL", "IMAX 70MM")
NTFY_TOPIC      = os.environ.get("NTFY_TOPIC")                          # REQUIRED for pushes
NTFY_SERVER     = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
FORCE_TEST_PUSH = os.environ.get("FORCE_TEST_PUSH") == "1"

SHOWTIMES_URL = f"https://www.amctheatres.com/movies/{MOVIE_PATH}/showtimes?date={TARGET_DATE}"
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*[ap]m\b", re.I)
OTHER_FORMATS = ["Dolby Cinema", "Laser at AMC", "IMAX with Laser",
                 "Digital", "RealD", "XL at AMC", "Nearby Theatres"]


def notify(title, message, click_url, priority="urgent", tags="clapper"):
    if not NTFY_TOPIC:
        print("!! NTFY_TOPIC not set — cannot send push.", file=sys.stderr)
        return
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        method="POST",
        headers={"Title": title, "Priority": priority, "Tags": tags, "Click": click_url},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        print(f"ntfy push sent -> HTTP {r.status}")


def dump_diagnostics(page, label="diag"):
    print(f"--- DIAG [{label}] ---")
    try:
        btns = [b for b in page.get_by_role("button").all_inner_texts() if b.strip()]
        print("BUTTONS:", btns[:40])
    except Exception as e:
        print("BUTTONS: <err>", e)
    try:
        inputs = page.locator("input").evaluate_all(
            "els => els.map(e => ({ph: e.placeholder, type: e.type, val: e.value}))")
        print("INPUTS:", inputs[:20])
    except Exception as e:
        print("INPUTS: <err>", e)
    try:
        d = page.locator("dialog[open]")
        print("DIALOG TEXT:", repr(d.first.inner_text()[:2000]) if d.count() else "<no open dialog>")
    except Exception as e:
        print("DIALOG TEXT: <err>", e)
    print("--- END DIAG ---")


def _wait_count(locator, tries=16, gap=500):
    for _ in range(tries):
        try:
            if locator.count():
                return True
        except Exception:
            pass
        locator.page.wait_for_timeout(gap)
    return False


def handle_cookie_consent(page):
    patterns = [
        r"reject all", r"reject non.?essential", r"decline all", r"^decline$",
        r"continue without accepting", r"only necessary", r"necessary cookies only",
        r"accept all cookies", r"accept all", r"i accept", r"^accept$", r"^agree$",
    ]
    waited = 0
    while waited < 5000:
        for pat in patterns:
            btn = page.get_by_role("button", name=re.compile(pat, re.I))
            if btn.count():
                try:
                    btn.first.click(timeout=4000)
                    page.wait_for_timeout(1500)
                    print(f"Cookie consent: clicked '{pat}'")
                    return True
                except Exception:
                    pass
        page.wait_for_timeout(1000)
        waited += 1000
    print("Cookie consent: no banner found (continuing)")
    return False


def select_theatre(page):
    body = page.inner_text("body").lower()
    if THEATRE_NAME.lower() in body and "select a theatre" not in body:
        return

    search = page.locator(
        "dialog[open] input, input[placeholder*='Theatre' i], "
        "input[placeholder*='city' i], input[placeholder*='zip' i]"
    ).first
    try:
        search.wait_for(state="visible", timeout=10000)
    except Exception:
        b = page.get_by_role("button", name=re.compile(r"select a theatre|find a theatre", re.I))
        if b.count():
            try:
                b.first.click(timeout=4000)
            except Exception:
                pass
        search.wait_for(state="visible", timeout=10000)

    # Type with real keystrokes; nudge any combobox list open.
    search.click()
    search.press_sequentially("Lincoln Square", delay=130)
    page.wait_for_timeout(1500)
    try:
        search.press("ArrowDown")
    except Exception:
        pass

    sugg = page.get_by_text(re.compile(r"AMC Lincoln Square 13", re.I))
    if not _wait_count(sugg, tries=18, gap=500):   # up to ~9s
        dump_diagnostics(page, "no-autocomplete")
        return
    sugg.first.click()
    page.wait_for_timeout(3500)

    dlg = page.locator("dialog[open]")
    target = dlg.get_by_text(re.compile(re.escape(THEATRE_ADDRESS), re.I))
    if not target.count():
        target = dlg.get_by_text(re.compile(r"AMC Lincoln Square 13", re.I))
    if target.count():
        target.first.click()
        page.wait_for_timeout(1200)

    cont = dlg.get_by_role("button", name=re.compile(
        r"continue|view showtimes|see showtimes|select a theatre|done|confirm", re.I))
    if cont.count():
        cont.first.click()
    else:
        dump_diagnostics(page, "no-continue")

    page.wait_for_timeout(4500)


def detect(page):
    full = page.inner_text("body")
    if THEATRE_NAME not in full:
        return "ANOMALY", []
    lines = [l.strip() for l in full.splitlines() if l.strip()]
    start = next((i for i, l in enumerate(lines) if THEATRE_NAME in l), None)
    if start is None:
        return "ANOMALY", []
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if "Nearby Theatres" in lines[j] or re.match(r"^AMC .+\d", lines[j]):
            end = j
            break
    section = lines[start:end]
    fmt_idx = next((k for k, l in enumerate(section) if FORMAT_LABEL.upper() in l.upper()), None)
    if fmt_idx is None:
        return "NOT_YET", []
    times = []
    for l in section[fmt_idx + 1:]:
        if any(f in l for f in OTHER_FORMATS) and not TIME_RE.search(l):
            break
        times += TIME_RE.findall(l)
    return ("OPEN", times) if times else ("NOT_YET", [])


def main():
    if FORCE_TEST_PUSH:
        notify("AMC watcher test OK",
               f"Pipeline works. Watching {THEATRE_NAME} / {FORMAT_LABEL} for {TARGET_DATE}.",
               SHOWTIMES_URL, priority="default")
        print("Sent test push; exiting.")
        return

    with sync_playwright() as p:
        # Full Chromium (headless=False) under Xvfb — much harder to fingerprint than headless shell.
        browser = p.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        ctx = browser.new_context(
            viewport={"width": 1366, "height": 1000},
            locale="en-US",
            timezone_id="America/New_York",
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = ctx.new_page()
        page.set_default_timeout(30000)
        try:
            page.goto(SHOWTIMES_URL, wait_until="domcontentloaded")
            handle_cookie_consent(page)
            select_theatre(page)
            page.wait_for_timeout(2000)

            state, times = detect(page)
            print(f"State: {state}   Times: {times}")

            if state == "OPEN":
                shown = ", ".join(times[:6])
                notify("ODYSSEY IMAX 70MM IS OPEN",
                       f"{THEATRE_NAME} — {FORMAT_LABEL} now listed for {TARGET_DATE} "
                       f"({shown}). Open now and grab H22.",
                       SHOWTIMES_URL)
            elif state == "ANOMALY":
                dump_diagnostics(page, "anomaly")
                page.screenshot(path="debug.png", full_page=True)
                print("ANOMALY: theatre not found after selection — see debug.png + DIAG above.",
                      file=sys.stderr)
            else:
                print("Not open yet — nothing to do.")
        except Exception as e:
            try:
                dump_diagnostics(page, "exception")
                page.screenshot(path="debug.png", full_page=True)
                print("Saved debug.png", file=sys.stderr)
            except Exception:
                pass
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
