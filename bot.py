import telebot
import os
import time
import io
import re
import hashlib
import json
import threading
import traceback
import unicodedata
from datetime import datetime, timedelta, timezone
from telebot import types
from dotenv import load_dotenv
import concurrent.futures
from max_playwright_parser import *
from DB_service import *
from debug_log import logger as debug_logger, preview as log_preview

load_dotenv()
print("🚀 MAX Parser Bot запущен")
bot = telebot.TeleBot(os.getenv("BOT_TOKEN"), parse_mode='HTML')
ADMIN_ID = int(os.getenv("ADMIN_ID"))
add_admin(ADMIN_ID, "main_admin")
_current_cycle_hashes = {}
NO_PREVIEW = {"disable_web_page_preview": True}
active_logins = {}
awaiting_password_from = {}
support_sessions = set(get_open_support_sessions())
support_message_routes = get_support_message_routes()
support_state_lock = threading.RLock()
pending_setup_modes = {}
customer_setup_state = {}
SUPPORT_END_TEXT = "✅ Завершить чат с поддержкой"

bot.set_my_commands([
    types.BotCommand("start", "Запустить бота"),
    types.BotCommand("help", "Связаться с тех поддержкой"),
    types.BotCommand("connect", "Подключить группу MAX"),
    types.BotCommand("mychats", "Мои группы парсинга"),
    types.BotCommand("admin", "Админ-панель")
])

#!==============================================================================
#! 1) ФУНКЦИИ, СВЯЗАННЫЕ С ПАРСИНГОМ
#!==============================================================================

def _is_emoji_only_text(text: str, emoji_values) -> bool:
    text = str(text or "").strip()
    values = [str(value) for value in (emoji_values or []) if str(value).strip()]
    if not text or not values:
        return False
    remainder = text
    for value in values:
        remainder = remainder.replace(value, "")
    remainder = remainder.replace("\ufe0f", "").replace("\u200d", "").strip()
    return not remainder

def _media_to_bytesio(media: dict) -> io.BytesIO:
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

_telegram_last_send = 0.0
_telegram_send_lock = threading.Lock()
_send_context = threading.local()
_POLL_MESSAGE_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sent_poll_messages.json")
_poll_message_cache = {}
_poll_message_cache_lock = threading.Lock()

def _load_poll_message_cache():
    global _poll_message_cache
    try:
        with open(_POLL_MESSAGE_CACHE_FILE, "r", encoding="utf-8") as cache_file:
            value = json.load(cache_file)
        if isinstance(value, dict):
            _poll_message_cache = value
    except (OSError, ValueError, TypeError):
        _poll_message_cache = {}

def _save_poll_message_cache_locked():
    temporary_path = _POLL_MESSAGE_CACHE_FILE + ".tmp"
    try:
        with open(temporary_path, "w", encoding="utf-8") as cache_file:
            json.dump(_poll_message_cache, cache_file, ensure_ascii=False)
        os.replace(temporary_path, _POLL_MESSAGE_CACHE_FILE)
    except OSError as exc:
        debug_logger.warning("POLL_CACHE_SAVE_FAILED path=%s error=%s", _POLL_MESSAGE_CACHE_FILE, log_preview(exc))

def _poll_cache_key(chat_id: int, post: dict) -> str:
    source_id = str(post.get("source_id") or "").strip()
    if post.get("source_id_is_index") or not source_id:
        poll = post.get("poll") or {}
        stable = "|".join(str(value or "") for value in (
            post.get("name"), post.get("date"), post.get("time"), poll.get("question")
        ))
        source_id = hashlib.md5(stable.encode("utf-8")).hexdigest()
    return f"{int(chat_id)}:{source_id}"

def _format_poll_message(poll: dict, comment: str = "", is_closed: bool = False) -> str:
    question = str(poll.get("question") or "Опрос").strip()
    if re.match(r"^(?:poll ended|опрос заверш[её]н|total votes?|всего голосов?)\b", question, re.I):
        question = "Опрос"
    rows = []
    for item in poll.get("options") or []:
        if isinstance(item, dict):
            label = str(item.get("text") or "").strip()
            raw_votes = item.get("votes")
        else:
            label, raw_votes = str(item or "").strip(), 0
        if re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", label):
            continue
        label = re.sub(r"^[⚪⚫▫•]+\s*", "", label)
        label = re.sub(r"\s+(?:your choice|ваш выбор|winner|победитель|you voted|вы проголосовали|selected)\b.*$", "", label, flags=re.I)
        try:
            votes = max(0, int(raw_votes if raw_votes is not None else 0))
        except (TypeError, ValueError):
            votes = 0
        suffix = re.match(r"^(.*?)(?:\s*[.,]\s*|\s+)(\d+)\s*(?:votes?|голос(?:а|ов)?)$", label, re.I)
        if suffix:
            label = suffix.group(1).strip()
            votes = max(votes, int(suffix.group(2)))
        result = re.match(r"^(.*?)\s*[.,]\s*(\d+)\s*[,;]\s*\d+(?:[.,]\d+)?\s*%.*$", label, re.I)
        if result:
            label = result.group(1).strip()
            votes = max(votes, int(result.group(2)))
        label = re.sub(r"\s*[,;·|]?\s*\d+(?:[.,]\d+)?\s*%.*$", "", label).strip(" -–—.,;:")
        if not label or re.match(r"^(?:poll ended|опрос заверш[её]н|total votes?|всего голосов?)\b", label, re.I):
            continue
        rows.append((label, votes))

    total_votes = sum(votes for _, votes in rows)
    try:
        total_votes = max(total_votes, int(poll.get("total_votes") or 0))
    except (TypeError, ValueError):
        pass
    poll_title = "Завершенный опрос" if is_closed else "Опрос"
    lines = [f"<b>📊 {poll_title}\n\n{escape_html(question)}</b>"]
    body_lines = []
    if rows:
        option_lines = "\n".join(f"{escape_html(label)}: {votes}" for label, votes in rows)
        body_lines.append(option_lines)
    comment = str(comment or "").strip()
    if comment:
        body_lines.append(f"Комментарий к опросу: {escape_html(comment)}")
    body_lines.append(f"<i>Всего ответов: {total_votes}</i>")
    body = "\n".join(body_lines)
    lines.append(f"<blockquote>{body}</blockquote>")
    return "\n".join(lines)

def _poll_is_closed(post: dict) -> bool:
    poll = post.get("poll") or {}
    if not isinstance(poll, dict):
        return False
    if poll.get("is_closed") is True:
        return True
    terminal_states = {
        "closed", "ended", "finished", "completed", "закрыт", "закрыто",
        "завершён", "завершен", "завершено",
    }
    if any(str(poll.get(key) or "").strip().lower() in terminal_states for key in (
        "status", "state", "completion_status"
    )):
        return True
    state = str(poll.get("raw_text") or "").lower()
    return bool(re.search(
        r"(?:\bpoll\s+(?:is\s+)?(?:closed|ended|finished)\b|"
        r"\bsurvey\s+(?:is\s+)?(?:closed|ended|finished)\b|"
        r"опрос\s+(?:закрыт|заверш[её]н)|голосование\s+(?:закрыто|завершено))",
        state,
        re.I,
    ))

def _poll_has_delivery(entry: dict) -> bool:
    return bool(
        entry.get("initial_message_id")
        or entry.get("message_id")
        or entry.get("final_sent")
    )

def _get_poll_delivery_state(chat_id: int, post: dict) -> dict:
    key = _poll_cache_key(chat_id, post)
    with _poll_message_cache_lock:
        entry = _poll_message_cache.get(key)
        return dict(entry) if isinstance(entry, dict) else {}

def _remember_sent_poll(chat_id: int, post: dict, message_id):
    if not message_id or not post.get("poll"):
        return
    key = _poll_cache_key(chat_id, post)
    closed = _poll_is_closed(post)
    text = _format_poll_message(
        post.get("poll") or {}, comment=post.get("text") or "", is_closed=closed
    )
    with _poll_message_cache_lock:
        entry = _poll_message_cache.get(key)
        entry = dict(entry) if isinstance(entry, dict) else {}
        sent_id = int(message_id)
        if closed:
            entry.update({
                "final_sent": True,
                "final_message_id": sent_id,
                "final_text": text,
            })
            entry.setdefault("message_id", sent_id)
            entry.setdefault("text", text)
        else:
            entry.setdefault("initial_message_id", entry.get("message_id") or sent_id)
            entry.setdefault("initial_text", entry.get("text") or text)
            entry.setdefault("message_id", sent_id)
            entry.setdefault("text", text)
            entry.setdefault("final_sent", False)
        entry["has_photo"] = any(
            media.get("type") == "image" for media in (post.get("media_files") or [])
        )
        entry["stage"] = "final" if closed else "initial"
        _poll_message_cache[key] = entry
        _save_poll_message_cache_locked()
    debug_logger.info(
        "POLL_SNAPSHOT_RECORDED chat_id=%s source_id=%s stage=%s telegram_message_id=%s final_sent=%s",
        chat_id, post.get("source_id") or "?", "final" if closed else "initial",
        int(message_id), bool(entry.get("final_sent")),
    )

