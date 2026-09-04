import telebot
import os
import time
import io
import re
import hashlib
import threading
from telebot import types
from dotenv import load_dotenv

from max_playwright_parser import (
    parse_max_group_media,
    is_new_message,
    save_message_cache,
    load_message_cache,
    clear_all_caches
)

load_dotenv()

print("🚀 MAX Parser Bot запущен")
bot = telebot.TeleBot(os.getenv("BOT_TOKEN"), parse_mode='HTML')

ADMIN_ID = os.getenv("ADMIN_ID")

CURRENT_CHAT_ID = None
_current_cycle_hashes = set()

PARSING_ACTIVE = False
PARSING_THREAD = None
CURRENT_CHAT_ID = None

def escape_html(text: str) -> str:
    if not text:
        return ""
    text = text.replace('&', '&amp;')
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')
    return text


def format_message(post: dict) -> str:
    raw_name = post.get('name', '').strip()
    raw_text = post.get('text', '').strip()
    raw_time = post.get('time', '').strip()

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

    if raw_time:
        result += f"\n\n🕐 <i>{escape_html(raw_time)}</i>"

    return result

def parse_max_loop(chat_id):
    global PARSING_ACTIVE
    while PARSING_ACTIVE:
        try:
            _current_cycle_hashes.clear()
            
            posts = parse_max_group_media()
            new_count = 0
            skipped_count = 0

            if not posts:
                pass
            else:
                for post in posts:
                    name = post.get('name', '').replace('👤', '').replace('Аноним', '')
                    text = post.get('text', '')
                    combined = f"{name} {text}"
                    normalized = " ".join(combined.split())
                    cycle_hash = hashlib.md5(normalized.encode('utf-8')).hexdigest()
                    
                    if cycle_hash in _current_cycle_hashes:
                        skipped_count += 1
                        continue
                    _current_cycle_hashes.add(cycle_hash)

                    if not is_new_message(post):
                        skipped_count += 1
                        continue

                    media_files = post.get('media_files', [])
                    msg_text = format_message(post)
                    try:
                        if media_files:
                            first = media_files[0]
                            file_obj = io.BytesIO(first['bytes'])
                            
                            if first['type'] == 'document':
                                filename = first.get('filename', 'document.pdf')
                                bot.send_document(chat_id, file_obj, caption=msg_text, visible_file_name=filename)
                            elif first['type'] == 'image':
                                bot.send_photo(chat_id, file_obj, caption=msg_text)

                            for extra in media_files[1:]:
                                extra_obj = io.BytesIO(extra['bytes'])
                                if extra['type'] == 'document':
                                    filename = extra.get('filename', 'document.pdf')
                                    bot.send_document(chat_id, extra_obj, visible_file_name=filename)
                                else:
                                    bot.send_photo(chat_id, extra_obj)
                                time.sleep(0.5)
                        else:
                            try:
                                bot.send_message(chat_id, msg_text)
                            except Exception as md_error:
                                print(f"⚠️ Ошибка MarkdownV2, отправляю без разметки: {md_error}")
                                plain_text = f"👤 {post.get('name', '')}\n\n{post.get('text', '')}\n\n🕐 {post.get('time', '')}"
                                bot.send_message(chat_id, plain_text)

                        new_count += 1
                        time.sleep(1.5)

                    except Exception as e:
                        print(f"❌ Ошибка отправки: {e}")
                if new_count > 0:
                    save_message_cache()
                    summary = f"✅ Отправлено: *{new_count}* новых"
                    if skipped_count > 0:
                        summary += f"\n⏭️ Пропущено: *{skipped_count}* (дубли)"
                    bot.send_message(chat_id, summary, reply_markup=comeback111())
                elif skipped_count > 0:
                    pass

            time.sleep(60) # Пауза 60 секунд между циклами парсинга

        except Exception as e:
            print(f"❌ Ошибка в цикле автопарсинга: {e}")
            time.sleep(60)


load_message_cache()


def menu_button():
    global PARSING_ACTIVE
    status_text = "▶️ Начать парсинг" if not PARSING_ACTIVE else "⏹ Остановить парсинг"
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.row(types.InlineKeyboardButton(text=status_text, callback_data='button'))
    keyboard.row(
        types.InlineKeyboardButton(text='🗑 Очистить кэш', callback_data='button1'),
        types.InlineKeyboardButton(text='📊 Статистика', callback_data='button2')
    )
    keyboard.row(
        types.InlineKeyboardButton(text='🆕 Обновления', callback_data='button4'),
        types.InlineKeyboardButton(text='📌 О боте', callback_data='button5')
    )
    keyboard.row(types.InlineKeyboardButton(text='🤖 Тест бота', callback_data='button3'))
    return keyboard


def comeback():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text='Вернуться 🔙', callback_data='button01'))
    return kb


def comeback111():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(text='Удалить сообщение 🗑', callback_data='button001'))
    return kb


@bot.message_handler(commands=['start'])
def start_bot(message):
    status = "🟢 Активен" if PARSING_ACTIVE else "🔴 Остановлен"
    text = f"🚀 <b>MAX\\_Parser готов\\!</b>\nСтатус: {status}\n\nВыберите действие:"
    bot.send_message(message.chat.id, text, reply_markup=menu_button())

