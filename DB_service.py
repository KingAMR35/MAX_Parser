import os
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_NAME = os.getenv("PGDATABASE", "MAX_Parser")
MAX_CUSTOMER_CHATS = 5
MULTI_MAX_CUSTOMER_CHATS = 10
STATS_TIMEZONE = ZoneInfo("Europe/Samara")


class ChatLimitReached(Exception):
    """Raised when a customer tries to connect more than the allowed chats."""


def get_connection():
    if DATABASE_URL:
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)

    options = {
        "dbname": DATABASE_NAME,
        "host": os.getenv("PGHOST"),
        "port": os.getenv("PGPORT", "5432"),
        "user": os.getenv("PGUSER"),
        "password": os.getenv("PGPASSWORD"),
        "sslmode": os.getenv("PGSSLMODE"),
    }
    return psycopg.connect(
        row_factory=dict_row,
        **{key: value for key, value in options.items() if value not in (None, "")},
    )


def init_db():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                added_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                first_seen TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                chat_id BIGINT PRIMARY KEY,
                title TEXT,
                max_url TEXT,
                phone TEXT,
                is_active BOOLEAN NOT NULL DEFAULT FALSE,
                message_count BIGINT NOT NULL DEFAULT 0,
                owner_id BIGINT,
                account_mode TEXT NOT NULL DEFAULT 'self',
                added_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_message_daily_stats (
                chat_id BIGINT NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
                sent_date DATE NOT NULL,
                sent_count BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, sent_date)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS support_sessions (
                user_id BIGINT PRIMARY KEY,
                opened_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS support_message_routes (
                admin_chat_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                PRIMARY KEY (admin_chat_id, message_id)
            )
        """)
        conn.execute("""
            ALTER TABLE support_message_routes
            ADD COLUMN IF NOT EXISTS admin_chat_id BIGINT
        """)
        primary_admin_id = int(os.getenv("ADMIN_ID") or 0)
        conn.execute(
            "UPDATE support_message_routes SET admin_chat_id = %s WHERE admin_chat_id IS NULL",
            (primary_admin_id,),
        )
        conn.execute("""
            ALTER TABLE support_message_routes
            ALTER COLUMN admin_chat_id SET NOT NULL
        """)
        route_primary_key = conn.execute("""
            SELECT c.conname,
                   array_agg(a.attname ORDER BY route_key.ordinality) AS columns
            FROM pg_constraint AS c
            JOIN LATERAL unnest(c.conkey) WITH ORDINALITY
                AS route_key(attnum, ordinality) ON TRUE
            JOIN pg_attribute AS a
              ON a.attrelid = c.conrelid AND a.attnum = route_key.attnum
            WHERE c.conrelid = 'support_message_routes'::regclass
              AND c.contype = 'p'
            GROUP BY c.conname
        """).fetchone()
        if not route_primary_key or list(route_primary_key["columns"]) != [
            "admin_chat_id", "message_id"
        ]:
            conn.execute("""
                ALTER TABLE support_message_routes
                DROP CONSTRAINT IF EXISTS support_message_routes_pkey
            """)
            conn.execute("""
                ALTER TABLE support_message_routes
                ADD CONSTRAINT support_message_routes_pkey
                PRIMARY KEY (admin_chat_id, message_id)
            """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_payment_orders (
                payload TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                plan_code TEXT NOT NULL CHECK (plan_code IN ('self', 'project', 'multi')),
                duration_months SMALLINT NOT NULL DEFAULT 1 CHECK (duration_months IN (1, 3, 12)),
                amount BIGINT NOT NULL CHECK (amount > 0),
                currency VARCHAR(3) NOT NULL DEFAULT 'RUB',
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'checkout', 'paid')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMPTZ NOT NULL DEFAULT (CURRENT_TIMESTAMP + INTERVAL '24 hours'),
                paid_at TIMESTAMPTZ,
                provider_payment_charge_id TEXT UNIQUE,
                telegram_payment_charge_id TEXT UNIQUE
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_telegram_payment_orders_user_created
            ON telegram_payment_orders (user_id, created_at DESC)
        """)

        conn.execute("""
            ALTER TABLE telegram_payment_orders
            DROP CONSTRAINT IF EXISTS telegram_payment_orders_plan_code_check
        """)
        conn.execute("""
            ALTER TABLE telegram_payment_orders
            ADD CONSTRAINT telegram_payment_orders_plan_code_check
            CHECK (plan_code IN ('self', 'project', 'multi'))
        """)
        conn.execute("""
            ALTER TABLE telegram_payment_orders
            ADD COLUMN IF NOT EXISTS duration_months SMALLINT NOT NULL DEFAULT 1
        """)
        conn.execute("""
            ALTER TABLE telegram_payment_orders
            DROP CONSTRAINT IF EXISTS telegram_payment_orders_duration_months_check
        """)
        conn.execute("""
            ALTER TABLE telegram_payment_orders
            ADD CONSTRAINT telegram_payment_orders_duration_months_check
            CHECK (duration_months IN (1, 3, 12))
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_subscriptions (
                user_id BIGINT PRIMARY KEY,
                plan_code TEXT NOT NULL CHECK (plan_code IN ('self', 'project')),
                active_until TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reminder_sent_at TIMESTAMPTZ
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_subscription_entitlements (
                user_id BIGINT NOT NULL,
                plan_code TEXT NOT NULL CHECK (plan_code IN ('self', 'project', 'multi')),
                active_until TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reminder_sent_at TIMESTAMPTZ,
                PRIMARY KEY (user_id, plan_code)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_telegram_subscription_entitlements_active_until
            ON telegram_subscription_entitlements (active_until)
        """)
        conn.execute("""
            INSERT INTO telegram_subscription_entitlements
                (user_id, plan_code, active_until, updated_at, reminder_sent_at)
            SELECT user_id, plan_code, active_until, updated_at, reminder_sent_at
            FROM telegram_subscriptions
            ON CONFLICT (user_id, plan_code) DO UPDATE SET
                active_until = GREATEST(
                    telegram_subscription_entitlements.active_until,
                    EXCLUDED.active_until
                ),
                updated_at = GREATEST(
                    telegram_subscription_entitlements.updated_at,
                    EXCLUDED.updated_at
                )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS customer_reviews (
                user_id BIGINT PRIMARY KEY,
                status TEXT NOT NULL CHECK (status IN (
                    'eligible', 'invited', 'writing', 'pending', 'approved', 'rejected', 'skipped'
                )),
                review_text TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            ALTER TABLE customer_reviews
            DROP CONSTRAINT IF EXISTS customer_reviews_status_check
        """)
        conn.execute("""
            ALTER TABLE customer_reviews
            ADD CONSTRAINT customer_reviews_status_check CHECK (status IN (
                'eligible', 'invited', 'writing', 'pending', 'approved', 'rejected', 'skipped'
            ))
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            WITH inserted AS (
                INSERT INTO app_migrations (version)
                VALUES ('2026-10-clear-test-approved-reviews')
                ON CONFLICT (version) DO NOTHING
                RETURNING version
            )
            UPDATE customer_reviews
            SET status = 'skipped', review_text = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE (review_text IS NOT NULL OR status = 'approved')
              AND EXISTS (SELECT 1 FROM inserted)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS customer_review_entries (
                review_id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                connection_key TEXT,
                status TEXT NOT NULL CHECK (status IN (
                    'eligible', 'invited', 'writing', 'pending', 'approved', 'rejected', 'skipped'
                )),
                review_text TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_review_entries_connection
            ON customer_review_entries (user_id, connection_key)
            WHERE connection_key IS NOT NULL
        """)
        conn.execute("""
            WITH inserted AS (
                INSERT INTO app_migrations (version)
                VALUES ('2026-10-customer-review-entries')
                ON CONFLICT (version) DO NOTHING
                RETURNING version
            )
            INSERT INTO customer_review_entries
                (user_id, status, review_text, created_at, updated_at)
            SELECT old.user_id, old.status, old.review_text, old.created_at, old.updated_at
            FROM customer_reviews AS old
            CROSS JOIN inserted
            WHERE old.status IN ('pending', 'approved')
              AND old.review_text IS NOT NULL
              AND LENGTH(BTRIM(old.review_text)) > 0
        """)

