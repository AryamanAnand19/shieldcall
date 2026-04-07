"""
Shield Service — port 8003
AI agent call sessions: real-time screening, whisper, takeover, summaries (Spec §5, §6, §7)
"""
import os
import json
import asyncio
import uuid
from typing import Optional

import anthropic
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import redis.asyncio as redis
import asyncpg

app = FastAPI(title="ShieldCall Shield Service")

REDIS_URL     = os.getenv("REDIS_URL",    "redis://localhost:6379/0")
DB_DSN        = os.getenv("DB_DSN",       "postgres://shield_user:shield_password@localhost:5432/shieldcall")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Mock mode: active when no real key is provided
MOCK_MODE = not ANTHROPIC_KEY or ANTHROPIC_KEY.startswith("sk-ant-replace")

MOCK_RESPONSES = [
    "Thank you for calling. Could I get your name and the reason for your call?",
    "I see. Can you tell me a bit more about that?",
    "Got it. And who should I say is calling?",
    "Thank you. One moment while I check if they're available.",
    "I'll pass your message along. Is there a number you can be reached at?",
]
_mock_turn = 0

redis_pool = None
db_pool    = None
claude     = None


@app.on_event("startup")
async def startup():
    global redis_pool, db_pool, claude
    redis_pool = redis.from_url(REDIS_URL, decode_responses=True)
    db_pool    = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)
    if not MOCK_MODE:
        claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_KEY)


@app.on_event("shutdown")
async def shutdown():
    await redis_pool.aclose()
    await db_pool.close()


# ─── Models ──────────────────────────────────────────────────────────────────

class SessionCreate(BaseModel):
    call_id:      str
    caller_e164:  str
    called_e164:  Optional[str] = None
    user_id:      Optional[str] = None
    ai_score_pre: Optional[float] = None
    agent_name:   Optional[str] = "Assistant"
    persona:      Optional[str] = "Professional and helpful."
    handling_rules: Optional[str] = "Always get the caller's name and purpose."


class SessionControl(BaseModel):
    action:  str            # 'whisper' | 'takeover' | 'drop'
    payload: Optional[str] = None   # text for whisper


class SpeakEvent(BaseModel):
    text: str
    speaker: str = "caller"   # 'caller' | 'user'


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _system_prompt(session: dict) -> str:
    name  = session.get("agent_name", "Assistant")
    pers  = session.get("persona",    "Professional and helpful.")
    rules = session.get("handling_rules", "Always get the caller's name and purpose.")
    return (
        f"You are {name}, an AI screening assistant intercepting an inbound phone call on behalf of "
        f"the user. Your persona: {pers}\n\n"
        f"Rules: {rules}\n\n"
        "Respond concisely as if speaking aloud — no markdown, no lists unless truly needed. "
        "Your goal is to screen the caller, determine their intent, and decide whether to escalate "
        "to the real user. If asked directly whether you are AI, you may deflect politely once, "
        "then acknowledge if pressed again."
    )


async def _load_session(session_id: str) -> dict:
    """Load session + agent config from DB."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT ss.*, ac.agent_name, ac.persona, ac.handling_rules
            FROM shield_sessions ss
            LEFT JOIN agent_configs ac ON ac.user_id = ss.user_id
            WHERE ss.id = $1
        """, uuid.UUID(session_id))
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    return dict(row)


async def _get_history(session_id: str, conn) -> list[dict]:
    """Return transcript utterances as Claude messages list."""
    rows = await conn.fetch("""
        SELECT speaker, text FROM transcript_events
        WHERE session_id = $1 AND event_type = 'utterance' AND text IS NOT NULL
        ORDER BY ts ASC
    """, uuid.UUID(session_id))
    messages = []
    for r in rows:
        role = "user" if r["speaker"] == "caller" else "assistant"
        messages.append({"role": role, "content": r["text"]})
    return messages


async def _store_utterance(session_id: str, speaker: str, text: str, ts: float, conn):
    await conn.execute("""
        INSERT INTO transcript_events (session_id, ts, speaker, text, event_type, is_final)
        VALUES ($1, $2, $3, $4, 'utterance', true)
    """, uuid.UUID(session_id), ts, speaker, text)


async def _broadcast(session_id: str, event: dict):
    """Publish transcript event to Redis pub/sub channel."""
    await redis_pool.publish(f"transcript:{session_id}", json.dumps(event))


# ─── Session Endpoints ────────────────────────────────────────────────────────

