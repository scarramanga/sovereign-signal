"""Session management routes for LinkedIn cookie capture and validation."""

import asyncio
import json
from typing import Any

from fastapi import APIRouter
from playwright.async_api import async_playwright
from pydantic import BaseModel
from sqlalchemy import text

from server.database import AsyncSessionLocal
from server.services.session_service import validate_session as validate_session_live

router = APIRouter()


class SessionImportRequest(BaseModel):
    cookies: list[dict[str, Any]]
    user_agent: str

LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
CAPTURE_TIMEOUT_SECONDS = 120
POLL_INTERVAL_SECONDS = 2


@router.post("/import")
async def import_session(body: SessionImportRequest):
    """Import a session captured externally (e.g. from a local Playwright script)."""
    if AsyncSessionLocal is None:
        return {"status": "error", "message": "Database not configured"}

    try:
        cookies_json = json.dumps(body.cookies)

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                text(
                    "INSERT INTO ss_sessions (platform, cookies, user_agent, valid) "
                    "VALUES ('linkedin', :cookies, :ua, true) "
                    "RETURNING id"
                ),
                {"cookies": cookies_json, "ua": body.user_agent},
            )
            row = result.fetchone()
            await db.commit()

        return {"session_id": row[0], "valid": True}

    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/capture")
async def capture_session():
    """Launch Playwright, wait for Andy to authenticate, capture li_at cookie."""
    if AsyncSessionLocal is None:
        return {"status": "error", "message": "Database not configured"}

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            context = await browser.new_context()
            page = await context.new_page()

            await page.goto(LINKEDIN_LOGIN_URL, wait_until="domcontentloaded")

            # Poll for li_at cookie (indicates successful LinkedIn login)
            elapsed = 0
            li_at_found = False
            while elapsed < CAPTURE_TIMEOUT_SECONDS:
                cookies = await context.cookies()
                if any(c["name"] == "li_at" for c in cookies):
                    li_at_found = True
                    break
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                elapsed += POLL_INTERVAL_SECONDS

            if not li_at_found:
                await browser.close()
                return {
                    "status": "timeout",
                    "message": f"No authenticated session detected within {CAPTURE_TIMEOUT_SECONDS} seconds",
                }

            # Capture all cookies and user agent
            all_cookies = await context.cookies()
            user_agent = await page.evaluate("navigator.userAgent")
            cookies_json = json.dumps(all_cookies)

            await browser.close()

        # Persist to ss_sessions
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                text(
                    "INSERT INTO ss_sessions (platform, cookies, user_agent, valid) "
                    "VALUES ('linkedin', :cookies, :ua, true) "
                    "RETURNING id, created_at"
                ),
                {"cookies": cookies_json, "ua": user_agent},
            )
            row = result.fetchone()
            await db.commit()

        return {
            "status": "captured",
            "session_id": row[0],
            "captured_at": row[1].isoformat(),
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/active")
async def active_session():
    """Return the current valid session status."""
    if AsyncSessionLocal is None:
        return {"status": "none", "valid": False}

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                text(
                    "SELECT id, created_at, last_used_at, valid "
                    "FROM ss_sessions "
                    "WHERE valid = true "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            )
            row = result.fetchone()

        if row is None:
            return {"status": "none", "valid": False}

        return {
            "status": "active",
            "session_id": row[0],
            "captured_at": row[1].isoformat(),
            "last_validated_at": row[2].isoformat() if row[2] else None,
            "valid": row[3],
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/validate")
async def validate_session():
    """Validate the active session against live LinkedIn."""
    if AsyncSessionLocal is None:
        return {"status": "error", "message": "Database not configured"}

    try:
        # Load the most recent valid session
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                text(
                    "SELECT id, cookies, user_agent "
                    "FROM ss_sessions "
                    "WHERE valid = true "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            )
            row = result.fetchone()

        if row is None:
            return {"status": "error", "message": "No valid session found"}

        session_id, cookies_text, user_agent = row[0], row[1], row[2]

        # Validate session using shared service
        is_valid = await validate_session_live(cookies_text, user_agent or "")

        # Update session status in database
        async with AsyncSessionLocal() as db:
            if is_valid:
                await db.execute(
                    text(
                        "UPDATE ss_sessions SET last_used_at = NOW(), updated_at = NOW() "
                        "WHERE id = :sid"
                    ),
                    {"sid": session_id},
                )
            else:
                await db.execute(
                    text(
                        "UPDATE ss_sessions SET valid = false, updated_at = NOW() "
                        "WHERE id = :sid"
                    ),
                    {"sid": session_id},
                )
            await db.commit()

        return {
            "status": "valid" if is_valid else "expired",
            "session_id": session_id,
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}