#! ============ АДМИНЫ ============

def add_admin(user_id: int, username: str = None):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO admins (user_id, username) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
            (user_id, username),
        )


def update_admin_username(user_id: int, username: str):
    with get_connection() as conn:
        conn.execute("UPDATE admins SET username = %s WHERE user_id = %s", (username, user_id))


def is_admin(user_id: int) -> bool:
    with get_connection() as conn:
        row = conn.execute("SELECT 1 AS found FROM admins WHERE user_id = %s", (user_id,)).fetchone()
        return row is not None


def get_all_admins():
    with get_connection() as conn:
        rows = conn.execute("SELECT user_id, username FROM admins ORDER BY added_at").fetchall()
        return list(rows)


def delete_admin(user_id: int):
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM admins WHERE user_id = %s", (user_id,))
        return cursor.rowcount == 1


def grant_subscription(user_id: int, plan_code: str, months: int):
    """Grant a free product subscription, extending its active period."""
    if plan_code not in {"self", "project", "multi"}:
        raise ValueError("Некорректный тариф подписки")
    months = int(months)
    if months not in {1, 3, 12}:
        raise ValueError("Срок подписки должен быть 1, 3 или 12 месяцев")
    with get_connection() as conn:
        return conn.execute("""
            INSERT INTO telegram_subscription_entitlements (user_id, plan_code, active_until)
            VALUES (%s, %s, CURRENT_TIMESTAMP + (%s * INTERVAL '1 month'))
            ON CONFLICT (user_id, plan_code) DO UPDATE SET
                active_until = GREATEST(
                    telegram_subscription_entitlements.active_until, CURRENT_TIMESTAMP
                ) + (%s * INTERVAL '1 month'),
                updated_at = CURRENT_TIMESTAMP,
                reminder_sent_at = NULL
            RETURNING user_id, plan_code, active_until
        """, (int(user_id), plan_code, months, months)).fetchone()