def _refresh_poll_snapshot(chat_id: int, post: dict, entry: dict, closed: bool) -> bool:
    message_id = (
        entry.get("final_message_id") or entry.get("message_id")
        if closed else entry.get("initial_message_id") or entry.get("message_id")
    )
    if not message_id:
        return False
    text = _format_poll_message(
        post.get("poll") or {}, comment=post.get("text") or "", is_closed=closed
    )
    text_key = "final_text" if closed else "initial_text"
    old_text = entry.get(text_key) or entry.get("text") or ""
    if text == old_text:
        return False

    has_photo = bool(entry.get("has_photo")) or any(
        media.get("type") == "image" for media in (post.get("media_files") or [])
    )
    if has_photo:
        if len(text) > 1024:
            debug_logger.warning(
                "POLL_SNAPSHOT_EDIT_SKIPPED chat_id=%s source_id=%s reason=caption_too_long chars=%s",
                chat_id, post.get("source_id") or "?", len(text),
            )
            return False
        edit = lambda: _telegram_call(
            bot.edit_message_caption, chat_id=chat_id, message_id=int(message_id),
            caption=text, parse_mode="HTML",
        )
    else:
        edit = lambda: _telegram_call(
            bot.edit_message_text, text, chat_id=chat_id, message_id=int(message_id),
            parse_mode="HTML", **NO_PREVIEW,
        )

    try:
        edit()
    except Exception as exc:
        debug_logger.warning(
            "POLL_SNAPSHOT_EDIT_FAILED chat_id=%s source_id=%s message_id=%s error=%s",
            chat_id, post.get("source_id") or "?", message_id, log_preview(exc, 200),
        )
        return False

    key = _poll_cache_key(chat_id, post)
    with _poll_message_cache_lock:
        current = _poll_message_cache.get(key)
        if not isinstance(current, dict):
            current = dict(entry)
            _poll_message_cache[key] = current
        current[text_key] = text
        current["text"] = text
        current["has_photo"] = has_photo
        _save_poll_message_cache_locked()
    debug_logger.info(
        "POLL_SNAPSHOT_UPDATED chat_id=%s source_id=%s stage=%s message_id=%s",
        chat_id, post.get("source_id") or "?", "final" if closed else "initial", message_id,
    )
    return True

_load_poll_message_cache()

def _telegram_result_ids(result):
    values = result if isinstance(result, (list, tuple)) else [result]
    return [str(getattr(item, "message_id", "")) for item in values if getattr(item, "message_id", None) is not None]

def _telegram_payload_preview(fn, args, kwargs):
    method = getattr(fn, "__name__", "telegram_api")
    if method == "send_message" and len(args) > 1:
        return log_preview(args[1], 240)
    if kwargs.get("caption"):
        return log_preview(kwargs["caption"], 240)
    if method == "send_media_group" and len(args) > 1:
        captions = [getattr(item, "caption", "") for item in (args[1] or []) if getattr(item, "caption", "")]
        return log_preview(" | ".join(captions), 240)
    return ""

def _rewind_telegram_uploads(value):
    if isinstance(value, io.IOBase):
        value.seek(0)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _rewind_telegram_uploads(item)
        return
    media = getattr(value, "media", None)
    if media is not None:
        _rewind_telegram_uploads(media)

def _telegram_call(fn, *args, **kwargs):
    global _telegram_last_send
    with _telegram_send_lock:
        for attempt in range(4):
            wait = 0.8 - (time.monotonic() - _telegram_last_send)
            if wait > 0:
                time.sleep(wait)
            try:
                for value in args:
                    _rewind_telegram_uploads(value)
                for value in kwargs.values():
                    _rewind_telegram_uploads(value)
                result = fn(*args, **kwargs)
                _telegram_last_send = time.monotonic()
                ctx = getattr(_send_context, "current", None)
                if ctx is not None:
                    ids = _telegram_result_ids(result)
                    ctx["api_ok"] += 1
                    ctx["message_ids"].extend(ids)
                    debug_logger.info(
                        "TELEGRAM_SEND_OK chat_id=%s source_id=%s method=%s telegram_message_ids=%s payload=%s",
                        ctx["chat_id"], ctx["source_id"], getattr(fn, "__name__", "telegram_api"),
                        ids or "<no-id>", _telegram_payload_preview(fn, args, kwargs) or "<media payload>"
                    )
                return result
            except Exception as e:
                text = str(e)
                ctx = getattr(_send_context, "current", None)
                if ctx is not None:
                    ctx["api_errors"] += 1
                    debug_logger.error(
                        "TELEGRAM_SEND_FAIL chat_id=%s source_id=%s method=%s payload=%s error=%s",
                        ctx["chat_id"], ctx["source_id"], getattr(fn, "__name__", "telegram_api"),
                        _telegram_payload_preview(fn, args, kwargs) or "<media payload>",
                        log_preview(text, 300)
                    )
                m = re.search(r"retry after (\d+)", text, re.I)
                if "429" in text and m and attempt < 3:
                    delay = int(m.group(1)) + 1
                    print(f"⏳ Telegram 429: ждём {delay} сек.")
                    time.sleep(delay)
                    continue
                raise

def _send_post_impl(chat_id: int, post: dict) -> None:
    msg_text = format_message(post)
    media_files = post.get("media_files", []) or []
    ctx = getattr(_send_context, "current", None)
    if ctx is not None:
        ctx["suppressed_media"] = 0
    send_media = []
    for media in media_files:
        if media.get("type") == "emoji":
            continue
        is_poll_image = bool(post.get("poll")) and media.get("type") == "image"
        if media.get("type") in {"image", "video_note"} and not is_poll_image and _media_already_sent(media, chat_id):
            if ctx is not None:
                ctx["suppressed_media"] += 1
            debug_logger.info(
                "MEDIA_SKIPPED chat_id=%s source_id=%s reason=media_already_sent hash=%s",
                chat_id, post.get("source_id") or post.get("idx") or "?",
                media.get("hash") or get_media_hash(data=media.get("bytes")),
            )
            continue
        send_media.append(media)

    location = post.get("location") or {}
    contact = post.get("contact") or {}
    poll = post.get("poll") or {}
    special_sent = False

    def yandex_link(loc: dict) -> str:
        lat=loc.get("latitude"); lon=loc.get("longitude")
        raw=str(loc.get("url") or "").strip()
        if re.match(r"^https?://",raw,re.I) and re.search(r"yandex\.(?:ru|com)/maps",raw,re.I):
            return raw
        if lat is not None and lon is not None:
            try:
                return f"https://yandex.ru/maps/?ll={float(lon)}%2C{float(lat)}&pt={float(lon)}%2C{float(lat)}&z=17"
            except Exception:
                pass
        return raw or "https://yandex.ru/maps/"

    if location and (location.get("url") or (location.get("latitude") is not None and location.get("longitude") is not None)):
        url=yandex_link(location)
        caption=f'Геопозиция: <a href="{escape_html(url)}">ссылка на геопозицию</a>'
        map_media=next((m for m in send_media if m.get("type")=="image"),None)
        if map_media:
            obj=_media_to_bytesio(map_media)
            try:
                _telegram_call(bot.send_photo,chat_id,obj,caption=caption,parse_mode="HTML")
            except Exception as exc:
                print(f"⚠️ [{chat_id}] Не удалось отправить карту с caption: {exc}")
                _telegram_call(bot.send_message,chat_id,caption,parse_mode="HTML",**NO_PREVIEW)
            send_media=[]
        else:
            _telegram_call(bot.send_message,chat_id,caption,parse_mode="HTML",**NO_PREVIEW)
            send_media=[]
        special_sent=True

    if contact.get("is_contact"):
        name=" ".join(str(contact.get(key) or "").strip() for key in ("first_name", "last_name") if str(contact.get(key) or "").strip())
        name=name or str(contact.get("name") or "").strip()
        phone=str(contact.get("phone") or "").strip()
        name=re.sub(r"\s*(?:already in your contacts|уже в ваших контактах)\s*", " ", name, flags=re.I).strip()
        detail_value=phone or "не найден"
        text=(
            "<b>Контакт</b>\n\n"
            "<blockquote>"
            f"Имя: {escape_html(name or 'не указано')}\n"
            f"Номер телефона: <code>{escape_html(detail_value)}</code>"
            "</blockquote>"
        )
        _telegram_call(bot.send_message,chat_id,text,parse_mode="HTML",**NO_PREVIEW)
        special_sent=True
        send_media=[]

    if poll:
        poll_text = _format_poll_message(
            poll, comment=post.get("text") or "", is_closed=_poll_is_closed(post)
        )
        poll_image = next((media for media in send_media if media.get("type") == "image"), None)
        if poll_image:
            image_file = _media_to_bytesio(poll_image)
            if len(poll_text) <= 1024:
                _telegram_call(bot.send_photo, chat_id, image_file, caption=poll_text, parse_mode="HTML")
            else:
                _telegram_call(bot.send_photo, chat_id, image_file)
                _telegram_call(bot.send_message, chat_id, poll_text, parse_mode="HTML", **NO_PREVIEW)
        else:
            _telegram_call(bot.send_message, chat_id, poll_text, parse_mode="HTML", **NO_PREVIEW)
        special_sent=True
        send_media=[]

    if special_sent:
        msg_text=""

    actual_text=_clean_browser_video_fallback(post.get("text") or "")
    emoji_texts=post.get("emoji_texts") or []
    if not special_sent and emoji_texts:
        emoji_text = actual_text if _is_emoji_only_text(actual_text, emoji_texts) else ""
        if not actual_text:
            emoji_text = "".join(str(x) for x in emoji_texts if str(x).strip()).strip()
        if emoji_text and not actual_text:
            actual_text = emoji_text
            emoji_post = dict(post)
            emoji_post["text"] = emoji_text
            msg_text = format_message(emoji_post)

    if not send_media:
        if actual_text and not special_sent:
            _telegram_call(bot.send_message,chat_id,msg_text,**NO_PREVIEW)
        return

    has_voice_or_circle=any(m.get("type") in ("voice","video_note") for m in send_media)
    caption_pending=bool(actual_text) and not special_sent and not has_voice_or_circle

    photos=[m for m in send_media if m.get("type")=="image"]
    album_sent_ids=set()
    if len(photos)>=2:
        for start in range(0,len(photos)-1,10):
            chunk=photos[start:start+10]
            if len(chunk)<2:
                break
            album=[]
            caption_in_album=False
            for media in chunk:
                obj=_media_to_bytesio(media)
                caption=msg_text if caption_pending and not caption_in_album and len(msg_text)<=1024 else None
                parse_mode="HTML" if caption else None
                album.append(types.InputMediaPhoto(obj,caption=caption,parse_mode=parse_mode))
                if caption:
                    caption_in_album=True
            try:
                _telegram_call(bot.send_media_group,chat_id,album)
                album_sent_ids.update(id(m) for m in chunk)
                if caption_in_album:
                    caption_pending=False
            except Exception as album_error:
                print(f"⚠️ [{chat_id}] Не удалось отправить фотоальбом: {album_error}")
                break
        if album_sent_ids:
            send_media=[m for m in send_media if id(m) not in album_sent_ids]

    for media in send_media:
        media_type=media.get("type")
        obj=_media_to_bytesio(media)
        filename=media.get("filename") or "document.bin"
        try:
            if media_type=="image":
                if caption_pending and len(msg_text)<=1024:
                    _telegram_call(bot.send_photo,chat_id,obj,caption=msg_text)
                    caption_pending=False
                else:
                    _telegram_call(bot.send_photo,chat_id,obj)
            elif media_type=="video_note":
                _telegram_call(bot.send_video_note,chat_id,obj)
                caption_pending=False
            elif media_type=="video":
                if caption_pending and len(msg_text)<=1024:
                    _telegram_call(bot.send_video,chat_id,obj,caption=msg_text,supports_streaming=True)
                    caption_pending=False
                else:
                    _telegram_call(bot.send_video,chat_id,obj,supports_streaming=True)
            elif media_type=="voice":
                try:
                    _telegram_call(bot.send_voice,chat_id,obj)
                except Exception as voice_error:
                    print(f"⚠️ [{chat_id}] send_voice не принят Telegram: {voice_error}; пробуем audio")
                    obj.seek(0)
                    _telegram_call(bot.send_audio,chat_id,obj,title=filename)
                caption_pending=False
            elif media_type=="audio":
                _telegram_call(bot.send_audio,chat_id,obj,title=filename)
                caption_pending=False
            elif media_type=="animation":
                _telegram_call(bot.send_animation,chat_id,obj)
            elif media_type=="sticker":
                _telegram_call(bot.send_photo,chat_id,obj)
            else:
                if caption_pending and len(msg_text)<=1024:
                    _telegram_call(bot.send_document,chat_id,obj,caption=msg_text,visible_file_name=filename)
                    caption_pending=False
                else:
                    _telegram_call(bot.send_document,chat_id,obj,visible_file_name=filename)
        except Exception as error:
            print(f"⚠️ [{chat_id}] Ошибка отправки {media_type}: {error}")
            if media_type not in ("voice","video_note","image","sticker"):
                try:
                    obj.seek(0)
                    _telegram_call(bot.send_document,chat_id,obj,visible_file_name=filename)
                except Exception as fallback_error:
                    raise RuntimeError(
                        f"Не отправилось медиа {media_type}; fallback send_document также завершился ошибкой: {fallback_error}"
                    ) from error
            else:
                raise
        time.sleep(0.5)

    if caption_pending:
        _telegram_call(bot.send_message,chat_id,msg_text,**NO_PREVIEW)

