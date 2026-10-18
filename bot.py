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
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from telebot import types
from dotenv import load_dotenv
import concurrent.futures
from max_playwright_parser import *
from DB_service import *
from debug_log import logger as debug_logger, preview as log_preview
from remote_auth_service import RemoteAuthService, SESSION_TTL_SECONDS

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
remote_auth_service = RemoteAuthService()
print("🚀 MAX Parser Bot запущен")
bot = telebot.TeleBot(os.getenv("BOT_TOKEN"), parse_mode='HTML')
ADMIN_ID = int(os.getenv("ADMIN_ID"))
add_admin(ADMIN_ID, "main_admin")
TELEGRAM_PAYMENT_PROVIDER_TOKEN = os.getenv("TELEGRAM_PAYMENT_PROVIDER_TOKEN", "").strip()
YOOKASSA_RECEIPTS_SETTING = os.getenv("YOOKASSA_RECEIPTS_ENABLED", "").strip().lower()
YOOKASSA_RECEIPTS_SETTING_CONFIRMED = YOOKASSA_RECEIPTS_SETTING in {
    "1", "true", "yes", "on", "0", "false", "no", "off"
}
YOOKASSA_RECEIPTS_ENABLED = YOOKASSA_RECEIPTS_SETTING in {"1", "true", "yes", "on"}
YOOKASSA_RECEIPT_VAT_CODE = os.getenv("YOOKASSA_RECEIPT_VAT_CODE", "").strip()
YOOKASSA_RECEIPT_PAYMENT_MODE = os.getenv("YOOKASSA_RECEIPT_PAYMENT_MODE", "").strip()
YOOKASSA_RECEIPT_PAYMENT_SUBJECT = os.getenv("YOOKASSA_RECEIPT_PAYMENT_SUBJECT", "").strip()
YOOKASSA_RECEIPT_TAX_SYSTEM_CODE = os.getenv("YOOKASSA_RECEIPT_TAX_SYSTEM_CODE", "").strip()
PAYMENTS_ENABLED = bool(TELEGRAM_PAYMENT_PROVIDER_TOKEN)
PAYMENT_ACCESS_ENFORCEMENT_ENABLED = os.getenv("PAYMENT_ACCESS_ENFORCEMENT_ENABLED").strip().lower() in {"1", "true", "yes", "on"}
PAYMENT_ADMIN_BYPASS = os.getenv("PAYMENT_ADMIN_BYPASS", "true").strip().lower() in {"1", "true", "yes", "on"}
_current_cycle_hashes = {}
NO_PREVIEW = {"disable_web_page_preview": True}
active_logins = {}
awaiting_password_from = {}
awaiting_phone_from = {}
awaiting_code_from = {}
support_sessions = set(get_open_support_sessions())


