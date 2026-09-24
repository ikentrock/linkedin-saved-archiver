#!/usr/bin/env python3
"""
LinkedIn Saved Posts archiver.

Reads your LinkedIn "Saved posts" list, opens each post to capture the full
content, appends it to a Google Sheet (via an Apps Script web app) and — only
after the sheet confirms the row exists — unsaves the post on LinkedIn.

Usage:
  python archiver.py --dry-run --limit 3     # extract & print only, touches nothing
  python archiver.py --no-unsave --limit 10  # archive to sheet, keep posts saved
  python archiver.py --limit 25              # archive + unsave (default mode)
"""

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
PROFILE_DIR = Path.home() / ".linkedin-archiver" / "browser-profile"
STATE_PATH = HERE / "state.json"
DEBUG_DIR = HERE / "debug"

SAVED_URL = "https://www.linkedin.com/my-items/saved-posts/"
POST_URL = "https://www.linkedin.com/feed/update/{urn}/"

# Menu labels for "Unsave" in English and Spanish LinkedIn UIs (config can add more).
UNSAVE_LABELS = ["unsave", "dejar de guardar", "no guardar", "eliminar de guardad", "quitar de guardad"]
URN_RE = re.compile(r"urn:li:(activity|ugcPost|share):(\d+)")


# ---------------------------------------------------------------- helpers

def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def pause(cfg, factor=1.0):
    lo, hi = cfg.get("delay_seconds", [3, 7])
    time.sleep(random.uniform(lo, hi) * factor)


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def activity_date(post_id):
    """LinkedIn IDs embed a millisecond timestamp in their first 41 bits."""
    try:
        ms = int(post_id) >> 22
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        if 2003 <= dt.year <= datetime.now().year + 1:
            return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OverflowError, OSError):
        pass
    return ""


def dump_debug(page, name):
    DEBUG_DIR.mkdir(exist_ok=True)
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{name}.png"), full_page=True)
        (DEBUG_DIR / f"{name}.html").write_text(page.content())
        log(f"  debug snapshot saved to debug/{name}.*")
    except Exception as e:  # noqa: BLE001
        log(f"  could not save debug snapshot: {e}")


# ---------------------------------------------------------------- sheet API

class Sheet:
    def __init__(self, cfg):
        self.url = cfg["webapp_url"]
        self.token = cfg["token"]

    def call(self, payload):
        payload = {"token": self.token, **payload}
        last_err = None
        for attempt in range(4):
            if attempt:
                time.sleep(2 * attempt)
            r = requests.post(self.url, data=json.dumps(payload),
                              headers={"Content-Type": "text/plain;charset=utf-8"},
                              timeout=60, allow_redirects=True)
            try:
                r.raise_for_status()
                data = r.json()
            except (requests.exceptions.RequestException, ValueError) as e:
                last_err = e
                log(f"  (sheet call failed, retrying: {e})")
                continue
            if not data.get("ok"):
                raise RuntimeError(f"Sheet error: {data.get('error')}")
            return data
        raise RuntimeError(f"Sheet unreachable after retries: {last_err}")

    def known_ids(self):
        return set(self.call({"action": "known_ids"})["ids"])

    def append(self, post):
        res = self.call({"action": "append", "posts": [post]})["results"][0]
        return res["status"]

    def set_status(self, post_id, status):
        try:
            self.call({"action": "set_status", "post_id": post_id, "status": status})
        except Exception as e:  # noqa: BLE001
            log(f"  (could not update status in sheet: {e})")


# ---------------------------------------------------------------- LinkedIn

def ensure_logged_in(page):
    page.goto(SAVED_URL, wait_until="domcontentloaded")
    if "/login" not in page.url and "my-items" in page.url:
        return
    log("Not logged in. Please log in to LinkedIn in the opened browser window "
        "(you have 5 minutes). Your session is kept for next runs.")
    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(3)
        if "/login" not in page.url and ("/feed" in page.url or "my-items" in page.url):
            page.goto(SAVED_URL, wait_until="domcontentloaded")
            if "/login" not in page.url and "my-items" in page.url:
                return
    sys.exit("Login not completed in time.")


def collect_saved(page, cfg, want):
    """Scroll the saved-posts list and return [(post_id, urn)] in page order."""
    found = {}
    stale_rounds = 0
    while len(found) < want and stale_rounds < 4:
        hrefs = page.eval_on_selector_all(
            "main a[href*='urn:li:']", "els => els.map(e => e.href)")
        before = len(found)
        for h in hrefs:
            m = URN_RE.search(h)
            if m and m.group(2) not in found:
                found[m.group(2)] = f"urn:li:{m.group(1)}:{m.group(2)}"
        stale_rounds = stale_rounds + 1 if len(found) == before else 0
        # Load more: "Show more results" button or infinite scroll.
        btn = page.locator("button:has-text('Show more results'), "
                           "button:has-text('Mostrar más resultados')")
        if btn.count():
            try:
                btn.first.click(timeout=3000)
            except PWTimeout:
                pass
        page.mouse.wheel(0, 4000)
        pause(cfg, 0.4)
    return list(found.items())[:want]


