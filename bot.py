import telebot
import os
import time
import io
import re
import hashlib
import threading
import unicodedata
from datetime import datetime
from telebot import types
from dotenv import load_dotenv
import concurrent.futures
from max_playwright_parser import *
from DB_service import *

#! ==============================================================================
#! 2) БЛОК ИНИЦИАЛИЗАЦИИ
#! ==============================================================================
load_dotenv()
print("🚀 MAX Parser Bot запущен")
bot = telebot.TeleBot(os.getenv("BOT_TOKEN"), parse_mode='HTML')
ADMIN_ID = int(os.getenv("ADMIN_ID"))
add_admin(ADMIN_ID, "main_admin")
_current_cycle_hashes = {}
NO_PREVIEW = {"disable_web_page_preview": True}
active_logins = {}
awaiting_password_from = {}

#! ============ УТИЛИТЫ ============
bot.set_my_commands([
    types.BotCommand("start", "Запустить бота"),
    types.BotCommand("admin", "Админ-панель")
])

#! ==============================================================================
#! 3) ФУНКЦИИ, СВЯЗАННЫЕ С ПАРСИНГОМ
#! ==============================================================================
def _media_to_bytesio(media: dict) -> io.BytesIO:
    """Создаёт объект для отправки в Telegram без сохранения медиа на диск."""
    data = media.get("bytes")
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError(f"Медиа {media.get('type', 'unknown')} не содержит bytes")
    file_obj = io.BytesIO(bytes(data))
    filename = media.get("filename")
    if filename:
        file_obj.name = filename
    file_obj.seek(0)
    return file_obj

def _media_already_sent(media: dict, chat_id: int) -> bool:
    data = media.get("bytes")
    media_hash = media.get("hash") or get_media_hash(data=data)
    return not is_new_media(data=data, chat_id=chat_id, media_hash=media_hash)

def _send_post(chat_id: int, post: dict) -> None:
    """Отправляет сообщение целиком. Кэш обновляется только после успеха."""
    msg_text = format_message(post)
    media_files = post.get("media_files", []) or []
    
    # Не отправляем одну и ту же фотографию повторно, но само сообщение
    # всё равно будет отправлено, если текст у него новый.
    send_media = [
        media for media in media_files
        if not _media_already_sent(media, chat_id)
    ]
    
    if not send_media:
        bot.send_message(chat_id, msg_text, **NO_PREVIEW)
        return
        
    first = True
    for media in send_media:
        media_type = media.get("type")
        file_obj = _media_to_bytesio(media)
        
        if media_type == "image":
            if first:
                try:
                    # У Telegram caption есть лимит 1024 символа.
                    if len(msg_text) <= 1024:
                        bot.send_photo(chat_id, file_obj, caption=msg_text)
                    else:
                        bot.send_photo(chat_id, file_obj)
                        bot.send_message(chat_id, msg_text, **NO_PREVIEW)
                except Exception as photo_error:
                    print(f"⚠️ send_photo не удался: {photo_error}")
                    file_obj.seek(0)
                    filename = media.get("filename") or "image.jpg"
                    bot.send_document(chat_id, file_obj, visible_file_name=filename)
                    if msg_text:
                        bot.send_message(chat_id, msg_text, **NO_PREVIEW)
            else:
                try:
                    bot.send_photo(chat_id, file_obj)
                except Exception as photo_error:
                    print(f"⚠️ Дополнительное фото не отправилось: {photo_error}")
                    file_obj.seek(0)
                    bot.send_document(
                        chat_id,
                        file_obj,
                        visible_file_name=media.get("filename") or "image.jpg",
                    )
        elif media_type == "document":
            filename = media.get("filename") or "document.pdf"
            if first:
                if len(msg_text) <= 1024:
                    bot.send_document(
                        chat_id,
                        file_obj,
                        caption=msg_text,
                        visible_file_name=filename,
                    )
                else:
                    bot.send_document(chat_id, file_obj, visible_file_name=filename)
                    bot.send_message(chat_id, msg_text, **NO_PREVIEW)
            else:
                bot.send_document(chat_id, file_obj, visible_file_name=filename)
        else:
            raise ValueError(f"Неизвестный тип медиа: {media_type}")
            
        first = False
        time.sleep(0.5)

def _commit_successful_post(chat_id: int, post: dict) -> None:
    """Записывает сообщение и медиа в кэш только после успешной отправки."""
    mark_message_seen(post, chat_id=chat_id)
    for media in post.get("media_files", []) or []:
        data = media.get("bytes")
        if isinstance(data, (bytes, bytearray)) and data:
            mark_media_seen(
                data=data,
                chat_id=chat_id,
                media_hash=media.get("hash"),
            )
    save_message_cache()
    save_photo_cache()

def _process_posts(chat_id: int, title: str, posts: list, send_summary: bool = False) -> tuple:
    """Обрабатывает список постов и возвращает (new_count, skipped_count)."""
    new_count = 0
    skipped_count = 0
    cycle_ids = set()
    
    for post in posts:
        source_id = str(post.get("source_id") or "").strip()
        cycle_key = source_id or get_message_hash(post)
        if cycle_key in cycle_ids:
            skipped_count += 1
            continue
        cycle_ids.add(cycle_key)
        
        if not is_new_message(post, chat_id=chat_id):
            skipped_count += 1
            continue
            
        try:
            _send_post(chat_id, post)
            _commit_successful_post(chat_id, post)
            person_name = post.get("name", "Аноним")
            person_role = ""
            if "│" in person_name:
                person_name, person_role = [part.strip() for part in person_name.split("│", 1)]
            save_message(
                chat_id,
                person_name.replace("👤", "").replace("<b>", "").replace("</b>", "").strip(),
                person_role.replace("<i>", "").replace("</i>", "").strip(),
                post.get("text", ""),
                post.get("time", ""),
            )
            new_count += 1
            time.sleep(1.5)
        except Exception as error:
            print(f"❌ [{chat_id}] Ошибка отправки в чат {title}: {error}")
            
    save_message_cache()
    save_photo_cache()
    
    if send_summary and new_count > 0:
        summary_text = (
            f"✅ <b>Парсинг завершён для: {title}</b>\n\n"
            f"📦 <b>Новых сообщений:</b> {new_count}"
        )
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton(
            "🗑 Удалить это сообщение",
            callback_data="delete_this_msg"
        ))
        try:
            bot.send_message(
                chat_id,
                summary_text,
                parse_mode="HTML",
                reply_markup=kb,
                **NO_PREVIEW,
            )
        except Exception as error:
            print(f"❌ Не удалось отправить отчёт в чат {title}: {error}")
            
    return new_count, skipped_count

