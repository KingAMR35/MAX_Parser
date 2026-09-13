import sqlite3
import os
from datetime import datetime

DB_PATH = "MAX_Parser.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    con = conn.cursor()

    con.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            max_url TEXT,
            phone TEXT,
            is_active INTEGER DEFAULT 0,
            message_count INTEGER DEFAULT 0,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Миграции для существующей БД
    try:
        con.execute("ALTER TABLE chats ADD COLUMN message_count INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    try:
        con.execute("ALTER TABLE chats ADD COLUMN phone TEXT")
    except sqlite3.OperationalError:
        pass

    # ВАЖНО: Удаляем старую таблицу messages, если она существует, чтобы освободить место на диске
    con.execute("DROP TABLE IF EXISTS messages")
    
    conn.commit()
    conn.close()


#! ============ АДМИНЫ ============

def add_admin(user_id: int, username: str = None):
    conn = get_connection()
    conn.execute("INSERT OR IGNORE INTO admins (user_id, username) VALUES (?, ?)", (user_id, username))
    conn.commit()
    conn.close()


def update_admin_username(user_id: int, username: str):
    conn = get_connection()
    conn.execute("UPDATE admins SET username = ? WHERE user_id = ?", (username, user_id))
    conn.commit()
    conn.close()


def is_admin(user_id: int) -> bool:
    conn = get_connection()
    row = conn.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row is not None


def get_all_admins():
    conn = get_connection()
    rows = conn.execute("SELECT user_id, username FROM admins ORDER BY added_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_admin(user_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


#! ============ ПОЛЬЗОВАТЕЛИ ============

def add_or_update_user(user_id: int, username: str = None, first_name: str = None, last_name: str = None):
    conn = get_connection()
    conn.execute("""
        INSERT INTO users (user_id, username, first_name, last_name, last_seen)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_name = excluded.last_name,
            last_seen = CURRENT_TIMESTAMP
    """, (user_id, username, first_name, last_name))
    conn.commit()
    conn.close()


def get_all_users():
    conn = get_connection()
    rows = conn.execute("""
        SELECT user_id, username, first_name, last_name, first_seen, last_seen
        FROM users ORDER BY last_seen DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_user_count():
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return count


#! ============ ЧАТЫ ============

def add_chat(chat_id: int, title: str, max_url: str = None, phone: str = None):
    conn = get_connection()
    conn.execute("""
        INSERT OR IGNORE INTO chats (chat_id, title, max_url, phone, is_active, message_count)
        VALUES (?, ?, ?, ?, 0, 0)
    """, (chat_id, title, max_url, phone))
    conn.commit()
    conn.close()


def update_chat_url(chat_id: int, max_url: str):
    conn = get_connection()
    conn.execute("UPDATE chats SET max_url = ? WHERE chat_id = ?", (max_url, chat_id))
    conn.commit()
    conn.close()


def update_chat_phone(chat_id: int, phone: str):
    conn = get_connection()
    conn.execute("UPDATE chats SET phone = ? WHERE chat_id = ?", (phone, chat_id))
    conn.commit()
    conn.close()


def update_chat_title(chat_id: int, title: str):
    conn = get_connection()
    conn.execute("UPDATE chats SET title = ? WHERE chat_id = ?", (title, chat_id))
    conn.commit()
    conn.close()


def get_chat_by_id(chat_id: int):
    conn = get_connection()
    row = conn.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_active_chats():
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM chats
        WHERE is_active = 1 AND max_url IS NOT NULL AND phone IS NOT NULL
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_chats():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM chats ORDER BY added_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def toggle_chat(chat_id: int, active: bool):
    conn = get_connection()
    conn.execute("UPDATE chats SET is_active = ? WHERE chat_id = ?",
                 (1 if active else 0, chat_id))
    conn.commit()
    conn.close()


def delete_chat(chat_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
    # Удаление записей из messages больше не требуется
    conn.commit()
    conn.close()


#! ============ СЧЕТЧИКИ СООБЩЕНИЙ ============

def increment_message_count(chat_id: int):
    """Увеличивает счетчик сообщений для чата на 1"""
    conn = get_connection()
    conn.execute("UPDATE chats SET message_count = message_count + 1 WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def get_chat_stats(chat_id: int):
    conn = get_connection()
    row = conn.execute("SELECT message_count FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    count = row['message_count'] if row else 0
    # Возвращаем общее число и для "сегодня", так как детальное логирование по дням отключено для экономии места
    return {"total": count, "today": count}


def get_global_stats():
    conn = get_connection()
    total_chats = conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
    active_chats = conn.execute("SELECT COUNT(*) FROM chats WHERE is_active = 1").fetchone()[0]
    
    # Суммируем счетчики всех чатов
    total_msgs_row = conn.execute("SELECT SUM(message_count) FROM chats").fetchone()
    total_msgs = total_msgs_row[0] if total_msgs_row[0] is not None else 0
    
    conn.close()
    return {
        "total_chats": total_chats,
        "active_chats": active_chats,
        "total_msgs": total_msgs,
        "today_msgs": total_msgs
    }

init_db()