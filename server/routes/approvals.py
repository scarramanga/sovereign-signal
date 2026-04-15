"""Approval response routes — handles approve/edit actions from email links."""

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
from playwright.async_api import async_playwright
from sqlalchemy import text

from server.database import AsyncSessionLocal

router = APIRouter()

APPROVAL_EXPIRY_HOURS = 48


async def post_linkedin_reply(context_json: str, reply_text: str) -> bool:
    """Post a reply to a LinkedIn comment using the active session."""
    if AsyncSessionLocal is None:
        return False

    # Fetch active session
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT cookies, user_agent FROM ss_sessions "
                "WHERE platform = 'linkedin' AND valid = true "
                "ORDER BY created_at DESC LIMIT 1"
            )
        )
        session_row = result.fetchone()

    if session_row is None:
        return False

    cookies_json, user_agent = session_row[0], session_row[1] or ""
    context = json.loads(context_json)
    post_url = context.get("post_url", "")

    if not post_url:
        return False

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        browser_context = await browser.new_context(user_agent=user_agent)

        cookie_list = json.loads(cookies_json)
        await browser_context.add_cookies(cookie_list)

        page = await browser_context.new_page()
        await page.goto(post_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        try:
            # Look for the comment reply box
            reply_button = page.locator("button:has-text('Reply')")
            if await reply_button.count() > 0:
                await reply_button.first.click()
                await page.wait_for_timeout(1000)

            # Find the comment input and type the reply
            comment_box = page.locator(
                "div.ql-editor[contenteditable='true']"
            ).last
            await comment_box.click()
            await comment_box.fill(reply_text)
            await page.wait_for_timeout(500)

            # Submit the comment
            submit_button = page.locator(
                "button.comments-comment-box__submit-button"
            )
            if await submit_button.count() > 0:
                await submit_button.first.click()
                await page.wait_for_timeout(2000)

            await browser.close()
            return True

        except Exception:
            await browser.close()
            return False


@router.get("/respond", response_class=HTMLResponse)
async def respond_to_approval(
    token: str = Query(...),
    action: str = Query(...),
    text_param: str = Query(None, alias="text"),
):
    """Handle approval link clicks from email."""
    if AsyncSessionLocal is None:
        return HTMLResponse("<html><body><p>Database not configured.</p></body></html>")

    # Look up the approval by token
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT id, draft_text, context_json, status, created_at "
                "FROM ss_approvals WHERE approval_token = :token LIMIT 1"
            ),
            {"token": token},
        )
        row = result.fetchone()

    if row is None:
        return HTMLResponse(
            "<html><body><p>This approval link has expired or already been used.</p></body></html>"
        )

    approval_id, draft_text, context_json, status, created_at = (
        row[0],
        row[1],
        row[2],
        row[3],
        row[4],
    )

    # Check if already handled
    if status != "pending":
        return HTMLResponse(
            "<html><body><p>This approval link has expired or already been used.</p></body></html>"
        )

    # Check expiry
    expiry = created_at.replace(tzinfo=timezone.utc) + timedelta(hours=APPROVAL_EXPIRY_HOURS)
    if datetime.now(timezone.utc) > expiry:
        return HTMLResponse(
            "<html><body><p>This approval link has expired or already been used.</p></body></html>"
        )

    if action not in ("approve", "edit"):
        return HTMLResponse(
            "<html><body><p>Invalid action. Use 'approve' or 'edit'.</p></body></html>"
        )

    # Determine which text to post
    reply_text = draft_text
    new_status = "approved"
    if action == "edit":
        if not text_param:
            return HTMLResponse(
                "<html><body><p>Edit action requires a 'text' parameter with your edited reply.</p></body></html>"
            )
        reply_text = text_param
        new_status = "edited"

    # Post the reply to LinkedIn
    posted = await post_linkedin_reply(context_json, reply_text)

    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                "UPDATE ss_approvals SET "
                "status = :status, approved_text = :reply, "
                "approved_at = :now, posted_at = :posted_at, responded_at = :now "
                "WHERE id = :aid"
            ),
            {
                "status": new_status,
                "reply": reply_text,
                "now": now,
                "posted_at": now if posted else None,
                "aid": approval_id,
            },
        )
        await db.commit()

    if posted:
        msg = "Reply posted. You can close this tab."
        if action == "edit":
            msg = "Edited reply posted. You can close this tab."
    else:
        msg = "Approval recorded but reply could not be posted automatically. Please post manually."

    return HTMLResponse(f"<html><body><p>{msg}</p></body></html>")