def run_one_parse_cycle():
    active_chats = get_active_chats()
    results = []
    if not active_chats:
        return "⚠️ Нет активных чатов для парсинга"
        
    for chat in active_chats:
        chat_id = chat["chat_id"]
        max_url = chat["max_url"]
        title = chat["title"]
        try:
            posts = parse_max_group_media(group_url=max_url, chat_id=chat_id)
            new_count, skipped_count = _process_posts(
                chat_id,
                title,
                posts,
                send_summary=False,
            )
            if new_count:
                results.append(
                    f"✅ <b>{title}</b>: +{new_count} новых, ⏭ {skipped_count} пропущено"
                )
            else:
                results.append(
                    f"ℹ️ <b>{title}</b>: новых сообщений нет (⏭ {skipped_count} пропущено)"
                )
        except Exception as error:
            error_str = str(error)
            if "SESSION_EXPIRED" in error_str:
                debug_path = get_parse_debug_screenshot(chat_id)
                if "|" in error_str:
                    candidate = error_str.split("|", 1)[1]
                    if candidate.endswith(".png"):
                        debug_path = candidate
                error_msg = (
                    f"⚠️ <b>{title}</b>: сессия истекла!\n"
                    f"Используйте `/login {chat_id}`"
                )
                if debug_path and os.path.exists(debug_path):
                    try:
                        with open(debug_path, "rb") as photo:
                            bot.send_photo(
                                ADMIN_ID,
                                photo,
                                caption=f"🔍 Диагностика: {title}",
                                parse_mode="HTML",
                            )
                        clear_parse_debug_screenshot(chat_id)
                    except Exception as send_error:
                        print(f"⚠️ Не удалось отправить диагностику: {send_error}")
                results.append(error_msg)
            elif "NO_SESSION" in error_str:
                results.append(
                    f"⚠️ <b>{title}</b>: нет сессии! Используйте `/login {chat_id}`"
                )
            else:
                print(f"❌ Ошибка парсинга чата {title}: {error}")
                results.append(
                    f"❌ <b>{title}</b>: {escape_html(error_str[:200])}"
                )
                
    if not results:
        return "ℹ️ Нет результатов"
        
    report = "\n\n".join(results)
    success_chats = sum(
        1 for result in results
        if result.startswith("✅") or result.startswith("ℹ️")
    )
    header = (
        f"🧪 <b>Тест парсинга завершен</b>\n\n"
        f"📊 Успешно: {success_chats}/{len(active_chats)}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    return header + report

def process_single_chat(chat):
    """Обрабатывает один чат в отдельном потоке."""
    chat_id = chat["chat_id"]
    max_url = chat["max_url"]
    title = chat["title"]
    try:
        posts = parse_max_group_media(group_url=max_url, chat_id=chat_id)
        _process_posts(chat_id, title, posts, send_summary=True)
        return True
    except Exception as error:
        print(f"❌ Ошибка парсинга чата {title}: {error}")
        return False

def background_parser():
    time.sleep(5)
    while True:
        try:
            active_chats = get_active_chats()
            if not active_chats:
                time.sleep(60)
                continue
            print(f"🔄 Начинаю параллельный парсинг {len(active_chats)} чатов...")
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(active_chats)
            ) as executor:
                futures = [
                    executor.submit(process_single_chat, chat)
                    for chat in active_chats
                ]
                concurrent.futures.wait(futures)
            print("✅ Цикл парсинга завершен. Ожидание 60 секунд до следующего...")
            time.sleep(60)
        except Exception as error:
            print(f"❌ Критическая ошибка парсера: {error}")
            time.sleep(60)

load_message_cache()
threading.Thread(target=background_parser, daemon=True).start()

#! ==============================================================================
#! 4) ОСТАЛЬНЫЕ ФУНКЦИИ
#! ==============================================================================
def visual_len(s):
    length = 0
    for char in s:
        cp = ord(char)
        if (0x1F000 <= cp <= 0x1FFFF or
            0x2600 <= cp <= 0x27BF or
            0x2B50 <= cp <= 0x2B55 or
            0x2300 <= cp <= 0x23FF or
            0x2700 <= cp <= 0x27BF or
            0xFE00 <= cp <= 0xFE0F or
            0x200D == cp or
            0x1F900 <= cp <= 0x1F9FF or
            0x1FA00 <= cp <= 0x1FAFF):
            length += 2
        else:
            eaw = unicodedata.east_asian_width(char)
            if eaw in ('F', 'W'):
                length += 2
            else:
                length += 1
    return length

def pad_right(s, target_width):
    current = visual_len(s)
    if current >= target_width:
        return s
    return s + ' ' * (target_width - current)

def center_in_width(s, target_width):
    current = visual_len(s)
    if current >= target_width:
        return s
    total_padding = target_width - current
    left = total_padding // 2
    right = total_padding - left
    return ' ' * left + s + ' ' * right

def safe_edit_message(text, chat_id, message_id, reply_markup=None, **kwargs):
    """Безопасно редактирует сообщение. Если message_id=None или сообщение не найдено — отправляет новое."""
    try:
        if message_id is None:
            if reply_markup:
                bot.send_message(chat_id, text, reply_markup=reply_markup, **kwargs)
            else:
                bot.send_message(chat_id, text, **kwargs)
        else:
            if reply_markup:
                bot.edit_message_text(text, chat_id, message_id, reply_markup=reply_markup, **kwargs)
            else:
                bot.edit_message_text(text, chat_id, message_id, **kwargs)
    except Exception as e:
        error_str = str(e)
        if "message is not modified" in error_str:
            pass
        elif "message to edit not found" in error_str or "message_id is empty" in error_str:
            try:
                if reply_markup:
                    bot.send_message(chat_id, text, reply_markup=reply_markup, **kwargs)
                else:
                    bot.send_message(chat_id, text, **kwargs)
            except Exception as send_err:
                print(f"️ Не удалось отправить сообщение: {send_err}")
        else:
            raise

def escape_html(text: str) -> str:
    if not text:
        return ""
    text = text.replace('&', '&amp;')
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')
    return text

def format_time_to_24h(time_str: str) -> str:
    if not time_str:
        return ""
    time_str = time_str.strip().upper()
    if 'AM' in time_str or 'PM' in time_str:
        try:
            dt = datetime.strptime(time_str.replace(' ', ''), "%I:%M%p")
            return dt.strftime("%H:%M")
        except ValueError:
            pass
    match = re.search(r'(\d{1,2}:\d{2})', time_str)
    if match:
        h, m = match.group(1).split(':')
        return f"{int(h):02d}:{m}"
    return time_str