@bot.callback_query_handler(func=lambda call: call.data == 'button')
def parse_max_command(call):
    global PARSING_ACTIVE, PARSING_THREAD, CURRENT_CHAT_ID
    chat_id = call.message.chat.id

    if call.from_user.id != int(ADMIN_ID):
        bot.answer_callback_query(call.id, "Только для админа 🤓", show_alert=True)
        return

    if PARSING_ACTIVE:
        PARSING_ACTIVE = False
        CURRENT_CHAT_ID = None
        bot.edit_message_text("🛑 *Автопарсинг остановлен*", chat_id, call.message.message_id, reply_markup=comeback())
        print("🛑 Автопарсинг остановлен")
        return

    PARSING_ACTIVE = True
    CURRENT_CHAT_ID = chat_id
    bot.edit_message_text(
        "▶️ <b>Автопарсинг ЗАПУЩЕН</b>\n⏳ Проверка каждые <b>60 сек</b>\\.",
        chat_id, call.message.message_id, reply_markup=comeback()
    )
    
    PARSING_THREAD = threading.Thread(target=parse_max_loop, args=(chat_id,), daemon=True)
    PARSING_THREAD.start()

@bot.callback_query_handler(func=lambda call: call.data == 'button01')
def back_to_menu(call):
    status = "🟢 Активен" if PARSING_ACTIVE else "🔴 Остановлен"
    text = f"🚀 <b>MAX\\_Parser готов\\!</b>\nСтатус: {status}\n\nВыберите действие:"
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=menu_button())


@bot.callback_query_handler(func=lambda call: call.data == 'button001')
def delete_msg(call):
    bot.delete_message(call.message.chat.id, call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data == 'button3')
def test(call):
    if call.from_user.id != int(ADMIN_ID):
        bot.answer_callback_query(call.id, "Только для админа 🤓", show_alert=True)
        return
    bot.edit_message_text("✅ <b>БОТ РАБОТАЕТ ШТАТНО\\!</b>", call.message.chat.id, call.message.message_id, reply_markup=comeback())


@bot.callback_query_handler(func=lambda call: call.data == 'button1')
def clear_cache(call):
    if call.from_user.id != int(ADMIN_ID):
        bot.answer_callback_query(call.id, "Только для админа 🤓", show_alert=True)
        return
    
    clear_all_caches()
    bot.edit_message_text("🗑 <b>Кэш очищен\\!</b>", call.message.chat.id, call.message.message_id, reply_markup=comeback())

@bot.callback_query_handler(func=lambda call: call.data == 'button2')
def status(call):
    if call.from_user.id != int(ADMIN_ID):
        bot.answer_callback_query(call.id, "Только для админа 🤓", show_alert=True)
        return
    
    from max_playwright_parser import message_cache
    status = "🟢 Активен" if PARSING_ACTIVE else "🔴 Остановлен"
    text = f"📊 <b>СТАТИСТИКА</b>\n📦 Кэш: <b>{len(message_cache)}</b> сообщений\n⚙️ Парсинг: {status}"
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=comeback())


@bot.callback_query_handler(func=lambda call: call.data == 'button4')
def updates(call):
    text = (
        "🚀 <b>Обновления Max\\_Parser</b>\n\n"
        "✨ <b>Что нового в последней версии:</b>\n\n"
        "🕰️ <b>Хронология:</b> Сообщения приходят в правильном порядке \\(от старых к новым\\)\\.\n"
        "📎 <b>Медиа:</b> Добавлена поддержка пересылки не только фото, но и документов \\(файлов\\)\\.\n"
        "🛡️ <b>Фильтрация:</b> Системные уведомления \\(\"добавил\", \"удалил\"\\) и аватарки автоматически игнорируются\\.\n"
        "🎨 <b>Оформление:</b> Имя отправителя, текст и время аккуратно разделены для удобного чтения\\.\n"
    )
    bot.edit_message_text(
        text, 
        call.message.chat.id, 
        call.message.message_id, 
        reply_markup=comeback(),
        parse_mode='HTML'
    )

@bot.callback_query_handler(func=lambda call: call.data == 'button5')
def info(call):
    text = (
        "ℹ️ <b>О боте Max\\_Parser</b>\n\n"
        "Этот бот разработан для автоматической пересылки сообщений, фотографий и документов из платформы Max прямо в этот Telegram\\-чат\\.\n\n"
        "⚙️ <b>Как это работает:</b>\n"
        "• Бот работает в фоновом режиме, проверяя чат каждые 60 секунд\\.\n"
        "• Управление \\(запуск, остановка, очистка\\) доступно <b>только администратору</b>\\.\n"
        "• Обычные пользователи могут просматривать только эту справку\\.\n\n"
        "Приятного использования\! 🚀"
    )
    bot.edit_message_text(
        text, 
        call.message.chat.id, 
        call.message.message_id, 
        reply_markup=comeback(),
        parse_mode='HTML'
    )


if __name__ == "__main__":
    try:
        print("✅ Запуск polling...")
        bot.infinity_polling(none_stop=True, timeout=60)
    except KeyboardInterrupt:
        print("🛑 Остановлен пользователем")
        save_message_cache()