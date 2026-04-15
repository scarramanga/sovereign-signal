"""Listener ingest endpoint — receives comments from Mac-side scraper."""

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from server.agents.listener import (
    comment_id_hash,
    draft_reply,
    is_comment_seen,
    mark_comment_seen,
    send_approval_email,
    store_approval,
)
from server.database import AsyncSessionLocal
from sqlalchemy import text

logger = logging.getLogger(__name__)
router = APIRouter()


class CommentIngestRequest(BaseModel):
    post_url: str
    commenter_name: str
    comment_text: str


@router.post("/ingest")
async def ingest_comment(body: CommentIngestRequest):
    """Receive a comment from the Mac scraper, dedup, draft, store, and email."""
    cid = comment_id_hash(body.post_url, body.commenter_name, body.comment_text)

    if await is_comment_seen(cid):
        return {"status": "skipped", "reason": "already_seen"}

    logger.info("New comment from %s — drafting reply", body.commenter_name)

    try:
        draft = await draft_reply(body.commenter_name, body.comment_text)
    except Exception as exc:
        logger.error("Claude API error: %s", exc)
        return {"status": "error", "reason": str(exc)}

    approval_id, token = await store_approval(
        draft,
        body.post_url,
        body.commenter_name,
        body.comment_text,
        cid,
    )

    email_id = await send_approval_email(
        body.commenter_name,
        body.comment_text,
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

    return {"status": "queued", "approval_id": approval_id}
