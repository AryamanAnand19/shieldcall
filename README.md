# ShieldCall

AI-powered real-time phone call detection and shielding system. Screens inbound calls using a weighted rule engine and an AI agent (Claude), identifies robocalls and AI dialers, and lets users intercept, whisper, or take over live calls.

---

## Architecture

```
Inbound Call
     │
     ▼
Asterisk (SIP/PJSIP)
     │  CURL to detection-api
     ▼
detection-api :8001  ──► Redis cache ──► PostgreSQL (NID)
     │
     ├── likely_ai   → AI agent screens call (shield-service)
     └── human / none → ring through
          │
     shield-service :8003
          │  Claude claude-opus-4-6
          ├── WebSocket transcript stream
          ├── Whisper / Takeover / Drop
          └── Post-call summary

Supporting services:
  nid-service     :8002  — Number Intelligence Database CRUD + score decay
  community-api   :8005  — Post-call votes + trust-weighted score blending
  user-api        :8000  — Auth, agent config, call history
```

---

## Services

| Service | Port | Description |
|---|---|---|
| `user-api` | 8000 | JWT auth, per-user agent config, call history |
| `detection-api` | 8001 | Real-time call scoring rule engine |
| `nid-service` | 8002 | Number Intelligence Database CRUD + score decay |
| `shield-service` | 8003 | Claude AI agent sessions, WebSocket transcript |
| `community-api` | 8005 | Community votes + score blending |
| PostgreSQL | 5432 | Primary database |
| Redis | 6379 | Cache + pub/sub for transcript streaming |

---

## Quick Start

### Prerequisites
- Docker Desktop
- Anthropic API key (get one at [console.anthropic.com](https://console.anthropic.com))

### 1. Clone
```bash
git clone https://github.com/AryamanAnand19/shieldcall.git
cd shieldcall
```

### 2. Configure
```bash
cp .env.example .env
# Edit .env and set your ANTHROPIC_API_KEY
```

### 3. Run
```bash
docker compose up --build
```

All services will start with health checks. PostgreSQL schema is auto-applied on first boot via `db_scripts/01_init.sql`.

---

## API Reference

### Detection
```
POST http://localhost:8001/v1/detect
{
  "call_id": "abc123",
  "caller_number": "+14155551234",
  "called_number": "+12125559876",
  "attestation": "A"
}
```
Returns `score`, `banner` (`likely_ai` / `possibly_automated` / `none`), and `reasons`.

### Shield Sessions
```
POST   http://localhost:8003/v1/shield/sessions          # create session
GET    http://localhost:8003/v1/shield/sessions/{id}     # session status
PATCH  http://localhost:8003/v1/shield/sessions/{id}     # whisper / takeover / drop
POST   http://localhost:8003/v1/shield/sessions/{id}/speak  # feed caller speech
WS     ws://localhost:8003/v1/shield/sessions/{id}/transcript  # live transcript
GET    http://localhost:8003/v1/calls/{call_id}/summary  # post-call summary
```

### Community Votes
```
POST http://localhost:8005/v1/number/{e164}/vote
{ "label": "ai", "trust_weight": 0.8 }

GET  http://localhost:8005/v1/number/{e164}/votes
```

### Auth
```
POST http://localhost:8000/v1/auth/register
POST http://localhost:8000/v1/auth/login
GET  http://localhost:8000/v1/agents/me
PUT  http://localhost:8000/v1/agents/me
GET  http://localhost:8000/v1/calls
```

---

## Scoring (Rule Engine §4.2)

| Signal | Weight |
|---|---|
| NID ai_score > 0.8 | +0.45 |
| NID ai_score > 0.5 | +0.20 |
| Known platform tag (Twilio, Telnyx, etc.) | +0.30 |
| `bulk_sender` flag | +0.20 |
| Community AI votes ratio (>5 votes) | up to +0.15 |
| VOIP origin | +0.05 |
| Low STIR/SHAKEN attestation (C or missing) | +0.10 |

Banners: `likely_ai` ≥ 0.70 · `possibly_automated` ≥ 0.40 · `none` < 0.40

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Claude API key |
| `JWT_SECRET` | No | JWT signing secret (change in prod) |
| `DB_DSN` | No | Postgres DSN (auto-set in Docker) |
| `REDIS_URL` | No | Redis URL (auto-set in Docker) |

---

## Project Structure

```
shieldcall/
├── backend/
│   ├── requirements.txt
│   ├── detection_api/    # Port 8001
│   ├── nid_service/      # Port 8002
│   ├── shield_service/   # Port 8003
│   ├── community_api/    # Port 8005
│   └── user_api/         # Port 8000
├── db_scripts/
│   └── 01_init.sql       # Auto-applied on first boot
├── telephony/
│   └── conf/             # Asterisk config
├── docker-compose.yml
└── .env.example
```