def _send_post(chat_id: int, post: dict) -> bool:
    post_id = str(post.get("source_id") or post.get("idx") or "?")
    context = {
        "chat_id": chat_id,
        "source_id": post_id,
        "api_ok": 0,
        "api_errors": 0,
        "message_ids": [],
        "suppressed_media": 0,
    }
    previous = getattr(_send_context, "current", None)
    _send_context.current = context
    media_types = [m.get("type", "unknown") for m in (post.get("media_files") or []) if isinstance(m, dict)]
    post_kind = (
        "poll" if post.get("poll") else
        "contact" if (post.get("contact") or {}).get("is_contact") else
        "location" if post.get("location") else
        "media" if media_types else
        "emoji" if post.get("emoji_texts") else "text"
    )
    debug_logger.info(
        "POST_SEND_START chat_id=%s source_id=%s kind=%s date=%s time=%s media=%s text=%s",
        chat_id, post_id, post_kind, post.get("date") or "<missing>",
        post.get("time") or "<missing>", media_types, log_preview(post.get("text")) or "<empty>"
    )
    try:
        _send_post_impl(chat_id, post)
        if post.get("poll") and context["message_ids"]:
            _remember_sent_poll(chat_id, post, context["message_ids"][0])
        if context["api_ok"]:
            result = "sent"
        elif context["suppressed_media"]:
            result = "duplicate_media_suppressed"
        else:
            result = "no_telegram_payload"
        log_method = debug_logger.warning if result != "sent" else debug_logger.info
        log_method(
            "POST_SEND_COMPLETE chat_id=%s source_id=%s result=%s api_ok=%s api_errors=%s telegram_message_ids=%s suppressed_media=%s",
            chat_id, post_id, result, context["api_ok"], context["api_errors"],
            context["message_ids"] or "<none>", context["suppressed_media"]
        )
        return result != "no_telegram_payload"
    except Exception as exc:
        debug_logger.exception(
            "POST_SEND_COMPLETE chat_id=%s source_id=%s result=failed api_ok=%s api_errors=%s error=%s",
            chat_id, post_id, context["api_ok"], context["api_errors"], log_preview(exc, 300)
        )
        raise
    finally:
        _send_context.current = previous

def _commit_successful_post(chat_id: int, post: dict) -> None:
    mark_message_seen(post, chat_id=chat_id)
    for media in post.get("media_files", []) or []:
        data = media.get("bytes")
        if isinstance(data, (bytes, bytearray)) and data:
            mark_media_seen(
                data=data,
                chat_id=chat_id,
                media_hash=media.get("hash"),
            )
            visual_hash = media.get("visual_hash")
            if visual_hash and visual_hash != media.get("hash"):
                mark_media_seen(
                    data=data,
                    chat_id=chat_id,
                    media_hash=visual_hash,
                )
    save_message_cache()
    save_photo_cache()

def _process_posts(chat_id: int, title: str, posts: list) -> tuple:
    if posts is None:
        raise TypeError(f"Парсер MAX для чата {title} вернул None вместо списка сообщений")
    if not isinstance(posts, list):
        raise TypeError(
            f"Парсер MAX для чата {title} вернул {type(posts).__name__} вместо списка сообщений"
        )
    new_count = 0
    skipped_count = 0
    failed_count = 0
    debug_logger.info("CHAT_PROCESS_START chat_id=%s title=%s posts=%s", chat_id, log_preview(title, 100), len(posts))
    cycle_ids = set()
    for post in posts:
        source_id = str(post.get("source_id") or "").strip()
        if post.get("source_id_is_index"):
            cycle_key = get_message_hash(post)
        else:
            cycle_key = source_id or get_message_hash(post)

        if cycle_key in cycle_ids:
            skipped_count += 1
            debug_logger.info("MESSAGE_SKIPPED chat_id=%s source_id=%s reason=duplicate_in_cycle", chat_id, source_id or "?")
            continue
        cycle_ids.add(cycle_key)

        force_poll_send = False
        if post.get("poll"):
            poll_state = _get_poll_delivery_state(chat_id, post)
            if _poll_is_closed(post):
                if poll_state.get("final_sent"):
                    _refresh_poll_snapshot(chat_id, post, poll_state, closed=True)
                    skipped_count += 1
                    debug_logger.info(
                        "MESSAGE_SKIPPED chat_id=%s source_id=%s reason=poll_final_already_sent",
                        chat_id, source_id or "?",
                    )
                    continue
                force_poll_send = True
                debug_logger.info(
                    "POLL_FINAL_SEND_START chat_id=%s source_id=%s question=%s",
                    chat_id, source_id or "?",
                    log_preview((post.get("poll") or {}).get("question"), 120),
                )
            elif _poll_has_delivery(poll_state):
                _refresh_poll_snapshot(chat_id, post, poll_state, closed=False)
                skipped_count += 1
                debug_logger.info(
                    "MESSAGE_SKIPPED chat_id=%s source_id=%s reason=poll_initial_already_sent",
                    chat_id, source_id or "?",
                )
                continue

        if not force_poll_send and not is_new_message(post, chat_id=chat_id):
            skipped_count += 1
            debug_logger.info("MESSAGE_SKIPPED chat_id=%s source_id=%s reason=seen_cache", chat_id, source_id or "?")
            continue

        try:
            sent = _send_post(chat_id, post)
            if not sent:
                skipped_count += 1
                debug_logger.warning(
                    "MESSAGE_NOT_SENT chat_id=%s source_id=%s reason=no_payload text=%s",
                    chat_id, source_id or "?", log_preview(post.get("text")) or "<empty>"
                )
                continue
            _commit_successful_post(chat_id, post)
            increment_message_count(chat_id)
            new_count += 1
            time.sleep(1.5)
        except Exception as error:
            failed_count += 1
            debug_logger.exception(
                "MESSAGE_PROCESS_FAILED chat_id=%s source_id=%s title=%s error=%s",
                chat_id, source_id or "?", log_preview(title, 100), log_preview(error, 300)
            )
            print(f"❌ [{chat_id}] Ошибка отправки в чат {title}: {error}")

    save_message_cache()
    save_photo_cache()
    debug_logger.info(
        "CHAT_PROCESS_COMPLETE chat_id=%s title=%s sent=%s skipped=%s failed=%s",
        chat_id, log_preview(title, 100), new_count, skipped_count, failed_count
    )
    return new_count, skipped_count