def get_user_subscriptions(user_id: int):
    """Return all currently active subscription products for one user."""
    with get_connection() as conn:
        return list(conn.execute("""
            SELECT user_id, plan_code, active_until
            FROM telegram_subscription_entitlements
            WHERE user_id = %s AND active_until > CURRENT_TIMESTAMP
            ORDER BY active_until DESC, plan_code
        """, (int(user_id),)).fetchall())


def revoke_subscription(user_id: int, plan_code: str) -> bool:
    """Revoke one product entitlement and its legacy row, if present."""
    if plan_code not in {"self", "project", "multi"}:
        raise ValueError("Некорректный тариф подписки")
    with get_connection() as conn:
        entitlement = conn.execute("""
            DELETE FROM telegram_subscription_entitlements
            WHERE user_id = %s AND plan_code = %s
        """, (int(user_id), plan_code))
        legacy = conn.execute("""
            DELETE FROM telegram_subscriptions
            WHERE user_id = %s AND plan_code = %s
        """, (int(user_id), plan_code))
        return entitlement.rowcount > 0 or legacy.rowcount > 0


#! ============ ПОЛЬЗОВАТЕЛИ ============

def add_or_update_user(user_id: int, username: str = None, first_name: str = None, last_name: str = None):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, first_name, last_name, last_seen)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                last_seen = CURRENT_TIMESTAMP
        """, (user_id, username, first_name, last_name))


def delete_user(user_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM users WHERE user_id = %s", (int(user_id),))
        return cursor.rowcount == 1


def get_all_users():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT user_id, username, first_name, last_name, first_seen, last_seen
            FROM users ORDER BY last_seen DESC
        """).fetchall()
        return list(rows)


def get_user_count():
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        return row["count"]


def open_support_session(user_id: int):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO support_sessions (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
            (user_id,),
        )


def get_open_support_sessions():
    with get_connection() as conn:
        rows = conn.execute("SELECT user_id FROM support_sessions").fetchall()
        return [int(row["user_id"]) for row in rows]


