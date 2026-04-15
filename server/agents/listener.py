"""Listener agent — watches LinkedIn comments, drafts replies via Claude, sends approval emails."""

import asyncio
import hashlib
import json
import logging
import os
import uuid
from pathlib import Path

import anthropic
import resend
from playwright.async_api import async_playwright
from sqlalchemy import text

from server.config import settings
from server.database import AsyncSessionLocal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

VOICE_AND_TONE = (Path(__file__).parent.parent / "content" / "voice_and_tone.md").read_text()

LINKEDIN_PROFILE_ACTIVITY_URL = "https://www.linkedin.com/in/andy-boss-b89856/recent-activity/all/"
BASE_URL = os.environ.get("SS_BASE_URL", settings.ss_base_url)


def comment_id_hash(post_url: str, commenter: str, comment_text: str) -> str:
    """Generate a stable hash for a comment to track 'seen' status."""
    raw = f"{post_url}|{commenter}|{comment_text}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


async def get_active_session() -> tuple[str, str] | None:
    """Fetch the most recent valid LinkedIn session (cookies JSON + user_agent)."""
    if AsyncSessionLocal is None:
        return None

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT cookies, user_agent FROM ss_sessions "
                "WHERE platform = 'linkedin' AND valid = true "
                "ORDER BY created_at DESC LIMIT 1"
            )
        )
        row = result.fetchone()

    if row is None:
        return None
    return row[0], row[1] or ""


async def is_comment_seen(cid: str) -> bool:
    """Check if a comment has already been processed."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT 1 FROM ss_jobs "
                "WHERE job_type = 'listener_seen' AND payload->>'comment_id' = :cid "
                "LIMIT 1"
            ),
            {"cid": cid},
        )
        return result.fetchone() is not None


async def mark_comment_seen(cid: str) -> None:
    """Record a comment as seen so it is skipped on future runs."""
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                "INSERT INTO ss_jobs (job_type, status, payload) "
                "VALUES ('listener_seen', 'done', :payload)"
            ),
            {"payload": json.dumps({"comment_id": cid})},
        )
        await db.commit()


async def scrape_posts_and_comments(cookies_json: str, user_agent: str) -> list[dict]:
    """Scrape Andy's recent LinkedIn posts and their comments using Playwright."""
    comments_found: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        context = await browser.new_context(user_agent=user_agent)

        cookie_list = json.loads(cookies_json)
        await context.add_cookies(cookie_list)

        page = await context.new_page()
        await page.goto(LINKEDIN_PROFILE_ACTIVITY_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # Collect post links from the activity feed (up to 5)
        post_elements = await page.query_selector_all(
            "a[href*='/feed/update/']"
        )
        post_urls: list[str] = []
        seen_urls: set[str] = set()
        for el in post_elements:
            href = await el.get_attribute("href")
            if href and "/feed/update/" in href:
                # Normalise to absolute URL without query params
                clean = href.split("?")[0]
                if clean not in seen_urls:
                    seen_urls.add(clean)
                    if not clean.startswith("http"):
                        clean = f"https://www.linkedin.com{clean}"
                    post_urls.append(clean)
            if len(post_urls) >= 5:
                break

        # Visit each post and scrape comments
        for post_url in post_urls:
            try:
                await page.goto(post_url, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)

                # Click "Load more comments" if visible
                try:
                    load_more = page.locator("button:has-text('Load more comments')")
                    if await load_more.count() > 0:
                        await load_more.first.click()
                        await page.wait_for_timeout(1500)
                except Exception:
                    pass

                # Extract comments
                comment_elements = await page.query_selector_all(
                    "article.comments-comment-item"
                )
                for cel in comment_elements:
                    try:
                        name_el = await cel.query_selector(
                            "span.comments-post-meta__name-text"
                        )
                        text_el = await cel.query_selector(
                            "span.comments-comment-item__main-content"
                        )
                        commenter_name = (
                            (await name_el.inner_text()).strip() if name_el else "Unknown"
                        )
                        comment_text = (
                            (await text_el.inner_text()).strip() if text_el else ""
                        )
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
                logger.warning("Failed to scrape post %s: %s", post_url, exc)
                continue

        await browser.close()

    return comments_found


async def draft_reply(commenter_name: str, comment_text: str) -> str:
    """Generate a draft reply in Andy's voice using the Claude API."""
    client = anthropic.Anthropic()

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=VOICE_AND_TONE,
        messages=[
            {
                "role": "user",
                "content": (
                    f"A person named {commenter_name} has commented on one of Andy's LinkedIn posts.\n\n"
                    f"The comment is:\n{comment_text}\n\n"
                    "Draft a reply in Andy's voice. Follow the Voice and Tone Reference Document exactly. "
                    "Reply only with the draft text — no preamble, no explanation, no quotation marks."
                ),
            }
        ],
    )
    return response.content[0].text.strip()