def _session_path_for_chat(chat):
    if chat.get("account_mode") == "project":
        return os.path.join("sessions", "project_account.json")
    return os.path.join("sessions", f"{chat['chat_id']}.json")

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
            posts = parse_max_group_media(
                group_url=max_url,
                chat_id=chat_id,
                session_path=_session_path_for_chat(chat),
            )
            new_count, skipped_count = _process_posts(
                chat_id,
                title,
                posts,
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
            login_command = "/project_login" if chat.get("account_mode") == "project" else f"/login {chat_id}"
            if "SESSION_EXPIRED" in error_str:
                debug_path = get_parse_debug_screenshot(chat_id)
                if "|" in error_str:
                    candidate = error_str.split("|", 1)[1]
                    if candidate.endswith(".png"):
                        debug_path = candidate
                error_msg = (
                    f"⚠️ <b>{title}</b>: сессия истекла!\n"
                    f"Используйте `{login_command}`"
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
                    f"⚠️ <b>{title}</b>: нет сессии! Используйте `{login_command}`"
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
    chat_id = chat["chat_id"]
    max_url = chat["max_url"]
    title = chat["title"]
    try:
        posts = parse_max_group_media(
            group_url=max_url,
            chat_id=chat_id,
            session_path=_session_path_for_chat(chat),
        )
        _process_posts(chat_id, title, posts)
        return True
    except Exception as error:
        print(f"❌ Ошибка парсинга чата {title}: {error}")
        traceback.print_exc()
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

#!==============================================================================
#! 2) ОСТАЛЬНЫЕ ФУНКЦИИ
#!==============================================================================
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

def _clean_browser_video_fallback(text: str) -> str:
    text = re.sub(
        r"(?:your browser does not support (?:this )?video|your browser does not support the video tag|"
        r"ваш браузер не поддерживает (?:это )?видео)(?:\s+\d{1,2}:\d{2})?\.?",
        " ", str(text or ""), flags=re.I,
    )
    lines = [re.sub(r"[\t ]+", " ", line).strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

def _escape_text_with_links(text: str, links: list) -> str:
    text = str(text or "")
    if not text or not links:
        return escape_html(text)
    output = []
    cursor = 0
    for link in links:
        if not isinstance(link, dict):
            continue
        label = str(link.get("text") or "")
        url = str(link.get("url") or "").strip()
        if not label or not re.match(r"^(?:https?://|tg:|mailto:|tel:)", url, re.I):
            continue
        start = text.find(label, cursor)
        if start < 0:
            continue
        output.append(escape_html(text[cursor:start]))
        safe_url = escape_html(url).replace('"', "&quot;")
        output.append(f'<a href="{safe_url}">{escape_html(label)}</a>')
        cursor = start + len(label)
    output.append(escape_html(text[cursor:]))
    return "".join(output)

def format_message_time(raw_time: str, raw_date: str = "") -> str:
    if not raw_time:
        return ""

    weekdays = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
    months = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
              'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря']

    date_str = ""
    value = (raw_date or "").strip()
    parsed_date = None
    for pattern in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            parsed_date = datetime.strptime(value, pattern)
            break
        except ValueError:
            pass
    if parsed_date is None and re.match(r"^(today|сегодня|yesterday|вчера)$", value, re.I):
        parsed_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if re.match(r"^(yesterday|вчера)$", value, re.I):
            parsed_date -= timedelta(days=1)
    if parsed_date is None:
        for pattern in ("%B %d, %Y", "%b %d, %Y", "%B %d", "%b %d"):
            try:
                parsed_date = datetime.strptime(value, pattern)
                if "%Y" not in pattern:
                    parsed_date = parsed_date.replace(year=datetime.now().year)
                break
            except ValueError:
                pass
    if parsed_date is None:
        m = re.match(r"^(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?:\s+(\d{4}))?$", value, re.I)
        if m:
            month_map = {name:i+1 for i,name in enumerate(months)}
            parsed_date = datetime(int(m.group(3) or datetime.now().year), month_map[m.group(2).lower()], int(m.group(1)))
    raw_time = raw_time.strip()
    time_str = raw_time
    try:
        raw_upper = raw_time.upper()
        if 'AM' in raw_upper or 'PM' in raw_upper:
            clock = datetime.strptime(raw_upper.replace(' ', ''), "%I:%M%p")
        elif ':' in raw_time:
            clock = datetime.strptime(raw_time, "%H:%M")
        else:
            clock = None
        if clock is not None:
            time_str = clock.strftime("%H:%M")
    except ValueError:
        pass

    if parsed_date is not None:
        date_str = f"{weekdays[parsed_date.weekday()]}, {parsed_date.day} {months[parsed_date.month - 1]}"
    else:
        date_str = "дата не определена"

    return f"<b><blockquote>🗓 {date_str} | 🕐 {time_str}</blockquote></b>"

def format_message(post: dict) -> str:
    raw_name = post.get('name', '').strip()
    raw_text = _clean_browser_video_fallback(post.get('text', '').strip())
    raw_time = post.get('time', '').strip()
    raw_date = post.get('date', '').strip()
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
    body = _escape_text_with_links(raw_text, post.get("inline_links") or [])
    result = f"{header}{body}"
    time_footer = format_message_time(raw_time, raw_date)
    if time_footer:
        result += f"\n\n{time_footer}"
    return result

def get_cancel_keyboard():
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

def _build_qr_keyboard(target_chat_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("✅ Готово, я отсканировал", callback_data=f"qr_ready_{target_chat_id}"))
    kb.add(types.InlineKeyboardButton("🔄 Обновить QR", callback_data=f"qr_refresh_{target_chat_id}"))
    kb.add(types.InlineKeyboardButton("❌ Отменить", callback_data=f"qr_cancel_{target_chat_id}"))
    return kb


def _qr_target_from_action(action):
    raw_target = action.split("_", 2)[2]
    return "project_account" if raw_target == "project_account" else int(raw_target)

def _build_password_keyboard(target_chat_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("❌ Отменить авторизацию", callback_data=f"qr_cancel_{target_chat_id}"))
    return kb

def do_qr_login(target_chat_id, admin_chat_id):
    is_project_account = target_chat_id == "project_account"
    chat = None if is_project_account else get_chat_by_id(target_chat_id)
    if not is_project_account and not chat:
        bot.send_message(admin_chat_id, "❌ Чат не найден", **NO_PREVIEW)
        return
    title = "аккаунта проекта" if is_project_account else chat["title"]
    session = active_logins.pop(target_chat_id, None)
    if session:
        try:
            session.close()
        except:
            pass
    awaiting_password_from.pop(target_chat_id, None)
    bot.send_message(
        admin_chat_id,
        f"⏳ Открываю web.max.ru для <b>{escape_html(title)}</b>...\nЭто займет несколько секунд.",
        parse_mode='HTML', **NO_PREVIEW
    )
    session = QRLoginSession(target_chat_id)
    status, data = session.start()
    if status == "already_logged_in":
        if not is_project_account and chat.get("account_mode") == "self" and chat.get("max_url"):
            toggle_chat(target_chat_id, True)
        bot.send_message(
            admin_chat_id,
            f"✅ <b>Уже авторизован!</b>\nСессия для <b>{escape_html(title)}</b> актуальна.",
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
                f"MAX: <b>{escape_html(title)}</b>\n\n"
                f"После сканирования нажмите <b>«✅ Готово, я отсканировал»</b>"
            ),
            parse_mode='HTML',
            reply_markup=kb
        )
    except Exception as e:
        bot.send_message(admin_chat_id, f"❌ Ошибка отправки скриншота: {e}", **NO_PREVIEW)
        session.close()
        del active_logins[target_chat_id]

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
    kb.row(types.InlineKeyboardButton("📢 Рассылка всем", callback_data="admin_broadcast"))
    kb.row(types.InlineKeyboardButton("💬 Отправить в чат", callback_data="admin_send_to_chat"))
    kb.row(types.InlineKeyboardButton("👤 Отправить пользователю", callback_data="admin_send_to_user"))
    kb.add(types.InlineKeyboardButton("🔑 MAX-аккаунт проекта", callback_data="admin_project_login"))
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
            elif not c.get('max_url'):
                status = "🟡"
                status_text = "Не настроен"
            else:
                status = "🔴"
                status_text = "Остановлен"
            url_mark = "✅" if c.get('max_url') else ""
            phone_mark = "✅" if c.get('phone') else "—"
            session_mark = "✅" if os.path.exists(_session_path_for_chat(c)) else "❌"
            stats = get_chat_stats(c['chat_id'])
            text += f"\n\n{status} <b>{c['title']}</b>"
            text += f"<blockquote>🆔 <code>{c['chat_id']}</code> │ {status_text}\n"
            text += f"🔗URL: {url_mark} │ 📱Телефон: {phone_mark} │ 🔑Сессия: {session_mark}\n"
            text += f"💬 Сегодня: {stats['today']} │ Всего: {stats['total']}</blockquote>"
    kb = types.InlineKeyboardMarkup(row_width=1)
    for c in chats:
        if c['is_active']:
            icon = "⏸"
        elif not c.get('max_url'):
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
    elif not chat.get('max_url'):
        status_text = "🟡 Нужно настроить"
    else:
        status_text = "🔴 Остановлен"
    session_file = _session_path_for_chat(chat)
    session_status = "✅ Активна" if os.path.exists(session_file) else "❌ Отсутствует"
    account_mode_text = "Аккаунт проекта" if chat.get("account_mode") == "project" else "Аккаунт пользователя"
    text = (
        f"📋 <b>{chat['title']}</b>\n\n"
        f"🆔 <b>ID:</b> <code>{chat['chat_id']}</code>\n"
        f"🔗 <b>URL:</b> {chat['max_url'] or ' не задан'}\n"
        f"📱 <b>Телефон:</b> {chat['phone'] or '❌ не задан'}\n"
        f"👤 <b>MAX-аккаунт:</b> {account_mode_text}\n"
        f"🧑‍💻 <b>Владелец Telegram:</b> <code>{chat.get('owner_id') or 'не назначен'}</code>\n"
        f"🔑 <b>Сессия:</b> {session_status}\n"
        f"⚙️ <b>Парсинг:</b> {status_text}\n\n"
        f"<blockquote>"
        f"💬 <b>Отправлено:</b> {stats['today']} сегодня / {stats['total']} всего\n"
        f"📦 <b>В кэше:</b> {cache_count} сообщений (защита от дублей)"
        f"</blockquote>"
    )
    kb = types.InlineKeyboardMarkup(row_width=1)
    if chat.get('max_url') and (chat.get('phone') or os.path.exists(session_file)):
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
    text = (
        f"📊 <b>Статистика: {chat['title']}</b>\n\n"
        f"<blockquote>"
        f"💬 <b>Отправлено всего:</b> {stats['total']}\n"
        f"🗃️ <b>В кэше (защита от дублей):</b> {cache_count} сообщений"
        f"</blockquote>\n\n"
        f"<i>Детальный список последних сообщений отключен для экономии места в БД и ускорения работы парсера.</i>"
    )
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

def process_add_chat_step1(message, admin_chat_id):
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
    if not re.match(r'^\\+?\d{10,15}$', phone.replace(' ', '').replace('-', '')):
        bot.send_message(message.chat.id, "❌ Неверный формат телефона", **NO_PREVIEW)
        return
    update_chat_phone(target_chat_id, phone)
    bot.send_message(message.chat.id, f"✅ Телефон обновлён!\n<code>{phone}</code>", **NO_PREVIEW)
    time.sleep(0.3)
    show_chat_details(message.chat.id, None, target_chat_id)


def send_content_to_user(user_id, message):
    try:
        caption = message.caption or ""

        if message.video:
            bot.send_video(user_id, message.video.file_id, caption=caption)
        elif message.photo:
            bot.send_photo(user_id, message.photo[-1].file_id, caption=caption)
        elif message.document:
            bot.send_document(user_id, message.document.file_id, caption=caption)
        elif message.audio:
            bot.send_audio(user_id, message.audio.file_id, caption=caption)
        elif message.voice:
            bot.send_voice(user_id, message.voice.file_id, caption=caption)
        elif message.animation:
            bot.send_animation(user_id, message.animation.file_id, caption=caption)
        elif message.sticker:
            bot.send_sticker(user_id, message.sticker.file_id)
        elif message.video_note:
            bot.send_video_note(user_id, message.video_note.file_id)
        elif message.text:
            bot.send_message(user_id, message.text)
        else:
            return False, "Тип сообщения не поддерживается"

        return True, None
    except Exception as e:
        return False, str(e)

def process_broadcast_message(message):
    users = select_users()
    ok_count = 0
    fail_count = 0
    failed_users = []

    for user in users:
        user_id = user["user_id"]

        ok, error = copy_message_to_chat(user_id, message)

        if ok:
            ok_count += 1
        else:
            fail_count += 1
            failed_users.append(f"{user_id}: {error}")

        time.sleep(0.05)

    report = (
        "✅ Рассылка завершена.\n"
        f"Отправлено: {ok_count}\n"
        f"Ошибок: {fail_count}"
    )

    if failed_users:
        report += "\n\nПервые ошибки:\n" + "\n".join(failed_users[:10])

    bot.send_message(message.chat.id, report)

def process_send_to_one(message, target_user_id):
    ok, error = copy_message_to_chat(target_user_id, message)

    if ok:
        bot.send_message(
            message.chat.id,
            f"✅ Сообщение отправлено пользователю {target_user_id}"
        )
    else:
        bot.send_message(
            message.chat.id,
            f"❌ Не удалось отправить пользователю {target_user_id}.\n"
            f"Причина: {error[:300]}"
        )

def process_send_to_chat(message, target_chat_id):
    ok, error = copy_message_to_chat(target_chat_id, message)

    if ok:
        bot.send_message(
            message.chat.id,
            f"✅ Сообщение отправлено в чат {target_chat_id}"
        )
    else:
        bot.send_message(
            message.chat.id,
            f"❌ Не удалось отправить в чат {target_chat_id}.\n"
            f"Причина: {error[:300]}"
        )

def process_send_to_user_content(message, target_user_id):
    ok, error = send_content_to_user(target_user_id, message)

    if ok:
        bot.send_message(
            message.chat.id,
            f"✅ Сообщение отправлено пользователю {target_user_id}"
        )
    else:
        bot.send_message(
            message.chat.id,
            f"❌ Не удалось отправить пользователю {target_user_id}.\n"
            f"Причина: {error[:300]}"
        )

def process_send_to_user_id(message):
    track_user(message)

    text = (message.text or "").strip()

    try:
        target_user_id = int(text)
    except (TypeError, ValueError):
        bot.send_message(
            message.chat.id,
            "❌ ID пользователя должен быть числом.\n"
            "Отправь ID ещё раз или нажми «❌ Отмена».",
            reply_markup=get_cancel_keyboard()
        )
        bot.register_next_step_handler(
            message,
            process_send_to_user_id
        )
        return

    prompt = bot.send_message(
        message.chat.id,
        f"Теперь отправь сообщение для пользователя {target_user_id}.",
        reply_markup=get_cancel_keyboard()
    )

    bot.register_next_step_handler(
        prompt,
        process_send_to_user_content,
        target_user_id
    )

def process_send_to_chat_id(message):
    track_user(message)

    text = (message.text or "").strip()

    try:
        target_chat_id = int(text)
    except (TypeError, ValueError):
        bot.send_message(
            message.chat.id,
            "❌ ID чата должен быть числом.\n"
            "Отправь ID ещё раз или нажми «❌ Отмена».",
            reply_markup=get_cancel_keyboard()
        )
        bot.register_next_step_handler(
            message,
            process_send_to_chat_id
        )
        return

    prompt = bot.send_message(
        message.chat.id,
        f"Теперь отправь сообщение для чата {target_chat_id}.",
        reply_markup=get_cancel_keyboard()
    )

    bot.register_next_step_handler(
        prompt,
        process_send_to_chat_content,
        target_chat_id
    )

def process_send_to_chat_content(message, target_chat_id):
    ok, error = send_content_to_user(target_chat_id, message)

    if ok:
        bot.send_message(
            message.chat.id,
            f"✅ Сообщение отправлено в чат {target_chat_id}"
        )
    else:
        bot.send_message(
            message.chat.id,
            f"❌ Не удалось отправить в чат {target_chat_id}.\n"
            f"Причина: {error[:300]}"
        )

def copy_message_to_chat(target_chat_id, source_message):
    try:
        bot.copy_message(
            chat_id=target_chat_id,
            from_chat_id=source_message.chat.id,
            message_id=source_message.message_id
        )
        return True, None
    except Exception as e:
        return False, str(e)


def start_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(types.InlineKeyboardButton("✔ Подключить MAX_Parser", callback_data="parse_setup"))
    keyboard.add(types.InlineKeyboardButton("🔎 О парсинге", callback_data="about_bot"))
    keyboard.add(types.InlineKeyboardButton("💬 Тех поддержка", callback_data="support_start"))
    return keyboard

def return_start():
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(types.InlineKeyboardButton("✔ Подключить MAX_Parser", callback_data="parse_setup"))
    keyboard.add(types.InlineKeyboardButton("🔙 Назад", callback_data="start_comeback"))
    return keyboard

def parser_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(types.InlineKeyboardButton("1️⃣ Мой аккаунт MAX — 250 ₽/мес", callback_data="my_acc_250"))
    keyboard.add(types.InlineKeyboardButton("2️⃣ Через готовый аккаунт — 500 ₽/мес", callback_data="not_my_acc_250"))
    keyboard.add(types.InlineKeyboardButton("📖 Инструкция", callback_data="instruction"))
    keyboard.add(types.InlineKeyboardButton("🏠 В главное меню", callback_data="start_home"))
    return keyboard


def _support_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    keyboard.add(types.KeyboardButton(SUPPORT_END_TEXT))
    return keyboard


def _support_admin_keyboard(user_id):
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton("❌ Завершить чат", callback_data=f"support_close_{user_id}"))
    return keyboard