def save_support_message_route(admin_chat_id: int, message_id: int, user_id: int):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO support_message_routes (admin_chat_id, message_id, user_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (admin_chat_id, message_id)
            DO UPDATE SET user_id = EXCLUDED.user_id
        """, (int(admin_chat_id), int(message_id), int(user_id)))


def get_support_message_routes():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT admin_chat_id, message_id, user_id FROM support_message_routes"
        ).fetchall()
        return {
            (int(row["admin_chat_id"]), int(row["message_id"])): int(row["user_id"])
            for row in rows
        }


def close_support_session(user_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM support_sessions WHERE user_id = %s", (user_id,))
        conn.execute("DELETE FROM support_message_routes WHERE user_id = %s", (user_id,))


def select_users():
    with get_connection() as conn:
        rows = conn.execute("SELECT user_id, username FROM users ORDER BY user_id ASC").fetchall()
        return list(rows)


#! ============ ОТЗЫВЫ ПОЛЬЗОВАТЕЛЕЙ ============

def offer_review(user_id: int) -> bool:
    """Create one review invitation after the user's first successful connection."""
    with get_connection() as conn:
        row = conn.execute("""
            INSERT INTO customer_reviews (user_id, status)
            VALUES (%s, 'eligible')
            ON CONFLICT (user_id) DO NOTHING
            RETURNING user_id
        """, (int(user_id),)).fetchone()
        return row is not None


def prepare_review_after_connection(user_id: int):
    """Rearm a skipped review offer after a new successful connection."""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO customer_reviews (user_id, status)
            VALUES (%s, 'eligible')
            ON CONFLICT (user_id) DO UPDATE SET
                status = CASE
                    WHEN customer_reviews.status IN ('skipped', 'rejected') THEN 'eligible'
                    ELSE customer_reviews.status
                END,
                review_text = CASE
                    WHEN customer_reviews.status IN ('skipped', 'rejected') THEN NULL
                    ELSE customer_reviews.review_text
                END,
                updated_at = CASE
                    WHEN customer_reviews.status IN ('skipped', 'rejected') THEN CURRENT_TIMESTAMP
                    ELSE customer_reviews.updated_at
                END
        """, (int(user_id),))


def begin_review(user_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("""
            UPDATE customer_reviews SET status = 'writing', updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s AND status IN ('eligible', 'invited', 'writing')
        """, (int(user_id),))
        return cursor.rowcount == 1


def claim_review_invitation(user_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("""
            UPDATE customer_reviews SET status = 'invited', updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s AND status = 'eligible'
        """, (int(user_id),))
        return cursor.rowcount == 1


def release_review_invitation(user_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("""
            UPDATE customer_reviews SET status = 'eligible', updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s AND status = 'invited'
        """, (int(user_id),))
        return cursor.rowcount == 1


def get_review_status(user_id: int):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT status FROM customer_reviews WHERE user_id = %s", (int(user_id),)
        ).fetchone()
        return row["status"] if row else None


def skip_review(user_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("""
            UPDATE customer_reviews SET status = 'skipped', updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s AND status IN ('eligible', 'invited')
        """, (int(user_id),))
        return cursor.rowcount == 1


def cancel_review(user_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("""
            UPDATE customer_reviews SET status = 'skipped', updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s AND status = 'writing'
        """, (int(user_id),))
        return cursor.rowcount == 1


def save_pending_review(user_id: int, review_text: str) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("""
            UPDATE customer_reviews
            SET status = 'pending', review_text = %s, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s AND status = 'writing'
        """, (str(review_text), int(user_id)))
        return cursor.rowcount == 1


def moderate_review(user_id: int, decision: str):
    if decision not in ('approved', 'rejected'):
        raise ValueError('Некорректное решение по отзыву')
    with get_connection() as conn:
        return conn.execute("""
            UPDATE customer_reviews
            SET status = %s, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s AND status = 'pending'
            RETURNING user_id, review_text, status, created_at, updated_at
        """, (decision, int(user_id))).fetchone()


def get_approved_reviews():
    with get_connection() as conn:
        return list(conn.execute("""
            SELECT r.user_id, r.review_text, r.created_at, u.username,
                   u.first_name, u.last_name
            FROM customer_reviews AS r
            LEFT JOIN users AS u ON u.user_id = r.user_id
            WHERE r.status = 'approved'
              AND r.review_text IS NOT NULL
              AND LENGTH(BTRIM(r.review_text)) > 0
            ORDER BY r.created_at DESC
        """).fetchall())


def create_review_entry(user_id: int, connection_key: str):
    """Return the review invitation associated with one successful connection."""
    connection_key = str(connection_key)
    with get_connection() as conn:
        row = conn.execute("""
            INSERT INTO customer_review_entries (user_id, connection_key, status)
            VALUES (%s, %s, 'eligible')
            ON CONFLICT (user_id, connection_key) WHERE connection_key IS NOT NULL DO NOTHING
            RETURNING review_id, user_id, status
        """, (int(user_id), connection_key)).fetchone()
        if row:
            return row
        return conn.execute("""
            SELECT review_id, user_id, status
            FROM customer_review_entries
            WHERE user_id = %s AND connection_key = %s
        """, (int(user_id), connection_key)).fetchone()


def get_review_entry(review_id: int):
    with get_connection() as conn:
        return conn.execute("""
            SELECT r.review_id, r.user_id, r.connection_key, r.status, r.review_text,
                   r.created_at, r.updated_at, u.username, u.first_name, u.last_name
            FROM customer_review_entries AS r
            LEFT JOIN users AS u ON u.user_id = r.user_id
            WHERE r.review_id = %s
        """, (int(review_id),)).fetchone()


def get_review_entry_status(review_id: int):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT status FROM customer_review_entries WHERE review_id = %s",
            (int(review_id),),
        ).fetchone()
        return row["status"] if row else None


def get_review_entry_owner(review_id: int):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT user_id FROM customer_review_entries WHERE review_id = %s",
            (int(review_id),),
        ).fetchone()
        return int(row["user_id"]) if row else None


def claim_review_entry_invitation(review_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("""
            UPDATE customer_review_entries
            SET status = 'invited', updated_at = CURRENT_TIMESTAMP
            WHERE review_id = %s AND status = 'eligible'
        """, (int(review_id),))
        return cursor.rowcount == 1


def release_review_entry_invitation(review_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("""
            UPDATE customer_review_entries
            SET status = 'eligible', updated_at = CURRENT_TIMESTAMP
            WHERE review_id = %s AND status = 'invited'
        """, (int(review_id),))
        return cursor.rowcount == 1


def begin_review_entry(review_id: int, user_id: int) -> bool:
    with get_connection() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (int(user_id),))
            active = conn.execute("""
                SELECT 1 AS writing
                FROM customer_review_entries
                WHERE user_id = %s AND status = 'writing'
                LIMIT 1
            """, (int(user_id),)).fetchone()
            if active:
                return False
            cursor = conn.execute("""
                UPDATE customer_review_entries
                SET status = 'writing', updated_at = CURRENT_TIMESTAMP
                WHERE review_id = %s AND user_id = %s AND status IN ('eligible', 'invited')
            """, (int(review_id), int(user_id)))
            return cursor.rowcount == 1


def get_user_writing_review_id(user_id: int):
    with get_connection() as conn:
        row = conn.execute("""
            SELECT review_id FROM customer_review_entries
            WHERE user_id = %s AND status = 'writing'
            ORDER BY updated_at DESC
            LIMIT 1
        """, (int(user_id),)).fetchone()
        return int(row["review_id"]) if row else None


def skip_review_entry(review_id: int, user_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("""
            UPDATE customer_review_entries
            SET status = 'skipped', updated_at = CURRENT_TIMESTAMP
            WHERE review_id = %s AND user_id = %s AND status IN ('eligible', 'invited')
        """, (int(review_id), int(user_id)))
        return cursor.rowcount == 1


def cancel_review_entry(review_id: int, user_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("""
            UPDATE customer_review_entries
            SET status = 'skipped', updated_at = CURRENT_TIMESTAMP
            WHERE review_id = %s AND user_id = %s AND status = 'writing'
        """, (int(review_id), int(user_id)))
        return cursor.rowcount == 1


def save_pending_review_entry(review_id: int, user_id: int, review_text: str) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("""
            UPDATE customer_review_entries
            SET status = 'pending', review_text = %s, updated_at = CURRENT_TIMESTAMP
            WHERE review_id = %s AND user_id = %s AND status = 'writing'
        """, (str(review_text), int(review_id), int(user_id)))
        return cursor.rowcount == 1


def moderate_review_entry(review_id: int, decision: str):
    if decision not in ("approved", "rejected"):
        raise ValueError("Некорректное решение по отзыву")
    with get_connection() as conn:
        return conn.execute("""
            UPDATE customer_review_entries
            SET status = %s, updated_at = CURRENT_TIMESTAMP
            WHERE review_id = %s AND status = 'pending'
            RETURNING review_id, user_id, review_text, status, created_at, updated_at
        """, (decision, int(review_id))).fetchone()


def get_approved_review_entries():
    with get_connection() as conn:
        return list(conn.execute("""
            SELECT r.review_id, r.user_id, r.review_text, r.created_at, u.username,
                   u.first_name, u.last_name
            FROM customer_review_entries AS r
            LEFT JOIN users AS u ON u.user_id = r.user_id
            WHERE r.status = 'approved'
              AND r.review_text IS NOT NULL
              AND LENGTH(BTRIM(r.review_text)) > 0
            ORDER BY r.created_at DESC, r.review_id DESC
        """).fetchall())


def delete_approved_review_entry(review_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("""
            DELETE FROM customer_review_entries
            WHERE review_id = %s AND status = 'approved'
        """, (int(review_id),))
        return cursor.rowcount == 1


#! ============ ЧАТЫ ============

def add_chat(
    chat_id: int,
    title: str,
    max_url: str = None,
    phone: str = None,
    owner_id: int = None,
    account_mode: str = "self",
):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO chats
                (chat_id, title, max_url, phone, is_active, message_count, owner_id, account_mode)
            VALUES (%s, %s, %s, %s, FALSE, 0, %s, %s)
            ON CONFLICT (chat_id) DO NOTHING
        """, (chat_id, title, max_url, phone, owner_id, account_mode))
        if owner_id is not None:
            conn.execute(
                "UPDATE chats SET owner_id = COALESCE(owner_id, %s) WHERE chat_id = %s",
                (owner_id, chat_id),
            )