EXTRACT_JS = r"""
() => {
  // LinkedIn (2026 redesign) ships fully hashed/obfuscated CSS class names, so
  // selectors here key off stable attributes (data-testid, href patterns, role)
  // and DOM order instead of class names.
  const textEl = document.querySelector('[data-testid="expandable-text-box"]');
  const root = (textEl && textEl.closest('[role="listitem"]')) || document.querySelector('main') || document.body;
  if (!root) return null;

  const clean = u => { try { const x = new URL(u); if (x.hostname.endsWith('linkedin.com') && (x.pathname.startsWith('/in/') || x.pathname.startsWith('/company/'))) return x.origin + x.pathname; return u; } catch(e) { return u; } };
  const unwrapSafety = u => {
    try {
      const x = new URL(u);
      if (x.hostname.endsWith('linkedin.com') && x.pathname.startsWith('/safety/go/')) {
        const real = x.searchParams.get('url');
        if (real) return decodeURIComponent(real);
      }
    } catch (e) {}
    return u;
  };

  // The actor (author) block is the first /in/ or /company/ link that wraps a
  // <p> (name); other matching links are avatar-only or unrelated.
  const candidateLinks = Array.from(root.querySelectorAll('a[href*="linkedin.com/in/"], a[href*="linkedin.com/company/"]'));
  const authorLink = candidateLinks.find(a => a.querySelectorAll('p').length > 0) || candidateLinks[0] || null;

  let author = '';
  if (authorLink) {
    const ps = authorLink.querySelectorAll('p');
    if (ps.length) author = ps[0].innerText.trim();
  }

  // Headline and posted-time are the next two <p> elements after the actor
  // block and before the post text, in DOM order.
  const followingPs = Array.from(root.querySelectorAll('p')).filter(p =>
    (!authorLink || !authorLink.contains(p)) && (!textEl || (!textEl.contains(p) && p !== textEl))
  );
  const afterAuthor = authorLink
    ? followingPs.filter(p => authorLink.compareDocumentPosition(p) & Node.DOCUMENT_POSITION_FOLLOWING)
    : followingPs;
  const beforeText = textEl
    ? afterAuthor.filter(p => p.compareDocumentPosition(textEl) & Node.DOCUMENT_POSITION_FOLLOWING)
    : afterAuthor;
  const headline = beforeText.length >= 1 ? beforeText[0].innerText.trim() : '';
  const posted_relative = beforeText.length >= 2 ? beforeText[1].innerText.trim().split('•')[0].trim() : '';

  const links = new Set();
  root.querySelectorAll('a[href]').forEach(a => {
    const h = a.href;
    if (!h || h.startsWith('javascript')) return;
    if (authorLink && authorLink.contains(a)) return;
    if (/linkedin\.com\/(feed\/hashtag|in\/|company\/|search)/.test(h)) return;
    if (/linkedin\.com\/(feed\/update|posts)\//.test(h)) { links.add(h.split('?')[0]); return; }
    links.add(unwrapSafety(h));
  });

  const media = new Set();
  root.querySelectorAll('img, video').forEach(m => {
    if (authorLink && authorLink.contains(m)) return;
    const s = m.currentSrc || m.src || m.getAttribute('data-src') || '';
    if (!s || s.startsWith('blob:') || s.includes('profile-displayphoto')) return;
    media.add(s.split('?')[0]);
  });

  return {
    author,
    author_headline: headline,
    author_url: authorLink ? clean(authorLink.href) : '',
    posted_relative,
    text: textEl ? textEl.innerText.trim() : '',
    links: [...links].slice(0, 30),
    media: [...media].slice(0, 30),
  };
}
"""


def expand_text(page):
    for sel in ["button.feed-shared-inline-show-more-text__see-more-less-toggle",
                "button:has-text('…more')", "button:has-text('...more')",
                "button:has-text('see more')", "button:has-text('ver más')",
                "button:has-text('…más')"]:
        loc = page.locator(sel)
        if loc.count():
            try:
                loc.first.click(timeout=2000)
                page.wait_for_timeout(500)
                return
            except PWTimeout:
                continue