def format_message(post: dict) -> str:
    raw_name = post.get('name', '').strip()
    raw_text = post.get('text', '').strip()
    raw_time = post.get('time', '').strip()
    
    if raw_name:
        raw_name = re.sub(r'\s*\d{1,2}:\d{2}\s*(AM|PM|am|pm)?\s*$', '', raw_name, flags=re.IGNORECASE).strip()
        raw_name = raw_name.replace('\n', ' ').replace('\r', '').strip()
        if not raw_name or raw_name == raw_text or raw_name == 'Аноним':
            raw_name = 'Аноним'
            
    if not raw_name or raw_name == 'Аноним':
        fwd_match = re.match(r'^Переслано:\s*([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)\s+(.+)$', raw_text, re.DOTALL)
        if fwd_match:
            raw_name = fwd_match.group(1).strip()
            raw_text = fwd_match.group(2).strip()
        else:
            role_match = re.match(
                r'^([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)\s+((?:Учитель|Учительница|Ученик|Ученица)\s*🎓\s+)(.+)$',
                raw_text, re.DOTALL
            )
            if role_match:
                raw_name = f"{role_match.group(1).strip()} {role_match.group(2).strip()}".strip()
                raw_text = role_match.group(3).strip()
            else:
                name_match = re.match(
                    r'^([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)\s{2,}(.+)$',
                    raw_text, re.DOTALL
                )
                if name_match:
                    raw_name = name_match.group(1).strip()
                    raw_text = name_match.group(2).strip()
                    
    person_name = raw_name
    person_role = ''
    if raw_name and raw_name != 'Аноним':
        role_extract = re.match(
            r'^(.+?)\s+((?:Учитель|Учительница|Ученик|Ученица)\s*🎓)\s*$',
            raw_name
        )
        if role_extract:
            person_name = role_extract.group(1).strip()
            person_role = role_extract.group(2).strip()
            
    if person_name and person_name != 'Аноним':
        if person_role:
            header = f"👤 <b>{escape_html(person_name)}</b> │ <i>{escape_html(person_role)}</i>\n\n"
        else:
            header = f"👤 <b>{escape_html(person_name)}</b>\n\n"
    else:
        header = ""
        
    body = escape_html(raw_text)
    result = f"{header}{body}"
    raw_time_formatted = format_time_to_24h(raw_time)
    if raw_time_formatted:
        result += f"\n\n🕐 <i>{escape_html(raw_time_formatted)}</i>"
    return result

def get_cancel_keyboard():
    """Клавиатура с кнопкой отмены"""
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_step"))
    return kb

def track_user(message_or_callback):
    try:
        user = message_or_callback.from_user
        username = user.username or ''
        first_name = user.first_name or ''
        last_name = user.last_name or ''
        add_or_update_user(user.id, username, first_name, last_name)
        if is_admin(user.id):
            update_admin_username(user.id, username)
    except Exception as e:
        print(f"⚠️ Ошибка track_user: {e}")

#! ============ QR-ЛОГИН ============
def _build_qr_keyboard(target_chat_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("✅ Готово, я отсканировал", callback_data=f"qr_ready_{target_chat_id}"))
    kb.add(types.InlineKeyboardButton("🔄 Обновить QR", callback_data=f"qr_refresh_{target_chat_id}"))
    kb.add(types.InlineKeyboardButton("❌ Отменить", callback_data=f"qr_cancel_{target_chat_id}"))
    return kb

def _build_password_keyboard(target_chat_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("❌ Отменить авторизацию", callback_data=f"qr_cancel_{target_chat_id}"))
    return kb

def do_qr_login(target_chat_id, admin_chat_id):
    chat = get_chat_by_id(target_chat_id)
    if not chat:
        bot.send_message(admin_chat_id, "❌ Чат не найден", **NO_PREVIEW)
        return
        
    session = active_logins.pop(target_chat_id, None)
    if session:
        try:
            session.close()
        except:
            pass
    awaiting_password_from.pop(target_chat_id, None)
    
    bot.send_message(
        admin_chat_id,
        f"⏳ Открываю web.max.ru для <b>{chat['title']}</b>...\nЭто займет несколько секунд.",
        parse_mode='HTML', **NO_PREVIEW
    )
    
    session = QRLoginSession(target_chat_id)
    status, data = session.start()
    
    if status == "already_logged_in":
        bot.send_message(
            admin_chat_id,
            f"✅ <b>Уже авторизован!</b>\nСессия для <b>{chat['title']}</b> актуальна.",
            parse_mode='HTML', **NO_PREVIEW
        )
        return
        
    if status == "error":
        bot.send_message(
            admin_chat_id,
            f"❌ <b>Ошибка:</b> <code>{data}</code>",
            parse_mode='HTML', **NO_PREVIEW
        )
        return
        
    active_logins[target_chat_id] = session
    try:
        photo_obj = io.BytesIO(data)
        kb = _build_qr_keyboard(target_chat_id)
        bot.send_photo(
            admin_chat_id,
            photo_obj,
            caption=(
                f"📱 <b>Отсканируйте QR-код</b> на этом скриншоте через приложение MAX\n\n"
                f"Чат: <b>{chat['title']}</b>\n\n"
                f"После сканирования нажмите <b>«✅ Готово, я отсканировал»</b>"
            ),
            parse_mode='HTML',
            reply_markup=kb
        )
    except Exception as e:
        bot.send_message(admin_chat_id, f"❌ Ошибка отправки скриншота: {e}", **NO_PREVIEW)
        session.close()
        del active_logins[target_chat_id]

#! ============ ФУНКЦИИ ОТОБРАЖЕНИЯ ИНТЕРФЕЙСА ============
def show_admin_menu(chat_id):
    stats = get_global_stats()
    user_count = get_user_count()
    stats_block = (
        f"📊 Активных чатов: {stats['active_chats']} / {stats['total_chats']}\n"
        f" Сообщений сегодня: {stats['today_msgs']}\n"
        f"📦 Всего сообщений: {stats['total_msgs']}\n"
        f"👥 Пользователей: {user_count}"
    )
    text = (
        "🛠 <b>ПАНЕЛЬ АДМИНИСТРАТОРА</b>\n\n"
        f"<blockquote>{stats_block}</blockquote>\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"⚙️ <b>Парсер:</b> 🟢 Работает в фоне"
    )
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("📋 Список чатов", callback_data="admin_chats"))
    kb.add(types.InlineKeyboardButton("📊 Статус парсинга", callback_data="admin_parse_status"))
    kb.row(
        types.InlineKeyboardButton("👑 Админы", callback_data="admin_admins"),
        types.InlineKeyboardButton("👤 Пользователи", callback_data="admin_users")
    )
    kb.add(types.InlineKeyboardButton("🗑 Очистить весь кэш", callback_data="admin_clear_cache", style="danger"))
    bot.send_message(chat_id, text, reply_markup=kb, **NO_PREVIEW)

def show_chats_list(chat_id, message_id=None):
    chats = get_all_chats()
    if not chats:
        text = "📋 <b>Список чатов</b>\n\nЧатов пока нет."
    else:
        text = "📋 <b>СПИСОК ЧАТОВ</b>"
        for c in chats:
            if c['is_active']:
                status = "🟢"
                status_text = "Активен"
            elif not c.get('max_url') or not c.get('phone'):
                status = "🟡"
                status_text = "Не настроен"
            else:
                status = "🔴"
                status_text = "Остановлен"
            url_mark = "✅" if c.get('max_url') else ""
            phone_mark = "✅" if c.get('phone') else "❌"
            session_mark = "✅" if os.path.exists(f"sessions/{c['chat_id']}.json") else ""
            stats = get_chat_stats(c['chat_id'])
            text += f"\n\n{status} <b>{c['title']}</b>"
            text += f"<blockquote>🆔 <code>{c['chat_id']}</code> │ {status_text}\n"
            text += f"🔗URL: {url_mark} │ 📱Телефон: {phone_mark} │ 🔑Сессия: {session_mark}\n"
            text += f"💬 Сегодня: {stats['today']} │ Всего: {stats['total']}</blockquote>"
            
    kb = types.InlineKeyboardMarkup(row_width=1)
    for c in chats:
        if c['is_active']:
            icon = "⏸"
        elif not c.get('max_url') or not c.get('phone'):
            icon = "⚙️"
        else:
            icon = "▶️"
        kb.add(types.InlineKeyboardButton(
            f"{icon} {c['title']}",
            callback_data=f"admin_chat_{c['chat_id']}", style="primary"
        ))
    kb.add(types.InlineKeyboardButton("➕ Добавить чат", callback_data="admin_add_chat"))
    kb.add(types.InlineKeyboardButton("🔙 Назад в меню", callback_data="admin_back"))
    
    if message_id:
        safe_edit_message(text, chat_id, message_id, reply_markup=kb, **NO_PREVIEW)
    else:
        bot.send_message(chat_id, text, reply_markup=kb, **NO_PREVIEW)