def _support_user_label(user):
    full_name = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
    full_name = escape_html(full_name or "Имя не указано")
    username = f"@{escape_html(user.username)}" if user.username else "не указан"
    return full_name, username


def _map_support_message(message_id, user_id):
    with support_state_lock:
        support_message_routes[message_id] = user_id
        save_support_message_route(message_id, user_id)


def _notify_support_admin(user, body_message=None):
    full_name, username = _support_user_label(user)
    header = bot.send_message(
        ADMIN_ID,
        "🆘 <b>Обращение в тех поддержку</b>\n"
        f"👤 {full_name}\n"
        f"🔎 Никнейм: {username}",
        parse_mode="HTML",
        reply_markup=_support_admin_keyboard(user.id),
        **NO_PREVIEW,
    )
    _map_support_message(header.message_id, user.id)
    if body_message is None:
        return
    try:
        copied = bot.copy_message(
            ADMIN_ID,
            body_message.chat.id,
            body_message.message_id,
        )
        _map_support_message(copied.message_id, user.id)
    except Exception as error:
        fallback = body_message.text or body_message.caption or "[Вложение не удалось скопировать]"
        copied = bot.send_message(
            ADMIN_ID,
            f"⚠️ <b>Сообщение не удалось скопировать</b>\n{escape_html(fallback[:3000])}\n\n"
            f"Причина: <code>{escape_html(str(error)[:500])}</code>",
            parse_mode="HTML",
            **NO_PREVIEW,
        )
        _map_support_message(copied.message_id, user.id)


def start_support(chat_id, user):
    if chat_id != user.id:
        bot.send_message(
            chat_id,
            "Чтобы написать в тех поддержку, откройте со мной личный чат и отправьте /help.",
            **NO_PREVIEW,
        )
        return
    track_user(type("SupportUserEvent", (), {"from_user": user})())
    with support_state_lock:
        if user.id in support_sessions:
            bot.send_message(
                chat_id,
                "Чат с поддержкой уже открыт. Напишите сообщение или нажмите кнопку завершения.",
                reply_markup=_support_keyboard(),
                **NO_PREVIEW,
            )
            return
        support_sessions.add(user.id)
        open_support_session(user.id)
    try:
        bot.send_message(
            chat_id,
            "💬 <b>Чат с тех поддержкой открыт</b>\n\n"
            "Напишите, с чем нужна помощь. Ваши сообщения и никнейм увидит поддержка. "
            "Ответы придут сюда от имени бота, личный аккаунт специалиста вам не показывается.\n\n"
            "Если вы проходите вход в MAX, сообщение с паролем двухэтапной проверки обрабатывается отдельно и поддержке не пересылается.\n\n"
            "Когда вопрос решён, нажмите кнопку ниже.",
            parse_mode="HTML",
            reply_markup=_support_keyboard(),
            **NO_PREVIEW,
        )
        _notify_support_admin(user)
    except Exception as error:
        with support_state_lock:
            support_sessions.discard(user.id)
            close_support_session(user.id)
        bot.send_message(
            chat_id,
            f"Не удалось открыть обращение. Попробуйте позже. ({escape_html(str(error)[:200])})",
            reply_markup=types.ReplyKeyboardRemove(),
            **NO_PREVIEW,
        )