def claim_chat(chat_id: int, title: str, owner_id: int, account_mode: str) -> bool:
    """Bind a Telegram destination to one customer; never transfer an existing owner."""
    with get_connection() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (int(owner_id),))
            conn.execute("""
                INSERT INTO chats (chat_id, title, owner_id, account_mode, is_active, message_count)
                VALUES (%s, %s, NULL, %s, FALSE, 0)
                ON CONFLICT (chat_id) DO NOTHING
            """, (chat_id, title, account_mode))
            row = conn.execute(
                "SELECT owner_id FROM chats WHERE chat_id = %s FOR UPDATE", (chat_id,)
            ).fetchone()
            if row and row["owner_id"] is not None and int(row["owner_id"]) != int(owner_id):
                return False
            if not row or row["owner_id"] is None:
                count = conn.execute(
                    "SELECT COUNT(*) AS count FROM chats WHERE owner_id = %s",
                    (int(owner_id),),
                ).fetchone()["count"]
                has_multi = conn.execute("""
                    SELECT 1 AS active
                    FROM telegram_subscription_entitlements
                    WHERE user_id = %s AND plan_code = 'multi'
                      AND active_until > CURRENT_TIMESTAMP
                    LIMIT 1
                """, (int(owner_id),)).fetchone() is not None
                chat_limit = MULTI_MAX_CUSTOMER_CHATS if has_multi else MAX_CUSTOMER_CHATS
                if int(count) >= chat_limit:
                    raise ChatLimitReached
            conn.execute("""
                UPDATE chats
                SET title = %s, owner_id = %s, account_mode = %s, max_url = NULL,
                    phone = NULL, is_active = FALSE
                WHERE chat_id = %s
            """, (title, owner_id, account_mode, chat_id))
            return True