def show_chat_details(chat_id, message_id, target_chat_id):
    chat = get_chat_by_id(target_chat_id)
    if not chat:
        bot.answer_callback_query(callback_query_id=None, text="Чат не найден")
        return
        
    stats = get_chat_stats(target_chat_id)
    cache_count = get_chat_cache_count(target_chat_id)
    
    if chat['is_active']:
        status_text = "🟢 Активен"
    elif not chat.get('max_url') or not chat.get('phone'):
        status_text = "🟡 Нужно настроить"
    else:
        status_text = "🔴 Остановлен"
        
    session_file = f"sessions/{target_chat_id}.json"
    session_status = "✅ Активна" if os.path.exists(session_file) else "❌ Отсутствует"
    
    text = (
        f"📋 <b>{chat['title']}</b>\n\n"
        f"🆔 <b>ID:</b> <code>{chat['chat_id']}</code>\n"
        f"🔗 <b>URL:</b> {chat['max_url'] or ' не задан'}\n"
        f"📱 <b>Телефон:</b> {chat['phone'] or '❌ не задан'}\n"
        f"🔑 <b>Сессия:</b> {session_status}\n"
        f"⚙️ <b>Парсинг:</b> {status_text}\n\n"
        f"<blockquote>"
        f"💬 <b>Отправлено:</b> {stats['today']} сегодня / {stats['total']} всего\n"
        f"📦 <b>В кэше:</b> {cache_count} сообщений (защита от дублей)"
        f"</blockquote>"
    )
    
    kb = types.InlineKeyboardMarkup(row_width=1)
    if chat.get('max_url') and chat.get('phone'):
        if chat['is_active']:
            kb.add(types.InlineKeyboardButton("⏹ Остановить парсинг", callback_data=f"admin_toggle_chat_{target_chat_id}", style="danger"))
        else:
            kb.add(types.InlineKeyboardButton("▶️ Запустить парсинг", callback_data=f"admin_toggle_chat_{target_chat_id}", style="success"))
    kb.add(types.InlineKeyboardButton("🔗 Задать URL группы MAX", callback_data=f"admin_set_url_{target_chat_id}"))
    kb.add(types.InlineKeyboardButton("📱 Задать номер телефона", callback_data=f"admin_set_phone_{target_chat_id}"))
    kb.add(types.InlineKeyboardButton("🔑 Авторизоваться (QR)", callback_data=f"admin_qr_login_{target_chat_id}"))
    kb.add(types.InlineKeyboardButton("🗑 Очистить кэш чата", callback_data=f"admin_clear_chat_cache_{target_chat_id}"))
    kb.add(types.InlineKeyboardButton("❌ Удалить чат", callback_data=f"admin_ask_delete_{target_chat_id}", style="danger"))
    kb.add(types.InlineKeyboardButton("🔙 К списку чатов", callback_data="admin_chats", style="primary"))
    safe_edit_message(text, chat_id, message_id, reply_markup=kb, **NO_PREVIEW)

def show_chat_statistics(chat_id, message_id, target_chat_id):
    chat = get_chat_by_id(target_chat_id)
    if not chat:
        bot.answer_callback_query(callback_query_id=None, text="Чат не найден")
        return
        
    stats = get_chat_stats(target_chat_id)
    cache_count = get_chat_cache_count(target_chat_id)
    recent_msgs = get_recent_messages(target_chat_id, 10)
    
    text = (
        f"📊 <b>Статистика: {chat['title']}</b>\n\n"
        f"<blockquote>"
        f"💬 <b>Отправлено сегодня:</b> {stats['today']}\n"
        f"📦 <b>Всего отправлено:</b> {stats['total']}\n"
        f"🗃️ <b>В кэше (защита от дублей):</b> {cache_count} сообщений"
        f"</blockquote>\n\n"
    )
    
    if recent_msgs:
        text += "🕐 <b>Последние сообщения:</b>\n\n"
        for msg in recent_msgs:
            name = msg['sender_name'] or 'Аноним'
            role = msg.get('sender_role', '')
            time_str = msg.get('msg_time', '')
            msg_text = (msg['text'] or '')[:60]
            if role:
                name = f"{name} │ {role}"
            text += f"👤 <b>{name}</b>\n"
            text += f"   {msg_text}...\n"
            if time_str:
                text += f"    {time_str}\n\n"
                
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🔙 Назад к настройкам чата", callback_data=f"admin_chat_{target_chat_id}"))
    safe_edit_message(text, chat_id, message_id, reply_markup=kb, **NO_PREVIEW)

def show_admins_list(chat_id, message_id):
    admins = get_all_admins()
    INNER_W = 36
    c_num = 3
    c_id = 8
    c_uname = INNER_W - c_num - c_id - 10
    line_top = "╔" + "═" * INNER_W + "╗"
    line_mid = "" + "═" * INNER_W + ""
    line_bot = "╚" + "═" * INNER_W + "╝"
    title = "👑 АДМИНЫ"
    title_line = f"║{center_in_width(title, INNER_W)}║"
    header = (
        f"║ {pad_right('№', c_num)} │ {pad_right('User ID', c_id)} │ "
        f"{pad_right('Username', c_uname)} ║"
    )
    lines = [line_top, title_line, line_mid, header, line_mid]
    
    for i, a in enumerate(admins, 1):
        uid = str(a['user_id'])
        uname = a.get('username') or '—'
        uname_display = f"@{uname}" if uname != '—' else '—'
        while visual_len(uname_display) > c_uname:
            uname_display = uname_display[:-1]
        row = (
            f"║ {pad_right(str(i), c_num)} │ {pad_right(uid, c_id)} │ "
            f"{pad_right(uname_display, c_uname)} ║"
        )
        lines.append(row)
    lines.append(line_bot)
    table = "\n".join(lines)
    text = f"<pre>{table}</pre>"
    
    kb = types.InlineKeyboardMarkup(row_width=1)
    for a in admins:
        uname = f"@{a['username']}" if a.get('username') else f"ID {a['user_id']}"
        if a['user_id'] != 5213315899 and a['user_id'] != ADMIN_ID:
            kb.add(types.InlineKeyboardButton(
                f" Удалить {uname}",
                callback_data=f"admin_ask_delete_admin_{a['user_id']}"
            ))
    kb.add(types.InlineKeyboardButton("➕ Добавить админа", callback_data="admin_add_admin"))
    kb.add(types.InlineKeyboardButton("🔙 Назад в меню", callback_data="admin_back"))
    safe_edit_message(text, chat_id, message_id, reply_markup=kb, **NO_PREVIEW)