def _close_support(user_id, notify_user=True):
    with support_state_lock:
        was_open = user_id in support_sessions
        support_sessions.discard(user_id)
        for message_id, routed_user_id in list(support_message_routes.items()):
            if routed_user_id == user_id:
                support_message_routes.pop(message_id, None)
        close_support_session(user_id)
    if was_open and notify_user:
        bot.send_message(
            user_id,
            "Чат с тех поддержкой завершён. Если понадобится помощь, снова отправьте /help.",
            reply_markup=types.ReplyKeyboardRemove(),
            **NO_PREVIEW,
        )
    return was_open
#!==============================================================================
#! 3) ОСТАЛЬНОЙ КОД (ОБРАБОТЧИКИ СООБЩЕНИЙ)
#!==============================================================================
@bot.message_handler(content_types=['new_chat_members'])
def on_bot_added_to_chat(message):
    for member in message.new_chat_members:
        if member.id == bot.get_me().id:
            chat_id = message.chat.id
            title = message.chat.title or f"Чат {chat_id}"
            owner_id = message.from_user.id if message.from_user else None
            setup_mode = pending_setup_modes.get(owner_id) if owner_id else None
            if not setup_mode:
                add_chat(chat_id, title)
            try:
                member_status = bot.get_chat_member(chat_id, bot.get_me().id).status
                if member_status not in ['administrator', 'creator']:
                    bot.send_message(
                        chat_id,
                        "👋 <b>Привет!</b>\n\n"
                        "Я — <b>MAX Parser Bot</b>. Меня добавили в этот чат.\n\n"
                        "Эту группу можно выбрать как место получения сообщений MAX.\n"
                        "В личном чате со мной отправьте /connect, выберите способ подключения, "
                        "а затем напишите здесь /bind.\n\n"
                        "Проверьте, что боту разрешено отправлять сообщения в эту группу.",
                        parse_mode='HTML', **NO_PREVIEW
                    )
                else:
                    bot.send_message(
                        chat_id,
                        "👋 <b>Привет!</b>\n\n"
                        "Я — <b>MAX Parser Bot</b>. Меня добавили в этот чат.\n\n"
                        "Группу можно выбрать как место получения сообщений MAX.\n"
                        "В личном чате со мной отправьте /connect, выберите способ подключения, "
                        "а затем напишите здесь /bind.",
                        parse_mode='HTML', **NO_PREVIEW
                    )
            except Exception as e:
                print(f"⚠️ Не удалось проверить права: {e}")
                bot.send_message(
                    chat_id,
                    "👋 <b>Привет!</b>\n\n"
                    "Я — <b>MAX Parser Bot</b>. Меня добавили в этот чат.\n\n"
                    "Чтобы выбрать эту группу для получения сообщений, отправьте /connect в личном чате со мной, "
                    "затем напишите здесь /bind.",
                    parse_mode='HTML', **NO_PREVIEW
                )
            if setup_mode and owner_id:
                _begin_customer_group_setup(chat_id, owner_id, setup_mode)
            return

@bot.message_handler(commands=['start1'])
def start_bot(message):
    track_user(message)
    text = (
        "👋 <b>Привет!</b>\n\n"
        "Меня зовут <b>MAX_Parser</b>.\n\n"
        "Я помогаю автоматически пересылать сообщения из выбранной группы "
        "мессенджера MAX в Telegram.\n\n"
        "📌 <b>Для чего я нужен</b>\n"
        "• Получать важные объявления из школьных, рабочих или дружеских чатов MAX.\n"
        "• Не пропустить сообщения, если ты больше предпочитаешь Telegram.\n"
        "• Собирать сообщения из одной группы в удобном для тебя Telegram-чате.\n\n"
        "⚡ <b>Почему это удобно</b>\n"
        "• Не нужно каждый раз открывать MAX и искать нужную группу.\n"
        "• Не нужно постоянно включать и выключать VPN.\n"
        "• Сообщения приходят автоматически, а ты просто читаешь их в Telegram.\n\n"

    )

    bot.send_message(message.chat.id, text, **NO_PREVIEW, reply_markup=start_keyboard())


@bot.message_handler(commands=['help1'])
def help_command(message):
    start_support(message.chat.id, message.from_user)


@bot.message_handler(commands=['connect1'])
def connect_command(message):
    track_user(message)
    if message.chat.type != "private":
        bot.reply_to(message, "Откройте личный чат со мной и отправьте /connect.")
        return
    bot.send_message(
        message.chat.id,
        "Выберите способ подключения.",
        reply_markup=parser_keyboard(),
        **NO_PREVIEW,
    )


@bot.message_handler(commands=['mychats1'])
def my_chats_command(message):
    track_user(message)
    if message.chat.type != "private":
        bot.reply_to(message, "Список подключений показывается только в личном чате. Отправьте мне /mychats напрямую.")
        return
    chats = get_user_chats(message.from_user.id)
    if not chats:
        bot.send_message(
            message.chat.id,
            "У вас пока нет подключённых Telegram-групп. Начните настройку командой /connect.",
            **NO_PREVIEW,
        )
        return
    lines = ["📬 <b>Ваши группы MAX_Parser</b>"]
    for chat in chats:
        state = "🟢 Парсинг включён" if chat.get("is_active") else "🟡 Ожидает настройки"
        mode = "ваш MAX-аккаунт" if chat.get("account_mode") != "project" else "аккаунт проекта"
        lines.append(
            f"\n<b>{escape_html(chat.get('title') or 'Группа')}</b>\n"
            f"{state} · {mode}"
        )
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML", **NO_PREVIEW)


def _begin_customer_group_setup(chat_id, owner_id, account_mode):
    title = None
    try:
        title = bot.get_chat(chat_id).title or f"Группа {chat_id}"
    except Exception:
        title = f"Группа {chat_id}"
    if not claim_chat(chat_id, title, owner_id, account_mode):
        pending_setup_modes.pop(owner_id, None)
        bot.send_message(
            owner_id,
            "Эта Telegram-группа уже подключена к другому аккаунту. Выберите свою группу и повторите /bind.",
            **NO_PREVIEW,
        )
        return False
    pending_setup_modes.pop(owner_id, None)
    customer_setup_state[owner_id] = {"chat_id": chat_id, "mode": account_mode}
    bot.send_message(
        owner_id,
        f"✅ Группа <b>{escape_html(title)}</b> закреплена за вами.\n\n"
        f"<b>Шаг 2 из {2 if account_mode == 'project' else 3}.</b> Пришлите сюда ссылку на группу MAX, из которой нужно получать сообщения.\n"
        "Как скопировать ссылку: откройте <a href=\"https://web.max.ru\">web.max.ru</a>, "
        "зайдите в нужную группу и скопируйте её ссылку из меню группы.\n\n"
        "Для отмены отправьте /cancel.",
        parse_mode="HTML",
        **NO_PREVIEW,
    )
    return True


@bot.message_handler(commands=['bind'])
def bind_customer_group(message):
    if message.chat.type == "private":
        bot.send_message(
            message.chat.id,
            "Отправьте /bind в той Telegram-группе, куда должны приходить сообщения.",
            **NO_PREVIEW,
        )
        return
    mode = pending_setup_modes.get(message.from_user.id)
    if not mode:
        bot.reply_to(message, "Сначала откройте личный чат со мной и нажмите /connect.")
        return
    _begin_customer_group_setup(message.chat.id, message.from_user.id, mode)
    bot.reply_to(message, "Настройку продолжим в личном чате с ботом.")


@bot.message_handler(
    func=lambda m: m.chat.type == "private"
    and m.from_user.id in customer_setup_state
    and not (m.text or "").startswith("/"),
    content_types=["text"],
)
def customer_source_url(message):
    state = customer_setup_state.get(message.from_user.id)
    if not state:
        return
    url = (message.text or "").strip()
    if not re.match(r"^https?://(?:web\.)?max\.ru/", url, re.I):
        bot.send_message(
            message.chat.id,
            "Не похоже на ссылку группы MAX. Пришлите полную ссылку вида <code>https://web.max.ru/…</code>.",
            parse_mode="HTML",
            **NO_PREVIEW,
        )
        return
    chat_id = state["chat_id"]
    mode = state["mode"]
    update_chat_url(chat_id, url)
    customer_setup_state.pop(message.from_user.id, None)
    if mode == "self":
        bot.send_message(
            message.chat.id,
            "<b>Шаг 3 из 3.</b> Сейчас открою страницу входа MAX и пришлю QR-код. "
            "Отсканируйте его в приложении MAX; авторизация будет сохранена только для вашей группы.",
            parse_mode="HTML",
            **NO_PREVIEW,
        )
        do_qr_login(chat_id, message.from_user.id)
        return

    project_session = os.path.join("sessions", "project_account.json")
    if os.path.exists(project_session):
        toggle_chat(chat_id, True)
        bot.send_message(
            message.chat.id,
            "✅ Ссылка сохранена. Группа добавлена в очередь парсинга. Проверьте, что аккаунт проекта MAX приглашён в исходную группу.",
            **NO_PREVIEW,
        )
    else:
        account_name = os.getenv("MAX_PROJECT_ACCOUNT_USERNAME", "").strip()
        account_line = f"Аккаунт проекта: <b>{escape_html(account_name)}</b>.\n" if account_name else ""
        bot.send_message(
            message.chat.id,
            "✅ Ссылка сохранена. Подключение включится, когда администратор настроит сессию аккаунта проекта.\n\n"
            f"{account_line}Если аккаунт ещё не добавлен в исходную MAX-группу, напишите в поддержку командой /help.",
            parse_mode="HTML",
            **NO_PREVIEW,
        )


