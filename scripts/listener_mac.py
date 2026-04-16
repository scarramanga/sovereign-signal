#!/usr/bin/env python3
"""Mac-side LinkedIn scraper for sovereign-signal.

Runs on Andy's Mac via launchd every 15 minutes. Scrapes LinkedIn posts
and comments using Playwright, then POSTs discovered comments to the
sovereign-signal pod for Claude drafting and email approval.
"""

import json
import os
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup
import httpx
from playwright.sync_api import sync_playwright

SS_API_URL = os.environ.get("SS_API_URL", "http://localhost:8080")
SS_LINKEDIN_PROFILE = os.environ.get(
    "SS_LINKEDIN_PROFILE",
    "https://www.linkedin.com/in/andy-boss-b89856/",
)
SESSION_FILE = Path.home() / ".sovereign-signal" / "linkedin_session.json"


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


def scrape_posts_and_comments(cookies: list[dict], user_agent: str) -> list[dict]:
    """Scrape Andy's recent LinkedIn posts and their comments using Playwright."""
    comments_found: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context()
        context.add_cookies(cookies)

        page = context.new_page()
        page.goto(SS_LINKEDIN_PROFILE, wait_until="domcontentloaded")
        page.wait_for_timeout(8000)

        # Collect post links from the activity feed (up to 5)
        post_elements = page.query_selector_all("a[href*='/feed/update/']")
        post_urls: list[str] = []
        seen_urls: set[str] = set()
        for el in post_elements:
            href = el.get_attribute("href")
            if href and "/feed/update/" in href:
                clean = href.split("?")[0]
                if clean not in seen_urls:
                    seen_urls.add(clean)
                    if not clean.startswith("http"):
                        clean = f"https://www.linkedin.com{clean}"
                    post_urls.append(clean)
            if len(post_urls) >= 5:
                break

        print(f"Post URLs found: {post_urls}")

        # Visit each post and scrape comments
        for post_url in post_urls:
            try:
                page.goto(post_url, wait_until="domcontentloaded")
                page.wait_for_timeout(5000)

                # Click "Load more comments" if visible
                try:
                    load_more = page.locator("button:has-text('Load more comments')")
                    if load_more.count() > 0:
                        load_more.first.click()
                        page.wait_for_timeout(1500)
                except Exception:
                    pass

                # Capture raw HTML and build name lookup by profile slug
                html = page.content()

                soup = BeautifulSoup(html, "html.parser")
                name_map: dict[str, str] = {}
                for a in soup.find_all(
                    "a",
                    class_=lambda c: c and "comments-comment-meta__image-link" in c,
                ):
                    href = a.get("href", "")
                    aria = a.get("aria-label", "")
                    slug_m = re.search(r"(/in/[^?\"]+)", href)
                    if slug_m and aria.startswith("View "):
                        name_part = aria[5:]
                        for sep in ["\u2019", "'", "\u2018"]:
                            if sep in name_part:
                                name_part = name_part.split(sep)[0]
                                break
                        name_map[slug_m.group(1).rstrip("/")] = name_part.strip()
                print(f"DEBUG names_from_html: {name_map}")

                # Extract comment texts via Playwright, look up name by profile slug
                comment_elements = page.query_selector_all(
                    ".comments-thread-item"
                )
                for cel in comment_elements:
                    try:
                        commenter_name = "Unknown"
                        # Try the avatar image link first (same element BS4 uses)
                        a_el = cel.query_selector(
                            "a.comments-comment-meta__image-link"
                        )
                        if not a_el:
                            a_el = cel.query_selector("a[href*='/in/']")
                        if a_el:
                            href = a_el.get_attribute("href") or ""
                            slug_match = re.search(r"(/in/[^?\"]+)", href)
                            slug = slug_match.group(1).rstrip("/") if slug_match else ""
                            commenter_name = name_map.get(slug, "Unknown")
                            print(
                                f"DEBUG slug={slug!r}, "
                                f"name={name_map.get(slug, 'MISSING')}, "
                                f"href={href[:80]!r}"
                            )

                        # Filter out Andy's own comments
                        if commenter_name == "Andy Boss":
                            continue

                        text_el = cel.query_selector(
                            "span.comments-comment-item__main-content"
                        )
                        comment_text = (
                            text_el.inner_text().strip() if text_el else ""
                        )
                        if comment_text:
                            print(f"DEBUG comment_text={comment_text[:120]!r}")
                            comments_found.append(
                                {
                                    "post_url": post_url,
                                    "commenter_name": commenter_name,
                                    "comment_text": comment_text,
                                }
                            )
                    except Exception:
                        continue

            except Exception as exc:
                print(f"ERROR: Failed to scrape post {post_url}: {exc}")
                continue

        browser.close()

    return comments_found


def post_comment_to_pod(comment: dict) -> None:
    """POST a single comment to the sovereign-signal pod for processing."""
    url = f"{SS_API_URL}/listener/ingest"
    try:
        resp = httpx.post(url, json=comment, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            print(f"OK: {comment['commenter_name']} — {data}")
        else:
            print(f"ERROR: POST {url} returned {resp.status_code}: {resp.text}")
    except Exception as exc:
        print(f"ERROR: POST {url} failed: {exc}")


def main() -> None:
    print("sovereign-signal Mac listener starting")

    cookies, user_agent = load_session()
    print(f"Session loaded — {len(cookies)} cookies")

    comments = scrape_posts_and_comments(cookies, user_agent)
    print(f"Found {len(comments)} comments across posts")

    for comment in comments:
        post_comment_to_pod(comment)

    print("sovereign-signal Mac listener done")


if __name__ == "__main__":
    main()
