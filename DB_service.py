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
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            sender_name TEXT,
            sender_role TEXT,
            text TEXT,
            msg_time TEXT,
            parsed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (chat_id) REFERENCES chats(chat_id)
        )
    """)

    try:
        con.execute("ALTER TABLE chats ADD COLUMN phone TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass

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
        INSERT OR IGNORE INTO chats (chat_id, title, max_url, phone, is_active)
        VALUES (?, ?, ?, ?, 0)
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
    conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


#! ============ СООБЩЕНИЯ ============

def save_message(chat_id: int, sender_name: str, sender_role: str, text: str, msg_time: str):
    conn = get_connection()
    conn.execute("""
        INSERT INTO messages (chat_id, sender_name, sender_role, text, msg_time)
        VALUES (?, ?, ?, ?, ?)
    """, (chat_id, sender_name, sender_role, text, msg_time))
    conn.commit()
    conn.close()

    trim_messages(chat_id, 100)


def get_chat_stats(chat_id: int):
    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,)).fetchone()[0]
    today = conn.execute("""
        SELECT COUNT(*) FROM messages
        WHERE chat_id = ? AND date(parsed_at) = date('now')
    """, (chat_id,)).fetchone()[0]
    conn.close()
    return {"total": total, "today": today}


def get_global_stats():
    conn = get_connection()
    total_chats = conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
    active_chats = conn.execute("SELECT COUNT(*) FROM chats WHERE is_active = 1").fetchone()[0]
    total_msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    today_msgs = conn.execute("""
        SELECT COUNT(*) FROM messages WHERE date(parsed_at) = date('now')
    """).fetchone()[0]
    conn.close()
    return {
        "total_chats": total_chats,
        "active_chats": active_chats,
        "total_msgs": total_msgs,
        "today_msgs": today_msgs
    }

def trim_messages(chat_id: int, max_count: int = 100):
    """Оставляет только последние max_count сообщений для чата"""
    conn = get_connection()
    conn.execute("""
        DELETE FROM messages 
        WHERE chat_id = ? AND id NOT IN (
            SELECT id FROM messages 
            WHERE chat_id = ? 
            ORDER BY parsed_at DESC 
            LIMIT ?
        )
    """, (chat_id, chat_id, max_count))
    conn.commit()
    conn.close()

def get_recent_messages(chat_id: int, limit: int = 10):
    """Возвращает последние сообщения для чата"""
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM messages 
        WHERE chat_id = ? 
        ORDER BY parsed_at DESC 
        LIMIT ?
    """, (chat_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

init_db()