@app.post("/v1/shield/sessions", status_code=201)
async def create_session(body: SessionCreate):
    """Create a new shield session when a call arrives (§5.1)."""
    sid = str(uuid.uuid4())
    import time
    ts = time.time()

    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO shield_sessions
                (id, user_id, call_id, caller_e164, called_e164, status, ai_score_pre, started_at)
            VALUES ($1, $2::uuid, $3, $4, $5, 'active', $6, NOW())
        """,
            uuid.UUID(sid),
            body.user_id,
            body.call_id,
            body.caller_e164,
            body.called_e164,
            body.ai_score_pre,
        )

    # Cache session metadata for quick reads
    await redis_pool.setex(f"session:{sid}", 7200, json.dumps({
        "id": sid,
        "call_id": body.call_id,
        "caller_e164": body.caller_e164,
        "status": "active",
        "agent_name": body.agent_name,
        "persona": body.persona,
        "handling_rules": body.handling_rules,
    }))

    return {"session_id": sid, "status": "active", "call_id": body.call_id}


@app.get("/v1/shield/sessions/{session_id}")
async def get_session(session_id: str):
    cached = await redis_pool.get(f"session:{session_id}")
    if cached:
        return json.loads(cached)
    session = await _load_session(session_id)
    return {k: str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
            for k, v in session.items()}


@app.patch("/v1/shield/sessions/{session_id}")
async def control_session(session_id: str, body: SessionControl):
    """
    Control actions (§6):
      whisper  — inject a hint to the AI agent mid-call
      takeover — mark session as human-controlled; agent stops responding
      drop     — end the call session
    """
    if body.action not in ("whisper", "takeover", "drop"):
        raise HTTPException(status_code=400, detail="action must be 'whisper', 'takeover', or 'drop'")

    import time
    ts = time.time()

    async with db_pool.acquire() as conn:
        if body.action == "whisper":
            await conn.execute("""
                UPDATE shield_sessions
                SET whisper_count = whisper_count + 1
                WHERE id = $1
            """, uuid.UUID(session_id))
            # Store whisper as a control event in transcript
            await conn.execute("""
                INSERT INTO transcript_events (session_id, ts, speaker, text, event_type, is_final)
                VALUES ($1, $2, 'user', $3, 'control', true)
            """, uuid.UUID(session_id), ts, f"[WHISPER] {body.payload}")

            await _broadcast(session_id, {
                "type": "whisper", "text": body.payload, "ts": ts
            })

        elif body.action == "takeover":
            await conn.execute("""
                UPDATE shield_sessions
                SET status = 'takeover', takeover_at = NOW()
                WHERE id = $1
            """, uuid.UUID(session_id))
            await redis_pool.delete(f"session:{session_id}")
            await _broadcast(session_id, {"type": "takeover", "ts": ts})

        elif body.action == "drop":
            await conn.execute("""
                UPDATE shield_sessions
                SET status = 'dropped', ended_at = NOW()
                WHERE id = $1
            """, uuid.UUID(session_id))
            await redis_pool.delete(f"session:{session_id}")
            await _broadcast(session_id, {"type": "drop", "ts": ts})

    return {"status": "ok", "action": body.action, "session_id": session_id}


# ─── Speech Input (ASR feed / testing) ───────────────────────────────────────

@app.post("/v1/shield/sessions/{session_id}/speak")
async def speak(session_id: str, body: SpeakEvent):
    """
    Inject a speech utterance into the session (§5.3).
    When speaker='caller', Claude generates an agent reply.
    When speaker='user' (whisper override), just stored.
    """
    import time
    ts = time.time()

    # Load session to check status and get agent config
    session = await _load_session(session_id)

    if session["status"] in ("dropped", "completed"):
        raise HTTPException(status_code=409, detail="Session is no longer active")

    async with db_pool.acquire() as conn:
        await _store_utterance(session_id, body.speaker, body.text, ts, conn)

        # Broadcast caller speech
        await _broadcast(session_id, {
            "type": "utterance", "speaker": body.speaker, "text": body.text, "ts": ts
        })

        if body.speaker != "caller" or session["status"] == "takeover":
            return {"status": "stored", "speaker": body.speaker}

        # Build message history for Claude
        history = await _get_history(session_id, conn)

    # If there's a pending whisper (last control event), prepend as system note
    whisper_hint = ""
    async with db_pool.acquire() as conn:
        w = await conn.fetchrow("""
            SELECT text FROM transcript_events
            WHERE session_id = $1 AND event_type = 'control'
            ORDER BY ts DESC LIMIT 1
        """, uuid.UUID(session_id))
        if w:
            whisper_hint = f"\n\n[User hint for this response: {w['text'].replace('[WHISPER] ', '')}]"

    system = _system_prompt(session) + whisper_hint

    # Stream Claude response, accumulate full text
    agent_reply = ""
    agent_ts = time.time()

    if MOCK_MODE:
        # Return scripted mock responses without calling Claude
        global _mock_turn
        agent_reply = MOCK_RESPONSES[_mock_turn % len(MOCK_RESPONSES)]
        _mock_turn += 1
        await _broadcast(session_id, {
            "type": "agent_chunk", "text": agent_reply, "ts": ts
        })
    else:
        async with db_pool.acquire() as conn:
            async with claude.messages.stream(
                model="claude-opus-4-6",
                max_tokens=512,
                thinking={"type": "adaptive"},
                system=system,
                messages=history,
            ) as stream:
                async for text_chunk in stream.text_stream:
                    agent_reply += text_chunk
                    await _broadcast(session_id, {
                        "type": "agent_chunk", "text": text_chunk, "ts": ts
                    })

            agent_ts = time.time()
            async with db_pool.acquire() as conn:
                await _store_utterance(session_id, "agent", agent_reply, agent_ts, conn)

    if MOCK_MODE:
        async with db_pool.acquire() as conn:
            await _store_utterance(session_id, "agent", agent_reply, agent_ts, conn)

    await _broadcast(session_id, {
        "type": "utterance", "speaker": "agent", "text": agent_reply, "ts": agent_ts
    })

    return {"status": "ok", "agent_reply": agent_reply, "mock": MOCK_MODE}


# ─── WebSocket Transcript Stream ─────────────────────────────────────────────

@app.websocket("/v1/shield/sessions/{session_id}/transcript")
async def transcript_ws(websocket: WebSocket, session_id: str):
    """
    Live transcript stream via WebSocket + Redis pub/sub (§7.1).
    Replays last 50 stored utterances on connect, then streams live events.
    """
    await websocket.accept()

    # Replay recent history
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT speaker, text, ts, event_type FROM transcript_events
                WHERE session_id = $1
                ORDER BY ts ASC
                LIMIT 50
            """, uuid.UUID(session_id))
        for r in rows:
            await websocket.send_json({
                "type": "replay",
                "speaker": r["speaker"],
                "text": r["text"],
                "ts": float(r["ts"]),
                "event_type": r["event_type"],
            })
    except Exception:
        pass

    # Subscribe to live events
    pubsub = redis_pool.pubsub()
    await pubsub.subscribe(f"transcript:{session_id}")

    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    await websocket.send_text(message["data"])
                except WebSocketDisconnect:
                    break
    finally:
        await pubsub.unsubscribe(f"transcript:{session_id}")
        await pubsub.aclose()


