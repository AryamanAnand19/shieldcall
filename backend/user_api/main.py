"""
User API — port 8000
Auth, agent config, call history (Spec §9.3, §10)
"""
import os
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
import redis.asyncio as redis
import asyncpg
from passlib.context import CryptContext
from jose import JWTError, jwt

app = FastAPI(title="ShieldCall User API")

REDIS_URL  = os.getenv("REDIS_URL",   "redis://localhost:6379/0")
DB_DSN     = os.getenv("DB_DSN",      "postgres://shield_user:shield_password@localhost:5432/shieldcall")
JWT_SECRET = os.getenv("JWT_SECRET",  "change-me-in-production")
JWT_ALG    = "HS256"
JWT_TTL    = 60 * 24 * 7   # 7 days in minutes

redis_pool = None
db_pool    = None

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2  = OAuth2PasswordBearer(tokenUrl="/v1/auth/login")


@app.on_event("startup")
async def startup():
    global redis_pool, db_pool
    redis_pool = redis.from_url(REDIS_URL, decode_responses=True)
    db_pool    = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)


@app.on_event("shutdown")
async def shutdown():
    await redis_pool.aclose()
    await db_pool.close()


# ─── JWT helpers ─────────────────────────────────────────────────────────────

def _create_token(user_id: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=JWT_TTL)
    return jwt.encode({"sub": user_id, "exp": exp}, JWT_SECRET, algorithm=JWT_ALG)


async def _current_user(token: str = Depends(oauth2)) -> dict:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
        user_id: str = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

    cached = await redis_pool.get(f"user:{user_id}")
    if cached:
        return json.loads(cached)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, phone_e164, trust_score, is_verified, is_premium FROM users WHERE id = $1",
            uuid.UUID(user_id)
        )
    if not row:
        raise HTTPException(status_code=401, detail="User not found")

    user = {k: str(v) if isinstance(v, uuid.UUID) else v for k, v in dict(row).items()}
    await redis_pool.setex(f"user:{user_id}", 3600, json.dumps(user))
    return user


# ─── Models ──────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email:      EmailStr
    password:   str
    phone_e164: Optional[str] = None


class AgentConfigUpdate(BaseModel):
    agent_name:          Optional[str] = None
    voice:               Optional[str] = None
    persona:             Optional[str] = None
    handling_rules:      Optional[str] = None
    active_schedule:     Optional[str] = None
    ai_call_handling:    Optional[str] = None
    escalation_triggers: Optional[List[str]] = None
    languages:           Optional[List[str]] = None
    is_active:           Optional[bool] = None


# ─── Auth ─────────────────────────────────────────────────────────────────────

@app.post("/v1/auth/register", status_code=201)
async def register(body: RegisterRequest):
    pw_hash = pwd_ctx.hash(body.password)
    uid = str(uuid.uuid4())
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (id, email, phone_e164, password_hash)
                VALUES ($1, $2, $3, $4)
            """, uuid.UUID(uid), body.email, body.phone_e164, pw_hash)
            # Create default agent config
            await conn.execute("""
                INSERT INTO agent_configs (user_id) VALUES ($1)
                ON CONFLICT (user_id) DO NOTHING
            """, uuid.UUID(uid))
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="Email already registered")

    token = _create_token(uid)
    return {"user_id": uid, "token": token}


@app.post("/v1/auth/login")
async def login(form: OAuth2PasswordRequestForm = Depends()):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, password_hash FROM users WHERE email = $1", form.username
        )
    if not row or not pwd_ctx.verify(form.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = _create_token(str(row["id"]))
    return {"access_token": token, "token_type": "bearer"}


# ─── Agent Config ──────────────────────────────────────────────────────────────

@app.get("/v1/agents/me")
async def get_agent(user: dict = Depends(_current_user)):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM agent_configs WHERE user_id = $1", uuid.UUID(user["id"])
        )
    if not row:
        raise HTTPException(status_code=404, detail="Agent config not found")
    return dict(row)


@app.put("/v1/agents/me")
async def update_agent(body: AgentConfigUpdate, user: dict = Depends(_current_user)):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Build dynamic SET clause
    fields = list(updates.keys())
    values = list(updates.values())
    set_clause = ", ".join(f"{f} = ${i+2}" for i, f in enumerate(fields))
    set_clause += f", updated_at = NOW()"

    async with db_pool.acquire() as conn:
        await conn.execute(
            f"UPDATE agent_configs SET {set_clause} WHERE user_id = $1",
            uuid.UUID(user["id"]), *values
        )
        # Upsert if no row existed
        row = await conn.fetchrow(
            "SELECT * FROM agent_configs WHERE user_id = $1", uuid.UUID(user["id"])
        )

    await redis_pool.delete(f"user:{user['id']}")
    return dict(row) if row else {"status": "ok"}


# ─── Call History ──────────────────────────────────────────────────────────────

@app.get("/v1/calls")
async def list_calls(
    limit:  int = 20,
    offset: int = 0,
    user: dict = Depends(_current_user)
):
    """Paginated call history for the authenticated user (§10.1)."""
    if limit > 100:
        limit = 100
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, caller_e164, called_e164, started_at, ended_at, duration_sec,
                   shield_active, ai_score_pre, ai_score_post, user_label, summary
            FROM call_records
            WHERE user_id = $1
            ORDER BY started_at DESC
            LIMIT $2 OFFSET $3
        """, uuid.UUID(user["id"]), limit, offset)
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM call_records WHERE user_id = $1", uuid.UUID(user["id"])
        )

    calls = [
        {k: str(v) if isinstance(v, (uuid.UUID,)) else
            v.isoformat() if hasattr(v, "isoformat") else v
         for k, v in dict(r).items()}
        for r in rows
    ]
    return {"total": total, "limit": limit, "offset": offset, "calls": calls}


@app.get("/v1/calls/{call_id}")
async def get_call(call_id: str, user: dict = Depends(_current_user)):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT * FROM call_records
            WHERE id = $1 AND user_id = $2
        """, uuid.UUID(call_id), uuid.UUID(user["id"]))
    if not row:
        raise HTTPException(status_code=404, detail="Call not found")
    return {k: str(v) if isinstance(v, uuid.UUID) else
                v.isoformat() if hasattr(v, "isoformat") else v
            for k, v in dict(row).items()}


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    result = {"status": "ok", "redis": "unknown", "db": "unknown"}
    try:
        await redis_pool.ping()
        result["redis"] = "ok"
    except Exception as e:
        result["redis"] = str(e)
    try:
        async with db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        result["db"] = "ok"
    except Exception as e:
        result["db"] = str(e)
    if result["redis"] != "ok" or result["db"] != "ok":
        result["status"] = "degraded"
    return result
