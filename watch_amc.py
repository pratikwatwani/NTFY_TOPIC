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
        headers={
            "Title": title,
            "Priority": priority,        # urgent = loud + vibrate on the phone
            "Tags": tags,
            "Click": click_url,          # tapping the notification opens this URL
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        print(f"ntfy push sent -> HTTP {r.status}")


def select_theatre(page):
    """Drive AMC's 'Find a Theatre' popup using visible-text locators (resilient to CSS changes)."""
    body = page.inner_text("body").lower()
    if THEATRE_NAME.lower() in body and "select a theatre" not in body:
        return  # showtimes for our theatre are already rendering

    for label in ("Select a Theatre", "Find a Theatre", "Change Theatre", "Select a nearby theatre"):
        btn = page.get_by_role("button", name=re.compile(label, re.I))
        if btn.count():
            btn.first.click()
            break

    search = page.get_by_placeholder(re.compile("city|zip|name", re.I))
    if not search.count():
        search = page.get_by_role("textbox")
    if search.count():
        search.first.fill(THEATRE_NAME)
        page.wait_for_timeout(2500)

    # Prefer the address match so we never pick "Lincoln, NE", etc.
    opt = page.get_by_text(re.compile(re.escape(THEATRE_ADDRESS), re.I))
    if not opt.count():
        opt = page.get_by_text(re.compile(re.escape(THEATRE_NAME), re.I))
    if opt.count():
        opt.first.click()
        page.wait_for_timeout(1000)

    cont = page.get_by_role("button", name=re.compile("continue|view showtimes|done", re.I))
    if cont.count():
        cont.first.click()
    page.wait_for_timeout(3500)


def detect(page):
    """
    Returns one of:
      ("OPEN", [times])   target theatre + format has showtimes  -> ALERT
      ("NOT_YET", [])     theatre visible but format absent       (expected pre-open)
      ("ANOMALY", [])     theatre not found at all                (possible IP block / selector drift)
    """
    full = page.inner_text("body")
    if THEATRE_NAME not in full:
        return "ANOMALY", []

    lines = [l.strip() for l in full.splitlines() if l.strip()]
    start = next((i for i, l in enumerate(lines) if THEATRE_NAME in l), None)
    if start is None:
        return "ANOMALY", []

    # Scope to this theatre's block: stop at "Nearby Theatres" or the next "AMC ...<number>" header.
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if "Nearby Theatres" in lines[j] or re.match(r"^AMC .+\d", lines[j]):
            end = j
            break
    section = lines[start:end]

    fmt_idx = next((k for k, l in enumerate(section) if FORMAT_LABEL.upper() in l.upper()), None)
    if fmt_idx is None:
        return "NOT_YET", []

    # Collect the showtimes listed under the IMAX 70MM block.
    times = []
    for l in section[fmt_idx + 1:]:
        if any(f in l for f in OTHER_FORMATS) and not TIME_RE.search(l):
            break
        times += TIME_RE.findall(l)
    return ("OPEN", times) if times else ("NOT_YET", [])


def main():
    # Manual end-to-end pipeline test: send a push and exit (no scraping).
    if FORCE_TEST_PUSH:
        notify("AMC watcher test OK",
               f"Pipeline works. Watching {THEATRE_NAME} / {FORMAT_LABEL} for {TARGET_DATE}.",
               SHOWTIMES_URL, priority="default")
        print("Sent test push; exiting.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 2000},
            locale="en-US",
        )
        page = ctx.new_page()
        page.set_default_timeout(30000)
        try:
            page.goto(SHOWTIMES_URL, wait_until="domcontentloaded")
            select_theatre(page)
            page.wait_for_timeout(4000)        # let client-side showtimes settle

            state, times = detect(page)
            print(f"State: {state}   Times: {times}")

            if state == "OPEN":
                shown = ", ".join(times[:6])
                notify(
                    "ODYSSEY IMAX 70MM IS OPEN",
                    f"{THEATRE_NAME} — {FORMAT_LABEL} now listed for {TARGET_DATE} "
                    f"({shown}). Open now and grab H22.",
                    SHOWTIMES_URL,
                )
            elif state == "ANOMALY":
                page.screenshot(path="debug.png", full_page=True)
                print("ANOMALY: theatre not found after selection — possible datacenter-IP "
                      "block or popup-selector change. See debug.png.", file=sys.stderr)
            else:
                print("Not open yet — nothing to do.")
        except Exception as e:
            try:
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
