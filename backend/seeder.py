import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime, timezone

# Matches our Docker settings
DB_DSN = "postgres://shield_user:shield_password@localhost:5432/shieldcall"

def seed_database():
    print("🚀 Seeding ShieldCall NID with test data...")
    
    # Connect to the database
    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor()

    # 1. We're going to seed some "test villains"
    # Format: (number, ai_score, platform_tag, is_voip, flags)
    test_data = [
        ("+12223334444", 0.95, ['twilio'], True, ['bulk_sender']), # High confidence AI
        ("+919999999999", 0.85, ['retell'], True, ['automated']),  # Known AI platform
        ("+15550001111", 0.45, [], True, ['new_number']),           # Suspicious
    ]

    now = datetime.now(timezone.utc)
    records = [
        (r[0], now, now, r[1], r[2], r[3], r[4]) for r in test_data
    ]

    # 2. Insert into the database
    query = """
        INSERT INTO nid_numbers (e164, first_seen, last_seen, ai_score, platform_tag, is_voip, flags)
        VALUES %s
        ON CONFLICT (e164) DO UPDATE SET ai_score = EXCLUDED.ai_score;
    """
    
    execute_values(cur, query, records)
    conn.commit()
    
    cur.close()
    conn.close()
    print("✅ Database seeded successfully with 3 test numbers.")

if __name__ == "__main__":
    seed_database()