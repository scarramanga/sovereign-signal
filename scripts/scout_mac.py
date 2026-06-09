#!/usr/bin/env python3
"""Mac-side LinkedIn newsfeed scout for sovereign-signal.

Runs on Andy's Mac via launchd (hourly). Scrapes the LinkedIn newsfeed
(linkedin.com/feed/) with Playwright, POSTs each discovered post to the
sovereign-signal pod for Claude relevance scoring, comment drafting, and email
approval, then posts approved comments back to LinkedIn.

LinkedIn blocks Playwright inside pods, so all scraping/posting happens here on
the Mac; all Claude/Resend/DB work happens server-side via the pod API.
"""

import json
import os
import re
import sys
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

SS_API_URL = os.environ.get("SS_API_URL", "http://localhost:8080")
SS_FEED_URL = os.environ.get("SS_FEED_URL", "https://www.linkedin.com/feed/")
SESSION_FILE = Path.home() / ".sovereign-signal" / "linkedin_session.json"

# How many posts to pull from the feed per run, and how many scroll passes to
# load them. Kept modest to bound Claude scoring cost and LinkedIn load.
MAX_POSTS = 25
SCROLL_PASSES = 8


def load_session() -> tuple[list[dict], str]:
    """Load LinkedIn cookies and user_agent from local JSON file."""
    if not SESSION_FILE.exists():
        print(f"ERROR: LinkedIn session file not found at {SESSION_FILE}")
        print("Run scripts/export_session.sh to export from the pod, or create manually.")
        sys.exit(1)

    data = json.loads(SESSION_FILE.read_text())
    cookies = data["cookies"]
    user_agent = data.get("user_agent", "")
    return cookies, user_agent


def _activity_urn_to_url(urn: str) -> str:
    """Build a canonical permalink from a LinkedIn activity/share URN."""
    return f"https://www.linkedin.com/feed/update/{urn}/"


def scrape_feed(cookies: list[dict], user_agent: str) -> list[dict]:
    """Scrape the LinkedIn newsfeed, returning a list of post dicts.

    Each dict: {post_url, post_author, post_text, engagement}. Selectors are
    best-effort against the current LinkedIn DOM and will need iteration over
    time, exactly like listener_mac.py.
    """
    posts: list[dict] = []
    seen_urns: set[str] = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context()
        context.add_cookies(cookies)

        page = context.new_page()
        page.goto(SS_FEED_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(8000)

        # Scroll to load a batch of feed posts.
        for _ in range(SCROLL_PASSES):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1500)

        # Each feed item carries a data-urn like "urn:li:activity:..."
        containers = page.query_selector_all("div.feed-shared-update-v2[data-urn]")
        if not containers:
            containers = page.query_selector_all("[data-urn*='urn:li:activity']")

        for el in containers:
            if len(posts) >= MAX_POSTS:
                break
            try:
                urn = el.get_attribute("data-urn") or ""
                m = re.search(r"(urn:li:(?:activity|share):\d+)", urn)
                if not m:
                    continue
                urn = m.group(1)
                if urn in seen_urns:
                    continue
                seen_urns.add(urn)

                post_url = _activity_urn_to_url(urn)

                # Author name
                author = ""
                author_el = el.query_selector(
                    ".update-components-actor__title span[aria-hidden='true'], "
                    ".update-components-actor__name span[aria-hidden='true'], "
                    ".update-components-actor__name"
                )
                if author_el:
                    author = (author_el.inner_text() or "").strip()

                # Post body text
                text_val = ""
                text_el = el.query_selector(
                    ".update-components-text, "
                    ".feed-shared-inline-show-more-text, "
                    ".update-components-update-v2__commentary"
                )
                if text_el:
                    text_val = (text_el.inner_text() or "").strip()

                # Skip empty / pure-media posts — nothing to evaluate.
                if not text_val:
                    continue

                # Engagement (captured as metadata only; not a filter)
                engagement = ""
                eng_el = el.query_selector(
                    ".social-details-social-counts__reactions-count, "
                    ".social-details-social-counts"
                )
                if eng_el:
                    engagement = (eng_el.inner_text() or "").strip()

                posts.append(
                    {
                        "post_url": post_url,
                        "post_author": author,
                        "post_text": text_val,
                        "engagement": engagement,
                    }
                )
            except Exception:
                continue

        browser.close()

    return posts


