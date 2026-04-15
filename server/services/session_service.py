"""Shared LinkedIn session validation logic used by routes and workers."""

import json

from playwright.async_api import async_playwright

LINKEDIN_FEED_URL = "https://www.linkedin.com/feed/"
LOGIN_INDICATORS = ["/login", "/checkpoint"]


async def validate_session(cookies: str, user_agent: str) -> bool:
    """Validate a LinkedIn session by restoring cookies and checking for login redirects.

    Returns True if the session is still valid, False if expired.
    """
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

    return not any(indicator in final_url for indicator in LOGIN_INDICATORS)
