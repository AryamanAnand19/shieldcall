import os
import json
import time
import asyncio
from typing import Optional, List

from fastapi import FastAPI
from pydantic import BaseModel
import redis.asyncio as redis
import asyncpg

app = FastAPI(title="ShieldCall Detection Engine")

# Config: env vars allow Docker deployment; localhost defaults work for local dev
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

class CallEvent(BaseModel):
    call_id:        str
    caller_number:  str
    called_number:  str
    originating_ip: Optional[str] = None
    attestation:    Optional[str] = None   # STIR/SHAKEN: A, B, C, or null
    sip_headers:    Optional[dict] = None


# ─── Rule Engine (Spec §4.2) ─────────────────────────────────────────────────

def run_rule_engine(nid: dict, event: CallEvent) -> tuple[float, list[str]]:
    """
    Weighted rule engine matching Section 4.2 of the spec.
    Returns (score 0.0–1.0, list of reason strings).
    """
    s = 0.0
    reasons: list[str] = []

    if nid:
        ai_score = float(nid.get("ai_score") or 0.5)

        # NID ai_score tiers
        if ai_score > 0.8:
            s += 0.45
            reasons.append("high_nid_score")
        elif ai_score > 0.5:
            s += 0.20
            reasons.append("elevated_nid_score")

        # Known platform tag — strongest single signal
        platform_tags = nid.get("platform_tag") or []
        if platform_tags:
            s += 0.30
            reasons.append(f"known_platform:{platform_tags[0]}")

        # Bulk sender flag
        flags = nid.get("flags") or []
        if "bulk_sender" in flags:
            s += 0.20
            reasons.append("bulk_sender")

        # Community votes
        votes = nid.get("community_votes") or {}
        if isinstance(votes, str):
            votes = json.loads(votes)
        ai_v   = int(votes.get("ai", 0))
        hum_v  = int(votes.get("human", 0))
        if ai_v > 5:
            ratio = ai_v / max(ai_v + hum_v, 1)
            s += ratio * 0.15
            reasons.append("community_votes")

        # VOIP origin (mild corroborating signal)
        if nid.get("is_voip"):
            s += 0.05
            reasons.append("is_voip")

    # STIR/SHAKEN attestation from the live call event
    attest = event.attestation
    if attest in ("C", None) and attest is not None:
        s += 0.10
        reasons.append("low_attestation")

    return min(s, 1.0), reasons


# ─── Core Detection Endpoint ─────────────────────────────────────────────────

@app.post("/v1/detect")
async def detect_call(event: CallEvent):
    """Main detection endpoint called by Asterisk / carrier gateway."""
    start = time.time()

    # 1. Fast cache lookup (target < 5 ms)
    cached = await redis_pool.get(f"nid:{event.caller_number}")
    nid    = json.loads(cached) if cached else {}

    # 2. DB fallback on cache miss
    if not nid:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM nid_numbers WHERE e164 = $1", event.caller_number
            )
            if row:
                nid = dict(row)
                await redis_pool.setex(
                    f"nid:{event.caller_number}", 3600,
                    json.dumps(nid, default=str)
                )

    # 3. Score
    score, reasons = run_rule_engine(nid, event)

    # 4. Banner decision
    if score >= 0.70:
        banner = "likely_ai"
    elif score >= 0.40:
        banner = "possibly_automated"
    else:
        banner = "none"

    # 5. Async side-effects (don't block the response)
    asyncio.create_task(_touch_number(event.caller_number))

    return {
        "status":        "success",
        "call_id":       event.call_id,
        "score":         round(score, 4),
        "banner":        banner,
        "reasons":       reasons,
        "platform_hint": (nid.get("platform_tag") or [None])[0],
        "latency_ms":    round((time.time() - start) * 1000, 2),
    }


@app.post("/v1/webhooks/call-event")
async def carrier_webhook(event: CallEvent):
    """Carrier webhook: receives call-setup events before the phone rings (§4.5)."""
    return await detect_call(event)


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    result = {"status": "ok", "redis": "unknown", "db": "unknown"}
    try:
        await redis_pool.ping()
        result["redis"] = "ok"
    except Exception as exc:
        result["redis"] = str(exc)

    try:
        async with db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        result["db"] = "ok"
    except Exception as exc:
        result["db"] = str(exc)

    if result["redis"] != "ok" or result["db"] != "ok":
        result["status"] = "degraded"
    return result


# ─── Background helpers ───────────────────────────────────────────────────────

async def _touch_number(e164: str):
    """Insert new number or bump call_count + last_seen without blocking detection."""
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO nid_numbers (e164, first_seen, last_seen, call_count)
                VALUES ($1, NOW(), NOW(), 1)
                ON CONFLICT (e164) DO UPDATE SET
                    last_seen  = NOW(),
                    call_count = nid_numbers.call_count + 1
            """, e164)
        # Invalidate stale cache entry so next lookup re-fetches updated count
        await redis_pool.delete(f"nid:{e164}")
    except Exception:
        pass
