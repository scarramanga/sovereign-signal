"""Listener agent — shared functions for comment processing, Claude drafting, and email approval."""

import hashlib
import json
import logging
import os
import uuid
from pathlib import Path

import anthropic
import resend
from sqlalchemy import text

from server.config import settings
from server.database import AsyncSessionLocal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

VOICE_AND_TONE = (Path(__file__).parent.parent / "content" / "voice_and_tone.md").read_text()

BASE_URL = os.environ.get("SS_BASE_URL", settings.ss_base_url)


def comment_id_hash(post_url: str, commenter: str, comment_text: str) -> str:
    """Generate a stable hash for a comment to track 'seen' status."""
    raw = f"{post_url}|{commenter}|{comment_text}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


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


async def draft_reply(commenter_name: str, comment_text: str) -> str:
    """Generate a draft reply in Andy's voice using the Claude API."""
    client = anthropic.AsyncAnthropic()

    response = await client.messages.create(
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
