"""
NID Service — port 8002
CRUD for the Number Intelligence Database (Spec §3, §9.1)
"""
import os
import json
from typing import Optional, List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import redis.asyncio as redis
import asyncpg

app = FastAPI(title="ShieldCall NID Service")

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


# ─── Models ──────────────────────────────────────────────────────────────────

class NIDUpsert(BaseModel):
    ai_score:       Optional[float]      = 0.5
    ai_score_conf:  Optional[float]      = 0.0
    platform_tag:   Optional[List[str]]  = []
    attestation:    Optional[str]        = None
    cnam:           Optional[str]        = None
    carrier:        Optional[str]        = None
    asn:            Optional[int]        = None
    country:        Optional[str]        = None
    is_voip:        Optional[bool]       = False
    flags:          Optional[List[str]]  = []
    community_votes: Optional[dict]      = None


# ─── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/v1/number/{e164}")
async def get_number(e164: str):
    """Full NID record for a number (§9.2)."""
    cached = await redis_pool.get(f"nid:{e164}")
    if cached:
        return {"source": "cache", "record": json.loads(cached)}

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM nid_numbers WHERE e164 = $1", e164)

    if not row:
        raise HTTPException(status_code=404, detail=f"{e164} not found in NID")

    record = dict(row)
    await redis_pool.setex(f"nid:{e164}", 3600, json.dumps(record, default=str))
    return {"source": "db", "record": record}


@app.post("/v1/number/{e164}", status_code=201)
async def upsert_number(e164: str, body: NIDUpsert):
    """Insert or update an NID record (used by crawler and enrichment services)."""
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO nid_numbers (
                e164, ai_score, ai_score_conf, platform_tag,
                attestation, cnam, carrier, asn, country,
                is_voip, community_votes, flags, last_seen
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,NOW())
            ON CONFLICT (e164) DO UPDATE SET
                ai_score      = EXCLUDED.ai_score,
                ai_score_conf = EXCLUDED.ai_score_conf,
                platform_tag  = EXCLUDED.platform_tag,
                attestation   = EXCLUDED.attestation,
                cnam          = EXCLUDED.cnam,
                carrier       = EXCLUDED.carrier,
                asn           = EXCLUDED.asn,
                country       = EXCLUDED.country,
                is_voip       = EXCLUDED.is_voip,
                community_votes = COALESCE(EXCLUDED.community_votes, nid_numbers.community_votes),
                flags         = EXCLUDED.flags,
                last_seen     = NOW()
        """,
            e164,
            body.ai_score, body.ai_score_conf,
            body.platform_tag, body.attestation,
            body.cnam, body.carrier, body.asn,
            body.country, body.is_voip,
            json.dumps(body.community_votes or {"ai": 0, "human": 0, "spam": 0}),
            body.flags,
        )

    await redis_pool.delete(f"nid:{e164}")
    return {"status": "ok", "e164": e164}


@app.post("/v1/number/{e164}/decay")
async def apply_score_decay(e164: str):
    """
    Apply spec §3.3 score decay: ai_score drifts toward 0.5 (uncertain)
    over 90 days without new call events. Intended to be called by a scheduler.
    """
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT ai_score, last_seen FROM nid_numbers WHERE e164 = $1", e164
        )
        if not row:
            raise HTTPException(status_code=404, detail="Number not found")

        # Days since last call event
        import datetime
        days_silent = (
            datetime.datetime.now(datetime.timezone.utc) - row["last_seen"]
        ).days

        if days_silent < 1:
            return {"status": "no_decay_needed", "e164": e164}

        current = float(row["ai_score"] or 0.5)
        # Linear decay toward 0.5 over 90 days
        decay_factor = min(days_silent / 90.0, 1.0)
        new_score = current + decay_factor * (0.5 - current)
        new_score = round(new_score, 4)

        await conn.execute(
            "UPDATE nid_numbers SET ai_score = $1 WHERE e164 = $2",
            new_score, e164
        )

    await redis_pool.delete(f"nid:{e164}")
    return {"status": "decayed", "e164": e164, "old_score": current, "new_score": new_score}


@app.get("/health")
async def health():
    return {"status": "ok"}