def post_to_pod(post: dict) -> None:
    """POST a single feed post to the sovereign-signal pod for scoring."""
    url = f"{SS_API_URL}/scout/ingest"
    try:
        resp = httpx.post(
            url,
            json={
                "post_url": post["post_url"],
                "post_author": post["post_author"],
                "post_text": post["post_text"],
            },
            timeout=60,
        )
        if resp.status_code == 200:
            print(f"OK: {post['post_author']} — {resp.json()}")
        else:
            print(f"ERROR: POST {url} returned {resp.status_code}: {resp.text}")
    except Exception as exc:
        print(f"ERROR: POST {url} failed: {exc}")


def poll_and_post_comments(cookies: list[dict]) -> None:
    """Poll for approved scout comments and post them as top-level comments."""
    url = f"{SS_API_URL}/approvals/pending-posts"
    try:
        resp = httpx.get(url, params={"source": "scout"}, timeout=30)
        if resp.status_code != 200:
            print(f"ERROR: GET {url} returned {resp.status_code}: {resp.text}")
            return
        pending = resp.json()
    except Exception as exc:
        print(f"ERROR: GET {url} failed: {exc}")
        return

    if not pending:
        print("No pending approved scout comments to post")
        return

    print(f"Found {len(pending)} approved scout comments to post")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context()
        context.add_cookies(cookies)

        for row in pending:
            try:
                post_url = row["post_url"]
                comment_text = row["reply_text"]
                approval_token = row["approval_token"]

                print(f"Posting scout comment on {post_url}")

                page = context.new_page()
                page.goto(post_url, wait_until="domcontentloaded")
                page.wait_for_timeout(5000)

                # Open the top-level comment composer if it is collapsed.
                comment_btn = page.locator(
                    "button[aria-label*='Comment'], button:has-text('Comment')"
                )
                if comment_btn.count() > 0:
                    try:
                        comment_btn.first.click()
                        page.wait_for_timeout(1500)
                    except Exception:
                        pass

                # The first contenteditable editor is the top-level comment box.
                composer = page.locator(
                    "div.ql-editor[contenteditable='true']"
                ).first
                composer.click()
                composer.type(comment_text)
                page.wait_for_timeout(1000)

                # Submit the comment.
                submit_btn = page.locator(
                    "button.comments-comment-box__submit-button, "
                    "button.comments-comment-box__submit-button--cr, "
                    "button:has-text('Comment')"
                )
                if submit_btn.count() > 0:
                    submit_btn.last.click()
                    page.wait_for_timeout(3000)

                    mark_url = f"{SS_API_URL}/approvals/mark-posted"
                    mark_resp = httpx.post(
                        mark_url,
                        json={"approval_token": approval_token},
                        timeout=30,
                    )
                    if mark_resp.status_code == 200:
                        print(f"Posted scout comment on {post_url}")
                    else:
                        print(
                            f"ERROR: mark-posted returned {mark_resp.status_code}: "
                            f"{mark_resp.text}"
                        )
                else:
                    raise Exception("Submit button not found")

            except Exception as exc:
                print(f"ERROR: Failed to post scout comment on {row.get('post_url', '?')}: {exc}")
                continue

        browser.close()


def main() -> None:
    print("sovereign-signal Mac scout starting")

    cookies, user_agent = load_session()
    print(f"Session loaded — {len(cookies)} cookies")

    posts = scrape_feed(cookies, user_agent)
    print(f"Found {len(posts)} feed posts to evaluate")

    for post in posts:
        post_to_pod(post)

    # Post any comments that have already been approved.
    poll_and_post_comments(cookies)

    print("sovereign-signal Mac scout done")


if __name__ == "__main__":
    main()
