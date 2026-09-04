import telebot
import os
import time
import io
import re
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
# Включаем MarkdownV2 глобально для красивого форматирования
bot = telebot.TeleBot(os.getenv("BOT_TOKEN"), parse_mode='MarkdownV2')

ADMIN_ID = os.getenv("ADMIN_ID")

PARSING_ACTIVE = False
PARSING_THREAD = None
CURRENT_CHAT_ID = None

def escape_md(text: str) -> str:
    """Экранирует спецсимволы для Telegram MarkdownV2"""
    if not text:
        return ""
    for char in r'_*[]()~`>#+-=|{}.!':
        text = text.replace(char, f'\\{char}')
    return text


def format_message(post: dict) -> str:
    """Формирует красивое сообщение с разделением имени, текста и времени"""
    name = post.get('name', '').strip()
    text = post.get('text', '').strip()
    msg_time = post.get('time', '').strip()

    # Заголовок с именем
    if name and name != 'Аноним':
        header = f"👤 *{escape_md(name)}*\n\n"
    else:
        header = ""

    # Тело сообщения
    body = escape_md(text)
    result = f"{header}{body}"

    # Время внизу
    if msg_time:
        result += f"\n\n🕐 _{escape_md(msg_time)}_"

    return result

def parse_max_loop(chat_id):
    """Основной цикл автопарсинга"""
    global PARSING_ACTIVE
    while PARSING_ACTIVE:
        try:
            print(f"\n🔄 Автопарсинг для чата {chat_id}...")
            posts = parse_max_group_media()
            new_count = 0
            skipped_count = 0

            if not posts:
                print("📭 Новых сообщений не найдено")
            else:
                for post in posts:
                    # ЕДИНСТВЕННОЕ место проверки на дубли (перед отправкой)
                    if not is_new_message(post):
                        print(f"⏭️ Пропущено (уже отправлено): {post['name'][:25]}")
                        skipped_count += 1
                        continue

                    media_files = post.get('media_files', [])
                    msg_text = format_message(post)
                    try:
                        if media_files:
                            first = media_files[0]
                            
                            # Создаём файловый объект из байтов в памяти
                            file_obj = io.BytesIO(first['bytes'])
                            
                            if first['type'] == 'document':
                                filename = first.get('filename', 'document.pdf')
                                bot.send_document(chat_id, file_obj, caption=msg_text, visible_file_name=filename)
                                print(f"✅ Отправлен документ из памяти: {filename}")
                                
                            elif first['type'] == 'image':
                                bot.send_photo(chat_id, file_obj, caption=msg_text)
                                print(f"✅ Отправлено фото из памяти: {len(first['bytes']) // 1024}KB")

                            # Отправка дополнительных файлов
                            for extra in media_files[1:]:
                                extra_obj = io.BytesIO(extra['bytes'])
                                if extra['type'] == 'document':
                                    filename = extra.get('filename', 'document.pdf')
                                    bot.send_document(chat_id, extra_obj, visible_file_name=filename)
                                else:
                                    bot.send_photo(chat_id, extra_obj)
                                time.sleep(0.5)
                        else:
                            bot.send_message(chat_id, msg_text)
                            print(f"✅ Отправлен текст: {post['name'][:25]}")

                        new_count += 1
                        time.sleep(1.5)

                    except Exception as e:
                        print(f"❌ Ошибка отправки: {e}")
                # Итоговое сообщение о результатах цикла
                if new_count > 0:
                    save_message_cache()
                    summary = f"✅ Отправлено: *{new_count}* новых"
                    if skipped_count > 0:
                        summary += f"\n⏭️ Пропущено: *{skipped_count}* (дубли)"
                    bot.send_message(chat_id, summary, reply_markup=comeback111())
                elif skipped_count > 0:
                    print(f"📭 Все {skipped_count} сообщений уже были отправлены ранее")

            time.sleep(20) # Пауза 20 секунд между циклами парсинга

        except Exception as e:
            print(f"❌ Ошибка в цикле автопарсинга: {e}")
            time.sleep(20)


# Загружаем кэш при старте бота
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


# ================= ОБРАБОТЧИКИ =================