def show_users_list(chat_id, message_id):
    users = get_all_users()
    INNER_W = 36
    c_num = 3
    c_id = 8
    c_uname = INNER_W - c_num - c_id - 10
    c_uname_2 = INNER_W - c_num - c_id - 2
    line_top = "╔" + "═" * INNER_W + "╗"
    line_mid = "╠" + "═" * INNER_W + "╣"
    line_bot = "╚" + "═" * INNER_W + "╝"
    title = "👤 ПОЛЬЗОВАТЕЛИ"
    title_line = f"║{center_in_width(title, INNER_W)}║"
    header = (
        f"║ {pad_right('№', c_num)} │ {pad_right('User ID', c_id)} │ "
        f"{pad_right('Username', c_uname_2)} ║"
    )
    lines = [line_top, title_line, line_mid, header, line_mid]
    
    for i, u in enumerate(users, 1):
        uid = str(u['user_id'])
        uname = u.get('username') or '—'
        uname_display = f"@{uname}" if uname != '—' else '—'
        while visual_len(uname_display) > c_uname:
            uname_display = uname_display[:-1]
        row = (
            f"║ {pad_right(str(i), c_num)} │ {pad_right(uid, c_id)} │ "
            f"{pad_right(uname_display, c_uname)} ║"
        )
        lines.append(row)
    lines.append(line_bot)
    table = "\n".join(lines)
    text = (
        f"<pre>{table}</pre>\n\n"
        f"<blockquote>Всего: {len(users)}</blockquote>"
    )
    
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🔙 Назад в меню", callback_data="admin_back"))
    safe_edit_message(text, chat_id, message_id, reply_markup=kb, **NO_PREVIEW)

#! ============ ОБРАБОТЧИКИ ВВОДА (STEP HANDLERS) ============
def process_add_chat_step1(message, admin_chat_id):
    """Получаем ID чата и сразу добавляем его в БД"""
    track_user(message)
    try:
        chat_id = int(message.text.strip())
    except:
        bot.send_message(message.chat.id, "❌ Неверный формат ID. Попробуйте ещё раз или нажмите Отмена", **NO_PREVIEW)
        bot.register_next_step_handler(message, process_add_chat_step1, admin_chat_id)
        return
        
    existing_chat = get_chat_by_id(chat_id)
    if existing_chat:
        bot.send_message(
            message.chat.id, 
            f"⚠️ <b>Чат уже существует!</b>\n\n"
            f"Чат с ID <code>{chat_id}</code> уже добавлен:\n"
            f"<b>{existing_chat['title']}</b>\n\n"
            f"Если хотите обновить настройки, используйте кнопку настроек в списке чатов.",
            parse_mode='HTML', **NO_PREVIEW
        )
        return
        
    try:
        chat_info = bot.get_chat(chat_id)
        title = chat_info.title or f"Чат {chat_id}"
        bot_member = bot.get_chat_member(chat_id, bot.get_me().id)
        if bot_member.status not in ['administrator', 'creator']:
            bot.send_message(
                message.chat.id,
                f"⚠️ <b>Бот не является администратором чата {title}!</b>\n\n"
                f"Добавьте бота в чат и назначьте администратором, затем повторите команду.\n\n"
                f"ID чата: <code>{chat_id}</code>",
                parse_mode='HTML', **NO_PREVIEW
            )
            return
            
        add_chat(chat_id, title)
        bot.send_message(
            message.chat.id,
            f"✅ Чат <b>{title}</b> добавлен!\n\n"
            f"🆔 ID: <code>{chat_id}</code>\n\n"
            f"Теперь настройте <b>URL группы MAX</b> и <b>номер телефона</b> через панель управления ниже.",
            parse_mode='HTML', **NO_PREVIEW
        )
        time.sleep(0.3)
        show_chat_details(message.chat.id, None, chat_id)
    except telebot.apihelper.ApiTelegramException as e:
        if e.error_code == 400 and "chat not found" in str(e).lower():
            bot.send_message(
                message.chat.id,
                f"❌ Чат с ID <code>{chat_id}</code> не найден.\n\n"
                f"Убедитесь, что бот добавлен в этот чат.",
                parse_mode='HTML', **NO_PREVIEW
            )
        else:
            bot.send_message(message.chat.id, f"❌ Ошибка: {e}", **NO_PREVIEW)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", **NO_PREVIEW)

def process_add_chat_step2(message, admin_chat_id):
    """Второй шаг: получаем URL"""
    track_user(message)
    if message.text == '/cancel':
        bot.send_message(message.chat.id, " Отменено", **NO_PREVIEW)
        if hasattr(process_add_chat_step1, 'pending_chats'):
            process_add_chat_step1.pending_chats.pop(message.from_user.id, None)
        return
        
    url = message.text.strip()
    if not url.startswith('http'):
        bot.send_message(message.chat.id, "❌ URL должен начинаться с http", **NO_PREVIEW)
        bot.register_next_step_handler(message, process_add_chat_step2, admin_chat_id)
        return
        
    if not hasattr(process_add_chat_step1, 'pending_chats'):
        process_add_chat_step1.pending_chats = {}
    pending = process_add_chat_step1.pending_chats.get(message.from_user.id)
    if not pending:
        bot.send_message(message.chat.id, "❌ Сессия добавления истекла. Начните заново.", **NO_PREVIEW)
        return
        
    chat_id, title = pending
    del process_add_chat_step1.pending_chats[message.from_user.id]
    add_chat(chat_id, title, max_url=url)
    bot.send_message(
        message.chat.id,
        f"✅ Чат <b>{title}</b> добавлен!\n\n"
        f"🆔 ID: <code>{chat_id}</code>\n"
        f"🔗 URL: <code>{url}</code>\n\n"
        f"Теперь настройте телефон через панель управления.",
        parse_mode='HTML', **NO_PREVIEW
    )
    time.sleep(0.3)
    show_chat_details(message.chat.id, None, chat_id)

def process_add_admin(message, admin_chat_id):
    track_user(message)
    try:
        user_id = int(message.text.strip())
        add_admin(user_id, None)
        bot.send_message(
            message.chat.id,
            f"✅ Админ добавлен!\nID: <code>{user_id}</code>",
            **NO_PREVIEW
        )
        time.sleep(0.3)
        show_admins_list(message.chat.id, None)
        return
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}", **NO_PREVIEW)

