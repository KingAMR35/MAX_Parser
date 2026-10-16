import os
import uuid

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_NAME = os.getenv("PGDATABASE", "MAX_Parser")


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
            CREATE TABLE IF NOT EXISTS support_sessions (
                user_id BIGINT PRIMARY KEY,
                opened_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS support_message_routes (
                message_id BIGINT PRIMARY KEY,
                user_id BIGINT NOT NULL
            )
        """)

        # Separate names keep the Telegram payment schema independent from any
        # earlier experimental payment tables that may still exist in Postgres.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_payment_orders (
                payload TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                plan_code TEXT NOT NULL CHECK (plan_code IN ('self', 'project')),
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
            CREATE TABLE IF NOT EXISTS telegram_subscriptions (
                user_id BIGINT PRIMARY KEY,
                plan_code TEXT NOT NULL CHECK (plan_code IN ('self', 'project')),
                active_until TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reminder_sent_at TIMESTAMPTZ
            )
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
        conn.execute("DELETE FROM admins WHERE user_id = %s", (user_id,))


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


def save_support_message_route(message_id: int, user_id: int):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO support_message_routes (message_id, user_id) VALUES (%s, %s)
            ON CONFLICT (message_id) DO UPDATE SET user_id = EXCLUDED.user_id
        """, (message_id, user_id))


def get_support_message_routes():
    with get_connection() as conn:
        rows = conn.execute("SELECT message_id, user_id FROM support_message_routes").fetchall()
        return {int(row["message_id"]): int(row["user_id"]) for row in rows}


def close_support_session(user_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM support_sessions WHERE user_id = %s", (user_id,))
        conn.execute("DELETE FROM support_message_routes WHERE user_id = %s", (user_id,))


def select_users():
    with get_connection() as conn:
        rows = conn.execute("SELECT user_id, username FROM users ORDER BY user_id ASC").fetchall()
        return list(rows)


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
            conn.execute("""
                INSERT INTO chats (chat_id, title, owner_id, account_mode, is_active, message_count)
                VALUES (%s, %s, %s, %s, FALSE, 0)
                ON CONFLICT (chat_id) DO NOTHING
            """, (chat_id, title, owner_id, account_mode))
            row = conn.execute(
                "SELECT owner_id FROM chats WHERE chat_id = %s FOR UPDATE", (chat_id,)
            ).fetchone()
            if row and row["owner_id"] is not None and int(row["owner_id"]) != int(owner_id):
                return False
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
    with get_connection() as conn:
        conn.execute("UPDATE chats SET message_count = message_count + 1 WHERE chat_id = %s", (chat_id,))


def get_chat_stats(chat_id: int):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT message_count FROM chats WHERE chat_id = %s", (chat_id,)
        ).fetchone()
    count = row["message_count"] if row else 0
    return {"total": count, "today": count}


def get_global_stats():
    with get_connection() as conn:
        total_chats = conn.execute("SELECT COUNT(*) AS count FROM chats").fetchone()["count"]
        active_chats = conn.execute(
            "SELECT COUNT(*) AS count FROM chats WHERE is_active = TRUE"
        ).fetchone()["count"]
        total_msgs = conn.execute(
            "SELECT COALESCE(SUM(message_count), 0) AS count FROM chats"
        ).fetchone()["count"]
    return {
        "total_chats": total_chats,
        "active_chats": active_chats,
        "total_msgs": total_msgs,
        "today_msgs": total_msgs,
    }


#! ============ TELEGRAM PAYMENTS / SUBSCRIPTIONS ============

def create_payment_order(user_id: int, plan_code: str, amount: int) -> str:
    if plan_code not in ("self", "project"):
        raise ValueError("Некорректный тариф")
    payload = f"maxparser_{uuid.uuid4().hex}"
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO telegram_payment_orders (payload, user_id, plan_code, amount)
            VALUES (%s, %s, %s, %s)
        """, (payload, int(user_id), plan_code, int(amount)))
    return payload


def get_payment_order(payload: str):
    with get_connection() as conn:
        return conn.execute("""
            SELECT payload, user_id, plan_code, amount, currency, status,
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
                SELECT payload, user_id, plan_code, amount, currency, status
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
                    "active_until": subscription["active_until"] if subscription else None,
                }
            if order["status"] != "checkout":
                raise ValueError("Счёт не был подтверждён перед оплатой")

            subscription = conn.execute("""
                INSERT INTO telegram_subscriptions (user_id, plan_code, active_until)
                VALUES (%s, %s, CURRENT_TIMESTAMP + INTERVAL '30 days')
                ON CONFLICT (user_id) DO UPDATE SET
                    plan_code = EXCLUDED.plan_code,
                    active_until = GREATEST(
                        telegram_subscriptions.active_until, CURRENT_TIMESTAMP
                    ) + INTERVAL '30 days',
                    updated_at = CURRENT_TIMESTAMP,
                    reminder_sent_at = NULL
                RETURNING active_until
            """, (int(user_id), order["plan_code"])).fetchone()

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
                "active_until": subscription["active_until"],
            }


def get_subscription(user_id: int):
    with get_connection() as conn:
        return conn.execute("""
            SELECT user_id, plan_code, active_until, updated_at, reminder_sent_at
            FROM telegram_subscriptions
            WHERE user_id = %s
        """, (int(user_id),)).fetchone()


def is_subscription_active(user_id: int) -> bool:
    with get_connection() as conn:
        row = conn.execute("""
            SELECT 1 AS active
            FROM telegram_subscriptions
            WHERE user_id = %s AND active_until > CURRENT_TIMESTAMP
        """, (int(user_id),)).fetchone()
        return row is not None


def get_active_subscription_user_ids():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT user_id FROM telegram_subscriptions
            WHERE active_until > CURRENT_TIMESTAMP
        """).fetchall()
        return {int(row["user_id"]) for row in rows}


def get_due_subscription_reminders():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT user_id, plan_code, active_until
            FROM telegram_subscriptions
            WHERE active_until > CURRENT_TIMESTAMP
              AND active_until <= CURRENT_TIMESTAMP + INTERVAL '3 days'
              AND reminder_sent_at IS NULL
            ORDER BY active_until
        """).fetchall()
        return list(rows)


def mark_subscription_reminder_sent(user_id: int):
    with get_connection() as conn:
        conn.execute("""
            UPDATE telegram_subscriptions
            SET reminder_sent_at = CURRENT_TIMESTAMP
            WHERE user_id = %s
              AND active_until > CURRENT_TIMESTAMP
              AND active_until <= CURRENT_TIMESTAMP + INTERVAL '3 days'
              AND reminder_sent_at IS NULL
        """, (int(user_id),))




init_db()