@bot.message_handler(commands=['start'])
def start_bot(message):
    status = "🟢 Активен" if PARSING_ACTIVE else "🔴 Остановлен"
    text = f"🚀 *MAX\\_Parser готов\\!*\nСтатус: {status}\n\nВыберите действие:"
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
        "▶️ *Автопарсинг ЗАПУЩЕН*\n⏳ Проверка каждые *20 сек*\\.",
        chat_id, call.message.message_id, reply_markup=comeback()
    )
    print(f"🚀 Автопарсинг запущен для чата {chat_id}")
    
    PARSING_THREAD = threading.Thread(target=parse_max_loop, args=(chat_id,), daemon=True)
    PARSING_THREAD.start()

@bot.callback_query_handler(func=lambda call: call.data == 'button01')
def back_to_menu(call):
    status = "🟢 Активен" if PARSING_ACTIVE else "🔴 Остановлен"
    text = f"🚀 *MAX\\_Parser готов\\!*\nСтатус: {status}\n\nВыберите действие:"
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=menu_button())


@bot.callback_query_handler(func=lambda call: call.data == 'button001')
def delete_msg(call):
    bot.delete_message(call.message.chat.id, call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data == 'button3')
def test(call):
    if call.from_user.id != int(ADMIN_ID):
        bot.answer_callback_query(call.id, "Только для админа 🤓", show_alert=True)
        return
    bot.edit_message_text("✅ *БОТ РАБОТАЕТ ШТАТНО\\!*", call.message.chat.id, call.message.message_id, reply_markup=comeback())


@bot.callback_query_handler(func=lambda call: call.data == 'button1')
def clear_cache(call):
    if call.from_user.id != int(ADMIN_ID):
        bot.answer_callback_query(call.id, "Только для админа 🤓", show_alert=True)
        return
    
    clear_all_caches()
    bot.edit_message_text("🗑 *Кэш очищен\\!*", call.message.chat.id, call.message.message_id, reply_markup=comeback())

@bot.callback_query_handler(func=lambda call: call.data == 'button2')
def status(call):
    if call.from_user.id != int(ADMIN_ID):
        bot.answer_callback_query(call.id, "Только для админа 🤓", show_alert=True)
        return
    
    from max_playwright_parser import message_cache
    status = "🟢 Активен" if PARSING_ACTIVE else "🔴 Остановлен"
    text = f"📊 *СТАТИСТИКА*\n📦 Кэш: *{len(message_cache)}* сообщений\n⚙️ Парсинг: {status}"
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=comeback())


@bot.callback_query_handler(func=lambda call: call.data == 'button4')
def updates(call):
    text = (
        "*Обновления Max\\_Parser* 🚀\n\n"
        "✨ *Что нового в последней версии:*\n\n"
        "🕰️ *Хронология:* Сообщения приходят в правильном порядке \\(от старых к новым\\)\\.\n"
        "📎 *Медиа:* Добавлена поддержка пересылки не только фото, но и документов \\(файлов\\)\\.\n"
        "🛡️ *Фильтрация:* Системные уведомления \\(\"добавил\", \"удалил\"\\) и аватарки автоматически игнорируются\\.\n"
        "🎨 *Оформление:* Имя отправителя, текст и время аккуратно разделены для удобного чтения\\.\n"
    )
    bot.edit_message_text(
        text, 
        call.message.chat.id, 
        call.message.message_id, 
        reply_markup=comeback(),
        parse_mode='MarkdownV2'
    )

@bot.callback_query_handler(func=lambda call: call.data == 'button5')
def info(call):
    text = (
        "ℹ️ *О боте Max\\_Parser*\n\n"
        "Этот бот разработан для автоматической пересылки сообщений, фотографий и документов из платформы Max прямо в этот Telegram\\-чат\\.\n\n"
        "⚙️ *Как это работает:*\n"
        "• Бот работает в фоновом режиме, проверяя чат каждые 20 секунд\\.\n"
        "• Управление \\(запуск, остановка, очистка\\) доступно **только администратору**\\.\n"
        "• Обычные пользователи могут просматривать только эту справку\\.\n\n"
        "Приятного использования\! 🚀"
    )
    bot.edit_message_text(
        text, 
        call.message.chat.id, 
        call.message.message_id, 
        reply_markup=comeback(),
        parse_mode='MarkdownV2'
    )


if __name__ == "__main__":
    try:
        print("✅ Запуск polling...")
        bot.infinity_polling(none_stop=True, timeout=60)
    except KeyboardInterrupt:
        print("🛑 Остановлен пользователем")
        save_message_cache()