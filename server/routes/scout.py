"""Scout ingest endpoint — receives newsfeed posts from the Mac-side scout scraper."""

import logging

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from server.agents.scout import (
    RELEVANCE_THRESHOLD,
    draft_comment,
    is_post_seen,
    mark_post_seen,
    post_id_hash,
    score_relevance,
    send_scout_approval_email,
    store_scout_approval,
)
from server.database import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter()


class PostIngestRequest(BaseModel):
    post_url: str
    post_author: str = ""
    post_text: str


@router.post("/ingest")
async def ingest_post(body: PostIngestRequest):
    """Receive a feed post from the Mac scraper: dedup, score, gate, draft, store, email."""
    pid = post_id_hash(body.post_url)

    if await is_post_seen(pid):
        return {"status": "skipped", "reason": "already_seen"}

    try:
        score, reason = await score_relevance(body.post_text, body.post_author)
    except Exception as exc:
        # Not marked seen — a transient scoring error retries on the next run.
        logger.error("Claude scoring error: %s", exc)
        return {"status": "error", "reason": str(exc)}

    if score < RELEVANCE_THRESHOLD:
        # Irrelevant posts are marked seen immediately so they are never re-scored.
        await mark_post_seen(pid, score)
        logger.info("Post below threshold (%.2f): %s", score, body.post_url)
        return {"status": "skipped", "reason": "below_threshold", "score": score}

    logger.info("Relevant post (%.2f) — drafting comment: %s", score, body.post_url)

    try:
        draft = await draft_comment(body.post_text, body.post_author)
    except Exception as exc:
        # Not marked seen — retry drafting next run rather than dropping a relevant post.
        logger.error("Claude drafting error: %s", exc)
        return {"status": "error", "reason": str(exc)}

    approval_id, token = await store_scout_approval(
        draft,
        body.post_url,
        body.post_author,
        body.post_text,
        pid,
        score,
        reason,
    )

    # Stored successfully — safe to mark seen now.
    await mark_post_seen(pid, score)

    email_id = await send_scout_approval_email(
        body.post_url,
        body.post_author,
        body.post_text,
        draft,
        token,
    )

    if email_id:
        async with AsyncSessionLocal() as db:
            await db.execute(
                text("UPDATE ss_approvals SET resend_email_id = :eid WHERE id = :aid"),
                {"eid": email_id, "aid": approval_id},
            )
            await db.commit()

    return {"status": "queued", "approval_id": approval_id, "score": score}
