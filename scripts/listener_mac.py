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
from playwright.sync_api import Error as PlaywrightError, sync_playwright

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


def safe_goto(page, url: str, ready_selector: str | None = None) -> bool:
    """Navigate to a LinkedIn URL, tolerating SPA client-side redirects.

    LinkedIn often starts its own navigation before Playwright's goto commits,
    which aborts the request (net::ERR_ABORTED / "frame was detached"). That is
    not a real failure — the document is usually fine once it settles, so we
    swallow the abort and continue. Any other error is re-raised. Returns True
    if the page looks usable (ready_selector appeared, or none was required).
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightError as exc:
        msg = str(exc)
        if "ERR_ABORTED" in msg or "frame was detached" in msg:
            print(f"WARN: goto {url} aborted by SPA redirect, continuing")
            page.wait_for_timeout(3000)
        else:
            raise
    if ready_selector:
        try:
            page.wait_for_selector(ready_selector, timeout=15000)
        except PlaywrightError:
            return False
    return True


def _normalize(s: str) -> str:
    """Collapse whitespace for robust text comparison."""
    return " ".join((s or "").split())


def find_reply_submit(composer):
    """Locate the reply composer's submit button.

    Deliberately avoids the comments-comment-box__submit-button class, which
    was broken in PR #44. Instead it scopes to the composer's enclosing form
    (or comment-box container) and matches the primary action button by
    accessible name — Reply / Comment / Post — which excludes 'Repost' and the
    page-level 'Start a post'. Selector precision here is only best-effort; the
    real safety net is reply_is_posted() verification after the click.
    """
    name_re = re.compile(r"\b(Reply|Comment|Post)\b", re.IGNORECASE)
    for scope_xpath in (
        "xpath=ancestor::form[1]",
        "xpath=ancestor::*[contains(@class, 'comment-box')][1]",
    ):
        box = composer.locator(scope_xpath)
        if box.count() == 0:
            continue
        btn = box.get_by_role("button", name=name_re)
        if btn.count() > 0:
            return btn.last
    return None


def reply_is_posted(page, thread, reply_text: str) -> bool:
    """Verify the reply actually rendered as a comment — the real safety net.

    The composer is a .ql-editor; posted comments render in
    .comments-comment-item__main-content. Finding our text in a rendered
    comment node (not the composer) means the submit genuinely landed. Checks
    the commenter's thread subtree first, then the whole page, since the reply
    can render in a sibling list node.
    """
    target = _normalize(reply_text)
    if not target:
        return False
    selector = "span.comments-comment-item__main-content"
    for scope in (thread, page):
        try:
            items = scope.locator(selector)
            count = items.count()
        except PlaywrightError:
            continue
        for i in range(count):
            try:
                if target in _normalize(items.nth(i).inner_text()):
                    return True
            except PlaywrightError:
                continue
    return False


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
        safe_goto(page, SS_LINKEDIN_PROFILE, ready_selector="a[href*='/feed/update/']")
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
                safe_goto(page, post_url)
                page.wait_for_timeout(5000)

                # Switch sort order to "Most recent" if the control exists
                try:
                    sort_btn = page.locator(
                        "button.comments-sort-order-toggle, "
                        "button[aria-label*='Sort'], "
                        "button:has-text('Most relevant')"
                    )
                    if sort_btn.count() > 0:
                        sort_btn.first.click()
                        page.wait_for_timeout(1000)
                        # Pick "Most recent" from the dropdown
                        recent_opt = page.locator(
                            "li:has-text('Most recent'), "
                            "div[role='option']:has-text('Most recent'), "
                            "button:has-text('Most recent')"
                        )
                        if recent_opt.count() > 0:
                            recent_opt.first.click()
                            page.wait_for_timeout(2000)
                except Exception:
                    pass

                # Click "Load more comments" repeatedly until all are visible
                for _ in range(20):
                    try:
                        load_more = page.locator(
                            "button.comments-comments-list__load-more-comments-button, "
                            "button:has-text('Load more comments')"
                        )
                        if load_more.count() > 0 and load_more.first.is_visible():
                            load_more.first.click()
                            page.wait_for_timeout(1500)
                        else:
                            break
                    except Exception:
                        break

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

                # Extract comment texts via Playwright, look up name by profile slug
                comment_elements = page.query_selector_all(
                    ".comments-thread-item"
                )
                # Also collect from broader selector to catch comments
                # outside standard thread-item containers
                extra_elements = page.query_selector_all(
                    "article.comments-comment-entity"
                )
                # Deduplicate by profile slug extracted from avatar link
                seen_slugs: set[str] = set()
                merged: list = []
                for el in list(comment_elements) + list(extra_elements):
                    try:
                        a_link = el.query_selector(
                            "a.comments-comment-meta__image-link"
                        )
                        if not a_link:
                            a_link = el.query_selector("a[href*='/in/']")
                        if a_link:
                            h = a_link.get_attribute("href") or ""
                            sm = re.search(r"(/in/[^?\"]+)", h)
                            slug_key = sm.group(1).rstrip("/") if sm else ""
                        else:
                            slug_key = ""
                    except Exception:
                        slug_key = ""
                    # Keep elements with no slug (don't silently discard)
                    if not slug_key or slug_key not in seen_slugs:
                        if slug_key:
                            seen_slugs.add(slug_key)
                        merged.append(el)
                comment_elements = merged
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

                        # Filter out Andy's own comments
                        if commenter_name == "Andy Boss":
                            continue

                        # Skip unresolved commenters
                        if commenter_name == "Unknown":
                            continue

                        text_el = cel.query_selector(
                            "span.comments-comment-item__main-content"
                        )
                        comment_text = (
                            text_el.inner_text().strip() if text_el else ""
                        )

                        # Skip Andy's replies (they start with the commenter's name)
                        if comment_text.startswith(commenter_name):
                            continue

                        if comment_text:
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


def poll_and_post(cookies: list[dict]) -> None:
    """Poll for approved replies and post them to LinkedIn via Playwright."""
    url = f"{SS_API_URL}/approvals/pending-posts"
    try:
        resp = httpx.get(url, timeout=30)
        if resp.status_code != 200:
            print(f"ERROR: GET {url} returned {resp.status_code}: {resp.text}")
            return
        pending = resp.json()
    except Exception as exc:
        print(f"ERROR: GET {url} failed: {exc}")
        return

    if not pending:
        print("No pending approved replies to post")
        return

    print(f"Found {len(pending)} approved replies to post")

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
                reply_text = row["reply_text"]
                commenter_name = row["commenter_name"]
                approval_token = row["approval_token"]

                print(f"Posting reply to {commenter_name} on {post_url}")

                page = context.new_page()
                if not safe_goto(
                    page, post_url, ready_selector="article.comments-comment-entity"
                ):
                    raise Exception(f"comments did not load for {post_url}")
                page.wait_for_timeout(5000)

                # Load all comments before searching for commenter
                for _ in range(20):
                    btn = page.locator("button:has-text('Load more comments')")
                    if btn.count() > 0:
                        btn.first.click()
                        page.wait_for_timeout(1500)
                    else:
                        break

                # Find the comment by commenter name
                name_el = page.locator(
                    f"span:has-text('{commenter_name}')"
                ).first
                # Scroll to the commenter's element
                name_el.scroll_into_view_if_needed()
                page.wait_for_timeout(1000)

                # Find the Reply button within the comment thread
                # Navigate up to the thread container, then find Reply
                thread = name_el.locator(
                    "xpath=ancestor::article[contains(@class, 'comments-comment-entity')]"
                )
                reply_btn = thread.locator("button:has-text('Reply')")
                reply_btn.first.click()

                page.wait_for_timeout(2000)

                # Type reply into the composer. Prefer the reply box inside the
                # commenter's thread; fall back to the last editor on the page.
                composer = thread.locator("div.ql-editor[contenteditable='true']")
                if composer.count() == 0:
                    composer = page.locator("div.ql-editor[contenteditable='true']")
                composer = composer.last
                composer.click()
                composer.type(reply_text)
                page.wait_for_timeout(1000)

                # Submit. The selector is best-effort (see find_reply_submit);
                # the verification below is what actually gates success.
                submit_btn = find_reply_submit(composer)
                if submit_btn is None:
                    raise Exception("reply submit button not found")
                submit_btn.click()
                page.wait_for_timeout(4000)

                # Real safety net: only report success if the reply actually
                # rendered as a comment. Otherwise raise so it is logged as an
                # error, left unmarked, and retried on the next run.
                if not reply_is_posted(page, thread, reply_text):
                    raise Exception(
                        f"submit did not produce a posted comment for {commenter_name}"
                    )

                # Confirmed posted — mark it so it is not retried.
                mark_url = f"{SS_API_URL}/approvals/mark-posted"
                mark_resp = httpx.post(
                    mark_url,
                    json={"approval_token": approval_token},
                    timeout=30,
                )
                if mark_resp.status_code == 200:
                    print(f"Posted reply to {commenter_name}")
                else:
                    print(
                        f"ERROR: mark-posted returned {mark_resp.status_code}: "
                        f"{mark_resp.text}"
                    )

            except Exception as exc:
                print(f"ERROR: Failed to post reply to {row.get('commenter_name', '?')}: {exc}")
                continue

        browser.close()


def main() -> None:
    print("sovereign-signal Mac listener starting")

    cookies, user_agent = load_session()
    print(f"Session loaded — {len(cookies)} cookies")

    comments = scrape_posts_and_comments(cookies, user_agent)
    print(f"Found {len(comments)} comments across posts")

    for comment in comments:
        post_comment_to_pod(comment)

    # Poll for approved replies and post them
    poll_and_post(cookies)

    print("sovereign-signal Mac listener done")


if __name__ == "__main__":
    main()