def extract_post(page, urn, post_id):
    url = POST_URL.format(urn=urn)
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_selector('[data-testid="expandable-text-box"], [role="listitem"]',
                               timeout=15000)
    except PWTimeout:
        return None, url
    page.wait_for_timeout(1500)
    expand_text(page)
    data = page.evaluate(EXTRACT_JS)
    if not data:
        return None, url
    data.update({
        "post_id": post_id,
        "post_url": url,
        "posted_date": activity_date(post_id),
        "archived_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    return data, url


def unsave_post(page, extra_labels):
    """Open the post's '...' control menu and click Unsave. Returns True on success."""
    labels = [s.lower() for s in UNSAVE_LABELS + extra_labels]
    trigger = page.locator(
        "button.feed-shared-control-menu__trigger, "
        "button[aria-label*='control menu' i], button[aria-label*='menú de control' i], "
        "button[aria-label*='Open control menu' i], button[aria-label*='More actions' i]").first
    try:
        trigger.click(timeout=5000)
    except PWTimeout:
        return False
    page.wait_for_timeout(800)
    items = page.locator(".artdeco-dropdown__content [role='button'], "
                         ".artdeco-dropdown__content li, [role='menuitem']")
    for i in range(items.count()):
        item = items.nth(i)
        try:
            t = (item.inner_text(timeout=1000) or "").strip().lower()
        except PWTimeout:
            continue
        if any(t.startswith(lbl) or lbl in t.split("\n")[0] for lbl in labels):
            item.click(timeout=3000)
            page.wait_for_timeout(1200)
            return True
    page.keyboard.press("Escape")
    return False


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None, help="max posts this run (default from config)")
    ap.add_argument("--dry-run", action="store_true", help="extract and print only; no sheet, no unsave")
    ap.add_argument("--no-unsave", action="store_true", help="write to sheet but keep posts saved")
    ap.add_argument("--debug", action="store_true", help="save screenshots/HTML when something fails")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        sys.exit("config.json missing — copy config.example.json to config.json and fill it in.")
    limit = args.limit or cfg.get("max_posts_per_run", 25)
    do_unsave = cfg.get("unsave", True) and not args.no_unsave and not args.dry_run
    extra_labels = cfg.get("extra_unsave_labels", [])

    sheet = None if args.dry_run else Sheet(cfg)
    known = set()
    if sheet:
        known = sheet.known_ids()
        log(f"Sheet reachable. {len(known)} posts already archived.")

    state = load_json(STATE_PATH, {"unsaved": [], "failed": {}})
    counts = {"archived": 0, "duplicate": 0, "unsaved": 0, "skipped": 0}

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, channel=cfg.get("browser_channel") or None,
            viewport={"width": 1280, "height": 900}, locale=cfg.get("locale", "en-US"))
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ensure_logged_in(page)
        page.wait_for_timeout(2500)

        # Ask for a few extra in case some fail or were unsaved earlier.
        items = collect_saved(page, cfg, limit + 10)
        items = [(pid, urn) for pid, urn in items if pid not in state["unsaved"]][:limit]
        log(f"Found {len(items)} saved posts to process.")
        if not items and args.debug:
            dump_debug(page, "saved_list")

        post_page = ctx.new_page()
        for n, (pid, urn) in enumerate(items, 1):
            log(f"[{n}/{len(items)}] {urn}")
            try:
                data, url = extract_post(post_page, urn, pid)
            except Exception as e:  # noqa: BLE001
                data, url = None, POST_URL.format(urn=urn)
                log(f"  extraction error: {e}")

            if not data or not (data.get("text") or data.get("author")):
                log("  could not read post content — skipping (left saved).")
                counts["skipped"] += 1
                state["failed"][pid] = "extract"
                if args.debug:
                    dump_debug(post_page, f"post_{pid}")
                continue

            if args.dry_run:
                print(json.dumps(data, ensure_ascii=False, indent=2))
                pause(cfg)
                continue

            status = "duplicate" if pid in known else sheet.append(data)
            if status not in ("appended", "duplicate"):
                log(f"  sheet did not confirm the row ({status}) — leaving post saved.")
                counts["skipped"] += 1
                state["failed"][pid] = status
                continue
            counts["archived" if status == "appended" else "duplicate"] += 1
            log(f"  {status} in sheet: {data['author']} — {data['text'][:60]!r}")

            if do_unsave:
                if unsave_post(post_page, extra_labels):
                    counts["unsaved"] += 1
                    state["unsaved"].append(pid)
                    state["failed"].pop(pid, None)
                    sheet.set_status(pid, "Unsaved on LinkedIn")
                    log("  unsaved on LinkedIn.")
                else:
                    log("  could not find the Unsave option — left saved.")
                    state["failed"][pid] = "unsave"
                    if args.debug:
                        dump_debug(post_page, f"unsave_{pid}")

            STATE_PATH.write_text(json.dumps(state, indent=1))
            pause(cfg)

        ctx.close()

    log("Done. " + ", ".join(f"{k}: {v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
