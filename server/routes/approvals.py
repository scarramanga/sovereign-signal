"""Approval response routes — handles approve/edit actions from email links."""

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from server.database import AsyncSessionLocal

router = APIRouter()

APPROVAL_EXPIRY_HOURS = 48


class MarkPostedRequest(BaseModel):
    approval_token: str


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

    # Mark as approved — Mac-side Playwright will poll and post
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                "UPDATE ss_approvals SET "
                "status = :status, approved_text = :reply, "
                "approved_at = :now, responded_at = :now "
                "WHERE id = :aid"
            ),
            {
                "status": new_status,
                "reply": reply_text,
                "now": now,
                "aid": approval_id,
            },
        )
        await db.commit()

    return HTMLResponse(
        "<html><body><h2>Approved. Reply will post within 15 minutes.</h2></body></html>"
    )


@router.get("/pending-posts")
async def get_pending_posts():
    """Return approved approvals that have not yet been posted."""
    if AsyncSessionLocal is None:
        return JSONResponse([])

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT id, approval_token, context_json, approved_text "
                "FROM ss_approvals "
                "WHERE status IN ('approved', 'edited') AND posted_at IS NULL"
            )
        )
        rows = result.fetchall()

    pending = []
    for row in rows:
        ctx = row[2] if isinstance(row[2], dict) else json.loads(row[2]) if row[2] else {}
        pending.append({
            "id": row[0],
            "approval_token": row[1],
            "post_url": ctx.get("post_url", ""),
            "reply_text": row[3] or "",
            "commenter_name": ctx.get("commenter_name", ""),
        })

    return JSONResponse(pending)


@router.post("/mark-posted")
async def mark_posted(body: MarkPostedRequest):
    """Mark an approved reply as posted."""
    if AsyncSessionLocal is None:
        return JSONResponse({"status": "error", "detail": "Database not configured"})

    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                "UPDATE ss_approvals SET posted_at = :now WHERE approval_token = :token"
            ),
            {"now": now, "token": body.approval_token},
        )
        await db.commit()

    return JSONResponse({"status": "ok"})
