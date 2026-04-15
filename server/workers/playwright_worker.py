"""Standalone Playwright worker process for LinkedIn session management.

Validates existing sessions on startup, then idles.
Session capture is triggered via the FastAPI API, not this worker.
"""

import asyncio
import json
import logging

from playwright.async_api import async_playwright
from sqlalchemy import text

from server.database import AsyncSessionLocal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

LINKEDIN_FEED_URL = "https://www.linkedin.com/feed/"
LOGIN_INDICATORS = ["/login", "/checkpoint"]


async def validate_session(session_id: int, cookies: str, user_agent: str) -> bool:
    """Validate a stored LinkedIn session by loading cookies and checking for redirects."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        context = await browser.new_context(user_agent=user_agent)

        cookie_list = json.loads(cookies)
        await context.add_cookies(cookie_list)

        page = await context.new_page()
        await page.goto(LINKEDIN_FEED_URL, wait_until="domcontentloaded")

        final_url = page.url
        await browser.close()

    is_valid = not any(indicator in final_url for indicator in LOGIN_INDICATORS)
    return is_valid


async def validate_stored_sessions():
    """Validate all sessions marked as valid in the database."""
    if AsyncSessionLocal is None:
        logger.error("Database not configured — cannot validate sessions")
        return

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT id, cookies, user_agent FROM ss_sessions "
                "WHERE valid = true ORDER BY created_at DESC"
            )
        )
        rows = result.fetchall()

    if not rows:
        logger.info("No valid sessions found to validate")
        return

    for row in rows:
        session_id, cookies, user_agent = row[0], row[1], row[2]
        logger.info("Validating session %s", session_id)
        try:
            is_valid = await validate_session(session_id, cookies, user_agent or "")
            async with AsyncSessionLocal() as db:
                if is_valid:
                    await db.execute(
                        text(
                            "UPDATE ss_sessions SET last_used_at = NOW(), updated_at = NOW() "
                            "WHERE id = :sid"
                        ),
                        {"sid": session_id},
                    )
                    logger.info("Session %s is valid", session_id)
                else:
                    await db.execute(
                        text(
                            "UPDATE ss_sessions SET valid = false, updated_at = NOW() "
                            "WHERE id = :sid"
                        ),
                        {"sid": session_id},
                    )
                    logger.warning("Session %s has expired", session_id)
                await db.commit()
        except Exception:
            logger.exception("Error validating session %s", session_id)


async def main():
    logger.info("Playwright worker starting — validating stored sessions")
    await validate_stored_sessions()
    logger.info("Validation complete — worker idling")
    # Keep the process alive so K8s doesn't restart it
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