def get_user_chats(owner_id: int):
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM chats WHERE owner_id = %s ORDER BY added_at DESC", (owner_id,)
        ).fetchall()
        return list(rows)


def update_chat_url(chat_id: int, max_url: str):
    with get_connection() as conn:
        conn.execute("UPDATE chats SET max_url = %s WHERE chat_id = %s", (max_url, chat_id))


def update_chat_phone(chat_id: int, phone: str):
    with get_connection() as conn:
        conn.execute("UPDATE chats SET phone = %s WHERE chat_id = %s", (phone, chat_id))


def update_chat_title(chat_id: int, title: str):
    with get_connection() as conn:
        conn.execute("UPDATE chats SET title = %s WHERE chat_id = %s", (title, chat_id))


def get_chat_by_id(chat_id: int):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM chats WHERE chat_id = %s", (chat_id,)).fetchone()
        return row


def get_active_chats():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM chats
            WHERE is_active = TRUE AND max_url IS NOT NULL
        """).fetchall()
        return list(rows)


def get_all_chats():
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM chats ORDER BY added_at DESC").fetchall()
        return list(rows)


def toggle_chat(chat_id: int, active: bool):
    with get_connection() as conn:
        conn.execute("UPDATE chats SET is_active = %s WHERE chat_id = %s", (bool(active), chat_id))


def delete_chat(chat_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM chats WHERE chat_id = %s", (chat_id,))


#! ============ СЧЕТЧИКИ СООБЩЕНИЙ ============

def increment_message_count(chat_id: int):
    sent_date = datetime.now(STATS_TIMEZONE).date()
    with get_connection() as conn:
        updated = conn.execute(
            "UPDATE chats SET message_count = message_count + 1 WHERE chat_id = %s RETURNING chat_id",
            (chat_id,),
        ).fetchone()
        if not updated:
            return
        conn.execute("""
            INSERT INTO chat_message_daily_stats (chat_id, sent_date, sent_count)
            VALUES (%s, %s, 1)
            ON CONFLICT (chat_id, sent_date)
            DO UPDATE SET sent_count = chat_message_daily_stats.sent_count + 1
        """, (chat_id, sent_date))


def get_chat_stats(chat_id: int):
    today = datetime.now(STATS_TIMEZONE).date()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT message_count FROM chats WHERE chat_id = %s", (chat_id,)
        ).fetchone()
        daily_row = conn.execute(
            "SELECT sent_count FROM chat_message_daily_stats WHERE chat_id = %s AND sent_date = %s",
            (chat_id, today),
        ).fetchone()
    count = row["message_count"] if row else 0
    today_count = daily_row["sent_count"] if daily_row else 0
    return {"total": count, "today": today_count}


def get_global_stats():
    today = datetime.now(STATS_TIMEZONE).date()
    with get_connection() as conn:
        total_chats = conn.execute("SELECT COUNT(*) AS count FROM chats").fetchone()["count"]
        active_chats = conn.execute(
            "SELECT COUNT(*) AS count FROM chats WHERE is_active = TRUE"
        ).fetchone()["count"]
        total_msgs = conn.execute(
            "SELECT COALESCE(SUM(message_count), 0) AS count FROM chats"
        ).fetchone()["count"]
        today_msgs = conn.execute(
            "SELECT COALESCE(SUM(sent_count), 0) AS count FROM chat_message_daily_stats WHERE sent_date = %s",
            (today,),
        ).fetchone()["count"]
    return {
        "total_chats": total_chats,
        "active_chats": active_chats,
        "total_msgs": total_msgs,
        "today_msgs": today_msgs,
    }


#! ============ TELEGRAM PAYMENTS ============

def create_payment_order(
    user_id: int, plan_code: str, amount: int, duration_months: int = 1
) -> str:
    if plan_code not in ("self", "project", "multi"):
        raise ValueError("Некорректный тариф")
    duration_months = int(duration_months)
    if duration_months not in {1, 3, 12}:
        raise ValueError("Некорректный срок подписки")
    payload = f"maxparser_{uuid.uuid4().hex}"
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO telegram_payment_orders
                (payload, user_id, plan_code, duration_months, amount)
            VALUES (%s, %s, %s, %s, %s)
        """, (payload, int(user_id), plan_code, duration_months, int(amount)))
    return payload