@bot.message_handler(commands=['cancel'])
def cancel_customer_setup(message):
    state = customer_setup_state.pop(message.from_user.id, None)
    pending_setup_modes.pop(message.from_user.id, None)
    if state:
        bot.send_message(message.chat.id, "Настройка отменена. Можно начать заново командой /connect.", **NO_PREVIEW)
    elif _close_support(message.from_user.id):
        return
    else:
        bot.send_message(message.chat.id, "Нечего отменять.", **NO_PREVIEW)


@bot.message_handler(func=lambda m: m.chat.type == "private" and m.text == SUPPORT_END_TEXT)
def end_support_button(message):
    if _close_support(message.from_user.id):
        bot.send_message(message.chat.id, "Спасибо за обращение!", **NO_PREVIEW)
    else:
        bot.send_message(message.chat.id, "Чат с поддержкой сейчас не открыт.", reply_markup=types.ReplyKeyboardRemove(), **NO_PREVIEW)


@bot.message_handler(
    func=lambda m: m.chat.type == "private"
    and m.from_user.id == ADMIN_ID
    and m.reply_to_message is not None
    and m.reply_to_message.message_id in support_message_routes
    and (
        not m.text
        or not m.text.startswith("/")
        or m.text.strip().lower() == "/close"
    ),
    content_types=["text", "photo", "video", "document", "audio", "voice", "animation", "sticker", "video_note", "location", "contact", "venue", "poll"],
)
def support_admin_reply(message):
    user_id = support_message_routes.get(message.reply_to_message.message_id)
    if not user_id:
        return
    if message.content_type == "text" and message.text.strip().lower() == "/close":
        if _close_support(user_id):
            bot.reply_to(message, "Диалог завершён. Пользователь получил уведомление.")
        else:
            bot.reply_to(message, "Этот диалог уже закрыт.")
        return
    with support_state_lock:
        if user_id not in support_sessions:
            bot.reply_to(message, "Диалог уже закрыт. Откройте новое обращение, если пользователь напишет снова.")
            return
    try:
        if message.content_type == "text" and len(message.text) <= 3900:
            bot.send_message(
                user_id,
                "🆘 <b>Ответ от тех поддержки</b>\n\n" + escape_html(message.text),
                parse_mode="HTML",
                **NO_PREVIEW,
            )
            return
        bot.send_message(
            user_id,
            "🆘 <b>Ответ от тех поддержки</b>",
            parse_mode="HTML",
            **NO_PREVIEW,
        )
        bot.copy_message(user_id, message.chat.id, message.message_id)
    except Exception as error:
        bot.reply_to(message, f"Не удалось доставить ответ: {escape_html(str(error)[:300])}")


@bot.message_handler(
    func=lambda m: m.chat.type == "private"
    and m.from_user.id in support_sessions
    and m.from_user.id not in set(awaiting_password_from.values()),
    content_types=["text", "photo", "video", "document", "audio", "voice", "animation", "sticker", "video_note", "location", "contact", "venue", "poll"],
)
def support_user_message(message):
    if message.content_type == "text" and message.text.startswith("/"):
        return
    try:
        _notify_support_admin(message.from_user, message)
    except Exception as error:
        bot.send_message(message.chat.id, f"Не удалось доставить сообщение в поддержку: {escape_html(str(error)[:200])}", **NO_PREVIEW)

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


@bot.message_handler(commands=['project_login'])
def start_project_account_login(message):
    track_user(message)
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⛔️ Только для админов")
        return
    do_qr_login("project_account", message.chat.id)

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    track_user(message)
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⛔️ У вас нет прав администратора")
        return
    show_admin_menu(message.chat.id)

