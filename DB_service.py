import sqlite3
import os
from datetime import datetime

DB_PATH = "MAX_Parser.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Создаёт таблицы при первом запуске"""
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
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            max_url TEXT,
            is_active INTEGER DEFAULT 1,
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
    
    conn.commit()
    conn.close()


# ============ АДМИНЫ ============

def add_admin(user_id: int, username: str = None):
    conn = get_connection()
    conn.execute("INSERT OR IGNORE INTO admins (user_id, username) VALUES (?, ?)", (user_id, username))
    conn.commit()
    conn.close()


def is_admin(user_id: int) -> bool:
    conn = get_connection()
    row = conn.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row is not None


def get_all_admins():
    conn = get_connection()
    rows = conn.execute("SELECT user_id, username FROM admins").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============ ЧАТЫ ============

def add_chat(chat_id: int, title: str, max_url: str):
    conn = get_connection()
    conn.execute("""
        INSERT OR REPLACE INTO chats (chat_id, title, max_url, is_active)
        VALUES (?, ?, ?, 1)
    """, (chat_id, title, max_url))
    conn.commit()
    conn.close()


def get_active_chats():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM chats WHERE is_active = 1").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_chats():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM chats ORDER BY added_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def toggle_chat(chat_id: int, active: bool):
    conn = get_connection()
    conn.execute("UPDATE chats SET is_active = ? WHERE chat_id = ?", (1 if active else 0, chat_id))
    conn.commit()
    conn.close()


def delete_chat(chat_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
    conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()



def save_message(chat_id: int, sender_name: str, sender_role: str, text: str, msg_time: str):
    conn = get_connection()
    conn.execute("""
        INSERT INTO messages (chat_id, sender_name, sender_role, text, msg_time)
        VALUES (?, ?, ?, ?, ?)
    """, (chat_id, sender_name, sender_role, text, msg_time))
    conn.commit()
    conn.close()


def get_chat_stats(chat_id: int):
    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,)).fetchone()[0]
    today = conn.execute("""
        SELECT COUNT(*) FROM messages 
        WHERE chat_id = ? AND date(parsed_at) = date('now')
    """, (chat_id,)).fetchone()[0]
    conn.close()
    return {"total": total, "today": today}


def get_recent_messages(chat_id: int, limit: int = 10):
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM messages 
        WHERE chat_id = ? 
        ORDER BY parsed_at DESC 
        LIMIT ?
    """, (chat_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_global_stats():
    conn = get_connection()
    total_chats = conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
    active_chats = conn.execute("SELECT COUNT(*) FROM chats WHERE is_active = 1").fetchone()[0]
    total_msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    today_msgs = conn.execute("SELECT COUNT(*) FROM messages WHERE date(parsed_at) = date('now')").fetchone()[0]
    conn.close()
    return {
        "total_chats": total_chats,
        "active_chats": active_chats,
        "total_msgs": total_msgs,
        "today_msgs": today_msgs
    }



init_db()