def get_payment_order(payload: str):
    with get_connection() as conn:
        return conn.execute("""
            SELECT payload, user_id, plan_code, duration_months, amount, currency, status,
                   created_at, expires_at, paid_at,
                   provider_payment_charge_id, telegram_payment_charge_id
            FROM telegram_payment_orders
            WHERE payload = %s
        """, (payload,)).fetchone()


def claim_payment_order_for_checkout(
    payload: str, user_id: int, currency: str, total_amount: int
) -> bool:
    """Atomically accept one checkout attempt for a pending, matching invoice."""
    with get_connection() as conn:
        row = conn.execute("""
            UPDATE telegram_payment_orders
            SET status = 'checkout'
            WHERE payload = %s
              AND user_id = %s
              AND currency = %s
              AND amount = %s
              AND status IN ('pending', 'checkout')
              AND expires_at > CURRENT_TIMESTAMP
            RETURNING payload
        """, (payload, int(user_id), currency, int(total_amount))).fetchone()
        return row is not None


def complete_payment_order(
    payload: str,
    user_id: int,
    currency: str,
    total_amount: int,
    provider_payment_charge_id: str,
    telegram_payment_charge_id: str,
):
    """Record a successful payment and extend the account subscription once."""
    with get_connection() as conn:
        with conn.transaction():
            order = conn.execute("""
                SELECT payload, user_id, plan_code, duration_months, amount, currency, status
                FROM telegram_payment_orders
                WHERE payload = %s
                FOR UPDATE
            """, (payload,)).fetchone()
            if not order:
                raise ValueError("Счёт не найден")
            if int(order["user_id"]) != int(user_id):
                raise ValueError("Счёт принадлежит другому пользователю")
            if order["currency"] != currency or int(order["amount"]) != int(total_amount):
                raise ValueError("Сумма или валюта платежа не совпадает со счётом")
            if order["status"] == "paid":
                subscription = get_subscription(user_id)
                return {
                    "already_paid": True,
                    "plan_code": order["plan_code"],
                    "duration_months": order["duration_months"],
                    "active_until": subscription["active_until"] if subscription else None,
                }
            if order["status"] != "checkout":
                raise ValueError("Счёт не был подтверждён перед оплатой")

            subscription = conn.execute("""
                INSERT INTO telegram_subscription_entitlements
                    (user_id, plan_code, active_until)
                VALUES (%s, %s, CURRENT_TIMESTAMP + (%s * INTERVAL '1 month'))
                ON CONFLICT (user_id, plan_code) DO UPDATE SET
                    active_until = GREATEST(
                        telegram_subscription_entitlements.active_until, CURRENT_TIMESTAMP
                    ) + (%s * INTERVAL '1 month'),
                    updated_at = CURRENT_TIMESTAMP,
                    reminder_sent_at = NULL
                RETURNING active_until
            """, (
                int(user_id), order["plan_code"], int(order["duration_months"]),
                int(order["duration_months"]),
            )).fetchone()

            conn.execute("""
                UPDATE telegram_payment_orders
                SET status = 'paid',
                    paid_at = CURRENT_TIMESTAMP,
                    provider_payment_charge_id = %s,
                    telegram_payment_charge_id = %s
                WHERE payload = %s
            """, (
                provider_payment_charge_id,
                telegram_payment_charge_id,
                payload,
            ))
            return {
                "already_paid": False,
                "plan_code": order["plan_code"],
                "duration_months": order["duration_months"],
                "active_until": subscription["active_until"],
            }