@bot.message_handler(
    func=lambda m: m.text is not None
    and not m.text.startswith('/')
    and m.from_user.id in set(awaiting_password_from.values())
)
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
    chat = get_chat_by_id(target_chat_id) if target_chat_id != "project_account" else None
    title = "аккаунта проекта" if target_chat_id == "project_account" else (chat["title"] if chat else "MAX")
    session = active_logins.get(target_chat_id)
    if not session:
        bot.send_message(message.chat.id, "❌ Сессия авторизации не найдена", **NO_PREVIEW)
        awaiting_password_from.pop(target_chat_id, None)
        return
    bot.send_message(message.chat.id, "⏳ Ввожу пароль...", **NO_PREVIEW)
    status, data = session.enter_password(password)
    awaiting_password_from.pop(target_chat_id, None)
    if status == "success":
        active_logins.pop(target_chat_id, None)
        if target_chat_id == "project_account":
            for customer_chat in get_all_chats():
                if customer_chat.get("account_mode") == "project" and customer_chat.get("max_url"):
                    toggle_chat(customer_chat["chat_id"], True)
        elif chat and chat.get("max_url"):
            toggle_chat(target_chat_id, True)
        bot.send_message(
            message.chat.id,
            f"🎉 <b>Успешно!</b>\nСессия для <b>{escape_html(title)}</b> сохранена.\nТеперь фоновый парсер сможет работать автоматически!",
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

#!==============================================================================
#! 4) ОБРАБОТЧИК ЛОГИКИ ВСЕХ КНОПОК
#!==============================================================================
@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_") or call.data.startswith("qr_") or call.data.startswith("support_close_") or call.data in ["delete_this_msg", "cancel_step"])
def admin_callbacks(call):
    track_user(call)
    action = call.data
    is_allowed_customer_qr = False
    if not is_admin(call.from_user.id) and action.startswith("qr_"):
        try:
            qr_target = _qr_target_from_action(action)
            chat = get_chat_by_id(qr_target) if qr_target != "project_account" else None
            is_allowed_customer_qr = bool(
                chat
                and int(chat.get("owner_id") or 0) == call.from_user.id
                and chat.get("account_mode") == "self"
            )
        except (ValueError, IndexError, TypeError):
            is_allowed_customer_qr = False
    if not is_admin(call.from_user.id) and not is_allowed_customer_qr:
        bot.answer_callback_query(call.id, "⛔️ Нет прав", show_alert=True)
        return
    if action.startswith("support_close_"):
        try:
            user_id = int(action.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Не удалось определить обращение", show_alert=True)
            return
        if support_message_routes.get(call.message.message_id) != user_id:
            bot.answer_callback_query(call.id, "Кнопка относится к уже завершённому обращению")
            return
        if _close_support(user_id):
            bot.answer_callback_query(call.id, "Диалог завершён")
            bot.send_message(call.message.chat.id, "✅ Чат с пользователем завершён.", **NO_PREVIEW)
        else:
            bot.answer_callback_query(call.id, "Этот диалог уже закрыт")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        return
    if action == "cancel_step":
        try:
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
        target_id = _qr_target_from_action(action)
        bot.answer_callback_query(call.id, "⏳ Проверяю вход...")
        session = active_logins.get(target_id)
        if not session:
            bot.send_message(call.message.chat.id, "❌ Сессия авторизации не найдена", **NO_PREVIEW)
            return
        chat = get_chat_by_id(target_id) if target_id != "project_account" else None
        title = "аккаунта проекта" if target_id == "project_account" else chat["title"]
        status, data = session.check()
        if status == "success":
            active_logins.pop(target_id, None)
            awaiting_password_from.pop(target_id, None)
            if target_id == "project_account":
                for customer_chat in get_all_chats():
                    if customer_chat.get("account_mode") == "project" and customer_chat.get("max_url"):
                        toggle_chat(customer_chat["chat_id"], True)
            elif chat and chat.get("max_url"):
                toggle_chat(target_id, True)
            bot.send_message(
                call.message.chat.id,
                f"🎉 <b>Успешно!</b>\nСессия для <b>{escape_html(title)}</b> сохранена.",
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
        target_id = _qr_target_from_action(action)
        bot.answer_callback_query(call.id, "🔄 Обновляю QR-код...")
        session = active_logins.get(target_id)
        if not session:
            bot.send_message(call.message.chat.id, "❌ Сессия авторизации не найдена", **NO_PREVIEW)
            return
        chat = get_chat_by_id(target_id) if target_id != "project_account" else None
        title = "аккаунта проекта" if target_id == "project_account" else chat["title"]
        status, data = session.refresh()
        if status == "success":
            active_logins.pop(target_id, None)
            if target_id == "project_account":
                for customer_chat in get_all_chats():
                    if customer_chat.get("account_mode") == "project" and customer_chat.get("max_url"):
                        toggle_chat(customer_chat["chat_id"], True)
            elif chat and chat.get("max_url"):
                toggle_chat(target_id, True)
            bot.send_message(
                call.message.chat.id,
                f"🎉 <b>Успешно!</b>\nСессия для <b>{escape_html(title)}</b> обновлена!",
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
        target_id = _qr_target_from_action(action)
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
    elif action == "admin_project_login":
        bot.answer_callback_query(call.id, "⏳ Открываю вход в MAX...")
        do_qr_login("project_account", call.message.chat.id)
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
    elif action == "admin_broadcast":
        track_user(call.message)

        prompt = bot.send_message(
            call.message.chat.id,
            "💬 Введите текст рассылки:"
        )

        bot.register_next_step_handler(prompt, process_broadcast_message)
    elif action == "admin_send_to_chat":
        prompt = bot.send_message(
            call.message.chat.id,
            "Отправь ID чата, в который нужно отправить сообщение.\n\n"
            "Пример для группы: <code>-1001234567890</code>",
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard()
        )

        bot.register_next_step_handler(
            prompt,
            process_send_to_chat_id
        )
    elif action == "admin_send_to_user":
        prompt = bot.send_message(
            call.message.chat.id,
            "Отправь ID пользователя, которому нужно отправить сообщение.\n\n"
            "Пример: <code>123456789</code>",
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard()
        )

        bot.register_next_step_handler(
            prompt,
            process_send_to_user_id
        )
    elif action.startswith("admin_chat_"):
        target_id = int(action.split("_")[-1])
        show_chat_details(call.message.chat.id, call.message.message_id, target_id)
    elif action.startswith("admin_toggle_chat_"):
        target_id = int(action.split("_")[-1])
        chat = get_chat_by_id(target_id)
        if chat:
            going_active = not bool(chat['is_active'])
            if going_active:
                session_exists = os.path.exists(_session_path_for_chat(chat))
                if not chat.get('max_url') or (not session_exists and not chat.get('phone')):
                    bot.answer_callback_query(
                        call.id,
                        "⚠️ Сначала задайте URL и настройте MAX-аккаунт!",
                        show_alert=True,
                    )
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

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.message:
        if call.data == "about_bot":
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text='''
<b>MAX_Parser — удобный мост между MAX и Telegram</b>\n
<i>Он помогает автоматически пересылать новые сообщения из выбранной группы MAX в Telegram. Ты не пропустишь важные объявления, обсуждения и файлы, даже если чаще пользуешься Telegram.</i>\n
<blockquote><b>Что умеет бот:</b>\n
• Пересылать новые сообщения из подключённой группы MAX.
• Отправлять текст, фото, видео, документы и другие поддерживаемые вложения.
• Работать автоматически: не нужно постоянно открывать MAX и проверять группу вручную.
• Помогать не терять важную информацию из школьных, рабочих или дружеских чатов.
<b>Почему это удобно:</b>\n
• Все важные сообщения приходят в Telegram.
• Не нужно каждый раз переключаться между приложениями.
• Не нужно постоянно включать и выключать VPN.
• Ты сам выбираешь группу и настраиваешь, откуда получать сообщения.</blockquote>\n
<blockquote><b>⚠️ Важно знать:</b>\n
Бот использует <b>автоматизированный доступ к MAX</b>, поэтому в редких случаях аккаунт может быть <b>временно ограничен на несколько дней</b>. Это редкая ситуация, но такие ограничения возможны со стороны MAX.\n
Если вы боитесь, что можете временно потерять доступ к аккаунту в MAX, то у MAX_Parser <b>есть отличное решение</b>:\n
MAX_Parser будет работать через <b>другой аккаунт</b>, который сделан специально для парсинга. Тебе не нужно авторизоваться в MAX и предоставлять доступ к своему аккаунту.\n
Достаточно будет просто пригласить этот аккаунт в группу MAX, из которой нужно пересылать сообщения. Бот будет автоматически получать новые сообщения и пересылать их в Telegram.\n
🔒 Тем самым, ваш аккаунт в MAX останется в <b>абсолютной безопасности</b>.</blockquote>

''', reply_markup=return_start(), parse_mode='HTML', **NO_PREVIEW)
        elif call.data == "start_comeback":
            text = (
                "👋 <b>Привет!</b>\n\n"
                "Меня зовут <b>MAX_Parser</b>.\n\n"
                "Я помогаю автоматически пересылать сообщения из выбранной группы "
                "мессенджера MAX в Telegram.\n\n"
                "📌 <b>Для чего я нужен</b>\n"
                "• Получать важные объявления из школьных, рабочих или дружеских чатов MAX.\n"
                "• Не пропустить сообщения, если ты больше предпочитаешь Telegram.\n"
                "• Собирать сообщения из одной группы в удобном для тебя Telegram-чате.\n\n"
                "⚡ <b>Почему это удобно</b>\n"
                "• Не нужно каждый раз открывать MAX и искать нужную группу.\n"
                "• Не нужно постоянно включать и выключать VPN.\n"
                "• Сообщения приходят автоматически, а ты просто читаешь их в Telegram.\n\n"
                "🔒 <b>Важно</b>\n"
                "Бот работает только с теми группами, доступ к которым тебе разрешён."
            )
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=text, reply_markup=start_keyboard(), parse_mode='HTML', **NO_PREVIEW)
        elif call.data == "parse_setup":
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=(
                    "<b>Подключение MAX → Telegram</b>\n\n"
                    "Выберите, какой аккаунт в мессенджере MAX будет пересылать сообщения.\n\n"
                    "<blockquote><b>1️⃣ Ваш аккаунт MAX</b>\n"
                    "Вы сканируете QR-код для своей группы. Сессия и сообщения привязаны к вашей Telegram-группе.</blockquote>\n"
                    "<blockquote><b>2️⃣ Аккаунт проекта</b>\n"
                    "В исходную группу добавляется отдельный аккаунт MAX проекта. Его имя можно получить у поддержки.</blockquote>\n\n"
                    "<i>Пошаговый порядок подключения — в инструкции ниже.</i>"
                ),
                reply_markup=parser_keyboard(),
                parse_mode="HTML",
                **NO_PREVIEW,
            )
        elif call.data in ("my_acc_250", "not_my_acc_250"):
            if call.message.chat.type != "private":
                bot.answer_callback_query(
                    call.id,
                    "Откройте личный чат с ботом, чтобы настроить подключение.",
                    show_alert=True,
                )
                return
            mode = "self" if call.data == "my_acc_250" else "project"
            pending_setup_modes[call.from_user.id] = mode
            track_user(call)
            label = "настройку своего аккаунта MAX" if mode == "self" else "настройку через готовый аккаунт"
            bot.answer_callback_query(call.id, "Настройка запущена")
            bot.send_message(
                call.from_user.id,
                f"<b>Шаг 1 из {2 if mode == 'project' else 3} · Telegram-группа</b>\n\n"
                f"Вы выбрали: <b>{label}</b>. Добавьте меня в Telegram-группу, в которую я буду присылать сообщения. "
                "Когда я появлюсь в группе, настройка продолжится автоматически. Если я уже состою в ней, то "
                "напишите в группе команду /bind.\n\n"
                "Убедитесь, что мне разрешено отправлять сообщения. Для отмены отправьте /cancel.",
                parse_mode="HTML",
                **NO_PREVIEW,
            )
            try:
                bot.edit_message_text(
                    "Настройка начата. Продолжите её в вашей Telegram-группе; подробная инструкция доступна по кнопке ниже.",
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=parser_keyboard(),
                    **NO_PREVIEW,
                )
            except Exception:
                pass
        elif call.data == "support_start":
            bot.answer_callback_query(call.id)
            start_support(call.message.chat.id, call.from_user)
        elif call.data == "start_home":
            text = (
                "👋 <b>Привет!</b>\n\n"
                "Меня зовут <b>MAX_Parser</b>.\n\n"
                "Я помогаю автоматически пересылать сообщения из выбранной группы "
                "мессенджера MAX в Telegram.\n\n"
                "📌 <b>Для чего я нужен</b>\n"
                "• Получать важные объявления из школьных, рабочих или дружеских чатов MAX.\n"
                "• Не пропустить сообщения, если ты больше предпочитаешь Telegram.\n"
                "• Собирать сообщения из одной группы в удобном для тебя Telegram-чате.\n\n"
                "⚡ <b>Почему это удобно</b>\n"
                "• Не нужно каждый раз открывать MAX и искать нужную группу.\n"
                "• Не нужно постоянно включать и выключать VPN.\n"
                "• Сообщения приходят автоматически, а ты просто читаешь их в Telegram.\n\n"
                "🔒 <b>Важно</b>\n"
                "Бот работает только с теми группами, доступ к которым тебе разрешён."
            )
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=text, reply_markup=start_keyboard(), parse_mode='HTML', **NO_PREVIEW)
        elif call.data == "instruction":
            instruction_kb = types.InlineKeyboardMarkup(row_width=1)
            instruction_kb.add(types.InlineKeyboardButton("💬 Задать вопрос к поддержке", callback_data="support_start"))
            instruction_kb.add(types.InlineKeyboardButton("🔙 К выбору подключения", callback_data="parse_setup"))
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=(
                    "<b>📖 Подробная инструкция по подключению</b>\n\n"
                    "<b>Что понадобится</b>\n"
                    "• Эта переписка с ботом.\n"
                    "• Ваша Telegram-группа для получения сообщений.\n"
                    "• Ссылка на группу MAX.\n\n"
                    "<b>Шаг 1 · Выберите Telegram-группу, в которую будут приходить сообщения из группы MAX</b>\n"
                    "После нажатия на кнопку «Подключить MAX_Parser» выберите способ работы с MAX. Затем добавьте этого бота "
                    "в Telegram-группу, куда должны приходить сообщения. После появления бота отправьте в группе /bind. "
                    "Если бот уже добавлен, то просто напишите /bind. Боту нужно разрешить отправлять сообщения.\n\n"
                    "<b>Шаг 2 · Укажите группу MAX</b>\n"
                    "Откройте <a href=\"https://web.max.ru\">web.max.ru</a>, войдите в MAX, перейдите в нужную группу "
                    "и скопируйте ссылку группы из её меню. Бот попросит прислать эту ссылку в личный чат. "
                    "Проверьте, что ссылка ведёт именно в нужную группу.\n\n"
                    "<b>Вариант 1 · Ваш аккаунт MAX</b>\n"
                    "После отправки ссылки бот покажет QR-код. Отсканируйте его камерой приложения MAX и подтвердите вход кнопкой "
                    "«Готово, я отсканировал». Если MAX запросит пароль двухэтапной проверки, отправьте его боту в этой личной переписке; "
                    "он нужен только для входа в MAX. После успешного входа парсер включится автоматически 🎉.\n\n"
                    "<b>Вариант 2 · Через готовый аккаунт MAX</b>\n"
                    "После того как вы добавили MAX_Pаrser в группу напишите в вашей группе в Telegram команду /bind и после, "
                    "пришлите БОТУ ссылку, которая ведет на вашу группу. Настройка завершена 🎉.\n\n"
                    "<b>Управление и помощь</b>\n"
                    "• /mychats — посмотреть свои подключения.\n"
                    "• /cancel — отменить текущий шаг настройки.\n"
                    "• /help — открыть анонимный чат с поддержкой.\n\n"
                ),
                reply_markup=instruction_kb,
                parse_mode="HTML",
                **NO_PREVIEW,
            )

if __name__ == "__main__":
    try:
        threading.Thread(target=background_parser, daemon=True).start()
        print("✅ Запуск polling...")
        bot.infinity_polling(none_stop=True, timeout=60)
    except KeyboardInterrupt:
        print(" Остановлен пользователем")
        save_message_cache()