async def store_approval(
    draft_text: str,
    post_url: str,
    commenter_name: str,
    comment_text: str,
    cid: str,
) -> tuple[int, str]:
    """Insert a pending approval row and return (approval_id, approval_token)."""
    token = str(uuid.uuid4())
    context_json = json.dumps(
        {
            "post_url": post_url,
            "commenter_name": commenter_name,
            "comment_text": comment_text,
            "comment_id": cid,
        }
    )

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "INSERT INTO ss_approvals "
                "(draft_text, status, approval_token, context_json) "
                "VALUES (:draft, 'pending', :token, :ctx) "
                "RETURNING id"
            ),
            {"draft": draft_text, "token": token, "ctx": context_json},
        )
        row = result.fetchone()
        await db.commit()

    return row[0], token


async def send_approval_email(
    commenter_name: str,
    comment_text: str,
    draft_text: str,
    approval_token: str,
) -> str | None:
    """Send an approval email via Resend and return the email ID."""
    resend.api_key = settings.resend_api_key

    from_email = settings.from_email
    to_email = settings.alert_email

    if not from_email or not to_email:
        logger.error("FROM_EMAIL or ALERT_EMAIL not configured — skipping email")
        return None

    body = (
        f"{commenter_name} commented on your post:\n\n"
        f'"{comment_text}"\n\n'
        f"Drafted reply:\n\n{draft_text}\n\n"
        f"Approve and post:\n"
        f"{BASE_URL}/approvals/respond?token={approval_token}&action=approve\n\n"
        f"Edit before posting (paste your edited reply after ?edit= or just reply to this email is coming later):\n"
        f"{BASE_URL}/approvals/respond?token={approval_token}&action=edit&text=PASTE_EDITED_REPLY_HERE\n\n"
        f"This draft will expire in 48 hours."
    )

    try:
        result = resend.Emails.send(
            {
                "from": from_email,
                "to": [to_email],
                "subject": f"Reply approval needed — {commenter_name}",
                "text": body,
            }
        )
        email_id = result.get("id") if isinstance(result, dict) else getattr(result, "id", None)
        return email_id
    except Exception as exc:
        logger.error("Failed to send approval email: %s", exc)
        return None


async def run_listener() -> None:
    """Main listener loop: scrape → draft → store → email."""
    logger.info("Listener agent starting")

    session = await get_active_session()
    if session is None:
        logger.warning("No valid LinkedIn session found — aborting")
        return

    cookies_json, user_agent = session

    logger.info("Scraping recent posts and comments")
    comments = await scrape_posts_and_comments(cookies_json, user_agent)
    logger.info("Found %d comments across posts", len(comments))

    new_count = 0
    for comment in comments:
        cid = comment_id_hash(
            comment["post_url"], comment["commenter_name"], comment["comment_text"]
        )

        if await is_comment_seen(cid):
            continue

        logger.info("New comment from %s — drafting reply", comment["commenter_name"])
        try:
            draft = await draft_reply(comment["commenter_name"], comment["comment_text"])
        except Exception as exc:
            logger.error("Claude API error: %s", exc)
            continue

        approval_id, token = await store_approval(
            draft,
            comment["post_url"],
            comment["commenter_name"],
            comment["comment_text"],
            cid,
        )

        email_id = await send_approval_email(
            comment["commenter_name"],
            comment["comment_text"],
            draft,
            token,
        )

        if email_id:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    text(
                        "UPDATE ss_approvals SET resend_email_id = :eid WHERE id = :aid"
                    ),
                    {"eid": email_id, "aid": approval_id},
                )
                await db.commit()

        await mark_comment_seen(cid)
        new_count += 1

    logger.info("Listener agent done — processed %d new comments", new_count)


async def main() -> None:
    """Entry point when run as a standalone module."""
    await run_listener()


if __name__ == "__main__":
    asyncio.run(main())