def process_set_url(message, target_chat_id, admin_chat_id):
    track_user(message)
    if message.text == '/cancel':
        bot.send_message(message.chat.id, " Отменено", **NO_PREVIEW)
        return
    url = message.text.strip()
    if not url.startswith('http'):
        bot.send_message(message.chat.id, "❌ URL должен начинаться с http", **NO_PREVIEW)
        return
    update_chat_url(target_chat_id, url)
    bot.send_message(message.chat.id, f"✅ URL обновлён!\n<code>{url}</code>", **NO_PREVIEW)
    time.sleep(0.3)
    show_chat_details(message.chat.id, None, target_chat_id)

def process_set_phone(message, target_chat_id, admin_chat_id):
    track_user(message)
    phone = message.text.strip()
    if not re.match(r'^\+?\d{10,15}$', phone.replace(' ', '').replace('-', '')):
        bot.send_message(message.chat.id, "❌ Неверный формат телефона", **NO_PREVIEW)
        return
    update_chat_phone(target_chat_id, phone)
    bot.send_message(message.chat.id, f"✅ Телефон обновлён!\n<code>{phone}</code>", **NO_PREVIEW)
    time.sleep(0.3)
    show_chat_details(message.chat.id, None, target_chat_id)

#! ==============================================================================
#! 5) ОСТАЛЬНОЙ КОД (ОБРАБОТЧИКИ СООБЩЕНИЙ)
#! ==============================================================================
@bot.message_handler(content_types=['new_chat_members'])
def on_bot_added_to_chat(message):
    for member in message.new_chat_members:
        if member.id == bot.get_me().id:
            chat_id = message.chat.id
            title = message.chat.title or f"Чат {chat_id}"
            add_chat(chat_id, title)
            try:
                member_status = bot.get_chat_member(chat_id, bot.get_me().id).status
                if member_status not in ['administrator', 'creator']:
                    bot.send_message(
                        chat_id,
                        "👋 <b>Привет!</b>\n\n"
                        "Я — <b>MAX Parser Bot</b>. Меня добавили в этот чат.\n\n"
                        "️ <b>Важно:</b> Чтобы я мог работать, мне нужны права администратора.\n"
                        "Пожалуйста, назначьте меня админом чата.\n\n"
                        "ℹ️ Администратор должен написать мне в ЛС команду /admin для настройки.",
                        parse_mode='HTML', **NO_PREVIEW
                    )
                else:
                    bot.send_message(
                        chat_id,
                        "👋 <b>Привет!</b>\n\n"
                        "Я — <b>MAX Parser Bot</b>. Меня добавили в этот чат.\n\n"
                        "✅ Права администратора получены. Готов к работе!\n\n"
                        "ℹ️ Администратор должен написать мне в ЛС команду /admin для настройки парсинга.",
                        parse_mode='HTML', **NO_PREVIEW
                    )
            except Exception as e:
                print(f"⚠️ Не удалось проверить права: {e}")
                bot.send_message(
                    chat_id,
                    "👋 <b>Привет!</b>\n\n"
                    "Я — <b>MAX Parser Bot</b>. Меня добавили в этот чат.\n\n"
                    "️ Для полноценной работы мне нужны права администратора.\n\n"
                    "ℹ️ Администратор должен написать мне в ЛС команду /admin.",
                    parse_mode='HTML', **NO_PREVIEW
                )
            return

@bot.message_handler(commands=['start'])
def start_bot(message):
    track_user(message)
    text = (
        " <b>Привет!</b>\n\n"
        "Я — <b>MAX_Parser</b>. Я автоматически пересылаю сообщения "
        "из мессенджера MAX в Telegram."
    )
    bot.send_message(message.chat.id, text, **NO_PREVIEW)

@bot.message_handler(commands=['login'])
def start_login_process(message):
    track_user(message)
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⛔️ Только для админов")
        return
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(
            message.chat.id,
            "❌ Использование: <code>/login &lt;chat_id&gt;</code>\n"
            "Пример: <code>/login -1001234567890</code>",
            parse_mode='HTML', **NO_PREVIEW
        )
        return
    try:
        target_chat_id = int(parts[1])
    except:
        bot.send_message(message.chat.id, "❌ chat_id должен быть числом", **NO_PREVIEW)
        return
    do_qr_login(target_chat_id, message.chat.id)

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    track_user(message)
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⛔️ У вас нет прав администратора")
        return
    show_admin_menu(message.chat.id)

@bot.message_handler(func=lambda m: m.from_user.id in [ADMIN_ID] and not m.text.startswith('/'))
def handle_password_input(message):
    user_id = message.from_user.id
    target_chat_id = None
    for chat_id, admin_id in list(awaiting_password_from.items()):
        if admin_id == user_id:
            target_chat_id = chat_id
            break
            
    if target_chat_id is None:
        return
        
    password = message.text.strip()
    chat = get_chat_by_id(target_chat_id)
    session = active_logins.pop(target_chat_id, None)
    if not session:
        bot.send_message(message.chat.id, "❌ Сессия авторизации не найдена", **NO_PREVIEW)
        awaiting_password_from.pop(target_chat_id, None)
        return
        
    bot.send_message(message.chat.id, "⏳ Ввожу пароль...", **NO_PREVIEW)
    status, data = session.enter_password(password)
    awaiting_password_from.pop(target_chat_id, None)
    
    if status == "success":
        bot.send_message(
            message.chat.id,
            f"🎉 <b>Успешно!</b>\nСессия для <b>{chat['title']}</b> сохранена.\nТеперь фоновый парсер сможет работать автоматически!",
            parse_mode='HTML', **NO_PREVIEW
        )
    elif status == "wrong_password":
        awaiting_password_from[target_chat_id] = user_id
        try:
            photo_obj = io.BytesIO(data)
            kb = _build_password_keyboard(target_chat_id)
            bot.send_photo(
                message.chat.id, photo_obj,
                caption=f"❌ <b>Неверный пароль!</b>\n\n👉 <b>Пришлите правильный пароль следующим сообщением</b>",
                parse_mode='HTML', reply_markup=kb
            )
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Ошибка: {e}", **NO_PREVIEW)
    else:
        bot.send_message(
            message.chat.id,
            f"❌ Ошибка: <code>{data}</code>",
            parse_mode='HTML', **NO_PREVIEW
        )