def get_subscription(user_id: int):
    with get_connection() as conn:
        return conn.execute("""
            SELECT user_id, plan_code, active_until, updated_at, reminder_sent_at
            FROM telegram_subscription_entitlements
            WHERE user_id = %s
            ORDER BY active_until DESC
            LIMIT 1
        """, (int(user_id),)).fetchone()


def is_subscription_active(user_id: int, account_mode: str = None) -> bool:
    if account_mode not in (None, "self", "project"):
        raise ValueError("Некорректный способ подключения")
    with get_connection() as conn:
        if account_mode is None:
            row = conn.execute("""
                SELECT 1 AS active
                FROM telegram_subscription_entitlements
                WHERE user_id = %s AND active_until > CURRENT_TIMESTAMP
                LIMIT 1
            """, (int(user_id),)).fetchone()
        else:
            row = conn.execute("""
                SELECT 1 AS active
                FROM telegram_subscription_entitlements
                WHERE user_id = %s
                  AND plan_code IN (%s, 'multi')
                  AND active_until > CURRENT_TIMESTAMP
                LIMIT 1
            """, (int(user_id), account_mode)).fetchone()
        return row is not None


def get_customer_chat_limit(user_id: int) -> int:
    with get_connection() as conn:
        row = conn.execute("""
            SELECT 1 AS active
            FROM telegram_subscription_entitlements
            WHERE user_id = %s AND plan_code = 'multi'
              AND active_until > CURRENT_TIMESTAMP
            LIMIT 1
        """, (int(user_id),)).fetchone()
        return MULTI_MAX_CUSTOMER_CHATS if row else MAX_CUSTOMER_CHATS


def get_active_subscription_user_ids():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT DISTINCT user_id FROM telegram_subscription_entitlements
            WHERE active_until > CURRENT_TIMESTAMP
        """).fetchall()
        return {int(row["user_id"]) for row in rows}


def get_active_subscription_access():
    """Return each user's currently active product entitlements."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT user_id, plan_code
            FROM telegram_subscription_entitlements
            WHERE active_until > CURRENT_TIMESTAMP
        """).fetchall()
        access = {}
        for row in rows:
            access.setdefault(int(row["user_id"]), set()).add(row["plan_code"])
        return access


def get_due_subscription_reminders():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT user_id, plan_code, active_until
            FROM telegram_subscription_entitlements
            WHERE active_until > CURRENT_TIMESTAMP
              AND active_until <= CURRENT_TIMESTAMP + INTERVAL '3 days'
              AND reminder_sent_at IS NULL
            ORDER BY active_until
        """).fetchall()
        return list(rows)


def mark_subscription_reminder_sent(user_id: int, plan_code: str):
    with get_connection() as conn:
        conn.execute("""
            UPDATE telegram_subscription_entitlements
            SET reminder_sent_at = CURRENT_TIMESTAMP
            WHERE user_id = %s
              AND plan_code = %s
              AND active_until > CURRENT_TIMESTAMP
              AND active_until <= CURRENT_TIMESTAMP + INTERVAL '3 days'
              AND reminder_sent_at IS NULL
        """, (int(user_id), plan_code))




init_db()
