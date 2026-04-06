"""
Community API — port 8005
Post-call feedback ingestion and trust-weighted NID score updates (Spec §11)
"""
import os
import json
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import redis.asyncio as redis
import asyncpg

app = FastAPI(title="ShieldCall Community API")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
DB_DSN    = os.getenv("DB_DSN",    "postgres://shield_user:shield_password@localhost:5432/shieldcall")

redis_pool = None
db_pool    = None


@app.on_event("startup")
async def startup():
    global redis_pool, db_pool
    redis_pool = redis.from_url(REDIS_URL, decode_responses=True)
    db_pool    = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)


@app.on_event("shutdown")
async def shutdown():
    await redis_pool.aclose()
    await db_pool.close()


# ─── Score Update Formula (Spec §11.3) ───────────────────────────────────────

def _blend_score(current: float, votes: dict) -> float:
    """
    Spec §11.3 formula:
      community_signal = ai_votes / (ai_votes + human_votes)
      new_score = 0.6 * community_signal + 0.4 * current_score
    Requires at least 3 total votes to update.
    """
    ai_w  = float(votes.get("ai",    0))
    hum_w = float(votes.get("human", 0))
    total = ai_w + hum_w
    if total < 3:
        return current
    community_signal = ai_w / total
    return round(0.6 * community_signal + 0.4 * current, 4)


# ─── Models ──────────────────────────────────────────────────────────────────

class VoteRequest(BaseModel):
    label:        str            # 'ai' | 'human' | 'spam'
    user_id:      Optional[str] = None
    trust_weight: Optional[float] = 0.5
    note:         Optional[str] = None


# ─── Endpoints ───────────────────────────────────────────────────────────────

@app.post("/v1/number/{e164}/vote")
async def submit_vote(e164: str, vote: VoteRequest):
    """
    Submit a post-call label for a number (§9.2, §11.1).
    Updates community_votes JSONB and recalculates ai_score.
    """
    if vote.label not in ("ai", "human", "spam"):
        raise HTTPException(status_code=400, detail="label must be 'ai', 'human', or 'spam'")

    async with db_pool.acquire() as conn:
        # 1. Record the individual vote
        await conn.execute("""
            INSERT INTO community_votes (number_e164, user_id, label, trust_weight, note)
            VALUES ($1, $2::uuid, $3, $4, $5)
        """, e164, vote.user_id, vote.label, vote.trust_weight or 0.5, vote.note)

        # 2. Ensure the number exists in NID, then atomically increment its vote bucket
        await conn.execute("""
            INSERT INTO nid_numbers (e164, community_votes, last_seen)
            VALUES ($1, '{"ai":0,"human":0,"spam":0}'::jsonb, NOW())
            ON CONFLICT (e164) DO UPDATE SET
                community_votes = nid_numbers.community_votes || jsonb_build_object(
                    'ai',    COALESCE((nid_numbers.community_votes->>'ai')::int,    0)
                             + CASE WHEN $2 = 'ai'    THEN 1 ELSE 0 END,
                    'human', COALESCE((nid_numbers.community_votes->>'human')::int, 0)
                             + CASE WHEN $2 = 'human' THEN 1 ELSE 0 END,
                    'spam',  COALESCE((nid_numbers.community_votes->>'spam')::int,  0)
                             + CASE WHEN $2 = 'spam'  THEN 1 ELSE 0 END
                ),
                last_seen = NOW()
        """, e164, vote.label)

        # 3. Re-read and recalculate ai_score with the spec blend formula
        row = await conn.fetchrow(
            "SELECT ai_score, community_votes FROM nid_numbers WHERE e164 = $1", e164
        )
        if row:
            votes_data = row["community_votes"]
            if isinstance(votes_data, str):
                votes_data = json.loads(votes_data)
            new_score = _blend_score(float(row["ai_score"] or 0.5), votes_data)
            await conn.execute(
                "UPDATE nid_numbers SET ai_score = $1 WHERE e164 = $2",
                new_score, e164
            )

    # Invalidate Redis cache so next detection call re-fetches updated record
    await redis_pool.delete(f"nid:{e164}")

    return {"status": "ok", "e164": e164, "label": vote.label}


@app.get("/v1/number/{e164}/votes")
async def get_votes(e164: str):
    """Aggregate vote summary for a number."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT community_votes, ai_score FROM nid_numbers WHERE e164 = $1", e164
        )
    if not row:
        raise HTTPException(status_code=404, detail="Number not found in NID")

    votes = row["community_votes"]
    if isinstance(votes, str):
        votes = json.loads(votes)
    return {"e164": e164, "votes": votes, "ai_score": row["ai_score"]}


@app.get("/health")
async def health():
    return {"status": "ok"}