# ─── Summary ─────────────────────────────────────────────────────────────────

@app.get("/v1/calls/{call_id}/summary")
async def get_summary(call_id: str):
    """
    Generate or return cached call summary (§8.2).
    Summarises the full transcript using Claude.
    """
    async with db_pool.acquire() as conn:
        session = await conn.fetchrow(
            "SELECT * FROM shield_sessions WHERE call_id = $1", call_id
        )
        if not session:
            raise HTTPException(status_code=404, detail="Call not found")

        # Return cached summary if exists
        if session["summary"]:
            return {"call_id": call_id, "summary": session["summary"], "cached": True}

        # Build transcript text
        rows = await conn.fetch("""
            SELECT speaker, text FROM transcript_events
            WHERE session_id = $1 AND event_type = 'utterance' AND text IS NOT NULL
            ORDER BY ts ASC
        """, session["id"])

    if not rows:
        return {"call_id": call_id, "summary": "No transcript available.", "cached": False}

    transcript_text = "\n".join(
        f"{r['speaker'].upper()}: {r['text']}" for r in rows
    )

    if MOCK_MODE:
        turns = len(rows)
        summary = (
            f"[MOCK SUMMARY] Call had {turns} turn(s). "
            "Caller intent was unclear. Agent gathered basic information. "
            "Call ended without escalation to the user."
        )
    else:
        response = await claude.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            thinking={"type": "adaptive"},
            messages=[{
                "role": "user",
                "content": (
                    "Summarise this phone call transcript in 2–4 sentences. "
                    "Include: caller intent, key information gathered, and outcome/disposition.\n\n"
                    f"TRANSCRIPT:\n{transcript_text}"
                )
            }]
        )
        summary = next(
            (b.text for b in response.content if hasattr(b, "text")), ""
        )

    # Persist summary
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE shield_sessions SET summary = $1 WHERE call_id = $2",
            summary, call_id
        )

    return {"call_id": call_id, "summary": summary, "cached": False}


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    result = {"status": "ok", "redis": "unknown", "db": "unknown", "claude": "unknown"}
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
    result["claude"] = "mock (no API key)" if MOCK_MODE else "ok"
    if any(v not in ("ok", "not configured") for v in [result["redis"], result["db"]]):
        result["status"] = "degraded"
    return result