#! ==============================================================================
#! 6) ОБРАБОТЧИК ЛОГИКИ ВСЕХ КНОПОК
#! ==============================================================================
@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_") or call.data.startswith("qr_") or call.data in ["delete_this_msg", "cancel_step"])
def admin_callbacks(call):
    track_user(call)
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔️ Нет прав", show_alert=True)
        return
        
    action = call.data
    
    if action == "cancel_step":
        try:
            print(f"🔄 Отмена действия для чата {call.message.chat.id}")
            bot.clear_step_handler(call.message)
            bot.delete_message(call.message.chat.id, call.message.message_id)
            bot.answer_callback_query(call.id, "❌ Отменено")
        except Exception as e:
            print(f"Ошибка при отмене: {e}")
            bot.answer_callback_query(call.id, "⚠️ Ошибка при отмене")
        return
        
    if action == "delete_this_msg":
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
            bot.answer_callback_query(call.id, "🗑 Сообщение удалено")
        except Exception as e:
            bot.answer_callback_query(call.id, "⚠️ Не удалось удалить")
            print(f"Ошибка удаления: {e}")
        return
        
    if action.startswith("qr_ready_"):
        target_id = int(action.split("_")[-1])
        bot.answer_callback_query(call.id, "⏳ Проверяю вход...")
        session = active_logins.get(target_id)
        if not session:
            bot.send_message(call.message.chat.id, "❌ Сессия авторизации не найдена", **NO_PREVIEW)
            return
        chat = get_chat_by_id(target_id)
        status, data = session.check()
        
        if status == "success":
            active_logins.pop(target_id, None)
            awaiting_password_from.pop(target_id, None)
            bot.send_message(
                call.message.chat.id,
                f"🎉 <b>Успешно!</b>\nСессия для <b>{chat['title']}</b> сохранена.",
                parse_mode='HTML', **NO_PREVIEW
            )
        elif status == "awaiting_password":
            awaiting_password_from[target_id] = call.from_user.id
            try:
                photo_obj = io.BytesIO(data)
                kb = _build_password_keyboard(target_id)
                bot.send_photo(
                    call.message.chat.id,
                    photo_obj,
                    caption=(
                        f" <b>Требуется ввод пароля!</b>\n\n"
                        f"После сканирования QR-кода MAX запрашивает пароль для подтверждения.\n\n"
                        f"👉 <b>Пришлите пароль от аккаунта MAX следующим сообщением</b>\n\n"
                        f"Для отмены нажмите кнопку ниже."
                    ),
                    parse_mode='HTML',
                    reply_markup=kb
                )
            except Exception as e:
                bot.send_message(call.message.chat.id, f"❌ Ошибка: {e}", **NO_PREVIEW)
        elif status == "waiting":
            bot.send_message(
                call.message.chat.id,
                f" <b>Вход ещё не выполнен.</b>\n\n"
                f"Убедитесь, что вы отсканировали QR-код в приложении MAX.\n"
                f"Затем нажмите <b>«✅ Готово, я отсканировал»</b> снова.",
                parse_mode='HTML',
                reply_markup=_build_qr_keyboard(target_id),
                **NO_PREVIEW
            )
        else:
            bot.send_message(
                call.message.chat.id,
                f"❌ Ошибка проверки: <code>{data}</code>",
                parse_mode='HTML', **NO_PREVIEW
            )
            
    elif action.startswith("qr_refresh_"):
        target_id = int(action.split("_")[-1])
        bot.answer_callback_query(call.id, "🔄 Обновляю QR-код...")
        session = active_logins.get(target_id)
        if not session:
            bot.send_message(call.message.chat.id, "❌ Сессия авторизации не найдена", **NO_PREVIEW)
            return
        chat = get_chat_by_id(target_id)
        status, data = session.refresh()
        
        if status == "success":
            active_logins.pop(target_id, None)
            bot.send_message(
                call.message.chat.id,
                f"🎉 <b>Успешно!</b>\nВы уже авторизованы в <b>{chat['title']}</b>!",
                parse_mode='HTML', **NO_PREVIEW
            )
            return
        if status == "error":
            bot.send_message(
                call.message.chat.id,
                f"❌ Ошибка обновления: <code>{data}</code>",
                parse_mode='HTML', **NO_PREVIEW
            )
            return
            
        try:
            photo_obj = io.BytesIO(data)
            bot.send_photo(
                call.message.chat.id,
                photo_obj,
                caption=(
                    f" <b>QR-код обновлён</b>\n\n"
                    f"Отсканируйте новый код и нажмите <b>«✅ Готово»</b>"
                ),
                parse_mode='HTML',
                reply_markup=_build_qr_keyboard(target_id)
            )
        except Exception as e:
            bot.send_message(call.message.chat.id, f"❌ Ошибка: {e}", **NO_PREVIEW)
            
    elif action.startswith("qr_cancel_"):
        target_id = int(action.split("_")[-1])
        session = active_logins.pop(target_id, None)
        if session:
            try:
                session.close()
            except:
                pass
        awaiting_password_from.pop(target_id, None)
        bot.answer_callback_query(call.id, "❌ Авторизация отменена", show_alert=True)
        bot.send_message(call.message.chat.id, "❌ Процесс авторизации отменён.", **NO_PREVIEW)
        
    elif action == "admin_chats":
        show_chats_list(call.message.chat.id, call.message.message_id)
        
    elif action == "admin_add_chat":
        msg = bot.send_message(
            call.message.chat.id,
            "➕ <b>Добавление чата</b>\n\n"
            "Отправьте <b>ID чата</b> (например: <code>-1001234567890</code>)\n\n"
            "Бот сам получит название чата.",
            parse_mode='HTML', 
            reply_markup=get_cancel_keyboard(),
            **NO_PREVIEW
        )
        bot.register_next_step_handler(msg, process_add_chat_step1, call.message.chat.id)
        
    elif action == "admin_add_admin":
        msg = bot.send_message(
            call.message.chat.id,
            "➕ <b>Добавление администратора</b>\n\n"
            "Отправьте user_id нового админа:\n\n"
            "Пример: <code>123456789</code>",
            parse_mode='HTML', 
            reply_markup=get_cancel_keyboard(),
            **NO_PREVIEW
        )
        bot.register_next_step_handler(msg, process_add_admin, call.message.chat.id)
        
    elif action == "admin_parse_status":
        bot.answer_callback_query(call.id, "📊 Собираю статус парсинга...")
        loading_msg = bot.send_message(
            call.message.chat.id,
            "⏳ <b>Анализ работы парсера...</b>\nПроверяю все активные чаты, это может занять 1-2 минуты.",
            parse_mode='HTML', **NO_PREVIEW
        )
        def status_thread():
            result = run_one_parse_cycle()
            try:
                bot.delete_message(call.message.chat.id, loading_msg.message_id)
            except:
                pass
            del_kb = types.InlineKeyboardMarkup(row_width=1)
            del_kb.add(types.InlineKeyboardButton(" Удалить это сообщение", callback_data="delete_this_msg"))
            try:
                bot.send_message(
                    call.message.chat.id,
                    f"📊 <b>Статус парсинга:</b>\n\n{result}",
                    parse_mode='HTML', reply_markup=del_kb, **NO_PREVIEW
                )
            except Exception as html_err:
                bot.send_message(
                    call.message.chat.id,
                    f"📊 Статус парсинга:\n\n{result}",
                    reply_markup=del_kb, **NO_PREVIEW
                )
        threading.Thread(target=status_thread, daemon=True).start()
        
    elif action == "admin_admins":
        show_admins_list(call.message.chat.id, call.message.message_id)
        
    elif action == "admin_users":
        show_users_list(call.message.chat.id, call.message.message_id)
        
    elif action == "admin_clear_cache":
        from max_playwright_parser import message_cache_by_chat
        stats = get_global_stats()
        cache_count = sum(len(v) for v in message_cache_by_chat.values())
        text = (
            f"⚠️ <b>Подтверждение очистки кэша</b>\n\n"
            f"📊 <b>Текущая статистика:</b>\n"
            f"• Сообщений в базе: <b>{stats['total_msgs']}</b>\n"
            f"• Хешей в кэше парсера: <b>{cache_count}</b>\n"
            f"• Активных чатов: <b>{stats['active_chats']}</b>\n\n"
            f"Вы действительно хотите очистить весь кэш?\n\n"
            f"<blockquote>Это действие нельзя отменить!</blockquote>"
        )
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.row(
            types.InlineKeyboardButton("✅ Да, очистить", callback_data="admin_yes_clear_cache"),
            types.InlineKeyboardButton("❌ Отмена", callback_data="admin_back")
        )
        safe_edit_message(text, call.message.chat.id, call.message.message_id, reply_markup=kb, **NO_PREVIEW)
        return
        
    elif action == "admin_yes_clear_cache":
        clear_all_caches()
        bot.answer_callback_query(call.id, "🗑 Кэш полностью очищен!", show_alert=True)
        show_admin_menu(call.message.chat.id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        return
        
    elif action == "admin_back":
        show_admin_menu(call.message.chat.id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
            
    elif action.startswith("admin_chat_"):
        target_id = int(action.split("_")[-1])
        show_chat_details(call.message.chat.id, call.message.message_id, target_id)
        
    elif action.startswith("admin_toggle_chat_"):
        target_id = int(action.split("_")[-1])
        chat = get_chat_by_id(target_id)
        if chat:
            if not chat.get('max_url') or not chat.get('phone'):
                bot.answer_callback_query(call.id, "⚠️ Сначала настройте URL и телефон!", show_alert=True)
                return
            toggle_chat(target_id, not bool(chat['is_active']))
        show_chat_details(call.message.chat.id, call.message.message_id, target_id)
        
    elif action.startswith("admin_clear_chat_cache_"):
        target_id = int(action.split("_")[-1])
        clear_chat_cache(target_id)
        bot.answer_callback_query(call.id, "Кэш чата очищен", show_alert=True)
        time.sleep(0.3)
        show_chat_details(call.message.chat.id, call.message.message_id, target_id)
        
    elif action.startswith("admin_qr_login_"):
        target_id = int(action.split("_")[-1])
        bot.answer_callback_query(call.id, "⏳ Открываю страницу...")
        do_qr_login(target_id, call.message.chat.id)
        
    elif action.startswith("admin_ask_delete_"):
        target_id = int(action.split("_")[-1])
        chat = get_chat_by_id(target_id)
        if not chat:
            bot.answer_callback_query(call.id, "Чат не найден", show_alert=True)
            return
        text = (
            f"⚠️ <b>Подтверждение удаления</b>\n\n"
            f"Вы действительно хотите удалить чат:\n"
            f"<b>{chat['title']}</b>?\n\n"
            f"<blockquote>🆔 ID: <code>{target_id}</code>\n"
            f"Это действие нельзя отменить!</blockquote>"
        )
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.row(
            types.InlineKeyboardButton("✅ Да, удалить", callback_data=f"admin_yes_delete_{target_id}"),
            types.InlineKeyboardButton("❌ Отмена", callback_data=f"admin_chat_{target_id}")
        )
        safe_edit_message(text, call.message.chat.id, call.message.message_id,
                          reply_markup=kb, **NO_PREVIEW)
                          
    elif action.startswith("admin_yes_delete_"):
        target_id = int(action.split("_")[-1])
        delete_chat(target_id)
        bot.answer_callback_query(call.id, "🗑 Чат удалён", show_alert=True)
        show_chats_list(call.message.chat.id, call.message.message_id)
        
    elif action.startswith("admin_ask_delete_admin_"):
        target_id = int(action.split("_")[-1])
        if target_id == 5213315899:
            bot.answer_callback_query(call.id, "⛔️ Нельзя удалить главного администратора!", show_alert=True)
            show_admins_list(call.message.chat.id, call.message.message_id)
            return
        admins = get_all_admins()
        admin = next((a for a in admins if a['user_id'] == target_id), None)
        if not admin:
            bot.answer_callback_query(call.id, "Админ не найден", show_alert=True)
            return
        uname = f"@{admin['username']}" if admin.get('username') else "без username"
        text = (
            f"⚠️ <b>Подтверждение удаления</b>\n\n"
            f"Вы действительно хотите удалить админа:\n"
            f"<b>{uname}</b>?\n\n"
            f"<blockquote>🆔 ID: <code>{target_id}</code>\n"
            f"Это действие нельзя отменить!</blockquote>"
        )
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.row(
            types.InlineKeyboardButton("✅ Да, удалить", callback_data=f"admin_yes_delete_admin_{target_id}"),
            types.InlineKeyboardButton("❌ Отмена", callback_data="admin_admins")
        )
        safe_edit_message(text, call.message.chat.id, call.message.message_id,
                          reply_markup=kb, **NO_PREVIEW)
                          
    elif action.startswith("admin_yes_delete_admin_"):
        target_id = int(action.split("_")[-1])
        if target_id == 5213315899:
            bot.answer_callback_query(call.id, "⛔️ Нельзя удалить главного администратора!", show_alert=True)
            show_admins_list(call.message.chat.id, call.message.message_id)
            return
        if target_id == ADMIN_ID:
            bot.answer_callback_query(call.id, "️ Нельзя удалить главного админа!", show_alert=True)
            show_admins_list(call.message.chat.id, call.message.message_id)
            return
        delete_admin(target_id)
        bot.answer_callback_query(call.id, "🗑 Админ удалён", show_alert=True)
        show_admins_list(call.message.chat.id, call.message.message_id)
        
    elif action.startswith("admin_set_url_"):
        target_id = int(action.split("_")[-1])
        msg = bot.send_message(
            call.message.chat.id,
            f"🔗 <b>Настройка URL для чата</b> <code>{target_id}</code>\n\n"
            "Отправьте новый URL группы MAX:\n"
            "(например: <code>https://web.max.ru/group/abc123</code>)",
            reply_markup=get_cancel_keyboard(),
            **NO_PREVIEW
        )
        bot.register_next_step_handler(msg, process_set_url, target_id, call.message.chat.id)
        
    elif action.startswith("admin_set_phone_"):
        target_id = int(action.split("_")[-1])
        msg = bot.send_message(
            call.message.chat.id,
            f"📱 <b>Настройка телефона для чата</b> <code>{target_id}</code>\n\n"
            "Отправьте номер телефона:\n"
            "(например: <code>+79991234567</code>)",
            reply_markup=get_cancel_keyboard(),
            **NO_PREVIEW
        )
        bot.register_next_step_handler(msg, process_set_phone, target_id, call.message.chat.id)
        
    elif action.startswith("admin_chat_stats_"):
        target_id = int(action.split("_")[-1])
        try:
            show_chat_statistics(call.message.chat.id, call.message.message_id, target_id)
        except Exception as e:
            bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}", show_alert=True)
            print(f"Ошибка статистики: {e}")

if __name__ == "__main__":
    try:
        print("✅ Запуск polling...")
        bot.infinity_polling(none_stop=True, timeout=60)
    except KeyboardInterrupt:
        print(" Остановлен пользователем")
        save_message_cache()