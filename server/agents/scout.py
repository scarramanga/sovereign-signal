"""Scout agent — shared functions for newsfeed post evaluation, Claude relevance
scoring, comment drafting, and email approval.

Mirrors the structure of server/agents/listener.py. The Mac-side scraper
(scripts/scout_mac.py) scrapes the LinkedIn feed and POSTs each post to
/scout/ingest; all Claude/Resend/DB work happens here, server-side, so no API
keys ever live on the Mac.
"""

import hashlib
import json
import logging
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import anthropic
import resend
from sqlalchemy import text

from server.config import settings
from server.database import AsyncSessionLocal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

VOICE_AND_TONE = (Path(__file__).parent.parent / "content" / "voice_and_tone.md").read_text()

BASE_URL = os.environ.get("SS_BASE_URL", settings.ss_base_url)

MODEL = "claude-sonnet-4-6"
RELEVANCE_THRESHOLD = 0.7

# Andy's thesis topics — the relevance filter for the feed.
THESIS_TOPICS = [
    "NZ monetary policy and interest rates",
    "NZ banking competition and profitability",
    "NZ housing affordability and debt",
    "Government fiscal policy and Crown balance sheet",
    "Debasement, inflation, and real returns",
    "KiwiSaver and retail investing",
    "Financial services disruption and fintech",
    "AGI, AI, and economic impact",
]


def post_id_hash(post_url: str) -> str:
    """Stable hash for a feed post, keyed on the canonical (query-stripped) URL."""
    parts = urlsplit(post_url)
    canonical = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


async def is_post_seen(pid: str) -> bool:
    """Check if a post has already been evaluated."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "SELECT 1 FROM ss_jobs "
                "WHERE job_type = 'scout_seen' AND payload->>'post_id' = :pid "
                "LIMIT 1"
            ),
            {"pid": pid},
        )
        return result.fetchone() is not None


async def mark_post_seen(pid: str, score: float) -> None:
    """Record a post as evaluated so it is never scored again."""
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                "INSERT INTO ss_jobs (job_type, status, payload) "
                "VALUES ('scout_seen', 'done', :payload)"
            ),
            {"payload": json.dumps({"post_id": pid, "score": score})},
        )
        await db.commit()


def _parse_json(raw: str) -> dict:
    """Best-effort JSON extraction from a model response (handles code fences)."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


async def score_relevance(post_text: str, author: str) -> tuple[float, str]:
    """Score a post 0-1 for relevance to Andy's thesis topics. Returns (score, reason)."""
    client = anthropic.AsyncAnthropic()
    topics = "\n".join(f"- {t}" for t in THESIS_TOPICS)

    response = await client.messages.create(
        model=MODEL,
        max_tokens=300,
        system=(
            "You score LinkedIn feed posts for relevance to Andy Boss's thesis topics. "
            "Andy is a macro-informed investor who writes on monetary policy, financial "
            "sovereignty, and the economics of AI. His thesis topics are:\n\n"
            f"{topics}\n\n"
            "Return ONLY a JSON object of the form "
            '{"score": <float between 0 and 1>, "reason": "<one short sentence>"}. '
            "A score of 1.0 means the post is squarely on-thesis and there is something "
            "substantive to add. A score of 0.0 means unrelated. Be strict — generic "
            "business, motivational, or off-topic content scores low."
        ),
        messages=[
            {
                "role": "user",
                "content": f"Post author: {author or 'Unknown'}\n\nPost text:\n{post_text}",
            }
        ],
    )

    data = _parse_json(response.content[0].text.strip())
    try:
        score = float(data.get("score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    return max(0.0, min(1.0, score)), str(data.get("reason", ""))


async def draft_comment(post_text: str, author: str) -> str:
    """Generate a draft comment in Andy's voice for someone else's post."""
    client = anthropic.AsyncAnthropic()

    response = await client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=VOICE_AND_TONE,
        messages=[
            {
                "role": "user",
                "content": (
                    f"{author or 'Someone'} published this post on LinkedIn:\n\n"
                    f"{post_text}\n\n"
                    "Draft a comment for Andy to leave on this post. This is opportunistic "
                    "engagement on someone else's post — follow the 'Commenting on other "
                    "people's posts' guidance in the Voice and Tone Reference Document exactly. "
                    "Lead with the idea, 2-4 sentences, add genuine value, never pitch. "
                    "Reply only with the comment text — no preamble, no explanation, no quotation marks."
                ),
            }
        ],
    )
    return response.content[0].text.strip()


async def store_scout_approval(
    draft_text: str,
    post_url: str,
    post_author: str,
    post_text: str,
    pid: str,
    score: float,
    reason: str,
) -> tuple[int, str]:
    """Insert a pending scout approval row and return (approval_id, approval_token)."""
    token = str(uuid.uuid4())
    context_json = json.dumps(
        {
            "post_url": post_url,
            "post_author": post_author,
            "post_id": pid,
            "relevance_score": score,
            "relevance_reason": reason,
        }
    )

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(
                "INSERT INTO ss_approvals "
                "(draft_text, status, approval_token, context_json, source, post_url, post_text) "
                "VALUES (:draft, 'pending', :token, :ctx, 'scout', :post_url, :post_text) "
                "RETURNING id"
            ),
            {
                "draft": draft_text,
                "token": token,
                "ctx": context_json,
                "post_url": post_url,
                "post_text": post_text,
            },
        )
        row = result.fetchone()
        await db.commit()

    return row[0], token


async def send_scout_approval_email(
    post_url: str,
    post_author: str,
    post_text: str,
    draft_text: str,
    approval_token: str,
) -> str | None:
    """Send a scout approval email via Resend and return the email ID."""
    resend.api_key = settings.resend_api_key

    from_email = settings.from_email
    to_email = settings.alert_email

    if not from_email or not to_email:
        logger.error("FROM_EMAIL or ALERT_EMAIL not configured — skipping email")
        return None

    snippet = post_text if len(post_text) <= 1500 else post_text[:1500] + "..."
    body = (
        f"Scout found a post worth commenting on.\n\n"
        f"Author: {post_author or 'Unknown'}\n"
        f"Post: {post_url}\n\n"
        f"Post text:\n\n\"{snippet}\"\n\n"
        f"Drafted comment:\n\n{draft_text}\n\n"
        f"Approve and post:\n"
        f"{BASE_URL}/approvals/respond?token={approval_token}&action=approve\n\n"
        f"Edit before posting (paste your edited comment after &text=):\n"
        f"{BASE_URL}/approvals/respond?token={approval_token}&action=edit&text=PASTE_EDITED_COMMENT_HERE\n\n"
        f"This draft will expire in 48 hours."
    )

    try:
        result = resend.Emails.send(
            {
                "from": from_email,
                "to": [to_email],
                "subject": f"Scout: comment opportunity — {post_author or 'LinkedIn post'}",
                "text": body,
            }
        )
        return result.get("id") if isinstance(result, dict) else getattr(result, "id", None)
    except Exception as exc:
        logger.error("Failed to send scout approval email: %s", exc)
        return None