def _video_upload_details(media: dict, post: dict) -> dict:
    """Build Telegram video duration and thumbnail metadata from the actual file."""
    data = media.get("bytes")
    if not isinstance(data, (bytes, bytearray)) or not data:
        return {}
    result = {}
    try:
        duration = float(media.get("duration") or post.get("media_duration_seconds") or 0)
        if duration > 0:
            result["duration"] = max(1, int(round(duration)))
    except (TypeError, ValueError):
        pass

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            ffmpeg = None
    if not ffmpeg:
        return result

    try:
        probe = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-i", "pipe:0", "-frames:v", "1",
                "-vf", "scale=320:320:force_original_aspect_ratio=decrease,setsar=1",
                "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            ],
            input=bytes(data), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=18, check=False,
        )
        details = probe.stderr.decode("utf-8", "replace")
        duration_match = re.search(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", details)
        if duration_match and "duration" not in result:
            seconds = (
                int(duration_match.group(1)) * 3600
                + int(duration_match.group(2)) * 60
                + float(duration_match.group(3))
            )
            if seconds > 0:
                result["duration"] = max(1, int(round(seconds)))
        dimensions = re.search(r"Video:.*?\b(\d{2,5})x(\d{2,5})(?:\s|,|\[|$)", details)
        if dimensions:
            result["width"] = int(dimensions.group(1))
            result["height"] = int(dimensions.group(2))
        thumbnail = probe.stdout
        if thumbnail.startswith(b"\xff\xd8\xff") and len(thumbnail) < 200_000:
            result["thumbnail"] = thumbnail
    except Exception as exc:
        debug_logger.warning(
            "VIDEO_METADATA_PROBE_FAILED source_id=%s error=%s",
            post.get("source_id") or post.get("idx") or "?", log_preview(exc, 180),
        )
    return result
support_message_routes = get_support_message_routes()
support_state_lock = threading.RLock()
PARSE_DIAGNOSTICS = {}
PARSE_DIAGNOSTICS_LOCK = threading.RLock()
CHAT_LIFECYCLE_LOCK = threading.RLock()
pending_setup_modes = {}
customer_setup_state = {}
awaiting_review_text = set()
remote_auth_monitors = set()
remote_auth_monitor_lock = threading.Lock()
SUPPORT_END_TEXT = "✅ Завершить чат с поддержкой"
MAX_PROJECT_ACCOUNT_PHONE = "+7 939 752 85 95"

bot.set_my_commands([
    types.BotCommand("start", "Запустить бота"),
    types.BotCommand("help", "Связаться с тех поддержкой"),
    types.BotCommand("connect", "Подключить чат MAX"),
    types.BotCommand("mychats", "Мои группы парсинга"),
    types.BotCommand("subscription", "Подписка и продление"),
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
                debug_logger.warning(
                    "PHOTO_ALBUM_SEND_FAILED chat_id=%s source_id=%s photos=%s error=%s",
                    chat_id, post.get("source_id") or post.get("idx") or "?",
                    len(chunk), log_preview(album_error, 240),
                )
                print(f"⚠️ [{chat_id}] Не удалось отправить фотоальбом с подписью: {album_error}")
                # Если Telegram отклонил именно подпись/разметку, сохраняем фотографии
                # одним альбомом без подписи, а текст отправляем отдельным сообщением.
                if caption_in_album:
                    try:
                        retry_album = [
                            types.InputMediaPhoto(_media_to_bytesio(media))
                            for media in chunk
                        ]
                        _telegram_call(bot.send_media_group, chat_id, retry_album)
                        album_sent_ids.update(id(media) for media in chunk)
                        if caption_pending:
                            try:
                                _telegram_call(bot.send_message, chat_id, msg_text, **NO_PREVIEW)
                            except Exception as text_error:
                                debug_logger.warning(
                                    "PHOTO_ALBUM_CAPTION_FALLBACK_FAILED chat_id=%s source_id=%s error=%s",
                                    chat_id, post.get("source_id") or post.get("idx") or "?",
                                    log_preview(text_error, 240),
                                )
                            caption_pending = False
                        continue
                    except Exception as retry_error:
                        debug_logger.warning(
                            "PHOTO_ALBUM_RETRY_FAILED chat_id=%s source_id=%s photos=%s error=%s",
                            chat_id, post.get("source_id") or post.get("idx") or "?",
                            len(chunk), log_preview(retry_error, 240),
                        )
                        print(f"⚠️ [{chat_id}] Не удалось повторно отправить фотоальбом: {retry_error}")
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
                    _telegram_call(bot.send_photo,chat_id,obj,caption=msg_text,parse_mode="HTML")
                    caption_pending=False
                else:
                    _telegram_call(bot.send_photo,chat_id,obj)
            elif media_type=="video_note":
                _telegram_call(bot.send_video_note,chat_id,obj)
                caption_pending=False
            elif media_type=="video":
                video_details = _video_upload_details(media, post)
                thumbnail = video_details.pop("thumbnail", None)
                if thumbnail:
                    thumbnail_file = io.BytesIO(thumbnail)
                    thumbnail_file.name = "video-thumbnail.jpg"
                    video_details["thumbnail"] = thumbnail_file
                video_options = {"supports_streaming": True, **video_details}
                if caption_pending and len(msg_text)<=1024:
                    _telegram_call(bot.send_video,chat_id,obj,caption=msg_text,**video_options)
                    caption_pending=False
                else:
                    _telegram_call(bot.send_video,chat_id,obj,**video_options)
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

def _diagnostic_post(post, status, detail=""):
    media_counts = {}
    for media in post.get("media_files", []) or []:
        if isinstance(media, dict):
            media_type = str(media.get("type") or "unknown")
            media_counts[media_type] = media_counts.get(media_type, 0) + 1
    media_summary = ", ".join(
        f"{media_type} ×{count}" for media_type, count in sorted(media_counts.items())
    ) or "нет"
    preview = " ".join(str(post.get("text") or "").split()).strip()
    if not preview:
        preview = "[без текста]"
    return {
        "source_id": str(post.get("source_id") or post.get("idx") or "?")[:80],
        "status": status,
        "detail": str(detail or "")[:120],
        "text": preview[:180],
        "media": media_summary[:120],
        "date": str(post.get("date") or "")[:24],
        "time": str(post.get("time") or "")[:16],
    }


def _store_parse_diagnostic(chat_id, title, sent, skipped, failed, posts, error=None):
    entry = {
        "chat_id": int(chat_id),
        "title": str(title or "")[:100],
        "updated_at": datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S"),
        "sent": int(sent),
        "skipped": int(skipped),
        "failed": int(failed),
        "posts": list(posts or [])[-10:],
        "error": str(error or "")[:240],
    }
    with PARSE_DIAGNOSTICS_LOCK:
        PARSE_DIAGNOSTICS[int(chat_id)] = entry


def _record_parse_diagnostic_failure(chat_id, title, error):
    _store_parse_diagnostic(chat_id, title, 0, 0, 1, [], error=error)


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
    diagnostic_posts = []
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
            diagnostic_posts.append(_diagnostic_post(post, "skipped", "дубль в текущей выборке"))
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
                    diagnostic_posts.append(_diagnostic_post(post, "skipped", "итог опроса уже отправлен"))
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
                diagnostic_posts.append(_diagnostic_post(post, "skipped", "опрос уже отправлен"))
                debug_logger.info(
                    "MESSAGE_SKIPPED chat_id=%s source_id=%s reason=poll_initial_already_sent",
                    chat_id, source_id or "?",
                )
                continue

        if not force_poll_send and not is_new_message(post, chat_id=chat_id):
            skipped_count += 1
            diagnostic_posts.append(_diagnostic_post(post, "skipped", "уже есть в кэше отправленных"))
            debug_logger.info("MESSAGE_SKIPPED chat_id=%s source_id=%s reason=seen_cache", chat_id, source_id or "?")
            continue

        try:
            sent = _send_post(chat_id, post)
            if not sent:
                skipped_count += 1
                diagnostic_posts.append(_diagnostic_post(post, "skipped", "нет поддерживаемого содержимого"))
                debug_logger.warning(
                    "MESSAGE_NOT_SENT chat_id=%s source_id=%s reason=no_payload text=%s",
                    chat_id, source_id or "?", log_preview(post.get("text")) or "<empty>"
                )
                continue
            _commit_successful_post(chat_id, post)
            increment_message_count(chat_id)
            new_count += 1
            diagnostic_posts.append(_diagnostic_post(post, "sent"))
            time.sleep(1.5)
        except Exception as error:
            failed_count += 1
            diagnostic_posts.append(_diagnostic_post(post, "failed", str(error)))
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
    _store_parse_diagnostic(
        chat_id, title, new_count, skipped_count, failed_count, diagnostic_posts
    )
    return new_count, skipped_count


def _process_chat_posts_if_present(chat_id, title, posts):
    with CHAT_LIFECYCLE_LOCK:
        if not get_chat_by_id(chat_id):
            return None
        return _process_posts(chat_id, title, posts)


def _record_parse_failure_if_present(chat_id, title, error):
    with CHAT_LIFECYCLE_LOCK:
        if get_chat_by_id(chat_id):
            _record_parse_diagnostic_failure(chat_id, title, error)


def _session_path_for_chat(chat):
    if chat.get("account_mode") == "project":
        return os.path.join("sessions", "project_account.json")
    return os.path.join("sessions", f"{chat['chat_id']}.json")


def _user_needs_subscription(user_id):
    if not PAYMENT_ACCESS_ENFORCEMENT_ENABLED:
        return False
    if PAYMENT_ADMIN_BYPASS and is_admin(user_id):
        return False
    return not is_subscription_active(user_id)


def _get_payable_active_chats():
    chats = get_active_chats()
    if not PAYMENT_ACCESS_ENFORCEMENT_ENABLED:
        return chats
    active_owners = get_active_subscription_user_ids()
    admin_ids = (
        {int(admin["user_id"]) for admin in get_all_admins()}
        if PAYMENT_ADMIN_BYPASS else set()
    )
    return [
        chat for chat in chats
        if not chat.get("owner_id")
        or int(chat["owner_id"]) in (active_owners | admin_ids)
    ]


def run_one_parse_cycle():
    active_chats = _get_payable_active_chats()
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
            processed = _process_chat_posts_if_present(chat_id, title, posts)
            if processed is None:
                continue
            new_count, skipped_count = processed
            if new_count:
                results.append(
                    f"✅ <b>{title}</b>: +{new_count} новых, ⏭ {skipped_count} пропущено"
                )
            else:
                results.append(
                    f"ℹ️ <b>{title}</b>: новых сообщений нет (⏭ {skipped_count} пропущено)"
                )
        except Exception as error:
            _record_parse_failure_if_present(chat_id, title, error)
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
        owner_id = int(chat.get("owner_id") or 0)
        if owner_id and _user_needs_subscription(owner_id):
            return False
        posts = parse_max_group_media(
            group_url=max_url,
            chat_id=chat_id,
            session_path=_session_path_for_chat(chat),
        )
        _process_chat_posts_if_present(chat_id, title, posts)
        if chat.get("is_active") and owner_id > 0:
            try:
                _maybe_offer_review(owner_id)
            except Exception as notify_error:
                debug_logger.warning(
                    "REVIEW_INVITATION_SEND_FAILED user_id=%s error_type=%s",
                    owner_id, type(notify_error).__name__,
                )
        return True
    except Exception as error:
        _record_parse_failure_if_present(chat_id, title, error)
        print(f"❌ Ошибка парсинга чата {title}: {error}")
        traceback.print_exc()
        return False

def background_parser():
    time.sleep(5)
    while True:
        try:
            active_chats = _get_payable_active_chats()
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
    kb.add(types.InlineKeyboardButton("📱 Войти по номеру телефона", callback_data=f"qr_phone_{target_chat_id}"))
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


def _build_auth_check_keyboard(target_chat_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🔄 Проверить вход MAX", callback_data=f"qr_ready_{target_chat_id}"))
    kb.add(types.InlineKeyboardButton("❌ Отменить авторизацию", callback_data=f"qr_cancel_{target_chat_id}"))
    return kb


def _auth_human_check_text(session, reason):
    if reason == "captcha":
        return (
            "🛡 MAX запросил дополнительную проверку безопасности. Проверьте приложение MAX, "
            "завершите подтверждение входа и затем нажмите «🔄 Проверить вход MAX». "
            "Либо отмените попытку и начните вход по номеру через защищённую страницу."
        )
    return (
        "MAX пока не подтвердил вход. Отсканируйте QR-код в приложении MAX, затем нажмите "
        "«✅ Готово, я отсканировал», или выберите вход по номеру через защищённую страницу."
    )

def _clear_auth_waiters(target_chat_id):
    awaiting_password_from.pop(target_chat_id, None)
    awaiting_phone_from.pop(target_chat_id, None)
    awaiting_code_from.pop(target_chat_id, None)


def _set_auth_waiter(waiting_map, target_chat_id, user_id):
    _clear_auth_waiters(target_chat_id)
    waiting_map[target_chat_id] = int(user_id)


def _complete_login(target_chat_id, output_chat_id):
    active_logins.pop(target_chat_id, None)
    _clear_auth_waiters(target_chat_id)
    chat = get_chat_by_id(target_chat_id) if target_chat_id != "project_account" else None
    if target_chat_id == "project_account":
        title = "аккаунта проекта"
    else:
        title = chat["title"] if chat else "MAX"
        if chat and chat.get("max_url"):
            toggle_chat(target_chat_id, True)
    bot.send_message(
        output_chat_id,
        f"🎉 <b>Успешно!</b>\nСессия для <b>{escape_html(title)}</b> сохранена.",
        parse_mode="HTML",
        **NO_PREVIEW,
    )
    if target_chat_id == "project_account":
        bot.send_message(
            output_chat_id,
            "Теперь проверю доступ аккаунта проекта к каждой исходной группе. "
            "Парсинг включится для группы только после успешного открытия в MAX.",
            **NO_PREVIEW,
        )
        threading.Thread(
            target=_verify_project_chats_after_login,
            args=(output_chat_id,),
            daemon=True,
        ).start()
    elif chat and chat.get("owner_id"):
        owner_id = int(chat["owner_id"])
        if not is_admin(owner_id):
            try:
                _maybe_offer_review(owner_id)
            except Exception as error:
                debug_logger.warning(
                    "REVIEW_INVITATION_SEND_FAILED user_id=%s error_type=%s",
                    owner_id, type(error).__name__,
                )


def _monitor_remote_login(target_chat_id, session, output_chat_id):
    monitor_key = (str(target_chat_id), id(session))
    deadline = time.monotonic() + SESSION_TTL_SECONDS
    try:
        while time.monotonic() < deadline:
            if active_logins.get(target_chat_id) is not session:
                remote_auth_service.revoke_session(session)
                return
            try:
                status, data = session.check()
            except Exception as error:
                status, data = "error", error
            if status == "success":
                remote_auth_service.revoke_session(session, completed=True)
                _complete_login(target_chat_id, output_chat_id)
                return
            if status == "error":
                if active_logins.get(target_chat_id) is session:
                    active_logins.pop(target_chat_id, None)
                remote_auth_service.revoke_session(session)
                try:
                    session.close()
                except Exception:
                    pass
                bot.send_message(
                    output_chat_id,
                    "⚠️ Не удалось проверить вход в MAX. Авторизация завершена; начните её заново.",
                    **NO_PREVIEW,
                )
                debug_logger.warning(
                    "REMOTE_MAX_LOGIN_CHECK_FAILED target=%s error_type=%s",
                    target_chat_id, type(data).__name__,
                )
                return
            time.sleep(1.5)
        if active_logins.get(target_chat_id) is session:
            active_logins.pop(target_chat_id, None)
        remote_auth_service.revoke_session(session)
        try:
            session.close()
        except Exception:
            pass
        bot.send_message(
            output_chat_id,
            "⌛ Время удалённого входа истекло. Запустите подключение ещё раз, чтобы получить новую одноразовую ссылку.",
            **NO_PREVIEW,
        )
    finally:
        with remote_auth_monitor_lock:
            remote_auth_monitors.discard(monitor_key)


def _start_remote_login_monitor(target_chat_id, session, output_chat_id):
    monitor_key = (str(target_chat_id), id(session))
    with remote_auth_monitor_lock:
        if monitor_key in remote_auth_monitors:
            return
        remote_auth_monitors.add(monitor_key)
    threading.Thread(
        target=_monitor_remote_login,
        args=(target_chat_id, session, output_chat_id),
        daemon=True,
        name=f"max-remote-auth-{target_chat_id}",
    ).start()


def do_qr_login(target_chat_id, admin_chat_id):
    if int(admin_chat_id) < 0:
        bot.send_message(
            admin_chat_id,
            "Для безопасности MAX авторизацию можно начать только в личном чате с ботом.",
            **NO_PREVIEW,
        )
        return
    is_project_account = target_chat_id == "project_account"
    chat = None if is_project_account else get_chat_by_id(target_chat_id)
    if not is_project_account and not chat:
        bot.send_message(admin_chat_id, "❌ Чат не найден", **NO_PREVIEW)
        return
    title = "аккаунта проекта" if is_project_account else chat["title"]
    session = active_logins.pop(target_chat_id, None)
    if session:
        remote_auth_service.revoke_session(session)
        try:
            session.close()
        except:
            pass
    _clear_auth_waiters(target_chat_id)
    bot.send_message(
        admin_chat_id,
        f"⏳ Подготавливаю QR-код для <b>{escape_html(title)}</b>...\nЭто займет несколько секунд.",
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
        if is_project_account:
            threading.Thread(
                target=_verify_project_chats_after_login,
                args=(admin_chat_id,),
                daemon=True,
            ).start()
        elif chat and chat.get("owner_id") and not is_admin(int(chat["owner_id"])):
            owner_id = int(chat["owner_id"])
            _maybe_offer_review(owner_id)
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
        bot.send_photo(
            admin_chat_id,
            io.BytesIO(data),
            caption=(
                f"📱 <b>Отсканируйте QR-код</b> в приложении MAX\n\n"
                f"MAX: <b>{escape_html(title)}</b>\n\n"
                "После сканирования нажмите <b>«✅ Готово, я отсканировал»</b>, "
                "или выберите вход по номеру через защищённую страницу."
            ),
            parse_mode="HTML",
            reply_markup=_build_qr_keyboard(target_chat_id),
        )
    except Exception as error:
        bot.send_message(admin_chat_id, f"❌ Ошибка отправки QR-кода: {error}", **NO_PREVIEW)
        session.close()
        active_logins.pop(target_chat_id, None)

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
    kb.add(types.InlineKeyboardButton("🧪 Последний разбор сообщений", callback_data="admin_parse_diagnostics"))
    kb.row(
        types.InlineKeyboardButton("👑 Админы", callback_data="admin_admins"),
        types.InlineKeyboardButton("👤 Пользователи", callback_data="admin_users")
    )
    kb.row(types.InlineKeyboardButton("📢 Рассылка всем", callback_data="admin_broadcast"))
    kb.row(types.InlineKeyboardButton("💬 Отправить в чат", callback_data="admin_send_to_chat"))
    kb.row(types.InlineKeyboardButton("👤 Отправить пользователю", callback_data="admin_send_to_user"))
    kb.add(types.InlineKeyboardButton("🎁 Бесплатная подписка", callback_data="admin_free_subscriptions"))
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
            session_mark = "✅" if os.path.exists(_session_path_for_chat(c)) else "❌"
            if c.get("account_mode") == "project":
                connection_status = f"🔗URL: {url_mark} │ 🔑Сессия: {session_mark}\n"
            else:
                phone_mark = "✅" if c.get('phone') else "—"
                connection_status = f"🔗URL: {url_mark} │ 📱Телефон: {phone_mark} │ 🔑Сессия: {session_mark}\n"
            stats = get_chat_stats(c['chat_id'])
            text += f"\n\n{status} <b>{c['title']}</b>"
            text += f"<blockquote>🆔 <code>{c['chat_id']}</code> │ {status_text}\n"
            text += connection_status
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
    phone_line = (
        f"📱 <b>Телефон:</b> {chat['phone'] or '❌ не задан'}\n"
        if chat.get("account_mode") != "project" else ""
    )
    text = (
        f"📋 <b>{chat['title']}</b>\n\n"
        f"🆔 <b>ID:</b> <code>{chat['chat_id']}</code>\n"
        f"🔗 <b>URL:</b> {chat['max_url'] or ' не задан'}\n"
        f"{phone_line}"
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
    if chat.get("account_mode") != "project":
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

ADMIN_LIST_PAGE_SIZE = 10


def _page_buttons(keyboard, page, page_count, prefix):
    if page_count <= 1:
        return
    row = []
    if page > 0:
        row.append(types.InlineKeyboardButton("◀️ Назад", callback_data=f"{prefix}_{page - 1}"))
    if page + 1 < page_count:
        row.append(types.InlineKeyboardButton("Вперёд ▶️", callback_data=f"{prefix}_{page + 1}"))
    if row:
        keyboard.row(*row)


def _short_button_label(value, limit=25):
    value = str(value or "").strip()
    return value if len(value) <= limit else value[:limit - 1] + "…"


def show_parse_diagnostics(chat_id, message_id, target_chat_id=None):
    chats = get_all_chats()
    with PARSE_DIAGNOSTICS_LOCK:
        diagnostics = {key: dict(value) for key, value in PARSE_DIAGNOSTICS.items()}

    kb = types.InlineKeyboardMarkup(row_width=1)
    if target_chat_id is None:
        text = "🧪 <b>Последние результаты парсинга</b>\n\n"
        if not chats:
            text += "Сначала добавьте чат для парсинга."
        else:
            text += "Выберите чат, чтобы увидеть последние сообщения и их статус."
            for chat in chats[:12]:
                chat_id_value = int(chat["chat_id"])
                entry = diagnostics.get(chat_id_value)
                title = escape_html(chat.get("title") or str(chat_id_value))
                if entry:
                    text += (
                        f"\n\n<b>{title}</b> · {escape_html(entry.get('updated_at') or '')}"
                        f"\n✅ {entry.get('sent', 0)} отправлено · "
                        f"⏭ {entry.get('skipped', 0)} пропущено · "
                        f"⚠️ {entry.get('failed', 0)} ошибок"
                    )
                    button_label = _short_button_label(
                        f"🧾 {chat.get('title') or chat_id_value}", 32
                    )
                else:
                    text += f"\n\n<b>{title}</b> · данных пока нет"
                    button_label = _short_button_label(
                        f"🔎 {chat.get('title') or chat_id_value}", 32
                    )
                kb.add(types.InlineKeyboardButton(
                    button_label,
                    callback_data=f"admin_parse_diag_{chat_id_value}"
                ))
        if chats:
            text += "\n\n<i>Данные хранятся в памяти до перезапуска бота.</i>"
        kb.add(types.InlineKeyboardButton("🔙 Назад в меню", callback_data="admin_back"))
        safe_edit_message(text, chat_id, message_id, reply_markup=kb, **NO_PREVIEW)
        return

    target_chat_id = int(target_chat_id)
    entry = diagnostics.get(target_chat_id)
    chat = get_chat_by_id(target_chat_id)
    title = (entry or {}).get("title") or (chat or {}).get("title") or str(target_chat_id)
    text = f"🧪 <b>Проверка: {escape_html(title)}</b>\n🆔 <code>{target_chat_id}</code>"
    if not entry:
        text += "\n\nРезультатов пока нет. Они появятся после следующего цикла парсинга."
    else:
        text += (
            f"\n🕒 {escape_html(entry.get('updated_at') or '')}"
            f"\n✅ Отправлено: {entry.get('sent', 0)}"
            f" · ⏭ Пропущено: {entry.get('skipped', 0)}"
            f" · ⚠️ Ошибок: {entry.get('failed', 0)}"
        )
        if entry.get("error"):
            text += f"\n\n⚠️ <b>Ошибка разбора:</b> {escape_html(entry['error'])}"
        for item in entry.get("posts", []):
            status_label = {
                "sent": "✅ отправлено",
                "skipped": "⏭ пропущено",
                "failed": "⚠️ ошибка",
            }.get(item.get("status"), "ℹ️ обработано")
            preview = escape_html(item.get("text") or "[без текста]")
            detail = escape_html(item.get("detail") or "")
            text += (
                f"\n\n<b>{status_label}</b> · ID <code>{escape_html(item.get('source_id') or '?')}</code>"
                f"\n{preview}"
                f"\n📎 {escape_html(item.get('media') or 'нет')}"
            )
            if item.get("date") or item.get("time"):
                text += f" · {escape_html(' '.join(x for x in (item.get('date'), item.get('time')) if x))}"
            if detail:
                text += f"\n<i>{detail}</i>"
    kb.add(types.InlineKeyboardButton("🔙 К списку результатов", callback_data="admin_parse_diagnostics"))
    kb.add(types.InlineKeyboardButton("🔙 В меню", callback_data="admin_back"))
    safe_edit_message(text[:3900], chat_id, message_id, reply_markup=kb, **NO_PREVIEW)


def show_admins_list(chat_id, message_id, page=0):
    admins = get_all_admins()
    page_count = max(1, (len(admins) + ADMIN_LIST_PAGE_SIZE - 1) // ADMIN_LIST_PAGE_SIZE)
    page = max(0, min(int(page), page_count - 1))
    first = page * ADMIN_LIST_PAGE_SIZE
    shown = admins[first:first + ADMIN_LIST_PAGE_SIZE]
    text = f"👑 <b>Администраторы</b> · {len(admins)} всего"
    if admins:
        text += f" · {first + 1}–{first + len(shown)} из {len(admins)}"
        for index, admin in enumerate(shown, first + 1):
            username = f"@{escape_html(admin['username'])}" if admin.get("username") else "без username"
            text += (
                f"\n\n<b>{index}. {username}</b>"
                f"\n🆔 ID: <code>{admin['user_id']}</code>"
            )
    else:
        text += "\n\nСписок пока пуст."
    kb = types.InlineKeyboardMarkup(row_width=1)
    for admin in shown:
        if admin["user_id"] != ADMIN_ID:
            label = _short_button_label(f"🗑 Удалить @{admin.get('username') or admin['user_id']}", 32)
            kb.add(types.InlineKeyboardButton(
                label,
                callback_data=f"admin_ask_delete_admin_{admin['user_id']}"
            ))
    _page_buttons(kb, page, page_count, "admin_admins_page")
    kb.add(types.InlineKeyboardButton("➕ Добавить админа", callback_data="admin_add_admin"))
    kb.add(types.InlineKeyboardButton("🔙 Назад в меню", callback_data="admin_back"))
    safe_edit_message(text, chat_id, message_id, reply_markup=kb, **NO_PREVIEW)


def show_users_list(chat_id, message_id, page=0):
    users = get_all_users()
    page_count = max(1, (len(users) + ADMIN_LIST_PAGE_SIZE - 1) // ADMIN_LIST_PAGE_SIZE)
    page = max(0, min(int(page), page_count - 1))
    first = page * ADMIN_LIST_PAGE_SIZE
    shown = users[first:first + ADMIN_LIST_PAGE_SIZE]
    text = f"👤 <b>Пользователи</b> · {len(users)} всего"
    if users:
        text += f" · {first + 1}–{first + len(shown)} из {len(users)}"
        for index, user in enumerate(shown, first + 1):
            name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
            name = escape_html(name or "Имя не указано")
            username = f"@{escape_html(user['username'])}" if user.get("username") else "username не указан"
            text += (
                f"\n\n<b>{index}. {name}</b>"
                f"\n{username}"
                f"\n🆔 ID: <code>{user['user_id']}</code>"
            )
    else:
        text += "\n\nПользователей пока нет."
    kb = types.InlineKeyboardMarkup(row_width=1)
    _page_buttons(kb, page, page_count, "admin_users_page")
    kb.add(types.InlineKeyboardButton("🔙 Назад в меню", callback_data="admin_back"))
    safe_edit_message(text, chat_id, message_id, reply_markup=kb, **NO_PREVIEW)


def show_free_subscription_users(chat_id, message_id, page=0):
    users = get_all_users()
    page_count = max(1, (len(users) + ADMIN_LIST_PAGE_SIZE - 1) // ADMIN_LIST_PAGE_SIZE)
    page = max(0, min(int(page), page_count - 1))
    first = page * ADMIN_LIST_PAGE_SIZE
    shown = users[first:first + ADMIN_LIST_PAGE_SIZE]
    text = f"🎁 <b>Выдать бесплатную подписку</b> · {len(users)} пользователей\n\nВыберите пользователя:"
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for user in shown:
        name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
        display = f"@{user['username']}" if user.get("username") else (name or str(user["user_id"]))
        text += f"\n\n<b>{escape_html(name or display)}</b> · <code>{user['user_id']}</code>"
        keyboard.add(types.InlineKeyboardButton(
            _short_button_label(f"🎁 {display}", 32),
            callback_data=f"admin_free_sub_user_{user['user_id']}"
        ))
    if not users:
        text = "🎁 <b>Бесплатная подписка</b>\n\nПользователей пока нет."
    _page_buttons(keyboard, page, page_count, "admin_free_sub_page")
    keyboard.add(types.InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_back"))
    safe_edit_message(text, chat_id, message_id, reply_markup=keyboard, **NO_PREVIEW)


def show_free_subscription_options(chat_id, message_id, user_id):
    user = next((item for item in get_all_users() if int(item["user_id"]) == int(user_id)), None)
    if not user:
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("🔙 К списку", callback_data="admin_free_subscriptions"))
        safe_edit_message(
            "Пользователь не найден. Обновите список и попробуйте ещё раз.",
            chat_id, message_id,
            reply_markup=keyboard,
            **NO_PREVIEW,
        )
        return
    name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
    username = f"@{escape_html(user['username'])}" if user.get("username") else "без username"
    text = (
        "🎁 <b>Выдача бесплатной подписки</b>\n\n"
        f"Пользователь: <b>{escape_html(name or 'Имя не указано')}</b> ({username})\n"
        f"ID: <code>{user_id}</code>\n\n"
        "Выберите срок и способ подключения. Если подписка уже активна, новый срок добавится к ней."
    )
    current = get_subscription(user_id)
    if current and current["active_until"] > datetime.now(timezone.utc):
        active_until = current["active_until"].astimezone(timezone.utc)
        text += f"\n\nСейчас действует до: <b>{active_until:%d.%m.%Y %H:%M} UTC</b>."
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for days, period in ((30, "30 дней"), (90, "3 месяца"), (365, "1 год")):
        keyboard.add(types.InlineKeyboardButton(
            f"🎁 {period} · ваш MAX",
            callback_data=f"admin_free_sub_grant_self_{days}_{user_id}"
        ))
        keyboard.add(types.InlineKeyboardButton(
            f"🎁 {period} · MAX проекта",
            callback_data=f"admin_free_sub_grant_project_{days}_{user_id}"
        ))
    keyboard.add(types.InlineKeyboardButton("🔙 К списку", callback_data="admin_free_subscriptions"))
    safe_edit_message(text, chat_id, message_id, reply_markup=keyboard, **NO_PREVIEW)


def show_user_send_selector(chat_id, message_id, page=0):
    users = get_all_users()
    page_count = max(1, (len(users) + ADMIN_LIST_PAGE_SIZE - 1) // ADMIN_LIST_PAGE_SIZE)
    page = max(0, min(int(page), page_count - 1))
    first = page * ADMIN_LIST_PAGE_SIZE
    shown = users[first:first + ADMIN_LIST_PAGE_SIZE]
    text = f"👤 <b>Выберите пользователя</b> · {len(users)} всего"
    if users:
        for user in shown:
            name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
            name = escape_html(name or "Имя не указано")
            username = f"@{escape_html(user['username'])}" if user.get("username") else "username не указан"
            text += f"\n\n<b>{name}</b> · {username}\n🆔 ID: <code>{user['user_id']}</code>"
    else:
        text += "\n\nСписок пользователей пока пуст."
    kb = types.InlineKeyboardMarkup(row_width=1)
    for user in shown:
        kb.add(types.InlineKeyboardButton(
            "👤 Выбрать",
            callback_data=f"admin_send_user_pick_{user['user_id']}"
        ))
    _page_buttons(kb, page, page_count, "admin_send_users_page")
    kb.add(types.InlineKeyboardButton("✍️ Ввести ID вручную", callback_data="admin_send_user_manual"))
    kb.add(types.InlineKeyboardButton("🔙 Назад в меню", callback_data="admin_back"))
    safe_edit_message(text, chat_id, message_id, reply_markup=kb, **NO_PREVIEW)


def show_chat_send_selector(chat_id, message_id, page=0):
    chats = get_all_chats()
    page_count = max(1, (len(chats) + ADMIN_LIST_PAGE_SIZE - 1) // ADMIN_LIST_PAGE_SIZE)
    page = max(0, min(int(page), page_count - 1))
    first = page * ADMIN_LIST_PAGE_SIZE
    shown = chats[first:first + ADMIN_LIST_PAGE_SIZE]
    text = f"💬 <b>Выберите чат</b> · {len(chats)} всего"
    if chats:
        for chat in shown:
            title = escape_html(chat.get("title") or "Без названия")
            text += f"\n\n<b>{title}</b>\n🆔 ID: <code>{chat['chat_id']}</code>"
    else:
        text += "\n\nЧатов пока нет."
    kb = types.InlineKeyboardMarkup(row_width=1)
    for chat in shown:
        kb.add(types.InlineKeyboardButton(
            "💬 Выбрать",
            callback_data=f"admin_send_chat_pick_{chat['chat_id']}"
        ))
    _page_buttons(kb, page, page_count, "admin_send_chats_page")
    kb.add(types.InlineKeyboardButton("✍️ Ввести ID вручную", callback_data="admin_send_chat_manual"))
    kb.add(types.InlineKeyboardButton("🔙 Назад в меню", callback_data="admin_back"))
    safe_edit_message(text, chat_id, message_id, reply_markup=kb, **NO_PREVIEW)


def _prompt_send_to_user(chat_id, user_id):
    prompt = bot.send_message(
        chat_id,
        f"Теперь отправьте сообщение пользователю <code>{user_id}</code>.",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard(),
        **NO_PREVIEW,
    )
    bot.register_next_step_handler(prompt, process_send_to_user_content, int(user_id))


def _prompt_send_to_chat(chat_id, target_chat_id):
    prompt = bot.send_message(
        chat_id,
        f"Теперь отправьте сообщение в чат <code>{target_chat_id}</code>.",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard(),
        **NO_PREVIEW,
    )
    bot.register_next_step_handler(prompt, process_send_to_chat_content, int(target_chat_id))


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
        header = "📨 <b>Сообщение от админа</b>\n\n"
        caption = message.caption or ""

        def send_captioned(send_method, file_id):
            formatted_caption = header + escape_html(caption)
            if len(formatted_caption) <= 1024:
                send_method(
                    user_id, file_id, caption=formatted_caption, parse_mode="HTML"
                )
            else:
                bot.send_message(user_id, header, parse_mode="HTML", **NO_PREVIEW)
                send_method(user_id, file_id, caption=caption)

        if message.video:
            send_captioned(bot.send_video, message.video.file_id)
        elif message.photo:
            send_captioned(bot.send_photo, message.photo[-1].file_id)
        elif message.document:
            send_captioned(bot.send_document, message.document.file_id)
        elif message.audio:
            send_captioned(bot.send_audio, message.audio.file_id)
        elif message.voice:
            send_captioned(bot.send_voice, message.voice.file_id)
        elif message.animation:
            send_captioned(bot.send_animation, message.animation.file_id)
        elif message.sticker:
            bot.send_message(user_id, header, parse_mode="HTML", **NO_PREVIEW)
            bot.send_sticker(user_id, message.sticker.file_id)
        elif message.video_note:
            bot.send_message(user_id, header, parse_mode="HTML", **NO_PREVIEW)
            bot.send_video_note(user_id, message.video_note.file_id)
        elif message.text:
            formatted_text = header + escape_html(message.text)
            if len(formatted_text) <= 4096:
                bot.send_message(
                    user_id, formatted_text, parse_mode="HTML", **NO_PREVIEW
                )
            else:
                bot.send_message(user_id, header, parse_mode="HTML", **NO_PREVIEW)
                bot.send_message(user_id, message.text, **NO_PREVIEW)
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

def process_send_to_chat(message, target_chat_id):
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
    keyboard.add(types.InlineKeyboardButton("⭐ Отзывы", callback_data="reviews_show"))
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


def subscription_renewal_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton("Продлить · свой MAX-аккаунт · 250 ₽", callback_data="renew_self"))
    keyboard.add(types.InlineKeyboardButton("Продлить · аккаунт проекта · 500 ₽", callback_data="renew_project"))
    return keyboard


def _subscription_plan(plan_code):
    plans = {
        "self": (25000, "подключение через ваш аккаунт MAX"),
        "project": (50000, "подключение через аккаунт проекта MAX"),
    }
    if plan_code not in plans:
        raise ValueError("Некорректный тариф подписки")
    return plans[plan_code]


def _yookassa_receipt_provider_data(plan_code, amount):
    if not YOOKASSA_RECEIPTS_ENABLED:
        return None
    try:
        vat_code = int(YOOKASSA_RECEIPT_VAT_CODE)
    except (TypeError, ValueError):
        raise RuntimeError("не задан YOOKASSA_RECEIPT_VAT_CODE")
    if not YOOKASSA_RECEIPT_PAYMENT_MODE or not YOOKASSA_RECEIPT_PAYMENT_SUBJECT:
        raise RuntimeError(
            "задайте YOOKASSA_RECEIPT_PAYMENT_MODE и YOOKASSA_RECEIPT_PAYMENT_SUBJECT"
        )

    _, plan_description = _subscription_plan(plan_code)
    receipt = {
        "items": [{
            "description": f"MAX_Parser — подписка на 30 дней ({plan_description})",
            "quantity": "1.00",
            "amount": {
                "value": f"{amount // 100}.{amount % 100:02d}",
                "currency": "RUB",
            },
            "vat_code": vat_code,
            "payment_mode": YOOKASSA_RECEIPT_PAYMENT_MODE,
            "payment_subject": YOOKASSA_RECEIPT_PAYMENT_SUBJECT,
        }]
    }
    if YOOKASSA_RECEIPT_TAX_SYSTEM_CODE:
        try:
            receipt["tax_system_code"] = int(YOOKASSA_RECEIPT_TAX_SYSTEM_CODE)
        except ValueError:
            raise RuntimeError("YOOKASSA_RECEIPT_TAX_SYSTEM_CODE должен быть числом")
    return json.dumps({"receipt": receipt}, ensure_ascii=False)


def _send_subscription_invoice(user_id, plan_code):
    if not TELEGRAM_PAYMENT_PROVIDER_TOKEN:
        bot.send_message(
            user_id,
            "Оплата пока не настроена. Попросите администратора добавить платёжный токен в настройки бота.",
            **NO_PREVIEW,
        )
        return False
    if not YOOKASSA_RECEIPTS_SETTING_CONFIRMED:
        bot.send_message(
            user_id,
            "Администратор ещё не подтвердил настройки фискальных чеков ЮKassa. "
            "Счёт пока нельзя выставить; пожалуйста, обратитесь в поддержку командой /help.",
            **NO_PREVIEW,
        )
        return False
    amount, plan_description = _subscription_plan(plan_code)
    try:
        provider_data = _yookassa_receipt_provider_data(plan_code, amount)
    except RuntimeError as error:
        debug_logger.error("YOOKASSA_RECEIPT_CONFIGURATION_INCOMPLETE: %s", error)
        bot.send_message(
            user_id,
            "Оплата временно недоступна: не завершена настройка фискального чека. "
            "Администратору нужно проверить параметры ЮKassa.",
            **NO_PREVIEW,
        )
        return False

    try:
        payload = create_payment_order(user_id, plan_code, amount)
        invoice_options = {
            "chat_id": user_id,
            "title": "MAX_Parser — подписка",
            "description": f"Доступ к MAX_Parser на 30 дней · {plan_description}",
            "invoice_payload": payload,
            "provider_token": TELEGRAM_PAYMENT_PROVIDER_TOKEN,
            "currency": "RUB",
            "prices": [types.LabeledPrice(label="Подписка на 30 дней", amount=amount)],
            "start_parameter": payload,
            "need_email": YOOKASSA_RECEIPTS_ENABLED,
            "send_email_to_provider": YOOKASSA_RECEIPTS_ENABLED,
            "is_flexible": False,
        }
        if provider_data:
            invoice_options["provider_data"] = provider_data
        bot.send_invoice(**invoice_options)
        return True
    except Exception as error:
        debug_logger.error(
            "TELEGRAM_INVOICE_SEND_FAILED user_id=%s plan=%s error_type=%s",
            user_id, plan_code, type(error).__name__,
        )
        bot.send_message(
            user_id,
            "Не удалось выставить счёт. Попробуйте ещё раз чуть позже или напишите в поддержку командой /help.",
            **NO_PREVIEW,
        )
        return False


def _send_review_invitation(user_id):
    if get_review_status(user_id) != "eligible" or not claim_review_invitation(user_id):
        return False
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton(
        "✍️ Оставить отзыв", callback_data=f"review_write_{int(user_id)}"
    ))
    keyboard.add(types.InlineKeyboardButton(
        "Не хочу", callback_data=f"review_skip_{int(user_id)}"
    ))
    try:
        bot.send_message(
            int(user_id),
            "🎉 <b>Подключение завершено</b>\n\n"
            "Расскажите, пожалуйста, как всё прошло и удобно ли настраивать MAX_Parser. "
            "Отзыв появится в общем списке только после проверки администратором.",
            parse_mode="HTML",
            reply_markup=keyboard,
            **NO_PREVIEW,
        )
    except Exception:
        release_review_invitation(user_id)
        raise
    return True


def _maybe_offer_review(user_id):
    status = get_review_status(user_id)
    if status is None:
        offer_review(user_id)
        status = get_review_status(user_id)
    if status == "eligible":
        return _send_review_invitation(user_id)
    return False


def show_approved_reviews(chat_id, message_id, page=0):
    reviews = get_approved_reviews()
    if not reviews:
        text = "⭐ <b>Отзывы</b>\n\nПока нет опубликованных отзывов."
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("🏠 В главное меню", callback_data="start_home"))
        safe_edit_message(text, chat_id, message_id, reply_markup=keyboard, parse_mode="HTML", **NO_PREVIEW)
        return
    page_size = 1
    page_count = (len(reviews) + page_size - 1) // page_size
    page = max(0, min(int(page), page_count - 1))
    review = reviews[page]
    created_at = review.get("created_at")
    date_label = created_at.strftime("%d.%m.%Y") if created_at else ""
    review_text = escape_html(str(review.get("review_text") or "").strip()[:700])
    text = (
        f"⭐ <b>Отзывы о MAX_Parser</b> · {page + 1}/{len(reviews)}\n\n"
        f"<blockquote>{review_text}</blockquote>"
    )
    if date_label:
        text += f"\n\n<i>{date_label}</i>"
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    row = []
    if page > 0:
        row.append(types.InlineKeyboardButton("◀️", callback_data=f"reviews_page_{page - 1}"))
    if page + 1 < page_count:
        row.append(types.InlineKeyboardButton("▶️", callback_data=f"reviews_page_{page + 1}"))
    if row:
        keyboard.row(*row)
    keyboard.add(types.InlineKeyboardButton("🏠 В главное меню", callback_data="start_home"))
    safe_edit_message(
        text,
        chat_id,
        message_id,
        reply_markup=keyboard,
        parse_mode="HTML",
        **NO_PREVIEW,
    )


def _send_review_for_moderation(user_id, review_text):
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.row(
        types.InlineKeyboardButton("✅ Одобрить", callback_data=f"review_approve_{int(user_id)}"),
        types.InlineKeyboardButton("❌ Отклонить", callback_data=f"review_reject_{int(user_id)}"),
    )
    bot.send_message(
        ADMIN_ID,
        "📝 <b>Новый отзыв на проверку</b>\n"
        f"👤 User ID: <code>{int(user_id)}</code>\n\n"
        f"<blockquote>{escape_html(review_text[:500])}</blockquote>",
        parse_mode="HTML",
        reply_markup=keyboard,
        **NO_PREVIEW,
    )


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
                **NO_PREVIEW,
            )
            return
        support_sessions.add(user.id)
        open_support_session(user.id)
    try:
        bot.send_message(
            chat_id,
            "💬 <b>Чат с тех поддержкой открыт</b>\n\n"
            "Напишите, с чем нужна помощь. "
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

@bot.message_handler(commands=['start'])
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


@bot.message_handler(commands=['help', 'help1'])
def help_command(message):
    start_support(message.chat.id, message.from_user)


@bot.message_handler(commands=['subscription'])
def subscription_status_command(message):
    track_user(message)
    if message.chat.type != "private":
        bot.reply_to(message, "Откройте личный чат со мной и отправьте /subscription.")
        return
    if not PAYMENTS_ENABLED:
        bot.send_message(
            message.chat.id,
            "Оплата ещё не настроена. Подключение пока работает в тестовом режиме.",
            **NO_PREVIEW,
        )
        return
    subscription = get_subscription(message.from_user.id)
    if subscription and subscription["active_until"] > datetime.now(timezone.utc):
        active_until = subscription["active_until"].astimezone(timezone.utc)
        text = (
            "💳 <b>Ваша подписка MAX_Parser активна</b>\n\n"
            f"Действует до: <b>{active_until:%d.%m.%Y %H:%M} UTC</b>.\n"
            "Подписка распространяется на все подключённые группы вашего аккаунта.\n\n"
            "Продлить её можно заранее — оплаченный срок добавится к текущему."
        )
    else:
        text = (
            "💳 <b>Активной подписки нет</b>\n\n"
            "Выберите тариф, чтобы оформить доступ или продлить его."
        )
    bot.send_message(
        message.chat.id,
        text,
        parse_mode="HTML",
        reply_markup=subscription_renewal_keyboard(),
        **NO_PREVIEW,
    )


@bot.pre_checkout_query_handler(func=lambda query: True)
def handle_pre_checkout_query(query):
    try:
        accepted = claim_payment_order_for_checkout(
            payload=query.invoice_payload,
            user_id=query.from_user.id,
            currency=query.currency,
            total_amount=query.total_amount,
        )
    except Exception as error:
        accepted = False
        debug_logger.error(
            "PRE_CHECKOUT_VALIDATION_FAILED user_id=%s error_type=%s",
            query.from_user.id, type(error).__name__,
        )
    if accepted:
        bot.answer_pre_checkout_query(query.id, ok=True)
    else:
        bot.answer_pre_checkout_query(
            query.id,
            ok=False,
            error_message="Счёт устарел или уже обрабатывается. Вернитесь в бот и запросите новый счёт.",
        )


@bot.message_handler(content_types=["successful_payment"])
def handle_successful_payment(message):
    payment = message.successful_payment
    user_id = message.from_user.id
    try:
        result = complete_payment_order(
            payload=payment.invoice_payload,
            user_id=user_id,
            currency=payment.currency,
            total_amount=payment.total_amount,
            provider_payment_charge_id=payment.provider_payment_charge_id,
            telegram_payment_charge_id=payment.telegram_payment_charge_id,
        )
    except Exception as error:
        debug_logger.error(
            "SUCCESSFUL_PAYMENT_ACTIVATION_FAILED user_id=%s provider_charge_id=%s error_type=%s",
            user_id,
            getattr(payment, "provider_payment_charge_id", None),
            type(error).__name__,
        )
        bot.send_message(
            user_id,
            "✅ Telegram подтвердил оплату, но не удалось автоматически обновить подписку. "
            "Я уже передал информацию администратору; пожалуйста, напишите в поддержку командой /help.",
            **NO_PREVIEW,
        )
        try:
            bot.send_message(
                ADMIN_ID,
                "⚠️ Платёж прошёл, но подписка не активировалась автоматически.\n"
                f"Пользователь: <code>{user_id}</code>\n"
                f"ID платежа ЮKassa: <code>{escape_html(getattr(payment, 'provider_payment_charge_id', '') or 'не получен')}</code>\n"
                f"Payload: <code>{escape_html(payment.invoice_payload)}</code>",
                parse_mode="HTML",
                **NO_PREVIEW,
            )
        except Exception:
            pass
        return

    if result["already_paid"]:
        bot.send_message(user_id, "✅ Этот платёж уже был обработан. Подписка активна.", **NO_PREVIEW)
        return

    active_until = result["active_until"].astimezone(timezone.utc)
    amount_rub = payment.total_amount / 100
    plan_label = (
        "Подключение через ваш аккаунт MAX"
        if result["plan_code"] == "self"
        else "Подключение через аккаунт проекта MAX"
    )
    paid_at = datetime.fromtimestamp(message.date, tz=timezone.utc)
    provider_charge_id = escape_html(payment.provider_payment_charge_id or "не получен")
    fiscal_receipt_note = (
        "🧾 Данные для фискального чека переданы платёжному провайдеру."
        if YOOKASSA_RECEIPTS_ENABLED
        else "ℹ️ Это подтверждение оплаты от MAX_Parser. Фискальные чеки ЮKassa в текущих настройках не подключены."
    )
    bot.send_message(
        user_id,
        "🧾 <b>Квитанция MAX_Parser</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "✅ <b>Статус:</b> оплачено\n"
        "📦 <b>Услуга:</b> подписка на 30 дней\n"
        f"🔧 <b>Способ:</b> {plan_label}\n"
        f"💳 <b>Сумма:</b> {amount_rub:.2f} ₽\n"
        f"🕒 <b>Оплачено:</b> {paid_at:%d.%m.%Y %H:%M} UTC\n"
        f"📅 <b>Действует до:</b> {active_until:%d.%m.%Y %H:%M} UTC\n"
        f"🔖 <b>ID платежа:</b> <code>{provider_charge_id}</code>\n\n"
        f"<blockquote>Доступ распространяется на все группы вашего аккаунта.\n"
        "Автоматического списания нет — заранее напомню о продлении.</blockquote>\n"
        f"<i>{fiscal_receipt_note}</i>",
        parse_mode="HTML",
        **NO_PREVIEW,
    )
    try:
        bot.send_message(
            ADMIN_ID,
            "💳 <b>Успешный платёж MAX_Parser</b>\n"
            f"Пользователь: <code>{user_id}</code>\n"
            f"Сумма: <b>{amount_rub:.2f} ₽</b>\n"
            f"ID платежа ЮKassa: <code>{escape_html(payment.provider_payment_charge_id or 'не получен')}</code>",
            parse_mode="HTML",
            **NO_PREVIEW,
        )
    except Exception:
        pass

    if not get_user_chats(user_id):
        pending_setup_modes[user_id] = result["plan_code"]
        label = "ваш аккаунт MAX" if result["plan_code"] == "self" else "аккаунт проекта MAX"
        bot.send_message(
            user_id,
            "Теперь можно подключить первую группу.\n\n"
            f"Выбранный способ: <b>{label}</b>. Добавьте меня в нужную Telegram-группу и отправьте там /bind. "
            "Если я уже нахожусь в ней, просто отправьте /bind. Затем пришлите ссылку на источник MAX.",
            parse_mode="HTML",
            **NO_PREVIEW,
        )


def subscription_reminder_loop():
    while True:
        try:
            for subscription in get_due_subscription_reminders():
                user_id = int(subscription["user_id"])
                active_until = subscription["active_until"].astimezone(timezone.utc)
                try:
                    bot.send_message(
                        user_id,
                        "⏰ <b>Скоро закончится подписка MAX_Parser</b>\n\n"
                        f"Доступ действует до <b>{active_until:%d.%m.%Y %H:%M} UTC</b>.\n"
                        "Автоматического списания нет. Если хотите продолжить пользоваться парсером, продлите подписку заранее.",
                        parse_mode="HTML",
                        reply_markup=subscription_renewal_keyboard(),
                        **NO_PREVIEW,
                    )
                    mark_subscription_reminder_sent(user_id)
                except Exception as error:
                    debug_logger.error(
                        "SUBSCRIPTION_REMINDER_SEND_FAILED user_id=%s error_type=%s",
                        user_id, type(error).__name__,
                    )
        except Exception as error:
            debug_logger.error("SUBSCRIPTION_REMINDER_LOOP_FAILED error_type=%s", type(error).__name__)
        time.sleep(6 * 60 * 60)


@bot.message_handler(commands=['connect'])
def connect_command(message):
    track_user(message)
    if message.chat.type != "private":
        bot.reply_to(message, "Откройте личный чат со мной и отправьте /connect.")
        return
    bot.send_message(
        message.chat.id,
        f"Выберите способ подключения. Подключено чатов: {len(get_user_chats(message.from_user.id))}/{MAX_CUSTOMER_CHATS}.",
        reply_markup=parser_keyboard(),
        **NO_PREVIEW,
    )


def _chat_limit_notice():
    return (
        f"К вашему аккаунту можно подключить до {MAX_CUSTOMER_CHATS} чатов. "
        "Если нужно больше, напишите в поддержку командой /help."
    )


def _is_max_source_url(value):
    try:
        parsed = urlsplit(str(value or "").strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        return (
            parsed.scheme.lower() in {"http", "https"}
            and (host == "max.ru" or host.endswith(".max.ru"))
            and bool(parsed.path)
        )
    except (TypeError, ValueError):
        return False


@bot.message_handler(commands=['mychats'])
def my_chats_command(message):
    track_user(message)
    if message.chat.type != "private":
        bot.reply_to(message, "Список подключений показывается только в личном чате. Отправьте мне /mychats напрямую.")
        return
    _show_my_chats(message.chat.id, message.from_user.id)


def _show_my_chats(chat_id, owner_id, message_id=None):
    chats = get_user_chats(owner_id)
    if not chats:
        text = "У вас пока нет подключённых Telegram-групп. Начните настройку командой /connect."
        if message_id:
            safe_edit_message(
                text, chat_id, message_id,
                reply_markup=types.InlineKeyboardMarkup(), **NO_PREVIEW
            )
        else:
            bot.send_message(chat_id, text, **NO_PREVIEW)
        return
    lines = ["📬 <b>Ваши группы MAX_Parser</b>"]
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for index, chat in enumerate(chats, 1):
        state = "🟢 Парсинг включён" if chat.get("is_active") else "🟡 Ожидает настройки"
        mode = "ваш MAX-аккаунт" if chat.get("account_mode") != "project" else "аккаунт проекта"
        lines.append(
            f"\n<b>{index}. {escape_html(chat.get('title') or 'Группа')}</b>\n"
            f"{state} · {mode}"
        )
        keyboard.add(types.InlineKeyboardButton(
            f"🗑 Удалить №{index}",
            callback_data=f"mychat_delete_{chat['chat_id']}"
        ))
    if message_id:
        safe_edit_message("\n".join(lines), chat_id, message_id, reply_markup=keyboard, parse_mode="HTML", **NO_PREVIEW)
    else:
        bot.send_message(chat_id, "\n".join(lines), reply_markup=keyboard, parse_mode="HTML", **NO_PREVIEW)


def _delete_chat_everywhere(chat_id):
    chat_id = int(chat_id)
    with CHAT_LIFECYCLE_LOCK:
        chat = get_chat_by_id(chat_id)
        if not chat:
            return False
        delete_chat(chat_id)
        clear_chat_cache(chat_id)
        with PARSE_DIAGNOSTICS_LOCK:
            PARSE_DIAGNOSTICS.pop(chat_id, None)
        with _poll_message_cache_lock:
            prefix = f"{chat_id}:"
            for key in [key for key in _poll_message_cache if key.startswith(prefix)]:
                _poll_message_cache.pop(key, None)
            _save_poll_message_cache_locked()
        paths = []
        if chat.get("account_mode") != "project":
            paths.append(_session_path_for_chat(chat))
        paths.extend((
            os.path.join("sessions", f"parse_debug_{chat_id}.png"),
            os.path.join("sessions", f"wrong_chat_debug_{chat_id}.png"),
        ))
        for path in paths:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError as error:
                debug_logger.warning("CHAT_DELETE_FILE_FAILED chat_id=%s path=%s error=%s", chat_id, path, log_preview(error))
    try:
        bot.leave_chat(chat_id)
    except Exception as error:
        debug_logger.warning(
            "CHAT_DELETE_LEAVE_FAILED chat_id=%s error=%s",
            chat_id, log_preview(error),
        )
    return True


def _chat_delete_allowed(user_id, chat):
    try:
        return is_admin(user_id) or int(chat.get("owner_id") or 0) == int(user_id)
    except (TypeError, ValueError):
        return False


def _project_access_help():
    return (
        "Чтобы читать закрытый чат, аккаунт проекта <code>"
        f"{escape_html(MAX_PROJECT_ACCOUNT_PHONE)}</code> должен вступить в него. "
        "Можно добавить его по номеру или прислать боту ссылку-приглашение: бот попробует открыть её и вступить. "
        "Если MAX отправит заявку, владелец или администратор должен её одобрить. "
        "Для канала достаточно права читать публикации — права администратора не нужны. "
        "Открытые чаты можно подключить по ссылке, если MAX показывает сообщения аккаунту проекта. "
        "После отправки ссылки бот проверит доступ; если MAX не даст открыть чат, парсинг останется выключен."
    )


def _verify_project_chat_access(chat_id, notify_chat_id=None):
    chat = get_chat_by_id(chat_id)
    if not chat or chat.get("account_mode") != "project" or not chat.get("max_url"):
        return False
    owner_id = int(chat.get("owner_id") or 0)
    destination = int(notify_chat_id or owner_id or ADMIN_ID)
    session_path = _session_path_for_chat(chat)
    if not os.path.exists(session_path):
        if destination:
            bot.send_message(
                destination,
                "Ссылка на группу сохранена, но аккаунт проекта ещё не авторизован. "
                "Подключение включится после настройки сессии администратором.",
                **NO_PREVIEW,
            )
        return False
    try:
        posts = parse_max_group_media(
            group_url=chat["max_url"],
            chat_id=chat_id,
            session_path=session_path,
        )
        current = get_chat_by_id(chat_id)
        if (
            not current
            or current.get("account_mode") != "project"
            or current.get("max_url") != chat.get("max_url")
            or int(current.get("owner_id") or 0) != owner_id
        ):
            return False
        _process_chat_posts_if_present(chat_id, current.get("title") or "MAX", posts)
        toggle_chat(chat_id, True)
        bot.send_message(
            destination,
            "✅ <b>Доступ проверен.</b> Аккаунт проекта видит источник MAX, пересылка запущена.\n\n"
            "Если в группе пока нет сообщений, парсер продолжит проверять её автоматически.",
            parse_mode="HTML",
            **NO_PREVIEW,
        )
        if owner_id > 0 and not is_admin(owner_id):
            try:
                _maybe_offer_review(owner_id)
            except Exception as notify_error:
                debug_logger.warning(
                    "REVIEW_INVITATION_SEND_FAILED user_id=%s error_type=%s",
                    owner_id, type(notify_error).__name__,
                )
        return True
    except Exception as error:
        debug_logger.warning(
            "PROJECT_GROUP_ACCESS_CHECK_FAILED chat_id=%s owner_id=%s error_type=%s",
            chat_id, owner_id, type(error).__name__,
        )
        if destination:
            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(types.InlineKeyboardButton(
                "🔁 Проверить подключение",
                callback_data=f"project_retry_{chat_id}",
            ))
            if "MAX_INVITE_APPROVAL_PENDING" in str(error):
                access_message = (
                    "Заявка аккаунта проекта на вступление отправлена. "
                    "Когда владелец или администратор её одобрит, нажмите «Проверить подключение»."
                )
            elif "MAX_INVITE_JOIN_REQUIRED" in str(error):
                access_message = (
                    "Не удалось вступить в источник по этой ссылке. "
                    "Попросите владельца или администратора добавить аккаунт либо прислать новую ссылку. "
                    "После добавления нажмите «Проверить подключение»."
                )
            else:
                access_message = (
                    "Не удалось подтвердить доступ аккаунта проекта к источнику MAX. "
                    "Проверьте ссылку и убедитесь, что аккаунт добавлен и видит сообщения. "
                    "Затем нажмите «Проверить подключение»."
                )
            bot.send_message(
                destination,
                "⚠️ " + access_message + "\n\n" + _project_access_help(),
                parse_mode="HTML",
                reply_markup=keyboard,
                **NO_PREVIEW,
            )
        return False


def _verify_project_chats_after_login(notify_chat_id):
    for chat in get_all_chats():
        if chat.get("account_mode") == "project" and chat.get("max_url"):
            _verify_project_chat_access(
                chat["chat_id"],
                int(chat.get("owner_id") or notify_chat_id),
            )


def _begin_customer_group_setup(chat_id, owner_id, account_mode):
    if _user_needs_subscription(owner_id):
        pending_setup_modes.pop(owner_id, None)
        bot.send_message(
            owner_id,
            "Для подключения группы сначала оформите подписку командой /subscription.",
            reply_markup=subscription_renewal_keyboard(),
            **NO_PREVIEW,
        )
        return False
    title = None
    try:
        title = bot.get_chat(chat_id).title or f"Группа {chat_id}"
    except Exception:
        title = f"Группа {chat_id}"
    try:
        claimed = claim_chat(chat_id, title, owner_id, account_mode)
    except ChatLimitReached:
        pending_setup_modes.pop(owner_id, None)
        bot.send_message(owner_id, _chat_limit_notice(), **NO_PREVIEW)
        return False
    if not claimed:
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
        f"<b>Шаг 2 из {2 if account_mode == 'project' else 3}.</b> Пришлите ссылку на чат, группу или канал MAX, откуда получать сообщения.\n"
        "Как скопировать ссылку: откройте <a href=\"https://web.max.ru\">web.max.ru</a>, "
        "откройте нужный чат, группу или канал и скопируйте ссылку из меню.\n\n"
        + (f"Для открытого источника отправьте обычную ссылку. Для закрытого пришлите ссылку-приглашение — "
           f"бот попробует подключить аккаунт <code>{escape_html(MAX_PROJECT_ACCOUNT_PHONE)}</code>. "
           "Если MAX отправит заявку, владелец/администратор должен её одобрить. "
           "Затем бот проверит, видит ли аккаунт сообщения.\n\n"
           if account_mode == "project" else "")
        + "Для отмены отправьте /cancel.",
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
    if _user_needs_subscription(message.from_user.id):
        customer_setup_state.pop(message.from_user.id, None)
        bot.send_message(
            message.chat.id,
            "Срок подписки истёк до завершения настройки. Продлите её командой /subscription, затем повторите подключение.",
            reply_markup=subscription_renewal_keyboard(),
            **NO_PREVIEW,
        )
        return
    url = (message.text or "").strip()
    if not _is_max_source_url(url):
        bot.send_message(
            message.chat.id,
            "Не удалось распознать ссылку MAX. Пришлите ссылку на чат, группу или канал с домена max.ru.",
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
            "<b>Шаг 3 из 3.</b> Сейчас пришлю QR-код для входа в MAX. Отсканируйте его в приложении MAX "
            "и нажмите «Готово, я отсканировал». Авторизация будет сохранена только для вашей группы. "
            "Если удобнее входить по номеру, на QR-экране выберите защищённую страницу и завершите вход там.",
            parse_mode="HTML",
            **NO_PREVIEW,
        )
        do_qr_login(chat_id, message.from_user.id)
        return

    project_session = os.path.join("sessions", "project_account.json")
    if os.path.exists(project_session):
        bot.send_message(
            message.chat.id,
            "✅ Ссылка сохранена. Сейчас проверю, может ли аккаунт проекта открыть этот чат MAX. "
            "Парсинг включится только после успешной проверки.\n\n"
            f"{_project_access_help()}",
            parse_mode="HTML",
            **NO_PREVIEW,
        )
        threading.Thread(
            target=_verify_project_chat_access,
            args=(chat_id, message.chat.id),
            daemon=True,
        ).start()
    else:
        bot.send_message(
            message.chat.id,
            "✅ Ссылка сохранена. Подключение включится после настройки сессии аккаунта проекта "
            "и проверки доступа к источнику.\n\n"
            f"{_project_access_help()}\n\n"
            "Когда сессия будет готова, бот проверит доступ автоматически. Если MAX всё ещё не видит группу, "
            "в сообщении появится кнопка повторной проверки.",
            parse_mode="HTML",
            **NO_PREVIEW,
        )


@bot.message_handler(commands=['cancel'])
def cancel_customer_setup(message):
    if cancel_review(message.from_user.id):
        awaiting_review_text.discard(message.from_user.id)
        bot.send_message(message.chat.id, "Отзыв отменён. Спасибо, что подключились!", **NO_PREVIEW)
        return
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
    func=lambda m: m.chat.type == "private" and m.from_user.id in awaiting_review_text,
    content_types=["text"],
)
def handle_customer_review(message):
    user_id = int(message.from_user.id)
    review_text = (message.text or "").strip()
    if review_text.startswith("/"):
        bot.send_message(
            message.chat.id,
            "Отправьте отзыв обычным текстом или используйте /cancel для отмены.",
            **NO_PREVIEW,
        )
        return
    if not review_text:
        bot.send_message(message.chat.id, "Отзыв пустой. Напишите пару слов или отправьте /cancel.", **NO_PREVIEW)
        return
    if len(review_text) > 500:
        bot.send_message(message.chat.id, "Отзыв слишком длинный. Сократите его до 500 символов.", **NO_PREVIEW)
        return
    if not save_pending_review(user_id, review_text):
        awaiting_review_text.discard(user_id)
        bot.send_message(message.chat.id, "Не удалось сохранить отзыв. Откройте приглашение и попробуйте ещё раз.", **NO_PREVIEW)
        return
    awaiting_review_text.discard(user_id)
    try:
        _send_review_for_moderation(user_id, review_text)
    except Exception as error:
        debug_logger.warning(
            "REVIEW_MODERATION_NOTIFY_FAILED user_id=%s error_type=%s",
            user_id, type(error).__name__,
        )
        bot.send_message(
            message.chat.id,
            "Отзыв сохранён, но не удалось отправить его администратору. Напишите в /help, пожалуйста.",
            **NO_PREVIEW,
        )
        return
    bot.send_message(
        message.chat.id,
        "Спасибо! Отзыв отправлен администратору на проверку. После одобрения он появится в общем списке.",
        **NO_PREVIEW,
    )


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
    and m.from_user.id not in (
        set(awaiting_password_from.values())
        | set(awaiting_phone_from.values())
        | set(awaiting_code_from.values())
    ),
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

def _waiting_login_target(waiting_map, user_id):
    for target_id, owner_id in list(waiting_map.items()):
        if owner_id == user_id:
            return target_id
    return None


def _delete_auth_input(message):
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass


def _ask_for_auth_password(target_chat_id, user_id):
    _set_auth_waiter(awaiting_password_from, target_chat_id, user_id)
    bot.send_message(
        user_id,
        "🔐 MAX просит дополнительный пароль. Отправьте его следующим сообщением.\n"
        "После обработки сообщение с паролем удаляется из чата с ботом.",
        reply_markup=_build_password_keyboard(target_chat_id),
        **NO_PREVIEW,
    )


def _ask_for_auth_phone(target_chat_id, user_id, source_message=None, note=None):
    _set_auth_waiter(awaiting_phone_from, target_chat_id, user_id)
    if source_message:
        try:
            bot.edit_message_reply_markup(
                source_message.chat.id,
                source_message.message_id,
                reply_markup=None,
            )
        except Exception:
            pass
    prefix = f"{note}\n\n" if note else ""
    bot.send_message(
        user_id,
        prefix
        + "📱 Введите номер телефона, на который зарегистрирован MAX "
        "(например, +79991234567). Номер нужен только для входа и не сохраняется в базе MAX_Parser.",
        reply_markup=_build_password_keyboard(target_chat_id),
        **NO_PREVIEW,
    )


def _handle_auth_result(target_chat_id, user_id, status, data, stage):
    if status == "success":
        _complete_login(target_chat_id, user_id)
    elif status == "awaiting_code":
        _set_auth_waiter(awaiting_code_from, target_chat_id, user_id)
        bot.send_message(
            user_id,
            "📩 MAX перешёл к вводу кода. Проверьте SMS и чат «Коды авторизации» в MAX. "
            "Когда код придёт, отправьте его сюда; после обработки сообщение будет удалено.",
            reply_markup=_build_password_keyboard(target_chat_id),
            **NO_PREVIEW,
        )
    elif status == "wrong_code":
        _set_auth_waiter(awaiting_code_from, target_chat_id, user_id)
        bot.send_message(
            user_id,
            "❌ Код не подошёл. Отправьте актуальный код MAX ещё раз.",
            reply_markup=_build_password_keyboard(target_chat_id),
            **NO_PREVIEW,
        )
    elif status == "awaiting_password":
        _ask_for_auth_password(target_chat_id, user_id)
    elif status == "awaiting_phone":
        bot.send_message(
            user_id,
            "Для входа по номеру нажмите кнопку «📱 Войти по номеру телефона» и введите данные непосредственно в окне MAX.",
            reply_markup=_build_qr_keyboard(target_chat_id),
            **NO_PREVIEW,
        )
    elif status == "awaiting_human":
        _clear_auth_waiters(target_chat_id)
        session = active_logins.get(target_chat_id)
        bot.send_message(
            user_id,
            _auth_human_check_text(session, data),
            reply_markup=_build_auth_check_keyboard(target_chat_id),
            **NO_PREVIEW,
        )
    elif status == "waiting":
        session = active_logins.get(target_chat_id)
        if session:
            bot.send_message(
                user_id,
                "MAX пока не подтвердил вход. Отсканируйте QR-код и нажмите «✅ Готово, я отсканировал». "
                "Также можно выбрать вход по номеру через защищённую страницу.",
                reply_markup=_build_qr_keyboard(target_chat_id),
                **NO_PREVIEW,
            )
            return
        bot.send_message(
            user_id,
            "⚠️ Сессия входа больше не активна. Начните подключение заново, чтобы получить новый QR-код.",
            reply_markup=_build_qr_keyboard(target_chat_id),
            **NO_PREVIEW,
        )
    else:
        session = active_logins.get(target_chat_id)
        bot.send_message(
            user_id,
            f"❌ Не удалось продолжить вход ({stage}): <code>{escape_html(data or 'ошибка MAX')}</code>\n\n"
            "Попробуйте войти по QR-коду или снова открыть защищённую страницу входа по номеру.",
            parse_mode="HTML",
            reply_markup=_build_qr_keyboard(target_chat_id) if session else None,
            **NO_PREVIEW,
        )


@bot.message_handler(
    func=lambda m: m.chat.type == "private"
    and m.text is not None
    and m.from_user.id in set(awaiting_phone_from.values())
)
def handle_phone_input(message):
    target_chat_id = _waiting_login_target(awaiting_phone_from, message.from_user.id)
    if target_chat_id is None:
        return
    phone_digits = re.sub(r"\D", "", message.text or "")
    _delete_auth_input(message)
    if not 7 <= len(phone_digits) <= 15:
        bot.send_message(
            message.chat.id,
            "❌ Номер должен содержать от 7 до 15 цифр. Отправьте его ещё раз, например +79991234567.",
            reply_markup=_build_password_keyboard(target_chat_id),
            **NO_PREVIEW,
        )
        return
    session = active_logins.get(target_chat_id)
    if not session:
        _clear_auth_waiters(target_chat_id)
        bot.send_message(message.chat.id, "❌ Сессия авторизации уже закрыта.", **NO_PREVIEW)
        return
    _clear_auth_waiters(target_chat_id)
    bot.send_message(message.chat.id, "⏳ Передаю номер в открытую страницу MAX…", **NO_PREVIEW)
    status, data = session.enter_phone("+" + phone_digits)
    _handle_auth_result(target_chat_id, message.from_user.id, status, data, "номер телефона")


@bot.message_handler(
    func=lambda m: m.chat.type == "private"
    and m.text is not None
    and m.from_user.id in set(awaiting_code_from.values())
)
def handle_login_code_input(message):
    target_chat_id = _waiting_login_target(awaiting_code_from, message.from_user.id)
    if target_chat_id is None:
        return
    code_digits = re.sub(r"\D", "", message.text or "")
    _delete_auth_input(message)
    if not 4 <= len(code_digits) <= 8:
        bot.send_message(
            message.chat.id,
            "❌ Код должен содержать от 4 до 8 цифр. Отправьте его ещё раз.",
            reply_markup=_build_password_keyboard(target_chat_id),
            **NO_PREVIEW,
        )
        return
    session = active_logins.get(target_chat_id)
    if not session:
        _clear_auth_waiters(target_chat_id)
        bot.send_message(message.chat.id, "❌ Сессия авторизации уже закрыта.", **NO_PREVIEW)
        return
    _clear_auth_waiters(target_chat_id)
    bot.send_message(message.chat.id, "⏳ Проверяю код в MAX…", **NO_PREVIEW)
    status, data = session.enter_code(code_digits)
    _handle_auth_result(target_chat_id, message.from_user.id, status, data, "код")


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
    _delete_auth_input(message)
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
        _complete_login(target_chat_id, message.chat.id)
    elif status == "wrong_password":
        _set_auth_waiter(awaiting_password_from, target_chat_id, user_id)
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
@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_") or call.data.startswith("qr_") or call.data.startswith("support_close_") or call.data.startswith("review_") or call.data.startswith("reviews_") or call.data.startswith("project_retry_") or call.data in ["delete_this_msg", "cancel_step"])
def admin_callbacks(call):
    track_user(call)
    action = call.data
    is_allowed_customer_qr = False
    is_allowed_customer_review = action == "reviews_show" or action.startswith("reviews_page_")
    is_allowed_project_retry = False
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
    if not is_admin(call.from_user.id) and action.startswith(("review_write_", "review_skip_")):
        try:
            is_allowed_customer_review = int(action.rsplit("_", 1)[1]) == int(call.from_user.id)
        except (ValueError, IndexError):
            is_allowed_customer_review = False
    if not is_admin(call.from_user.id) and action.startswith("project_retry_"):
        try:
            retry_chat = get_chat_by_id(int(action.rsplit("_", 1)[1]))
            is_allowed_project_retry = bool(
                retry_chat
                and retry_chat.get("account_mode") == "project"
                and int(retry_chat.get("owner_id") or 0) == int(call.from_user.id)
            )
        except (ValueError, IndexError, TypeError):
            is_allowed_project_retry = False
    if not is_admin(call.from_user.id) and not is_allowed_customer_qr and not is_allowed_customer_review and not is_allowed_project_retry:
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
    if action.startswith("project_retry_"):
        try:
            retry_chat_id = int(action.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Некорректный ID группы", show_alert=True)
            return
        bot.answer_callback_query(call.id, "Проверяю доступ к MAX-группе")
        threading.Thread(
            target=_verify_project_chat_access,
            args=(retry_chat_id, call.from_user.id),
            daemon=True,
        ).start()
        return
    if action == "reviews_show":
        bot.answer_callback_query(call.id)
        show_approved_reviews(call.message.chat.id, call.message.message_id)
        return
    if action.startswith("reviews_page_"):
        try:
            page = int(action.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            page = 0
        bot.answer_callback_query(call.id)
        show_approved_reviews(call.message.chat.id, call.message.message_id, page)
        return
    if action.startswith("review_write_"):
        try:
            target_user = int(action.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Некорректное приглашение", show_alert=True)
            return
        if target_user != int(call.from_user.id) or not begin_review(target_user):
            bot.answer_callback_query(call.id, "Приглашение уже использовано или недоступно", show_alert=True)
            return
        awaiting_review_text.add(target_user)
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "✍️ Напишите отзыв одним текстовым сообщением (до 500 символов).\n"
            "Чтобы отменить, отправьте /cancel.",
            **NO_PREVIEW,
        )
        return
    if action.startswith("review_skip_"):
        try:
            target_user = int(action.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Некорректное приглашение", show_alert=True)
            return
        if target_user != int(call.from_user.id) or not skip_review(target_user):
            bot.answer_callback_query(call.id, "Приглашение уже использовано", show_alert=True)
            return
        bot.answer_callback_query(call.id, "Хорошо, пропускаем")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        bot.send_message(call.message.chat.id, "Хорошо, отзыв пропущен. Спасибо, что подключились!", **NO_PREVIEW)
        return
    if action.startswith("review_approve_") or action.startswith("review_reject_"):
        try:
            target_user = int(action.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Некорректный отзыв", show_alert=True)
            return
        decision = "approved" if action.startswith("review_approve_") else "rejected"
        review = moderate_review(target_user, decision)
        if not review:
            bot.answer_callback_query(call.id, "Отзыв уже обработан или не найден", show_alert=True)
            return
        bot.answer_callback_query(call.id, "Отзыв опубликован" if decision == "approved" else "Отзыв отклонён")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        if decision == "approved":
            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(types.InlineKeyboardButton("⭐ Смотреть отзывы", callback_data="reviews_show"))
            try:
                bot.send_message(target_user, "✅ Ваш отзыв одобрен и опубликован. Спасибо!", reply_markup=keyboard, **NO_PREVIEW)
            except Exception:
                pass
        else:
            try:
                bot.send_message(target_user, "Спасибо за отзыв. Администратор решил не публиковать его.", **NO_PREVIEW)
            except Exception:
                pass
        return
    if action.startswith("qr_phone_"):
        target_id = _qr_target_from_action(action)
        session = active_logins.get(target_id)
        if not session:
            bot.answer_callback_query(call.id, "Сессия авторизации не найдена", show_alert=True)
            return
        try:
            link, _record = remote_auth_service.create_link(
                session, target_id, call.from_user.id, call.message.chat.id
            )
        except RuntimeError as error:
            bot.answer_callback_query(call.id, "Нужна настройка HTTPS-ссылки сервера", show_alert=True)
            bot.send_message(
                call.message.chat.id,
                "Вход по номеру откроется через защищённую страницу удалённого браузера. "
                "Администратору нужно указать публичный HTTPS-домен и запустить сервис на сервере. "
                "Пока можно продолжить вход по QR-коду.\n\n"
                f"Причина: <code>{escape_html(str(error)[:300])}</code>",
                parse_mode="HTML",
                **NO_PREVIEW,
            )
            return
        bot.answer_callback_query(call.id, "Защищённая страница готова")
        cancel_keyboard = types.InlineKeyboardMarkup(row_width=1)
        cancel_keyboard.add(types.InlineKeyboardButton(
            "❌ Отменить авторизацию", callback_data=f"qr_cancel_{target_id}"
        ))
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=cancel_keyboard)
        except Exception:
            pass
        link_keyboard = types.InlineKeyboardMarkup(row_width=1)
        link_keyboard.add(types.InlineKeyboardButton("🔐 Открыть MAX и войти", url=link))
        bot.send_message(
            call.message.chat.id,
            "📱 <b>Вход в MAX по номеру</b>\n\n"
            "Откройте защищённую страницу и проходите вход прямо в окне MAX: выберите вход по номеру, "
            "введите номер, код и при необходимости пароль там же. Не отправляйте их боту.\n\n"
            "Ссылка одноразовая и действует 10 минут. Не пересылайте её. После успешного входа браузер "
            "закроется, а MAX-сессия сохранится на сервере. Сервер браузера обрабатывает ввод, "
            "но бот не получает номер/код через Telegram и не записывает текст ввода.",
            parse_mode="HTML",
            reply_markup=link_keyboard,
            **NO_PREVIEW,
        )
        _start_remote_login_monitor(target_id, session, call.message.chat.id)
    elif action.startswith("qr_ready_"):
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
            _complete_login(target_id, call.message.chat.id)
        elif status == "awaiting_password":
            _set_auth_waiter(awaiting_password_from, target_id, call.from_user.id)
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
        elif status == "awaiting_code":
            _set_auth_waiter(awaiting_code_from, target_id, call.from_user.id)
            bot.send_message(
                call.message.chat.id,
                "📩 MAX перешёл к вводу кода. Проверьте SMS и чат «Коды авторизации» в MAX. "
                "Когда код придёт, отправьте его следующим сообщением; после обработки оно будет удалено.",
                reply_markup=_build_password_keyboard(target_id),
                **NO_PREVIEW,
            )
        elif status == "awaiting_human":
            bot.send_message(
                call.message.chat.id,
                _auth_human_check_text(session, data),
                reply_markup=_build_auth_check_keyboard(target_id),
                **NO_PREVIEW,
            )
        elif status == "awaiting_phone":
            bot.send_message(
                call.message.chat.id,
                "MAX показывает форму ввода номера. Чтобы пройти вход самостоятельно, выберите «Войти по номеру телефона» "
                "и откройте защищённую страницу. Номер и код вводите только там, не в Telegram.",
                reply_markup=_build_qr_keyboard(target_id),
                **NO_PREVIEW,
            )
        elif status == "waiting":
            bot.send_message(
                call.message.chat.id,
                "⚠️ MAX пока не подтвердил вход. Убедитесь, что QR-код отсканирован в приложении MAX, "
                "и нажмите кнопку ещё раз. Либо откройте защищённую страницу для входа по номеру телефона.",
                reply_markup=_build_qr_keyboard(target_id),
                **NO_PREVIEW,
            )
        else:
            bot.send_message(
                call.message.chat.id,
                f"❌ Ошибка проверки: <code>{data}</code>",
                parse_mode='HTML', **NO_PREVIEW
            )
    elif action.startswith("qr_refresh_"):
        target_id = _qr_target_from_action(action)
        _clear_auth_waiters(target_id)
        bot.answer_callback_query(call.id, "🔄 Обновляю QR-код...")
        session = active_logins.get(target_id)
        if not session:
            bot.send_message(call.message.chat.id, "❌ Сессия авторизации не найдена", **NO_PREVIEW)
            return
        chat = get_chat_by_id(target_id) if target_id != "project_account" else None
        title = "аккаунта проекта" if target_id == "project_account" else chat["title"]
        status, data = session.refresh()
        if status == "success":
            _complete_login(target_id, call.message.chat.id)
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
            remote_auth_service.revoke_session(session)
            try:
                session.close()
            except:
                pass
        _clear_auth_waiters(target_id)
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
    elif action.startswith("admin_admins_page_"):
        show_admins_list(
            call.message.chat.id, call.message.message_id,
            int(action.rsplit("_", 1)[1]),
        )
    elif action == "admin_users":
        show_users_list(call.message.chat.id, call.message.message_id)
    elif action.startswith("admin_users_page_"):
        show_users_list(
            call.message.chat.id, call.message.message_id,
            int(action.rsplit("_", 1)[1]),
        )
    elif action == "admin_free_subscriptions":
        show_free_subscription_users(call.message.chat.id, call.message.message_id)
    elif action.startswith("admin_free_sub_page_"):
        show_free_subscription_users(
            call.message.chat.id, call.message.message_id,
            int(action.rsplit("_", 1)[1]),
        )
    elif action.startswith("admin_free_sub_user_"):
        try:
            target_user_id = int(action.rsplit("_", 1)[1])
        except ValueError:
            bot.answer_callback_query(call.id, "Не удалось определить пользователя", show_alert=True)
            return
        show_free_subscription_options(
            call.message.chat.id, call.message.message_id, target_user_id
        )
    elif action.startswith("admin_free_sub_grant_"):
        try:
            parts = action.split("_")
            plan_code, days_value, user_id_value = parts[4], parts[5], parts[6]
            target_user_id = int(user_id_value)
            days = int(days_value)
            if plan_code not in {"self", "project"} or days not in {30, 90, 365}:
                raise ValueError("invalid subscription choice")
            granted = grant_subscription(target_user_id, plan_code, days)
        except (ValueError, TypeError, IndexError) as error:
            bot.answer_callback_query(call.id, "Не удалось выдать подписку", show_alert=True)
            debug_logger.warning("ADMIN_FREE_SUBSCRIPTION_INVALID admin_id=%s error=%s", call.from_user.id, log_preview(error, 120))
            return
        except Exception as error:
            bot.answer_callback_query(call.id, "Ошибка базы данных", show_alert=True)
            debug_logger.exception("ADMIN_FREE_SUBSCRIPTION_FAILED admin_id=%s user_id=%s", call.from_user.id, target_user_id)
            return

        active_until = granted["active_until"].astimezone(timezone.utc)
        plan_label = "ваш аккаунт MAX" if plan_code == "self" else "аккаунт проекта MAX"
        delivery_note = ""
        try:
            bot.send_message(
                target_user_id,
                "🎁 <b>Вам выдан бесплатный доступ к MAX_Parser</b>\n\n"
                f"Срок: <b>{days} дней</b> · способ: <b>{plan_label}</b>.\n"
                f"Подписка действует до <b>{active_until:%d.%m.%Y %H:%M} UTC</b>.\n\n"
                "Подключить или посмотреть чаты можно через /connect и /mychats.",
                parse_mode="HTML",
                **NO_PREVIEW,
            )
        except Exception:
            delivery_note = "\nУведомление пользователю не доставлено — возможно, он ещё не запускал бота."
        text = (
            "✅ <b>Бесплатная подписка выдана</b>\n\n"
            f"Пользователь: <code>{target_user_id}</code>\n"
            f"Срок: {days} дней · {plan_label}\n"
            f"Действует до: <b>{active_until:%d.%m.%Y %H:%M} UTC</b>."
            f"{delivery_note}"
        )
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_back"))
        safe_edit_message(text, call.message.chat.id, call.message.message_id, reply_markup=keyboard, **NO_PREVIEW)
    elif action == "admin_parse_diagnostics":
        show_parse_diagnostics(call.message.chat.id, call.message.message_id)
    elif action.startswith("admin_parse_diag_"):
        show_parse_diagnostics(
            call.message.chat.id, call.message.message_id,
            int(action.rsplit("_", 1)[1]),
        )
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
        show_chat_send_selector(call.message.chat.id, call.message.message_id)
    elif action.startswith("admin_send_chats_page_"):
        show_chat_send_selector(
            call.message.chat.id, call.message.message_id,
            int(action.rsplit("_", 1)[1]),
        )
    elif action == "admin_send_to_user":
        show_user_send_selector(call.message.chat.id, call.message.message_id)
    elif action.startswith("admin_send_users_page_"):
        show_user_send_selector(
            call.message.chat.id, call.message.message_id,
            int(action.rsplit("_", 1)[1]),
        )
    elif action == "admin_send_user_manual":
        prompt = bot.send_message(
            call.message.chat.id,
            "Отправьте ID пользователя. Пример: <code>123456789</code>",
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard(),
            **NO_PREVIEW,
        )
        bot.register_next_step_handler(prompt, process_send_to_user_id)
    elif action == "admin_send_chat_manual":
        prompt = bot.send_message(
            call.message.chat.id,
            "Отправьте ID чата. Пример: <code>-1001234567890</code>",
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard(),
            **NO_PREVIEW,
        )
        bot.register_next_step_handler(prompt, process_send_to_chat_id)
    elif action.startswith("admin_send_user_pick_"):
        _prompt_send_to_user(
            call.message.chat.id, int(action.rsplit("_", 1)[1])
        )
    elif action.startswith("admin_send_chat_pick_"):
        _prompt_send_to_chat(
            call.message.chat.id, int(action.rsplit("_", 1)[1])
        )
    elif action.startswith("admin_user_send_"):
        _prompt_send_to_user(
            call.message.chat.id, int(action.rsplit("_", 1)[1])
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
                if chat.get("account_mode") == "project":
                    toggle_chat(target_id, False)
                    bot.answer_callback_query(call.id, "Проверяю доступ аккаунта проекта…")
                    threading.Thread(
                        target=_verify_project_chat_access,
                        args=(target_id, call.message.chat.id),
                        daemon=True,
                    ).start()
                    show_chat_details(call.message.chat.id, call.message.message_id, target_id)
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
    elif action.startswith("admin_ask_delete_") and not action.startswith("admin_ask_delete_admin_"):
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
    elif action.startswith("admin_yes_delete_") and not action.startswith("admin_yes_delete_admin_"):
        target_id = int(action.split("_")[-1])
        _delete_chat_everywhere(target_id)
        bot.answer_callback_query(call.id, "🗑 Чат удалён", show_alert=True)
        show_chats_list(call.message.chat.id, call.message.message_id)
    elif action.startswith("admin_ask_delete_admin_"):
        target_id = int(action.split("_")[-1])
        if target_id == ADMIN_ID:
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
        if target_id == ADMIN_ID:
            bot.answer_callback_query(call.id, "⛔️ Нельзя удалить главного администратора!", show_alert=True)
            show_admins_list(call.message.chat.id, call.message.message_id)
            return
        if not delete_admin(target_id):
            bot.answer_callback_query(call.id, "Админ уже удалён или не найден", show_alert=True)
            show_admins_list(call.message.chat.id, call.message.message_id)
            return
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

@bot.callback_query_handler(func=lambda call: call.data.startswith("mychat_"))
def my_chat_delete_callbacks(call):
    track_user(call)
    if not call.message or call.message.chat.type != "private":
        bot.answer_callback_query(call.id, "Откройте список в личном чате с ботом.", show_alert=True)
        return
    try:
        target_chat_id = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "Некорректный ID группы", show_alert=True)
        return
    chat = get_chat_by_id(target_chat_id)
    if not chat:
        bot.answer_callback_query(call.id, "Группа уже удалена", show_alert=True)
        _show_my_chats(call.message.chat.id, call.from_user.id, call.message.message_id)
        return
    if not _chat_delete_allowed(call.from_user.id, chat):
        bot.answer_callback_query(call.id, "Удалить группу может только её владелец или администратор.", show_alert=True)
        return
    if call.data.startswith("mychat_delete_"):
        cleanup_details = (
            "Бот покинет Telegram-группу; её настройки, история парсинга и личная сессия будут удалены."
            if chat.get("account_mode") != "project"
            else "Бот покинет Telegram-группу; её настройки и история удалятся. Общий аккаунт проекта останется для других групп."
        )
        text = (
            "⚠️ <b>Удалить подключённую группу?</b>\n\n"
            f"<b>{escape_html(chat.get('title') or 'Группа')}</b>\n"
            f"🆔 <code>{target_chat_id}</code>\n\n"
            f"{cleanup_details}"
        )
        keyboard = types.InlineKeyboardMarkup(row_width=2)
        keyboard.row(
            types.InlineKeyboardButton("🗑 Удалить", callback_data=f"mychat_confirm_delete_{target_chat_id}", style="danger"),
            types.InlineKeyboardButton("↩️ Отмена", callback_data=f"mychat_cancel_delete_{target_chat_id}")
        )
        safe_edit_message(text, call.message.chat.id, call.message.message_id, reply_markup=keyboard, parse_mode="HTML", **NO_PREVIEW)
        bot.answer_callback_query(call.id)
    elif call.data.startswith("mychat_cancel_delete_"):
        _show_my_chats(call.message.chat.id, call.from_user.id, call.message.message_id)
        bot.answer_callback_query(call.id, "Удаление отменено")
    elif call.data.startswith("mychat_confirm_delete_"):
        if _delete_chat_everywhere(target_chat_id):
            bot.answer_callback_query(call.id, "Группа удалена")
        else:
            bot.answer_callback_query(call.id, "Группа уже удалена", show_alert=True)
        _show_my_chats(call.message.chat.id, call.from_user.id, call.message.message_id)


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.message:
        if call.data == "about_bot":
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text='''
<b>MAX_Parser — удобный мост между MAX и Telegram</b>\n
<i>Он помогает автоматически пересылать новые сообщения из выбранного чата MAX в Telegram. Ты не пропустишь важные объявления, обсуждения и файлы, даже если чаще пользуешься Telegram.</i>\n
<blockquote><b>Что умеет бот:</b>\n
• Пересылать новые сообщения из подключённого чата MAX.
• Отправлять текст, фото, видео, документы и другие поддерживаемые вложения.
• Работать автоматически: не нужно постоянно открывать MAX и проверять группу вручную.
• Помогать не терять важную информацию из школьных, рабочих или дружеских чатов.
<b>Почему это удобно:</b>\n
• Все важные сообщения приходят в Telegram.
• Не нужно каждый раз переключаться между приложениями.
• Не нужно постоянно включать и выключать VPN.
• Ты сам выбираешь группу и настраиваешь, откуда получать сообщения.</blockquote>\n
<blockquote><b>Выберите способ подключения:</b>\n
• <b>Ваш аккаунт MAX:</b> вы входите в MAX, а бот читает выбранный вами чат.\n
• <b>Аккаунт проекта:</b> добавьте его в исходную группу или канал. В закрытом источнике владелец может подтвердить вступление.\n
В обоих случаях бот пересылает сообщения только из источников, к которым у выбранного аккаунта есть доступ.</blockquote>

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
                    "Бот будет читать только выбранный вами чат. Авторизация сохраняется для этого подключения.</blockquote>\n"
                    "<blockquote><b>2️⃣ Аккаунт проекта</b>\n"
                    f"Добавьте аккаунт проекта <code>{MAX_PROJECT_ACCOUNT_PHONE}</code> в нужный MAX-чат. "
                    "После отправки ссылки бот проверит, видит ли он сообщения.</blockquote>\n\n"
                    f"Можно подключить до {MAX_CUSTOMER_CHATS} чатов. Чтобы подключить больше, напишите в поддержку через /help.\n\n"
                    "<i>Пошаговый порядок подключения — в инструкции ниже.</i>"
                ),
                reply_markup=parser_keyboard(),
                parse_mode="HTML",
                **NO_PREVIEW,
            )
        elif call.data in ("renew_self", "renew_project"):
            if not call.message or call.message.chat.type != "private":
                bot.answer_callback_query(
                    call.id,
                    "Продление доступно в личном чате с ботом.",
                    show_alert=True,
                )
                return
            mode = "self" if call.data == "renew_self" else "project"
            track_user(call)
            bot.answer_callback_query(call.id, "Готовлю счёт")
            _send_subscription_invoice(call.from_user.id, mode)
        elif call.data in ("my_acc_250", "not_my_acc_250"):
            if call.message.chat.type != "private":
                bot.answer_callback_query(
                    call.id,
                    "Откройте личный чат с ботом, чтобы настроить подключение.",
                    show_alert=True,
                )
                return
            mode = "self" if call.data == "my_acc_250" else "project"
            track_user(call)
            if len(get_user_chats(call.from_user.id)) >= MAX_CUSTOMER_CHATS:
                bot.answer_callback_query(call.id, "Достигнут лимит подключений", show_alert=True)
                bot.send_message(call.from_user.id, _chat_limit_notice(), **NO_PREVIEW)
                return
            if _user_needs_subscription(call.from_user.id):
                bot.answer_callback_query(call.id, "Сначала оформите подписку")
                _send_subscription_invoice(call.from_user.id, mode)
                return
            pending_setup_modes[call.from_user.id] = mode
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
                    "• Ссылка на чат, группу или канал MAX.\n\n"
                    "<b>Шаг 1 · Выберите Telegram-группу, в которую будут приходить сообщения из MAX</b>\n"
                    "После нажатия на кнопку «Подключить MAX_Parser» выберите способ работы с MAX. Затем добавьте этого бота "
                    "в Telegram-группу, куда должны приходить сообщения. После появления бота отправьте в группе /bind. "
                    "Если бот уже добавлен, то просто напишите /bind. Боту нужно разрешить отправлять сообщения.\n\n"
                    "<b>Шаг 2 · Укажите источник MAX</b>\n"
                    "Откройте <a href=\"https://web.max.ru\">web.max.ru</a>, перейдите в нужный чат, группу или канал "
                    "и скопируйте ссылку из меню. Отправьте её боту в личном чате.\n\n"
                    "<b>Вариант 1 · Ваш аккаунт MAX</b>\n"
                    "После отправки ссылки бот пришлёт QR-код. Отсканируйте его в приложении MAX и нажмите "
                    "«Готово, я отсканировал». Если MAX запросит пароль двухэтапной проверки, отправьте его боту в этой "
                    "личной переписке; он нужен только для входа в MAX. После успешного входа парсер включится автоматически 🎉.\n"
                    "Вместо QR откройте защищённую ссылку с экрана входа и самостоятельно выберите вход по номеру. "
                    "Номер, код и пароль вводятся в удалённой странице MAX, не в Telegram. Сервер MAX_Parser технически "
                    "обрабатывает ввод, но приложение не сохраняет его и не пересылает в Telegram.\n\n"
                    "<b>Вариант 2 · Через готовый аккаунт MAX</b>\n"
                    f"Попросите владельца или администратора добавить в источник аккаунт <code>{MAX_PROJECT_ACCOUNT_PHONE}</code>. "
                    "Для закрытого источника можно отправить боту ссылку-приглашение: он попробует вступить. "
                    "Если MAX попросит одобрение, его должен дать владелец или администратор. "
                    "Для канала достаточно обычного доступа к публикациям — права администратора не нужны.\n"
                    "В личном чате с ботом выберите вариант 2, добавьте бота в Telegram-группу назначения и отправьте там /bind. "
                    "Затем пришлите ссылку на источник MAX. Бот проверит, может ли аккаунт проекта открыть сообщения, "
                    "и включит пересылку только после успешной проверки. Для закрытых источников владелец должен разрешить доступ.\n\n"
                    f"К одному аккаунту можно подключить до {MAX_CUSTOMER_CHATS} чатов. Чтобы подключить больше, напишите в поддержку через /help.\n\n"
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
        if remote_auth_service.enabled:
            try:
                if remote_auth_service.start():
                    print("🔐 Сервис защищённого входа по номеру запущен на loopback")
            except OSError as error:
                debug_logger.error("REMOTE_AUTH_SERVICE_START_FAILED error_type=%s", type(error).__name__)
                print("⚠️ Сервис входа по номеру не запустился; QR-вход остаётся доступен")
        else:
            print("ℹ️ Вход по номеру ждёт REMOTE_AUTH_BASE_URL с HTTPS-доменом; QR-вход доступен")
        threading.Thread(target=background_parser, daemon=True).start()
        if PAYMENTS_ENABLED:
            threading.Thread(target=subscription_reminder_loop, daemon=True).start()
        print("✅ Запуск polling...")
        bot.infinity_polling(none_stop=True, timeout=60)
    except KeyboardInterrupt:
        print(" Остановлен пользователем")
        save_message_cache()
