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
import uuid
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
try:
    BOT_USER_ID = int(os.getenv("BOT_TOKEN", "").split(":", 1)[0])
    delete_user(BOT_USER_ID)
except (TypeError, ValueError):
    BOT_USER_ID = None
except Exception as error:
    BOT_USER_ID = None
    debug_logger.warning(
        "BOT_USER_TABLE_CLEANUP_FAILED error_type=%s", type(error).__name__
    )
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
auth_login_review_events = {}
auth_qr_message_ids = {}
awaiting_password_from = {}
awaiting_phone_from = {}
awaiting_code_from = {}
support_sessions = set(get_open_support_sessions())


def _video_upload_details(media: dict, post: dict) -> dict:
    """Build Telegram video duration and thumbnail metadata from the actual file."""
    data = media.get("bytes")
    temp_path = str(media.get("temp_path") or "")
    if (not isinstance(data, (bytes, bytearray)) or not data) and not (temp_path and os.path.isfile(temp_path)):
        return {}
    result = {}
    try:
        duration = float(media.get("duration") or post.get("media_duration_seconds") or 0)
        if duration > 0:
            result["duration"] = max(1, int(round(duration)))
    except (TypeError, ValueError):
        pass

    poster_data = media.get("thumbnail_bytes")
    if isinstance(poster_data, (bytes, bytearray)) and poster_data:
        try:
            from PIL import Image, ImageOps
            with Image.open(io.BytesIO(bytes(poster_data))) as source:
                poster = ImageOps.exif_transpose(source).convert("RGB")
                poster.thumbnail((320, 320))
                thumbnail = io.BytesIO()
                poster.save(thumbnail, format="JPEG", quality=85, optimize=True)
            thumbnail_bytes = thumbnail.getvalue()
            if thumbnail_bytes.startswith(b"\xff\xd8\xff") and len(thumbnail_bytes) <= 200_000:
                result["thumbnail"] = thumbnail_bytes
        except Exception as exc:
            debug_logger.info(
                "VIDEO_POSTER_CONVERSION_FAILED source_id=%s error=%s",
                post.get("source_id") or post.get("idx") or "?", log_preview(exc, 160),
            )

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
        input_source = temp_path if temp_path and os.path.isfile(temp_path) else "pipe:0"
        probe = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-i", input_source, "-frames:v", "1",
                "-vf", "scale=320:320:force_original_aspect_ratio=decrease,setsar=1",
                "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            ],
            input=bytes(data) if input_source == "pipe:0" else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
        if "thumbnail" not in result and thumbnail.startswith(b"\xff\xd8\xff") and len(thumbnail) < 200_000:
            result["thumbnail"] = thumbnail
    except Exception as exc:
        debug_logger.warning(
            "VIDEO_METADATA_PROBE_FAILED source_id=%s error=%s",
            post.get("source_id") or post.get("idx") or "?", log_preview(exc, 180),
        )
    return result


def _media_is_mp4_file(media: dict) -> bool:
    """True when the downloaded payload is an MPEG-4 container Telegram can play inline."""
    data = media.get("bytes")
    if isinstance(data, (bytes, bytearray)) and len(data) >= 12:
        return bytes(data[4:8]) == b"ftyp"
    temp_path = str(media.get("temp_path") or "")
    if temp_path and os.path.isfile(temp_path):
        try:
            with open(temp_path, "rb") as source:
                header = source.read(12)
            return len(header) >= 12 and header[4:8] == b"ftyp"
        except OSError:
            return False
    return False


def _normalize_inline_video_media(media: dict, post: dict) -> bool:
    """Promote an MP4 hidden in a generic file card to Telegram's playable video type."""
    if media.get("type") not in {"document", "file", "unknown"} or not _media_is_mp4_file(media):
        return False
    media["type"] = "video_note" if post.get("video_note_hint") else "video"
    filename = str(media.get("filename") or "").strip()
    stem, suffix = os.path.splitext(filename)
    if suffix.lower() != ".mp4" or re.fullmatch(r"\d+", stem):
        media["filename"] = "video.mp4"
    if not media.get("content_type") or "video" not in str(media.get("content_type")).lower():
        media["content_type"] = "video/mp4"
    debug_logger.info(
        "TELEGRAM_MEDIA_RECLASSIFIED source_id=%s media_type=video filename=%s spooled=%s",
        post.get("source_id") or post.get("idx") or "?", media.get("filename"),
        bool(media.get("temp_path")),
    )
    return True


def _telegram_media_input(media: dict, post: dict, caption: str = ""):
    """Build a photo/video album item while keeping the original uploaded bytes."""
    file_obj = _media_to_bytesio(media)
    parse_mode = "HTML" if caption else None
    if media.get("type") == "image":
        return types.InputMediaPhoto(file_obj, caption=caption or None, parse_mode=parse_mode)

    details = _video_upload_details(media, post)
    thumbnail = details.pop("thumbnail", None)
    if thumbnail:
        thumbnail_file = io.BytesIO(thumbnail)
        thumbnail_file.name = "video-thumbnail.jpg"
        details["thumbnail"] = thumbnail_file
    details["supports_streaming"] = True
    return types.InputMediaVideo(
        file_obj, caption=caption or None, parse_mode=parse_mode, **details
    )
support_message_routes = get_support_message_routes()
support_state_lock = threading.RLock()
PARSE_DIAGNOSTICS = {}
PARSE_DIAGNOSTICS_LOCK = threading.RLock()
CHAT_LIFECYCLE_LOCK = threading.RLock()
pending_setup_modes = {}
customer_setup_state = {}
project_access_message_ids = {}
project_invite_pending = {}
project_admin_source_prompts = {}
awaiting_review_text = {}
remote_auth_monitors = set()
remote_auth_monitor_lock = threading.Lock()
SUPPORT_END_TEXT = "✅ Завершить чат с поддержкой"
MAX_PROJECT_ACCOUNT_PHONE = "+7 939 752 85 95"
try:
    MAX_CONCURRENT_CHAT_PARSERS = max(
        1, min(2, int(os.getenv("MAX_CONCURRENT_CHAT_PARSERS", "2")))
    )
except (TypeError, ValueError):
    MAX_CONCURRENT_CHAT_PARSERS = 2
PARSER_CYCLE_INTERVAL_SECONDS = 30
try:
    TELEGRAM_MIN_SEND_INTERVAL_SECONDS = max(
        0.05, float(os.getenv("TELEGRAM_MIN_SEND_INTERVAL_SECONDS", "0.1"))
    )
except (TypeError, ValueError):
    TELEGRAM_MIN_SEND_INTERVAL_SECONDS = 0.1

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

class _NamedUploadFile(io.IOBase):
    """File-backed Telegram upload stream with the source filename preserved."""
    def __init__(self, path: str, filename: str):
        super().__init__()
        self._stream = open(path, "rb")
        self.name = filename or os.path.basename(path)

    def read(self, size=-1):
        return self._stream.read(size)

    def seek(self, offset, whence=io.SEEK_SET):
        return self._stream.seek(offset, whence)

    def tell(self):
        return self._stream.tell()

    def fileno(self):
        return self._stream.fileno()

    def readable(self):
        return True

    def seekable(self):
        return True

    def close(self):
        try:
            self._stream.close()
        finally:
            super().close()


def _media_to_bytesio(media: dict) -> io.IOBase:
    temp_path = str(media.get("temp_path") or "")
    filename = str(media.get("filename") or "")
    if temp_path and os.path.isfile(temp_path):
        file_obj = _NamedUploadFile(temp_path, filename)
        ctx = getattr(_send_context, "current", None)
        if ctx is not None:
            ctx.setdefault("upload_files", []).append(file_obj)
        return file_obj
    data = media.get("bytes")
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError(f"Медиа {media.get('type', 'unknown')} не содержит bytes")
    file_obj = io.BytesIO(bytes(data))
    if filename:
        file_obj.name = filename
    file_obj.seek(0)
    return file_obj

def _media_already_sent(media: dict, chat_id: int) -> bool:
    data = media.get("bytes")
    media_hash = media.get("hash") or get_media_hash(data=data)
    return not is_new_media(data=data, chat_id=chat_id, media_hash=media_hash)


def _cleanup_post_media(post: dict) -> None:
    for media in post.get("media_files", []) or []:
        path = str(media.get("temp_path") or "") if isinstance(media, dict) else ""
        if path:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                debug_logger.warning("MEDIA_SPOOL_CLEANUP_FAILED path=%s error=%s", path, log_preview(exc, 160))

_telegram_last_send = 0.0
_telegram_last_send_by_chat = {}
_telegram_group_send_times = {}
_telegram_send_lock = threading.Lock()
_send_context = threading.local()
_POLL_MESSAGE_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sent_poll_messages.json")
_SENT_PARSER_MESSAGES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sent_parser_messages.json")
_poll_message_cache = {}
_poll_message_cache_lock = threading.Lock()
_sent_parser_messages = []
_sent_parser_messages_lock = threading.RLock()
_SENT_MESSAGE_DELETE_WINDOW = timedelta(hours=47)

def _load_sent_parser_messages():
    global _sent_parser_messages
    try:
        with open(_SENT_PARSER_MESSAGES_FILE, "r", encoding="utf-8") as records_file:
            value = json.load(records_file)
        _sent_parser_messages = [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    except (OSError, ValueError, TypeError):
        _sent_parser_messages = []

def _save_sent_parser_messages_locked():
    temporary_path = _SENT_PARSER_MESSAGES_FILE + ".tmp"
    try:
        with open(temporary_path, "w", encoding="utf-8") as records_file:
            json.dump(_sent_parser_messages, records_file, ensure_ascii=False)
        os.replace(temporary_path, _SENT_PARSER_MESSAGES_FILE)
    except OSError as exc:
        debug_logger.warning(
            "SENT_PARSER_MESSAGES_SAVE_FAILED path=%s error=%s",
            _SENT_PARSER_MESSAGES_FILE, log_preview(exc),
        )

def _post_media_hashes(post: dict) -> list[str]:
    hashes = []
    for media in post.get("media_files", []) or []:
        if not isinstance(media, dict):
            continue
        media_hash = str(media.get("hash") or media.get("content_hash") or "").strip()
        if not media_hash:
            data = media.get("bytes")
            if isinstance(data, (bytes, bytearray)) and data:
                media_hash = get_media_hash(data=data)
            elif media.get("url"):
                media_hash = get_media_hash(url=str(media.get("url")))
        if media_hash and media_hash not in hashes:
            hashes.append(media_hash)
    return hashes


def _post_time_key(value) -> str:
    raw = re.sub(r"\s+", " ", str(value or "").strip()).upper().replace(".", "")
    for time_format in ("%H:%M", "%I:%M %p", "%I:%M%p"):
        try:
            return datetime.strptime(raw, time_format).strftime("%H:%M")
        except ValueError:
            continue
    return raw.casefold()


def _record_parser_sent_post(chat_id: int, post: dict, message_ids, partial=False, captioned=False, text_sent=False):
    ids = []
    for value in message_ids or []:
        try:
            message_id = int(value)
        except (TypeError, ValueError):
            continue
        if message_id not in ids:
            ids.append(message_id)
    if not ids:
        return

    preview = re.sub(r"<[^>]*>", " ", str(post.get("text") or ""))
    preview = " ".join(preview.split())[:100]
    if not preview:
        if post.get("poll"):
            preview = "Опрос"
        elif (post.get("contact") or {}).get("is_contact"):
            preview = "Контакт"
        elif post.get("location"):
            preview = "Геопозиция"
        else:
            preview = "Медиа или вложение"
    record = {
        "record_id": uuid.uuid4().hex[:12],
        "chat_id": int(chat_id),
        "message_ids": ids,
        "source_id": str(post.get("source_id") or post.get("idx") or "")[:80],
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "preview": preview,
        "partial": bool(partial),
        "captioned": bool(captioned),
        "text_sent": bool(text_sent),
        "has_media": bool(post.get("media_files")),
        "media_hashes": _post_media_hashes(post),
        "source_id_is_index": bool(post.get("source_id_is_index")),
        "name": str(post.get("name") or "")[:100],
        "date": str(post.get("date") or "")[:32],
        "time": str(post.get("time") or "")[:24],
    }
    with _sent_parser_messages_lock:
        _sent_parser_messages.append(record)
        per_chat = []
        for item in _sent_parser_messages:
            try:
                if int(item.get("chat_id") or 0) == int(chat_id):
                    per_chat.append(item)
            except (TypeError, ValueError):
                continue
        keep_ids = {item.get("record_id") for item in per_chat[-50:]}
        retained = []
        for item in _sent_parser_messages:
            try:
                same_chat = int(item.get("chat_id") or 0) == int(chat_id)
            except (TypeError, ValueError):
                same_chat = False
            if not same_chat or item.get("record_id") in keep_ids:
                retained.append(item)
        _sent_parser_messages[:] = retained[-1000:]
        _save_sent_parser_messages_locked()


def _find_uncaptioned_media_record(chat_id: int, post: dict):
    source_id = str(post.get("source_id") or post.get("idx") or "").strip()
    if not source_id or not str(post.get("text") or "").strip():
        return None
    post_hashes = set(_post_media_hashes(post))
    post_is_index = bool(post.get("source_id_is_index"))
    with _sent_parser_messages_lock:
        for item in reversed(_sent_parser_messages):
            try:
                same_chat = int(item.get("chat_id") or 0) == int(chat_id)
            except (TypeError, ValueError):
                same_chat = False
            if not same_chat or str(item.get("source_id") or "").strip() != source_id:
                continue
            # Older records predate the explicit caption flag. Their fallback
            # preview means the original delivery contained media without text.
            captioned = item.get("captioned")
            if captioned is True or item.get("text_sent") is True:
                continue
            if captioned is None and item.get("preview") != "Медиа или вложение":
                continue
            record_is_index = bool(item.get("source_id_is_index"))
            if not item.get("has_media") and not item.get("media_hashes"):
                # Pre-migration records used this preview for media-only sends.
                # Stable MAX IDs are sufficient to try attaching the late caption.
                if record_is_index or post_is_index or item.get("preview") != "Медиа или вложение":
                    continue
            if post_is_index or record_is_index:
                old_hashes = set(str(value) for value in (item.get("media_hashes") or []) if value)
                has_matching_media = bool(old_hashes.intersection(post_hashes))
                old_date = str(item.get("date") or "").strip()
                new_date = str(post.get("date") or "").strip()
                old_time = str(item.get("time") or "").strip()
                new_time = str(post.get("time") or "").strip()
                old_name = str(item.get("name") or "").strip().casefold()
                new_name = str(post.get("name") or "").strip().casefold()
                if old_hashes and post_hashes and not has_matching_media:
                    continue
                if old_date and new_date and old_date != new_date:
                    continue
                if old_time and new_time and _post_time_key(old_time) != _post_time_key(new_time):
                    continue
                if old_name and new_name and old_name != new_name:
                    continue
                if not has_matching_media and not (old_date and new_date and old_time and new_time):
                    continue
            return dict(item)
    return None


def _mark_parser_record_captioned(record_id: str, post: dict):
    preview = re.sub(r"<[^>]*>", " ", str(post.get("text") or ""))
    preview = " ".join(preview.split())[:100]
    with _sent_parser_messages_lock:
        for index, item in enumerate(_sent_parser_messages):
            if item.get("record_id") != record_id:
                continue
            updated = dict(item)
            updated["captioned"] = True
            updated["text_sent"] = False
            updated["preview"] = preview or updated.get("preview") or "Медиа или вложение"
            updated["name"] = str(post.get("name") or updated.get("name") or "")[:100]
            updated["date"] = str(post.get("date") or updated.get("date") or "")[:32]
            updated["time"] = str(post.get("time") or updated.get("time") or "")[:24]
            media_hashes = _post_media_hashes(post)
            if media_hashes:
                updated["media_hashes"] = media_hashes
            _sent_parser_messages[index] = updated
            _save_sent_parser_messages_locked()
            return True
    return False


def _mark_parser_record_text_sent(record_id: str, post: dict, message_ids):
    preview = re.sub(r"<[^>]*>", " ", str(post.get("text") or ""))
    preview = " ".join(preview.split())[:100]
    with _sent_parser_messages_lock:
        for index, item in enumerate(_sent_parser_messages):
            if item.get("record_id") != record_id:
                continue
            updated = dict(item)
            ids = [int(value) for value in (updated.get("message_ids") or []) if str(value).lstrip("-").isdigit()]
            for value in message_ids or []:
                try:
                    message_id = int(value)
                except (TypeError, ValueError):
                    continue
                if message_id not in ids:
                    ids.append(message_id)
            updated["message_ids"] = ids
            updated["text_sent"] = True
            updated["preview"] = preview or updated.get("preview") or "Медиа или вложение"
            _sent_parser_messages[index] = updated
            _save_sent_parser_messages_locked()
            return True
    return False

def _sent_record_is_deletable(record, now=None):
    try:
        sent_at = datetime.fromisoformat(str(record.get("sent_at") or "").replace("Z", "+00:00"))
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return timedelta(0) <= current - sent_at <= _SENT_MESSAGE_DELETE_WINDOW
    except (TypeError, ValueError):
        return False

def _find_sent_parser_record(record_id):
    with _sent_parser_messages_lock:
        return next(
            (dict(item) for item in _sent_parser_messages if item.get("record_id") == record_id),
            None,
        )

def _finish_sent_parser_record(record_id, remaining_ids):
    with _sent_parser_messages_lock:
        for index, item in enumerate(_sent_parser_messages):
            if item.get("record_id") != record_id:
                continue
            if remaining_ids:
                updated = dict(item)
                updated["message_ids"] = [int(value) for value in remaining_ids]
                _sent_parser_messages[index] = updated
            else:
                _sent_parser_messages.pop(index)
            _save_sent_parser_messages_locked()
            return

_load_sent_parser_messages()

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


def _telegram_call_has_media_caption(method_name, args, kwargs) -> bool:
    if method_name not in {"send_photo", "send_video", "send_document", "send_animation", "send_media_group"}:
        return False
    if str(kwargs.get("caption") or "").strip():
        return True
    media = kwargs.get("media")
    if media is None and method_name == "send_media_group" and len(args) > 1:
        media = args[1]
    if isinstance(media, (list, tuple)):
        return any(
            str(getattr(item, "caption", "") or (item.get("caption") if isinstance(item, dict) else "") or "").strip()
            for item in media
        )
    return False

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
    method_name = getattr(fn, "__name__", "")
    destination = kwargs.get("chat_id")
    if destination is None and method_name.startswith("send_") and args:
        destination = args[0]
    try:
        destination_key = int(destination) if destination is not None else None
    except (TypeError, ValueError):
        destination_key = None
    send_cost = 1
    if method_name == "send_media_group":
        media_group = kwargs.get("media")
        if media_group is None and len(args) > 1:
            media_group = args[1]
        send_cost = max(1, len(media_group or []))
    attempt = 0
    while attempt < 4:
        wait = 0.0
        reserved = False
        with _telegram_send_lock:
            now = time.monotonic()
            wait = TELEGRAM_MIN_SEND_INTERVAL_SECONDS - (now - _telegram_last_send)
            if method_name.startswith("send_") and destination_key is not None:
                # Telegram allows at most 20 messages/minute in a group and
                # recommends at most one message/second in a private chat.
                last_chat_send = _telegram_last_send_by_chat.get(destination_key, 0.0)
                if destination_key < 0:
                    wait = max(wait, 3.0 - (now - last_chat_send))
                    recent_times = [
                        sent_at for sent_at in _telegram_group_send_times.get(destination_key, [])
                        if now - sent_at < 60
                    ]
                    _telegram_group_send_times[destination_key] = recent_times
                    excess = len(recent_times) + send_cost - 20
                    if excess > 0 and len(recent_times) >= excess:
                        wait = max(wait, recent_times[excess - 1] + 60 - now)
                else:
                    wait = max(wait, send_cost * 1.0 - (now - last_chat_send))
            if wait <= 0:
                reserved = True
                _telegram_last_send = now
                if method_name.startswith("send_") and destination_key is not None:
                    _telegram_last_send_by_chat[destination_key] = now
                    if destination_key < 0:
                        recent_times = _telegram_group_send_times.setdefault(destination_key, [])
                        recent_times.extend([now] * send_cost)
        if not reserved:
            time.sleep(max(0.05, wait))
            continue

        try:
            for value in args:
                _rewind_telegram_uploads(value)
            for value in kwargs.values():
                _rewind_telegram_uploads(value)
            result = fn(*args, **kwargs)
        except Exception as e:
            text = str(e)
            attempt += 1
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
            if "429" in text and m and attempt < 4:
                delay = int(m.group(1)) + 1
                print(f"⏳ Telegram 429: ждём {delay} сек.")
                time.sleep(delay)
                continue
            raise

        ctx = getattr(_send_context, "current", None)
        if ctx is not None:
            ids = _telegram_result_ids(result)
            ctx["api_ok"] += 1
            ctx["message_ids"].extend(ids)
            if method_name == "send_message":
                ctx["text_sent"] = True
            if _telegram_call_has_media_caption(method_name, args, kwargs):
                ctx["caption_sent"] = True
            debug_logger.info(
                "TELEGRAM_SEND_OK chat_id=%s source_id=%s method=%s telegram_message_ids=%s payload=%s",
                ctx["chat_id"], ctx["source_id"], getattr(fn, "__name__", "telegram_api"),
                ids or "<no-id>", _telegram_payload_preview(fn, args, kwargs) or "<media payload>"
            )
        return result
    raise RuntimeError("Не удалось отправить сообщение в Telegram после нескольких попыток")


def _attach_caption_to_prior_media(chat_id: int, post: dict) -> bool:
    record = _find_uncaptioned_media_record(chat_id, post)
    if not record:
        return False
    message_ids = record.get("message_ids") or []
    if not message_ids:
        return False
    caption = format_message(post)
    if not caption or len(caption) > 1024:
        return False
    try:
        bot.edit_message_caption(
            chat_id=chat_id,
            message_id=int(message_ids[0]),
            caption=caption,
            parse_mode="HTML",
        )
    except Exception as exc:
        debug_logger.warning(
            "MEDIA_CAPTION_ATTACH_FAILED chat_id=%s source_id=%s message_id=%s error=%s",
            chat_id, post.get("source_id") or post.get("idx") or "?", message_ids[0],
            log_preview(exc, 220),
        )
        if "message is not modified" in str(exc).casefold():
            _mark_parser_record_captioned(str(record.get("record_id") or ""), post)
            return True
        try:
            fallback = _telegram_call(bot.send_message, chat_id, caption, **NO_PREVIEW)
        except Exception as fallback_exc:
            debug_logger.warning(
                "MEDIA_CAPTION_TEXT_FALLBACK_FAILED chat_id=%s source_id=%s error=%s",
                chat_id, post.get("source_id") or post.get("idx") or "?",
                log_preview(fallback_exc, 220),
            )
            return False
        _mark_parser_record_text_sent(
            str(record.get("record_id") or ""), post, _telegram_result_ids(fallback)
        )
        debug_logger.info(
            "MEDIA_CAPTION_SENT_AS_TEXT chat_id=%s source_id=%s",
            chat_id, post.get("source_id") or post.get("idx") or "?",
        )
        return True
    _mark_parser_record_captioned(str(record.get("record_id") or ""), post)
    debug_logger.info(
        "MEDIA_CAPTION_ATTACHED chat_id=%s source_id=%s message_id=%s",
        chat_id, post.get("source_id") or post.get("idx") or "?", message_ids[0],
    )
    return True

def _send_post_impl(chat_id: int, post: dict) -> None:
    msg_text = format_message(post)
    media_files = post.get("media_files", []) or []
    emoji_image_urls = {
        str(url).strip() for url in (post.get("emoji_image_urls") or []) if str(url).strip()
    }
    ctx = getattr(_send_context, "current", None)
    if ctx is not None:
        ctx["suppressed_media"] = 0
    send_media = []
    for media in media_files:
        if media.get("type") == "emoji" or str(media.get("url") or "").strip() in emoji_image_urls:
            continue
        _normalize_inline_video_media(media, post)
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
    if post.get("media_omitted"):
        actual_text = f"{actual_text}\n\n📎 Вложение пропущено из-за ограничения размера Telegram.".strip()
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

    album_media = [
        media for media in send_media
        if media.get("type") == "image"
        or (media.get("type") == "video" and _media_is_mp4_file(media))
    ]
    album_sent_ids=set()
    if len(album_media)>=2:
        for start in range(0,len(album_media),10):
            chunk=album_media[start:start+10]
            if len(chunk)<2:
                continue
            album=[]
            caption_in_album=False
            for media in chunk:
                caption=msg_text if caption_pending and not caption_in_album and len(msg_text)<=1024 else None
                album.append(_telegram_media_input(media, post, caption=caption or ""))
                if caption:
                    caption_in_album=True
            try:
                _telegram_call(bot.send_media_group,chat_id,album)
                album_sent_ids.update(id(m) for m in chunk)
                if caption_in_album:
                    caption_pending=False
            except Exception as album_error:
                debug_logger.warning(
                    "MEDIA_ALBUM_SEND_FAILED chat_id=%s source_id=%s media=%s error=%s",
                    chat_id, post.get("source_id") or post.get("idx") or "?",
                    len(chunk), log_preview(album_error, 240),
                )
                print(f"⚠️ [{chat_id}] Не удалось отправить альбом медиа: {album_error}")
                # Если Telegram отклонил подпись/HTML, повторяем тот же альбом без неё.
                if caption_in_album:
                    try:
                        retry_album = [_telegram_media_input(media, post) for media in chunk]
                        _telegram_call(bot.send_media_group, chat_id, retry_album)
                        album_sent_ids.update(id(media) for media in chunk)
                        if caption_pending:
                            try:
                                _telegram_call(bot.send_message, chat_id, msg_text, **NO_PREVIEW)
                            except Exception as text_error:
                                debug_logger.warning(
                                    "MEDIA_ALBUM_CAPTION_FALLBACK_FAILED chat_id=%s source_id=%s error=%s",
                                    chat_id, post.get("source_id") or post.get("idx") or "?",
                                    log_preview(text_error, 240),
                                )
                            caption_pending = False
                        continue
                    except Exception as retry_error:
                        debug_logger.warning(
                            "MEDIA_ALBUM_RETRY_FAILED chat_id=%s source_id=%s media=%s error=%s",
                            chat_id, post.get("source_id") or post.get("idx") or "?",
                            len(chunk), log_preview(retry_error, 240),
                        )
                        print(f"⚠️ [{chat_id}] Не удалось повторно отправить альбом: {retry_error}")
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
                if _media_is_mp4_file(media):
                    video_options = _video_upload_details(media, post)
                    thumbnail = video_options.pop("thumbnail", None)
                    if thumbnail:
                        thumbnail_file = io.BytesIO(thumbnail)
                        thumbnail_file.name = "video-thumbnail.jpg"
                        video_options["thumbnail"] = thumbnail_file
                    video_options["supports_streaming"] = True
                    if caption_pending and len(msg_text)<=1024:
                        video_options.update(caption=msg_text, parse_mode="HTML")
                    try:
                        _telegram_call(bot.send_video, chat_id, obj, **video_options)
                        if caption_pending and len(msg_text)<=1024:
                            caption_pending=False
                    except Exception as video_error:
                        debug_logger.warning(
                            "TELEGRAM_VIDEO_FALLBACK_TO_DOCUMENT source_id=%s filename=%s error=%s",
                            post.get("source_id") or post.get("idx") or "?", filename,
                            log_preview(video_error, 240),
                        )
                        print(f"⚠️ [{chat_id}] Telegram не принял видео как видео: {video_error}; отправляю исходник файлом")
                        obj.seek(0)
                        document_options = {"visible_file_name": filename}
                        if caption_pending and len(msg_text)<=1024:
                            document_options.update(caption=msg_text, parse_mode="HTML")
                        _telegram_call(bot.send_document, chat_id, obj, **document_options)
                        if caption_pending and len(msg_text)<=1024:
                            caption_pending=False
                else:
                    debug_logger.warning(
                        "TELEGRAM_VIDEO_SENT_AS_DOCUMENT source_id=%s filename=%s content_type=%s bytes=%s spooled=%s",
                        post.get("source_id") or post.get("idx") or "?", filename,
                        media.get("content_type") or "<none>",
                        media.get("size_bytes") or len(media.get("bytes") or b""),
                        bool(media.get("temp_path")),
                    )
                    if caption_pending and len(msg_text)<=1024:
                        _telegram_call(
                            bot.send_document, chat_id, obj, caption=msg_text,
                            parse_mode="HTML", visible_file_name=filename,
                        )
                        caption_pending=False
                    else:
                        _telegram_call(bot.send_document, chat_id, obj, visible_file_name=filename)
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
            if media_type == "video":
                raise
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
        "caption_sent": False,
        "text_sent": False,
        "upload_files": [],
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
    sent_record_saved = False
    try:
        _send_post_impl(chat_id, post)
        if context["message_ids"]:
            _record_parser_sent_post(
                chat_id, post, context["message_ids"],
                captioned=context["caption_sent"], text_sent=context["text_sent"],
            )
            sent_record_saved = True
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
        if context["message_ids"] and not sent_record_saved:
            _record_parser_sent_post(
                chat_id, post, context["message_ids"], partial=True,
                captioned=context["caption_sent"], text_sent=context["text_sent"],
            )
        debug_logger.exception(
            "POST_SEND_COMPLETE chat_id=%s source_id=%s result=failed api_ok=%s api_errors=%s error=%s",
            chat_id, post_id, context["api_ok"], context["api_errors"], log_preview(exc, 300)
        )
        raise
    finally:
        for upload_file in context.get("upload_files", []):
            try:
                upload_file.close()
            except Exception:
                pass
        _send_context.current = previous

def _commit_successful_post(chat_id: int, post: dict) -> None:
    mark_message_seen(post, chat_id=chat_id)
    for media in post.get("media_files", []) or []:
        data = media.get("bytes")
        media_hash = media.get("hash")
        if media_hash or (isinstance(data, (bytes, bytearray)) and data):
            mark_media_seen(
                data=data,
                chat_id=chat_id,
                media_hash=media_hash,
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
    if post.get("media_omitted"):
        media_summary += "; одно крупное вложение пропущено"
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
    visible_posts = [
        item for item in (posts or [])
        if isinstance(item, dict) and item.get("status") in {"sent", "failed"}
    ][-10:]
    entry = {
        "chat_id": int(chat_id),
        "title": str(title or "")[:100],
        "updated_at": datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S"),
        "sent": int(sent),
        "skipped": int(skipped),
        "failed": int(failed),
        "posts": visible_posts,
        "error": str(error or "")[:240],
    }
    with PARSE_DIAGNOSTICS_LOCK:
        PARSE_DIAGNOSTICS[int(chat_id)] = entry


def _record_parse_diagnostic_failure(chat_id, title, error):
    _store_parse_diagnostic(chat_id, title, 0, 0, 1, [], error=error)


def _process_posts(chat_id: int, title: str, posts: list, record_diagnostics=True, diagnostics_out=None) -> tuple:
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
            _cleanup_post_media(post)
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
                    _cleanup_post_media(post)
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
                _cleanup_post_media(post)
                _refresh_poll_snapshot(chat_id, post, poll_state, closed=False)
                skipped_count += 1
                diagnostic_posts.append(_diagnostic_post(post, "skipped", "опрос уже отправлен"))
                debug_logger.info(
                    "MESSAGE_SKIPPED chat_id=%s source_id=%s reason=poll_initial_already_sent",
                    chat_id, source_id or "?",
                )
                continue

        if (
            not force_poll_send
            and str(post.get("text") or "").strip()
            and not post.get("location")
            and not (post.get("contact") or {}).get("is_contact")
            and _attach_caption_to_prior_media(chat_id, post)
        ):
            _commit_successful_post(chat_id, post)
            increment_message_count(chat_id)
            new_count += 1
            diagnostic_posts.append(
                _diagnostic_post(post, "sent", "подпись добавлена к ранее отправленному фото")
            )
            _cleanup_post_media(post)
            debug_logger.info(
                "MESSAGE_CAPTION_ATTACHED chat_id=%s source_id=%s",
                chat_id, source_id or "?",
            )
            continue

        if not force_poll_send and not is_new_message(post, chat_id=chat_id):
            _cleanup_post_media(post)
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
        except Exception as error:
            failed_count += 1
            diagnostic_posts.append(_diagnostic_post(post, "failed", str(error)))
            debug_logger.exception(
                "MESSAGE_PROCESS_FAILED chat_id=%s source_id=%s title=%s error=%s",
                chat_id, source_id or "?", log_preview(title, 100), log_preview(error, 300)
            )
            print(f"❌ [{chat_id}] Ошибка отправки в чат {title}: {error}")
        finally:
            _cleanup_post_media(post)

    save_message_cache()
    save_photo_cache()
    debug_logger.info(
        "CHAT_PROCESS_COMPLETE chat_id=%s title=%s sent=%s skipped=%s failed=%s",
        chat_id, log_preview(title, 100), new_count, skipped_count, failed_count
    )
    if diagnostics_out is not None:
        diagnostics_out.extend(diagnostic_posts)
    if record_diagnostics:
        _store_parse_diagnostic(
            chat_id, title, new_count, skipped_count, failed_count, diagnostic_posts
        )
    return new_count, skipped_count


def _process_chat_posts_if_present(chat_id, title, posts, record_diagnostics=True, diagnostics_out=None):
    with CHAT_LIFECYCLE_LOCK:
        if not get_chat_by_id(chat_id):
            for post in posts or []:
                _cleanup_post_media(post)
            return None
        try:
            return _process_posts(
                chat_id, title, posts,
                record_diagnostics=record_diagnostics,
                diagnostics_out=diagnostics_out,
            )
        except Exception:
            for post in posts or []:
                _cleanup_post_media(post)
            raise


def _record_parse_failure_if_present(chat_id, title, error):
    with CHAT_LIFECYCLE_LOCK:
        if get_chat_by_id(chat_id):
            _record_parse_diagnostic_failure(chat_id, title, error)


def _session_path_for_chat(chat):
    if chat.get("account_mode") == "project":
        return os.path.join("sessions", "project_account.json")
    return os.path.join("sessions", f"{chat['chat_id']}.json")


def _user_needs_subscription(user_id, account_mode=None):
    if not PAYMENT_ACCESS_ENFORCEMENT_ENABLED:
        return False
    if PAYMENT_ADMIN_BYPASS and is_admin(user_id):
        return False
    return not is_subscription_active(user_id, account_mode)


def _get_payable_active_chats():
    chats = get_active_chats()
    if not PAYMENT_ACCESS_ENFORCEMENT_ENABLED:
        return chats
    active_access = get_active_subscription_access()
    admin_ids = (
        {int(admin["user_id"]) for admin in get_all_admins()}
        if PAYMENT_ADMIN_BYPASS else set()
    )
    return [
        chat for chat in chats
        if not chat.get("owner_id")
        or int(chat["owner_id"]) in admin_ids
        or bool(
            active_access.get(int(chat["owner_id"]), set())
            & {chat.get("account_mode", "self"), "multi"}
        )
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
        parse_counts = {"new": 0, "skipped": 0}
        parse_diagnostics = []

        def consume_post(post):
            processed = _process_chat_posts_if_present(
                chat_id, title, [post], record_diagnostics=False,
                diagnostics_out=parse_diagnostics,
            )
            if processed is not None:
                parse_counts["new"] += processed[0]
                parse_counts["skipped"] += processed[1]

        try:
            parse_max_group_media(
                group_url=max_url,
                chat_id=chat_id,
                session_path=_session_path_for_chat(chat),
                post_callback=consume_post,
            )
            if not get_chat_by_id(chat_id):
                continue
            new_count, skipped_count = parse_counts["new"], parse_counts["skipped"]
            _store_parse_diagnostic(
                chat_id, title, new_count, skipped_count,
                sum(item.get("status") == "failed" for item in parse_diagnostics),
                parse_diagnostics,
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
            _store_parse_diagnostic(
                chat_id, title, parse_counts["new"], parse_counts["skipped"],
                sum(item.get("status") == "failed" for item in parse_diagnostics),
                parse_diagnostics, error=error,
            )
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
    parse_counts = {"new": 0, "skipped": 0}
    parse_diagnostics = []

    def consume_post(post):
        processed = _process_chat_posts_if_present(
            chat_id, title, [post], record_diagnostics=False,
            diagnostics_out=parse_diagnostics,
        )
        if processed is not None:
            parse_counts["new"] += processed[0]
            parse_counts["skipped"] += processed[1]

    try:
        owner_id = int(chat.get("owner_id") or 0)
        if owner_id and _user_needs_subscription(owner_id, chat.get("account_mode", "self")):
            return False
        parse_max_group_media(
            group_url=max_url,
            chat_id=chat_id,
            session_path=_session_path_for_chat(chat),
            post_callback=consume_post,
        )
        if get_chat_by_id(chat_id):
            _store_parse_diagnostic(
                chat_id, title, parse_counts["new"], parse_counts["skipped"],
                sum(item.get("status") == "failed" for item in parse_diagnostics),
                parse_diagnostics,
            )
        return True
    except Exception as error:
        _store_parse_diagnostic(
            chat_id, title, parse_counts["new"], parse_counts["skipped"],
            sum(item.get("status") == "failed" for item in parse_diagnostics),
            parse_diagnostics, error=error,
        )
        print(f"❌ Ошибка парсинга чата {title}: {error}")
        traceback.print_exc()
        return False

def background_parser():
    time.sleep(5)
    while True:
        cycle_started = time.monotonic()
        try:
            active_chats = _get_payable_active_chats()
            if not active_chats:
                time.sleep(max(1, PARSER_CYCLE_INTERVAL_SECONDS - (time.monotonic() - cycle_started)))
                continue
            print(f"🔄 Начинаю параллельный парсинг {len(active_chats)} чатов...")
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(len(active_chats), MAX_CONCURRENT_CHAT_PARSERS)
            ) as executor:
                futures = [
                    executor.submit(process_single_chat, chat)
                    for chat in active_chats
                ]
                concurrent.futures.wait(futures)
            elapsed = time.monotonic() - cycle_started
            wait_seconds = max(0, PARSER_CYCLE_INTERVAL_SECONDS - elapsed)
            print(
                f"✅ Цикл парсинга завершен за {elapsed:.1f} сек. "
                f"Следующий цикл через {wait_seconds:.1f} сек."
            )
            if wait_seconds:
                time.sleep(wait_seconds)
        except Exception as error:
            print(f"❌ Критическая ошибка парсера: {error}")
            time.sleep(max(1, PARSER_CYCLE_INTERVAL_SECONDS - (time.monotonic() - cycle_started)))


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
    if post.get("media_omitted"):
        omission_note = "📎 Одно из вложений слишком большое для отправки в Telegram и пропущено."
        body = f"{body}\n\n{omission_note}" if body else omission_note
    if post.get("video_preview_omitted"):
        video_note = "⚠️ Не удалось получить оригинал видео из MAX. Чтобы не отправлять уменьшенное превью, видео пропущено."
        body = f"{body}\n\n{video_note}" if body else video_note
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
        if getattr(user, "is_bot", False) or (
            BOT_USER_ID is not None and int(user.id) == BOT_USER_ID
        ):
            return
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
    kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data=f"qr_cancel_{target_chat_id}"))
    return kb


def _qr_target_from_action(action):
    raw_target = action.split("_", 2)[2]
    return "project_account" if raw_target == "project_account" else int(raw_target)

def _build_password_keyboard(target_chat_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data=f"qr_cancel_{target_chat_id}"))
    return kb


def _build_auth_check_keyboard(target_chat_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🔄 Проверить вход MAX", callback_data=f"qr_ready_{target_chat_id}"))
    kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data=f"qr_cancel_{target_chat_id}"))
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


def _clear_auth_qr_controls(target_chat_id):
    for output_chat_id, message_id in auth_qr_message_ids.pop(target_chat_id, set()):
        try:
            bot.edit_message_reply_markup(output_chat_id, message_id, reply_markup=None)
        except Exception:
            pass


def _track_auth_message(target_chat_id, message):
    if message:
        auth_qr_message_ids.setdefault(target_chat_id, set()).add(
            (int(message.chat.id), int(message.message_id))
        )


def _complete_login(target_chat_id, output_chat_id):
    review_event_id = auth_login_review_events.pop(target_chat_id, None)
    active_logins.pop(target_chat_id, None)
    _clear_auth_waiters(target_chat_id)
    _clear_auth_qr_controls(target_chat_id)
    chat = get_chat_by_id(target_chat_id) if target_chat_id != "project_account" else None
    if target_chat_id == "project_account":
        title = "Готового аккаунта"
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
            "Теперь проверю доступ готового аккаунта к каждой исходной группе. "
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
        if review_event_id:
            try:
                _offer_review_after_connection(
                    owner_id,
                    f"{target_chat_id}:{chat.get('account_mode') or 'self'}:{review_event_id}",
                )
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
            if active_logins.get(target_chat_id) is not session:
                remote_auth_service.revoke_session(session)
                return
            if status == "success":
                remote_auth_service.revoke_session(session, completed=True)
                _complete_login(target_chat_id, output_chat_id)
                return
            if status == "error":
                if active_logins.get(target_chat_id) is session:
                    active_logins.pop(target_chat_id, None)
                    auth_login_review_events.pop(target_chat_id, None)
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
            auth_login_review_events.pop(target_chat_id, None)
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
    title = "готового аккаунта" if is_project_account else chat["title"]
    _clear_auth_qr_controls(target_chat_id)
    auth_login_review_events.pop(target_chat_id, None)
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
        elif chat and chat.get("owner_id"):
            owner_id = int(chat["owner_id"])
            _offer_review_after_connection(
                owner_id,
                f"{target_chat_id}:{chat.get('account_mode') or 'self'}:{uuid.uuid4().hex}",
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
    auth_login_review_events[target_chat_id] = uuid.uuid4().hex
    try:
        qr_message = bot.send_photo(
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
        auth_qr_message_ids.setdefault(target_chat_id, set()).add(
            (int(admin_chat_id), int(qr_message.message_id))
        )
    except Exception as error:
        bot.send_message(admin_chat_id, f"❌ Ошибка отправки QR-кода: {error}", **NO_PREVIEW)
        session.close()
        active_logins.pop(target_chat_id, None)
        auth_login_review_events.pop(target_chat_id, None)

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
    kb.add(types.InlineKeyboardButton("🗑 Удалить сообщения парсера", callback_data="admin_sent_messages"))
    kb.row(
        types.InlineKeyboardButton("👑 Админы", callback_data="admin_admins"),
        types.InlineKeyboardButton("👤 Пользователи", callback_data="admin_users")
    )
    kb.row(types.InlineKeyboardButton("📢 Рассылка всем", callback_data="admin_broadcast"))
    kb.row(types.InlineKeyboardButton("💬 Отправить в чат", callback_data="admin_send_to_chat"))
    kb.row(types.InlineKeyboardButton("👤 Отправить пользователю", callback_data="admin_send_to_user"))
    kb.add(types.InlineKeyboardButton("🎁 Бесплатная подписка", callback_data="admin_free_subscriptions"))
    kb.add(types.InlineKeyboardButton("🔧 Управление подпиской", callback_data="admin_revoke_subscriptions"))
    kb.add(types.InlineKeyboardButton("🗑 Удалить отзывы", callback_data="admin_review_delete"))
    kb.add(types.InlineKeyboardButton("🔑 MAX-готовый аккаунт", callback_data="admin_project_login"))
    kb.add(types.InlineKeyboardButton("🗑 Очистить весь кэш", callback_data="admin_clear_cache", style="danger"))
    bot.send_message(chat_id, text, reply_markup=kb, **NO_PREVIEW)

def _sent_parser_records_snapshot():
    with _sent_parser_messages_lock:
        return [dict(item) for item in _sent_parser_messages if isinstance(item, dict)]

def _sent_record_time_label(record):
    try:
        sent_at = datetime.fromisoformat(str(record.get("sent_at") or "").replace("Z", "+00:00"))
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        return sent_at.astimezone().strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        return "время неизвестно"

def _sent_parser_chat_title(target_chat_id):
    try:
        chat = get_chat_by_id(int(target_chat_id))
        if chat and chat.get("title"):
            return str(chat["title"])
    except Exception:
        pass
    return str(target_chat_id)

def _show_sent_parser_chats(chat_id, message_id=None):
    records = [r for r in _sent_parser_records_snapshot() if _sent_record_is_deletable(r)]
    grouped = {}
    for record in records:
        try:
            target_chat_id = int(record.get("chat_id"))
        except (TypeError, ValueError):
            continue
        grouped[target_chat_id] = grouped.get(target_chat_id, 0) + 1

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    if grouped:
        for target_chat_id, count in sorted(
            grouped.items(), key=lambda item: _sent_record_latest_time(item[0]), reverse=True
        ):
            title = _sent_parser_chat_title(target_chat_id)
            label = f"🗑 {title[:38]} · {count}"
            keyboard.add(types.InlineKeyboardButton(
                label, callback_data=f"admin_sent_chat_{target_chat_id}"
            ))
        text = (
            "🗑 <b>Удаление сообщений парсера</b>\n\n"
            "Выберите Telegram-группу. Здесь доступны отправки за последние 47 часов.\n\n"
            "Отметьте сообщения кнопками и подтвердите удаление."
        )
    else:
        text = (
            "🗑 <b>Удаление сообщений парсера</b>\n\n"
            "Новые отправки появятся здесь. Удалять их можно кнопками в этом разделе.\n\n"
            "Удаление доступно в течение 48 часов после отправки."
        )
    keyboard.add(types.InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_back"))
    if message_id:
        safe_edit_message(text, chat_id, message_id, reply_markup=keyboard, parse_mode="HTML", **NO_PREVIEW)
    else:
        bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="HTML", **NO_PREVIEW)

def _sent_record_latest_time(target_chat_id):
    newest = 0.0
    for record in _sent_parser_records_snapshot():
        try:
            if int(record.get("chat_id") or 0) != int(target_chat_id) or not _sent_record_is_deletable(record):
                continue
            sent_at = datetime.fromisoformat(str(record.get("sent_at") or "").replace("Z", "+00:00"))
            newest = max(newest, sent_at.timestamp())
        except (TypeError, ValueError, OSError):
            continue
    return newest

def _show_sent_parser_posts(chat_id, message_id, target_chat_id):
    records = [
        record for record in _sent_parser_records_snapshot()
        if str(record.get("chat_id")) == str(target_chat_id) and _sent_record_is_deletable(record)
    ]
    records.sort(key=lambda record: str(record.get("sent_at") or ""), reverse=True)
    records = records[:20]
    title = _sent_parser_chat_title(target_chat_id)
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for record in records:
        preview = " ".join(str(record.get("preview") or "Сообщение").split())[:34]
        label = f"🗑 {_sent_record_time_label(record)} · {preview}"
        keyboard.add(types.InlineKeyboardButton(
            label, callback_data=f"admin_sent_post_{record.get('record_id')}"
        ))
    keyboard.add(types.InlineKeyboardButton("🔙 К группам", callback_data="admin_sent_messages"))
    text = (
        f"🗑 <b>{escape_html(title)}</b>\n\n"
        "Выберите отправку, которую нужно удалить. Если у неё было несколько фото или файлов, удалится весь набор."
        if records else
        f"🗑 <b>{escape_html(title)}</b>\n\nНет сохранённых отправок для удаления."
    )
    safe_edit_message(text, chat_id, message_id, reply_markup=keyboard, parse_mode="HTML", **NO_PREVIEW)

def _show_sent_parser_confirmation(chat_id, message_id, record):
    title = _sent_parser_chat_title(record.get("chat_id"))
    preview = escape_html(str(record.get("preview") or "Сообщение"))
    count = len(record.get("message_ids") or [])
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.row(
        types.InlineKeyboardButton("✅ Удалить", callback_data=f"admin_sent_delete_{record['record_id']}"),
        types.InlineKeyboardButton("↩️ Назад", callback_data=f"admin_sent_chat_{record['chat_id']}"),
    )
    text = (
        "⚠️ <b>Удалить отправку?</b>\n\n"
        f"Группа: <b>{escape_html(title)}</b>\n"
        f"Время: <b>{_sent_record_time_label(record)}</b>\n"
        f"Сообщений Telegram: <b>{count}</b>\n"
        f"Текст: <blockquote>{preview}</blockquote>\n\n"
        "Удалятся только сообщения, отправленные этим ботом."
    )
    safe_edit_message(text, chat_id, message_id, reply_markup=keyboard, parse_mode="HTML", **NO_PREVIEW)

def _delete_sent_parser_record(record_id):
    record = _find_sent_parser_record(record_id)
    if not record:
        return 0, [], "Запись уже удалена или не найдена."
    if not _sent_record_is_deletable(record):
        return 0, list(record.get("message_ids") or []), "Telegram разрешает удалить отправленные сообщения только в течение 48 часов."
    target_chat_id = int(record.get("chat_id"))
    remaining = []
    deleted = 0
    errors = []
    for message_id in record.get("message_ids") or []:
        try:
            bot.delete_message(target_chat_id, int(message_id))
            deleted += 1
        except Exception as error:
            error_text = str(error).casefold()
            if "message to delete not found" in error_text or "message_id_invalid" in error_text:
                deleted += 1
                continue
            remaining.append(int(message_id))
            errors.append(str(error))
    _finish_sent_parser_record(record_id, remaining)
    return deleted, remaining, (errors[0] if errors else "")

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
    account_mode_text = "Готовый аккаунт" if chat.get("account_mode") == "project" else "Аккаунт пользователя"
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
        f"📦 <b>Ключей защиты от дублей:</b> {cache_count}"
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
        f"🗃️ <b>Ключей защиты от дублей:</b> {cache_count} (это не число отправок)"
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
            text += "Выберите чат, чтобы увидеть последние отправленные сообщения и ошибки."
            for chat in chats[:12]:
                chat_id_value = int(chat["chat_id"])
                entry = diagnostics.get(chat_id_value)
                title = escape_html(chat.get("title") or str(chat_id_value))
                if entry:
                    text += (
                        f"\n\n<b>{title}</b> · {escape_html(entry.get('updated_at') or '')}"
                        f"\n✅ {entry.get('sent', 0)} отправлено · "
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
            f" · ⚠️ Ошибок: {entry.get('failed', 0)}"
        )
        if entry.get("error"):
            text += f"\n\n⚠️ <b>Ошибка разбора:</b> {escape_html(entry['error'])}"
        if not entry.get("posts") and not entry.get("error"):
            text += "\n\nВ этом цикле ошибок нет. Новых сообщений для отправки не найдено."
        for item in entry.get("posts", []):
            status_label = {
                "sent": "✅ отправлено",
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
    text = f"👤 <b>Пользователи</b> · {len(users)} всего"
    if users:
        text += f" · {first + 1}–{first + len(shown)} из {len(users)}"
        text += "\n\nОтправьте сюда user ID из списка, чтобы выдать доступ."
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for index, user in enumerate(shown, first + 1):
        name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
        name = escape_html(name or "Имя не указано")
        username = f"@{escape_html(user['username'])}" if user.get("username") else "username не указан"
        text += (
            f"\n\n<b>{index}. {name}</b>"
            f"\n{username}"
            f"\n🆔 ID: <code>{user['user_id']}</code>"
        )
    if not users:
        text += "\n\nПользователей пока нет."
    _page_buttons(keyboard, page, page_count, "admin_free_sub_page")
    keyboard.add(types.InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_back"))
    safe_edit_message(text, chat_id, message_id, reply_markup=keyboard, **NO_PREVIEW)


def show_revoke_subscription_users(chat_id, message_id, page=0):
    users = get_all_users()
    page_count = max(1, (len(users) + ADMIN_LIST_PAGE_SIZE - 1) // ADMIN_LIST_PAGE_SIZE)
    page = max(0, min(int(page), page_count - 1))
    first = page * ADMIN_LIST_PAGE_SIZE
    shown = users[first:first + ADMIN_LIST_PAGE_SIZE]
    text = f"👤 <b>Пользователи</b> · {len(users)} всего"
    if users:
        text += f" · {first + 1}–{first + len(shown)} из {len(users)}"
        text += "\n\nОтправьте user ID пользователя, у которого нужно отключить подписку."
    else:
        text += "\n\nПользователей пока нет."
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for index, user in enumerate(shown, first + 1):
        name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
        name = escape_html(name or "Имя не указано")
        username = f"@{escape_html(user['username'])}" if user.get("username") else "username не указан"
        text += (
            f"\n\n<b>{index}. {name}</b>"
            f"\n{username}"
            f"\n🆔 ID: <code>{user['user_id']}</code>"
        )
    _page_buttons(keyboard, page, page_count, "admin_revoke_sub_page")
    keyboard.add(types.InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_back"))
    safe_edit_message(text, chat_id, message_id, reply_markup=keyboard, **NO_PREVIEW)


def process_revoke_subscription_user_id(message):
    admin_id = int(message.from_user.id)
    if not is_admin(admin_id):
        return
    raw_value = (message.text or "").strip()
    if not re.fullmatch(r"\d{1,20}", raw_value):
        prompt = bot.send_message(
            message.chat.id,
            "Пришлите только числовой user ID из таблицы пользователей.",
            **NO_PREVIEW,
        )
        bot.register_next_step_handler(prompt, process_revoke_subscription_user_id)
        return
    user_id = int(raw_value)
    user = next((item for item in get_all_users() if int(item["user_id"]) == user_id), None)
    if not user:
        prompt = bot.send_message(
            message.chat.id,
            "Пользователь с таким ID не найден. Проверьте ID в таблице и отправьте его ещё раз.",
            **NO_PREVIEW,
        )
        bot.register_next_step_handler(prompt, process_revoke_subscription_user_id)
        return
    show_revoke_subscription_options(message.chat.id, user_id)


def show_revoke_subscription_options(chat_id, user_id):
    subscriptions = get_user_subscriptions(user_id)
    if not subscriptions:
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_back"))
        bot.send_message(
            chat_id,
            f"ℹ️ У пользователя <code>{user_id}</code> сейчас нет активной подписки.",
            reply_markup=keyboard,
            parse_mode="HTML",
            **NO_PREVIEW,
        )
        return
    text = f"🔧 <b>Подписки пользователя</b>\n\nID: <code>{user_id}</code>"
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for subscription in subscriptions:
        plan_code = subscription["plan_code"]
        plan_label = SUBSCRIPTION_LABELS.get(plan_code, plan_code)
        active_until = subscription["active_until"].astimezone(timezone.utc)
        text += f"\n\n• <b>{escape_html(plan_label)}</b> · до {active_until:%d.%m.%Y %H:%M} UTC"
        keyboard.add(types.InlineKeyboardButton(
            f"Выбрать: {plan_label}",
            callback_data=f"admin_revoke_plan_{plan_code}_{int(user_id)}",
        ))
    keyboard.add(types.InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_back"))
    bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="HTML", **NO_PREVIEW)


def show_revoke_subscription_confirmation(chat_id, user_id, plan_code):
    subscription = next(
        (item for item in get_user_subscriptions(user_id) if item["plan_code"] == plan_code),
        None,
    )
    if not subscription:
        bot.send_message(
            chat_id,
            "У этого пользователя уже нет активного доступа по выбранному тарифу.",
            **NO_PREVIEW,
        )
        return
    plan_label = SUBSCRIPTION_LABELS.get(plan_code, plan_code)
    active_until = subscription["active_until"].astimezone(timezone.utc)
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton(
        "✅ Подтвердить отключение",
        callback_data=f"admin_revoke_confirm_{plan_code}_{int(user_id)}",
    ))
    keyboard.add(types.InlineKeyboardButton("❌ Отмена", callback_data="admin_back"))
    bot.send_message(
        chat_id,
        f"Отключить тариф <b>{escape_html(plan_label)}</b> у пользователя <code>{user_id}</code>?\n"
        f"Текущий срок действия: до {active_until:%d.%m.%Y %H:%M} UTC.\n\n"
        "Будет отключён весь оставшийся доступ по этому тарифу.",
        reply_markup=keyboard,
        parse_mode="HTML",
        **NO_PREVIEW,
    )


def process_free_subscription_user_id(message):
    admin_id = int(message.from_user.id)
    if not is_admin(admin_id):
        return
    raw_value = (message.text or "").strip()
    if not re.fullmatch(r"\d{1,20}", raw_value):
        prompt = bot.send_message(
            message.chat.id,
            "Пришлите только числовой user ID из таблицы пользователей.",
            **NO_PREVIEW,
        )
        bot.register_next_step_handler(prompt, process_free_subscription_user_id)
        return
    user_id = int(raw_value)
    user = next((item for item in get_all_users() if int(item["user_id"]) == user_id), None)
    if not user:
        prompt = bot.send_message(
            message.chat.id,
            "Пользователь с таким ID не найден. Проверьте ID в таблице и отправьте его ещё раз.",
            **NO_PREVIEW,
        )
        bot.register_next_step_handler(prompt, process_free_subscription_user_id)
        return
    show_free_subscription_options(message.chat.id, user_id)


def show_free_subscription_options(chat_id, user_id):
    user = next((item for item in get_all_users() if int(item["user_id"]) == int(user_id)), None)
    if not user:
        bot.send_message(chat_id, "Пользователь не найден. Обновите список и попробуйте ещё раз.", **NO_PREVIEW)
        return
    name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
    username = f"@{escape_html(user['username'])}" if user.get("username") else "без username"
    text = (
        "🎁 <b>Выдача бесплатной подписки</b>\n\n"
        f"Пользователь: <b>{escape_html(name or 'Имя не указано')}</b> ({username})\n"
        f"ID: <code>{user_id}</code>\n\n"
        "Сначала выберите мульти-подписку или один способ подключения. Затем выберите срок. "
        "Новый срок добавится к уже активному тарифу того же типа."
    )
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton(
        "✨ Мульти-подписка", callback_data=f"admin_gift_multi_{user_id}"
    ))
    keyboard.add(types.InlineKeyboardButton(
        "📦 Отдельная подписка", callback_data=f"admin_gift_separate_{user_id}"
    ))
    keyboard.add(types.InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_back"))
    bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="HTML", **NO_PREVIEW)


def show_free_subscription_separate_options(chat_id, user_id):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton(
        "1️⃣ Свой MAX-аккаунт", callback_data=f"admin_gift_plan_self_{user_id}"
    ))
    keyboard.add(types.InlineKeyboardButton(
        "2️⃣ Готовый аккаунт", callback_data=f"admin_gift_plan_project_{user_id}"
    ))
    keyboard.add(types.InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_back"))
    bot.send_message(
        chat_id,
        f"👤 Пользователь: <code>{user_id}</code>\n\nВыберите отдельный способ подключения:",
        reply_markup=keyboard,
        parse_mode="HTML",
        **NO_PREVIEW,
    )


def show_free_subscription_terms(chat_id, user_id, plan_code):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for months in (1, 3, 12):
        label = SUBSCRIPTION_PERIOD_LABELS[months]
        keyboard.add(types.InlineKeyboardButton(
            f"🎁 {label}",
            callback_data=f"admin_gift_term_{plan_code}_{months}_{user_id}",
        ))
    keyboard.add(types.InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_back"))
    bot.send_message(
        chat_id,
        f"🎁 Выдача доступа пользователю <code>{user_id}</code>\n"
        f"Тариф: <b>{escape_html(SUBSCRIPTION_LABELS[plan_code])}</b>\n\nВыберите срок:",
        reply_markup=keyboard,
        parse_mode="HTML",
        **NO_PREVIEW,
    )


def show_user_send_selector(chat_id, message_id, page=0):
    users = get_all_users()
    page_count = max(1, (len(users) + ADMIN_LIST_PAGE_SIZE - 1) // ADMIN_LIST_PAGE_SIZE)
    page = max(0, min(int(page), page_count - 1))
    first = page * ADMIN_LIST_PAGE_SIZE
    shown = users[first:first + ADMIN_LIST_PAGE_SIZE]
    text = f"👤 <b>Пользователи</b> · {len(users)} всего"
    if users:
        text += "\nСкопируйте ID пользователя из списка и отправьте его боту."
        for user in shown:
            name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
            name = escape_html(name or "Имя не указано")
            username = f"@{escape_html(user['username'])}" if user.get("username") else "username не указан"
            text += f"\n\n<b>{name}</b> · {username}\n🆔 ID: <code>{user['user_id']}</code>"
    else:
        text += "\n\nСписок пользователей пока пуст."
    kb = types.InlineKeyboardMarkup(row_width=1)
    _page_buttons(kb, page, page_count, "admin_send_users_page")
    kb.add(types.InlineKeyboardButton("🔙 Назад в меню", callback_data="admin_back"))
    safe_edit_message(text, chat_id, message_id, reply_markup=kb, **NO_PREVIEW)


def show_chat_send_selector(chat_id, message_id, page=0):
    chats = get_all_chats()
    page_count = max(1, (len(chats) + ADMIN_LIST_PAGE_SIZE - 1) // ADMIN_LIST_PAGE_SIZE)
    page = max(0, min(int(page), page_count - 1))
    first = page * ADMIN_LIST_PAGE_SIZE
    shown = chats[first:first + ADMIN_LIST_PAGE_SIZE]
    text = f"💬 <b>Чаты</b> · {len(chats)} всего"
    if chats:
        text += "\nСкопируйте ID чата из списка и отправьте его боту."
        for chat in shown:
            title = escape_html(chat.get("title") or "Без названия")
            text += f"\n\n<b>{title}</b>\n🆔 ID: <code>{chat['chat_id']}</code>"
    else:
        text += "\n\nЧатов пока нет."
    kb = types.InlineKeyboardMarkup(row_width=1)
    _page_buttons(kb, page, page_count, "admin_send_chats_page")
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


def process_add_chat_step1(message, admin_chat_id):
    track_user(message)
    try:
        chat_id = int(message.text.strip())
    except:
        bot.send_message(
            message.chat.id,
            "❌ Неверный формат ID. Попробуйте ещё раз или нажмите кнопку «❌ Отмена».",
            reply_markup=get_cancel_keyboard(),
            **NO_PREVIEW,
        )
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
            "Отправь ID ещё раз или нажми кнопку «❌ Отмена».",
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
            "Отправь ID ещё раз или нажми кнопку «❌ Отмена».",
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
    keyboard.add(types.InlineKeyboardButton("👑 Благодарности", callback_data="thanks_project"))
    keyboard.add(types.InlineKeyboardButton("⭐ Отзывы", callback_data="reviews_show"))
    return keyboard

def return_start():
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(types.InlineKeyboardButton("✔ Подключить MAX_Parser", callback_data="parse_setup"))
    keyboard.add(types.InlineKeyboardButton("🔙 Назад", callback_data="start_comeback"))
    return keyboard

def parser_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton("1️⃣ Свой MAX-аккаунт · от 249 ₽", callback_data="connect_self"))
    keyboard.add(types.InlineKeyboardButton("2️⃣ Готовый аккаунт · от 499 ₽", callback_data="connect_project"))
    keyboard.add(types.InlineKeyboardButton("✨ Мульти-подписка · от 599 ₽", callback_data="connect_multi"))
    keyboard.add(types.InlineKeyboardButton("ℹ️ Разница способов", callback_data="compare_modes"))
    keyboard.add(types.InlineKeyboardButton("📖 Инструкция", callback_data="instruction"))
    keyboard.add(types.InlineKeyboardButton("🎬 Видео инструкция", callback_data="video_instruction"))
    keyboard.add(types.InlineKeyboardButton("🏠 В главное меню", callback_data="start_home"))
    return keyboard


SUBSCRIPTION_PRICES = {
    "self": {1: 249, 3: 599, 12: 2499},
    "project": {1: 499, 3: 1199, 12: 5499},
    "multi": {1: 599, 3: 1499, 12: 6499},
}
SUBSCRIPTION_LABELS = {
    "self": "Подключение своего MAX-аккаунта",
    "project": "Подключение аккаунта MAX_Parser",
    "multi": "Мульти-подписка",
}
SUBSCRIPTION_PERIOD_LABELS = {1: "1 месяц", 3: "3 месяца", 12: "1 год"}


def subscription_products_keyboard(prefix="subscription_product"):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton(
        "1️⃣ Свой MAX-аккаунт · от 249 ₽", callback_data=f"{prefix}_self"
    ))
    keyboard.add(types.InlineKeyboardButton(
        "2️⃣ Готовый аккаунт · от 499 ₽", callback_data=f"{prefix}_project"
    ))
    keyboard.add(types.InlineKeyboardButton(
        "✨ Мульти-подписка · от 599 ₽", callback_data=f"{prefix}_multi"
    ))
    return keyboard


def subscription_period_keyboard(plan_code, back_callback="subscription_products"):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for months in (1, 3, 12):
        amount = SUBSCRIPTION_PRICES[plan_code][months]
        period = SUBSCRIPTION_PERIOD_LABELS[months]
        keyboard.add(types.InlineKeyboardButton(
            f"{period} · {amount:,} ₽".replace(",", " "),
            callback_data=f"subscription_buy_{plan_code}_{months}",
        ))
    keyboard.add(types.InlineKeyboardButton("🔙 К тарифам", callback_data=back_callback))
    return keyboard


def subscription_renewal_keyboard():
    return subscription_products_keyboard("subscription_product")


def _product_marketing_text(plan_code):
    if plan_code == "self":
        return (
            "Подключите свой аккаунт MAX и получайте сообщения из чатов, которые доступны именно вам. "
            "Подходит, если вы не хотите добавлять готовый аккаунт в свои чаты."
        )
    if plan_code == "project":
        return (
            "Сообщения читает аккаунт MAX_Parser. Вам не нужно входить в свой MAX: для закрытой группы "
            "достаточно предоставить готовому аккаунту доступ."
        )
    return (
        "Оба способа в одной подписке: можно подключать чаты через свой аккаунт MAX и через готовый "
        "аккаунт. Выбирайте удобный способ отдельно для каждого подключения."
    )


def show_subscription_terms(chat_id, message_id, plan_code, back_callback="subscription_products"):
    if plan_code not in SUBSCRIPTION_PRICES:
        return
    title = SUBSCRIPTION_LABELS[plan_code]
    chat_limit = MULTI_MAX_CUSTOMER_CHATS if plan_code == "multi" else MAX_CUSTOMER_CHATS
    text = (
        f"💳 <b>{escape_html(title)}</b>\n\n"
        f"{escape_html(_product_marketing_text(plan_code))}\n\n"
        f"<i>Подписка действует на весь ваш аккаунт MAX_Parser и покрывает до {chat_limit} подключённых чатов.</i>\n\n"
        "<b>Выберите срок подписки:</b>"
    )
    safe_edit_message(
        text,
        chat_id,
        message_id,
        reply_markup=subscription_period_keyboard(plan_code, back_callback),
        parse_mode="HTML",
        **NO_PREVIEW,
    )


def show_connection_method_picker(chat_id, message_id=None):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton(
        "1️⃣ Подключить через свой MAX", callback_data="setup_mode_self"
    ))
    keyboard.add(types.InlineKeyboardButton(
        "2️⃣ Подключить через готовый аккаунт", callback_data="setup_mode_project"
    ))
    keyboard.add(types.InlineKeyboardButton("🔙 Назад", callback_data="parse_setup"))
    text = (
        "✨ <b>Мульти-подписка активна</b>\n\n"
        "Вам доступны оба способа подключения. Выберите, какой использовать для этого чата."
    )
    if message_id:
        safe_edit_message(
            text, chat_id, message_id, reply_markup=keyboard,
            parse_mode="HTML", **NO_PREVIEW,
        )
    else:
        bot.send_message(
            chat_id, text, reply_markup=keyboard,
            parse_mode="HTML", **NO_PREVIEW,
        )


def _user_has_product_access(user_id, plan_code):
    if PAYMENT_ADMIN_BYPASS and is_admin(user_id):
        return True
    if plan_code == "multi":
        return (
            is_subscription_active(user_id, "self")
            and is_subscription_active(user_id, "project")
        )
    return is_subscription_active(user_id, plan_code)


def _begin_connection_for_plan(user_id, plan_code, chat_id, message_id=None):
    chat_limit = get_customer_chat_limit(user_id)
    if plan_code == "multi" and PAYMENTS_ENABLED:
        chat_limit = max(chat_limit, MULTI_MAX_CUSTOMER_CHATS)
    if len(get_user_chats(user_id)) >= chat_limit:
        bot.send_message(chat_id, _chat_limit_notice(chat_limit), **NO_PREVIEW)
        return
    if plan_code == "multi" and _user_has_product_access(user_id, "multi"):
        show_connection_method_picker(chat_id, message_id)
        return
    if plan_code in {"self", "project"} and _user_has_product_access(user_id, plan_code):
        _start_customer_setup_prompt(user_id, plan_code, chat_id, message_id)
        return
    if PAYMENTS_ENABLED:
        if message_id:
            show_subscription_terms(
                chat_id, message_id, plan_code, back_callback="parse_setup"
            )
        else:
            bot.send_message(chat_id, "Выберите срок подписки:", **NO_PREVIEW)
        return
    if plan_code == "multi":
        show_connection_method_picker(chat_id, message_id)
    else:
        _start_customer_setup_prompt(user_id, plan_code, chat_id, message_id)


def _start_customer_setup_prompt(user_id, account_mode, chat_id, message_id=None):
    if account_mode not in {"self", "project"}:
        return
    chat_limit = get_customer_chat_limit(user_id)
    if len(get_user_chats(user_id)) >= chat_limit:
        bot.send_message(chat_id, _chat_limit_notice(chat_limit), **NO_PREVIEW)
        return
    if _user_needs_subscription(user_id, account_mode):
        bot.send_message(
            chat_id,
            "Для этого способа подключения нужна активная подписка. Выберите тариф:",
            reply_markup=subscription_products_keyboard("subscription_product"),
            **NO_PREVIEW,
        )
        return
    pending_setup_modes[user_id] = account_mode
    label = "настройку своего аккаунта MAX" if account_mode == "self" else "настройку через готовый аккаунт"
    bot.send_message(
        chat_id,
        f"<b>Шаг 1 из {2 if account_mode == 'project' else 3} · Telegram-группа</b>\n\n"
        f"Вы выбрали: <b>{label}</b>. Добавьте меня в Telegram-группу, в которую я буду присылать сообщения. "
        "Когда я появлюсь в группе, настройка продолжится автоматически. Если я уже состою в ней, то "
        "напишите в группе команду /bind.\n\n"
        "Убедитесь, что мне разрешено отправлять сообщения. Чтобы остановить настройку, нажмите «❌ Отмена».",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard(),
        **NO_PREVIEW,
    )
    if message_id:
        try:
            bot.edit_message_text(
                "Настройка начата. Продолжите её в вашей Telegram-группе; подробная инструкция доступна по кнопке ниже.",
                chat_id,
                message_id,
                reply_markup=parser_keyboard(),
                **NO_PREVIEW,
            )
        except Exception:
            pass


def _subscription_plan(plan_code):
    if plan_code not in SUBSCRIPTION_PRICES:
        raise ValueError("Некорректный тариф подписки")
    return SUBSCRIPTION_LABELS[plan_code]


def _yookassa_receipt_provider_data(plan_code, duration_months, amount):
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

    receipt = {
        "items": [{
            "description": f"{SUBSCRIPTION_LABELS[plan_code]} — {SUBSCRIPTION_PERIOD_LABELS[duration_months]}",
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


def _send_subscription_invoice(user_id, plan_code, duration_months):
    if not TELEGRAM_PAYMENT_PROVIDER_TOKEN:
        bot.send_message(
            user_id,
            "Оплата пока не настроена.",
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
    if plan_code not in SUBSCRIPTION_PRICES or duration_months not in (1, 3, 12):
        raise ValueError("Некорректный тариф или срок подписки")
    amount = SUBSCRIPTION_PRICES[plan_code][duration_months] * 100
    plan_description = _subscription_plan(plan_code)
    try:
        provider_data = _yookassa_receipt_provider_data(plan_code, duration_months, amount)
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
        payload = create_payment_order(user_id, plan_code, amount, duration_months)
        invoice_options = {
            "chat_id": user_id,
            "title": "MAX_Parser · подписка",
            "description": f"{plan_description} · {SUBSCRIPTION_PERIOD_LABELS[duration_months]}",
            "invoice_payload": payload,
            "provider_token": TELEGRAM_PAYMENT_PROVIDER_TOKEN,
            "currency": "RUB",
            "prices": [types.LabeledPrice(
                label=f"{SUBSCRIPTION_PERIOD_LABELS[duration_months]} · MAX_Parser",
                amount=amount,
            )],
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


def _send_review_invitation(user_id, review_id):
    if get_review_entry_status(review_id) != "eligible" or not claim_review_entry_invitation(review_id):
        return False
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton(
        "✍️ Оставить отзыв", callback_data=f"review_write_{int(review_id)}"
    ))
    keyboard.add(types.InlineKeyboardButton(
        "Не хочу", callback_data=f"review_skip_{int(review_id)}"
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
        release_review_entry_invitation(review_id)
        raise
    return True


def _maybe_offer_review(user_id, connection_key):
    entry = create_review_entry(int(user_id), str(connection_key))
    if not entry:
        return False
    review_id = int(entry["review_id"])
    status = entry["status"]
    if status == "eligible":
        return _send_review_invitation(user_id, review_id)
    return False


def _offer_review_after_connection(user_id, connection_key):
    return _maybe_offer_review(int(user_id), str(connection_key))


def show_approved_reviews(chat_id, message_id, page=0):
    reviews = get_approved_review_entries()
    if not reviews:
        text = "⭐ <b>Отзывы</b>\n\nПока отзывов нет."
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
    if review.get("username"):
        author_label = f"@{escape_html(review['username'])}"
    else:
        author_name = " ".join(
            part for part in (review.get("first_name"), review.get("last_name")) if part
        )
        author_label = escape_html(author_name or "Пользователь MAX_Parser")
    review_text = escape_html(str(review.get("review_text") or "").strip()[:700])
    text = (
        f"⭐ <b>Отзывы о MAX_Parser</b> · {page + 1}/{len(reviews)}\n\n"
        f"👤 <b>{author_label}</b>\n\n"
        f"<blockquote>{review_text}</blockquote>"
    )
    if date_label:
        text += f"\n\n<i>{date_label}</i>"
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    row = []
    if page > 0:
        row.append(types.InlineKeyboardButton("◀️ Предыдущий", callback_data=f"reviews_page_{page - 1}"))
    if page + 1 < page_count:
        row.append(types.InlineKeyboardButton("Следующий ▶️", callback_data=f"reviews_page_{page + 1}"))
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


def show_admin_review_delete_list(chat_id, message_id, page=0):
    reviews = get_approved_review_entries()
    page_size = ADMIN_LIST_PAGE_SIZE
    page_count = max(1, (len(reviews) + page_size - 1) // page_size)
    page = max(0, min(int(page), page_count - 1))
    first = page * page_size
    shown = reviews[first:first + page_size]
    text = f"🗑 <b>Отзывы</b> · {len(reviews)} опубликовано"
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    if not reviews:
        text += "\n\nПока нет опубликованных отзывов."
    else:
        text += f" · {first + 1}–{first + len(shown)} из {len(reviews)}"
        for review in shown:
            username = review.get("username")
            if username:
                author = f"@{escape_html(username)}"
            else:
                author_name = " ".join(
                    part for part in (review.get("first_name"), review.get("last_name")) if part
                )
                author = escape_html(author_name or str(review["user_id"]))
            date = review.get("created_at")
            date_label = date.strftime("%d.%m.%Y") if date else ""
            preview = escape_html(str(review.get("review_text") or "").strip()[:150])
            text += (
                f"\n\n<b>{author}</b> · <code>{review['review_id']}</code>"
                f"\n{date_label}\n<blockquote>{preview}</blockquote>"
            )
            keyboard.add(types.InlineKeyboardButton(
                f"🗑 Удалить отзыв {review['review_id']}",
                callback_data=f"admin_review_delete_confirm_{int(review['review_id'])}_{page}",
            ))
    _page_buttons(keyboard, page, page_count, "admin_review_delete_page")
    keyboard.add(types.InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_back"))
    safe_edit_message(
        text, chat_id, message_id, reply_markup=keyboard, parse_mode="HTML", **NO_PREVIEW
    )


def show_admin_review_delete_confirmation(chat_id, message_id, review_id, page=0):
    review = get_review_entry(review_id)
    if not review or review.get("status") != "approved":
        return
    author_name = " ".join(
        part for part in (review.get("first_name"), review.get("last_name")) if part
    )
    author = f"@{escape_html(review['username'])}" if review.get("username") else escape_html(author_name or str(review["user_id"]))
    review_text = escape_html(str(review.get("review_text") or "").strip()[:500])
    text = (
        f"Удалить опубликованный отзыв пользователя {author}?\n\n"
        f"<blockquote>{review_text}</blockquote>"
    )
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton(
        "✅ Удалить отзыв",
        callback_data=f"admin_review_delete_yes_{int(review_id)}_{int(page)}",
    ))
    keyboard.add(types.InlineKeyboardButton(
        "🔙 Назад к отзывам",
        callback_data=f"admin_review_delete_page_{int(page)}",
    ))
    safe_edit_message(
        text, chat_id, message_id, reply_markup=keyboard, parse_mode="HTML", **NO_PREVIEW
    )


def _send_review_for_moderation(review_id, user_id, review_text):
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.row(
        types.InlineKeyboardButton("✅ Одобрить", callback_data=f"review_approve_{int(review_id)}"),
        types.InlineKeyboardButton("❌ Отклонить", callback_data=f"review_reject_{int(review_id)}"),
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


def _map_support_message(admin_chat_id, message_id, user_id):
    route_key = (int(admin_chat_id), int(message_id))
    with support_state_lock:
        support_message_routes[route_key] = int(user_id)
        save_support_message_route(admin_chat_id, message_id, user_id)


def _support_admin_ids():
    admin_ids = {int(ADMIN_ID)}
    try:
        admin_ids.update(int(admin["user_id"]) for admin in get_all_admins())
    except Exception as error:
        debug_logger.warning(
            "SUPPORT_ADMIN_LIST_FAILED error_type=%s", type(error).__name__
        )
    return sorted(admin_ids)


def _notify_support_admin_one(admin_id, user, body_message=None):
    full_name, username = _support_user_label(user)
    header_text = (
        "🆘 <b>Обращение в тех поддержку</b>\n"
        f"👤 {full_name}\n"
        f"🔎 Никнейм: {username}\n"
        f"🆔 ID: <code>{user.id}</code>"
    )
    if body_message is None:
        header = bot.send_message(
            admin_id,
            header_text,
            parse_mode="HTML",
            reply_markup=_support_admin_keyboard(user.id),
            **NO_PREVIEW,
        )
        _map_support_message(admin_id, header.message_id, user.id)
        return

    support_keyboard = _support_admin_keyboard(user.id)
    body_text = body_message.text or body_message.caption or ""
    escaped_body_text = escape_html(body_text)
    quoted_body_text = f"<blockquote>{escaped_body_text}</blockquote>" if body_text else ""
    content_type = getattr(body_message, "content_type", "")
    card_prefix = header_text + "\n💬 <b>Сообщение:</b>\n"

    if content_type == "text" and len(card_prefix) + len(escaped_body_text) <= 4096:
        card = bot.send_message(
            admin_id,
            card_prefix + quoted_body_text,
            parse_mode="HTML",
            reply_markup=support_keyboard,
            **NO_PREVIEW,
        )
        _map_support_message(admin_id, card.message_id, user.id)
        return

    caption_media_types = {"photo", "video", "document", "audio", "voice", "animation"}
    if content_type in caption_media_types:
        caption = card_prefix + (
            quoted_body_text or "<blockquote>[без подписи]</blockquote>"
        )
        if len(caption) <= 1024:
            try:
                copied = bot.copy_message(
                    admin_id,
                    body_message.chat.id,
                    body_message.message_id,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=support_keyboard,
                )
                _map_support_message(admin_id, copied.message_id, user.id)
                return
            except Exception as error:
                debug_logger.info(
                    "SUPPORT_MEDIA_CARD_COPY_FAILED user_id=%s error_type=%s",
                    user.id, type(error).__name__,
                )

    header = bot.send_message(
        admin_id,
        card_prefix.rstrip(),
        parse_mode="HTML",
        reply_markup=support_keyboard,
        **NO_PREVIEW,
    )
    _map_support_message(admin_id, header.message_id, user.id)
    if content_type == "text":
        try:
            quoted_message = bot.send_message(
                admin_id,
                quoted_body_text or "<blockquote>[без текста]</blockquote>",
                parse_mode="HTML",
                **NO_PREVIEW,
            )
            _map_support_message(admin_id, quoted_message.message_id, user.id)
        except Exception as error:
            debug_logger.warning(
                "SUPPORT_TEXT_CARD_SEND_FAILED user_id=%s error_type=%s",
                user.id, type(error).__name__,
            )
        return
    try:
        if content_type in caption_media_types and body_text:
            quoted_message = bot.send_message(
                admin_id,
                quoted_body_text,
                parse_mode="HTML",
                **NO_PREVIEW,
            )
            _map_support_message(admin_id, quoted_message.message_id, user.id)
            copied = bot.copy_message(
                admin_id,
                body_message.chat.id,
                body_message.message_id,
                caption="",
                parse_mode="HTML",
            )
        else:
            copied = bot.copy_message(
                admin_id,
                body_message.chat.id,
                body_message.message_id,
            )
        _map_support_message(admin_id, copied.message_id, user.id)
    except Exception as error:
        fallback = body_message.text or body_message.caption or "[Вложение не удалось скопировать]"
        copied = bot.send_message(
            admin_id,
            f"⚠️ <b>Сообщение не удалось скопировать</b>\n"
            f"<blockquote>{escape_html(fallback[:3000])}</blockquote>\n\n"
            f"Причина: <code>{escape_html(str(error)[:500])}</code>",
            parse_mode="HTML",
            **NO_PREVIEW,
        )
        _map_support_message(admin_id, copied.message_id, user.id)


def _notify_support_admin(user, body_message=None):
    delivered = 0
    for admin_id in _support_admin_ids():
        try:
            _notify_support_admin_one(admin_id, user, body_message)
            delivered += 1
        except Exception as error:
            debug_logger.warning(
                "SUPPORT_ADMIN_NOTIFY_FAILED admin_id=%s user_id=%s error_type=%s",
                admin_id, user.id, type(error).__name__,
            )
    if delivered == 0:
        raise RuntimeError("Не удалось доставить обращение ни одному администратору")


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
        for route_key, routed_user_id in list(support_message_routes.items()):
            if routed_user_id == user_id:
                support_message_routes.pop(route_key, None)
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
        active_methods = []
        if is_subscription_active(message.from_user.id, "self"):
            active_methods.append("свой MAX-аккаунт")
        if is_subscription_active(message.from_user.id, "project"):
            active_methods.append("готовый аккаунт")
        methods_label = " и ".join(active_methods) or "MAX_Parser"
        text = (
            "💳 <b>Ваша подписка MAX_Parser активна</b>\n\n"
            f"Доступны: <b>{escape_html(methods_label)}</b>\n"
            f"Действует до: <b>{active_until:%d.%m.%Y %H:%M} UTC</b>.\n"
            "Продлите её заранее: новый срок добавится к действующему."
        )
    else:
        text = (
            "💳 <b>Активной подписки нет</b>\n\n"
            "Выберите способ подключения и удобный срок. Мульти-подписка включает оба способа."
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
    plan_code = result["plan_code"]
    duration_months = int(result["duration_months"])
    plan_label = SUBSCRIPTION_LABELS[plan_code]
    duration_label = SUBSCRIPTION_PERIOD_LABELS[duration_months]
    paid_at = datetime.fromtimestamp(message.date, tz=timezone.utc)
    provider_charge_id = escape_html(payment.provider_payment_charge_id or "не получен")
    fiscal_receipt_note = (
        "🧾 Данные для фискального чека переданы платёжному провайдеру."
        if YOOKASSA_RECEIPTS_ENABLED
        else "ℹ️ Это подтверждение оплаты от MAX_Parser. Фискальные чеки ЮKassa в текущих настройках не подключены."
    )
    multi_note = "Мульти-подписка включает оба способа подключения.\n" if plan_code == "multi" else ""
    chat_limit = MULTI_MAX_CUSTOMER_CHATS if plan_code == "multi" else MAX_CUSTOMER_CHATS
    bot.send_message(
        user_id,
        "🧾 <b>Квитанция MAX_Parser</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "✅ <b>Статус:</b> оплачено\n"
        f"📦 <b>Услуга:</b> подписка · {duration_label}\n"
        f"🔧 <b>Способ:</b> {plan_label}\n"
        f"💳 <b>Сумма:</b> {amount_rub:.2f} ₽\n"
        f"🕒 <b>Оплачено:</b> {paid_at:%d.%m.%Y %H:%M} UTC\n"
        f"📅 <b>Действует до:</b> {active_until:%d.%m.%Y %H:%M} UTC\n"
        f"🔖 <b>ID платежа:</b> <code>{provider_charge_id}</code>\n\n"
        f"<blockquote>Подписка распространяется на весь ваш аккаунт MAX_Parser и покрывает до {chat_limit} подключённых чатов.\n"
        f"{multi_note}Для подключения чата отправьте /connect. Автоматического списания нет — заранее напомню о продлении.</blockquote>\n"
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
        if plan_code == "multi":
            show_connection_method_picker(user_id)
        else:
            _start_customer_setup_prompt(user_id, plan_code, user_id)


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
                    mark_subscription_reminder_sent(user_id, subscription["plan_code"])
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
        "Выберите способ подключения.",
        reply_markup=parser_keyboard(),
        **NO_PREVIEW,
    )


def _chat_limit_notice(chat_limit=MAX_CUSTOMER_CHATS):
    return (
        f"У вас уже подключено {chat_limit} чатов. "
        "Чтобы подключить ещё один, напишите в поддержку через /help."
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


def _max_source_link_kind(value):
    """Return whether a MAX URL looks like an invite or a direct source URL."""
    try:
        parsed = urlsplit(str(value or "").strip())
        if re.match(r"^/(?:join|invite|invitation)(?:/|$)", parsed.path, re.I):
            return "invite"
    except (TypeError, ValueError):
        pass
    return "direct"


def _setup_cancel_button(chat_id):
    return types.InlineKeyboardButton("❌ Отмена", callback_data=f"setup_cancel_{int(chat_id)}")


SETUP_CANCEL_NOTICE = "Настройка отменена. Её можно начать заново через /connect."


def _cancel_customer_connection(user_id, chat_id=None, callback_message=None):
    user_id = int(user_id)
    state = customer_setup_state.get(user_id) or {}
    try:
        state_chat_id = int(state.get("chat_id")) if state.get("chat_id") is not None else None
    except (TypeError, ValueError):
        state_chat_id = None
    if chat_id is None:
        matches_state = True
    else:
        try:
            matches_state = state_chat_id == int(chat_id)
        except (TypeError, ValueError):
            matches_state = False

    if matches_state:
        state = customer_setup_state.pop(user_id, {})
        pending_setup_modes.pop(user_id, None)
    else:
        state = {}

    session = active_logins.pop(chat_id, None) if chat_id is not None else None
    if chat_id is not None:
        auth_login_review_events.pop(chat_id, None)
        _clear_auth_waiters(chat_id)

    output_chat_id = callback_message.chat.id if callback_message else user_id
    messages_to_delete = set(auth_qr_message_ids.pop(chat_id, set())) if chat_id is not None else set()
    for key in ("prompt_message_id", "status_message_id"):
        try:
            message_id = int(state.get(key) or 0)
        except (TypeError, ValueError):
            message_id = 0
        if message_id:
            messages_to_delete.add((output_chat_id, message_id))
    clicked_message = None
    if callback_message:
        clicked_message = (callback_message.chat.id, callback_message.message_id)
        messages_to_delete.discard(clicked_message)
        try:
            bot.delete_message(*clicked_message)
        except Exception:
            pass
    try:
        bot.send_message(output_chat_id, SETUP_CANCEL_NOTICE, **NO_PREVIEW)
    except Exception as error:
        debug_logger.info(
            "SETUP_CANCEL_NOTICE_FAILED user_id=%s error_type=%s",
            user_id, type(error).__name__,
        )
    for target_chat_id, message_id in messages_to_delete:
        try:
            bot.delete_message(target_chat_id, message_id)
        except Exception:
            pass

    if chat_id is not None:
        try:
            invite_chat_id = int(chat_id)
        except (TypeError, ValueError):
            invite_chat_id = None
        if invite_chat_id is not None:
            try:
                _clear_project_invite_pending(invite_chat_id, delete_admin_messages=True)
            except Exception as error:
                debug_logger.info(
                    "SETUP_CANCEL_INVITE_CLEANUP_FAILED chat_id=%s error_type=%s",
                    chat_id, type(error).__name__,
                )
    if session:
        try:
            remote_auth_service.revoke_session(session)
        except Exception:
            pass
        try:
            session.close()
        except Exception:
            pass


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
        mode = "ваш MAX-аккаунт" if chat.get("account_mode") != "project" else "готовый аккаунт"
        lines.append(
            f"\n<b>{index}. {escape_html(chat.get('title') or 'Группа')}</b>\n"
            f"{state} · {mode}"
        )
        if chat.get("account_mode") == "project" and chat.get("max_url") and not chat.get("is_active"):
            keyboard.add(types.InlineKeyboardButton(
                f"🔎 Проверить доступ №{index}",
                callback_data=f"project_retry_{chat['chat_id']}",
            ))
        keyboard.add(types.InlineKeyboardButton(
            f"🗑 Удалить №{index}",
            callback_data=f"mychat_delete_{chat['chat_id']}"
        ))
    if message_id:
        safe_edit_message("\n".join(lines), chat_id, message_id, reply_markup=keyboard, parse_mode="HTML", **NO_PREVIEW)
    else:
        bot.send_message(chat_id, "\n".join(lines), reply_markup=keyboard, parse_mode="HTML", **NO_PREVIEW)


def _clear_project_invite_pending(chat_id, delete_admin_messages=False):
    pending = project_invite_pending.pop(int(chat_id), None)
    if not pending:
        return None
    prompt_id = pending.get("source_prompt_id")
    if prompt_id:
        project_admin_source_prompts.pop(int(prompt_id), None)
        if delete_admin_messages:
            try:
                bot.delete_message(ADMIN_ID, int(prompt_id))
            except Exception:
                pass
    if delete_admin_messages and pending.get("admin_message_id"):
        try:
            bot.edit_message_reply_markup(
                ADMIN_ID, int(pending["admin_message_id"]), reply_markup=None
            )
        except Exception:
            pass
    return pending


def _delete_chat_everywhere(chat_id):
    chat_id = int(chat_id)
    with CHAT_LIFECYCLE_LOCK:
        _clear_project_invite_pending(chat_id, delete_admin_messages=True)
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
        "Чтобы читать закрытую группу, готовому аккаунту <code>"
        f"{escape_html(MAX_PROJECT_ACCOUNT_PHONE)}</code> нужен доступ: добавьте его сами или отправьте ссылку-приглашение. "
        "Приглашение бот передаст администратору: он вручную откроет ссылку и вступит. "
        "После вступления администратор отправит боту обычную ссылку на чат для проверки. "
        "Если MAX попросит одобрить заявку, это должен сделать администратор. "
        "Для канала аккаунту достаточно доступа к публикациям — права администратора не нужны. "
        "Если добавить аккаунт или вступить по приглашению нельзя, этот способ не подойдёт: выберите подключение через свой аккаунт MAX."
    )


def _project_source_type_keyboard(chat_id):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton(
        "📢 Канал",
        callback_data=f"project_source_channel_{chat_id}",
    ))
    keyboard.add(types.InlineKeyboardButton(
        "👥 Группа или чат",
        callback_data=f"project_source_group_{chat_id}",
    ))
    keyboard.add(_setup_cancel_button(chat_id))
    return keyboard


def _project_source_type_text(chat_id):
    chat = get_chat_by_id(chat_id)
    title = escape_html((chat or {}).get("title") or f"Группа {chat_id}")
    return (
        f"✅ Telegram-группа <b>{title}</b> выбрана для получения сообщений.\n\n"
        "<b>Шаг 2 из 2.</b> Что хотите подключить в MAX?"
    )


def _project_source_instructions(owner_id, source_type, chat_id):
    setup_state = customer_setup_state.setdefault(
        int(owner_id), {"chat_id": int(chat_id), "mode": "project"}
    )
    setup_state["source_type"] = source_type
    setup_state["screen"] = "source_instructions"
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    if source_type == "group":
        keyboard.add(types.InlineKeyboardButton(
            "🔗 Ссылка приглашения",
            callback_data=f"project_help_invite_{chat_id}",
        ))
        text = (
            "<b>Шаг 2 из 2 · Группа или чат</b>\n\n"
            f"1) Добавьте в вашу MAX группу готовый аккаунт <code>{escape_html(MAX_PROJECT_ACCOUNT_PHONE)}</code>. "
            "2) Затем перейдите по ссылке https://web.max.ru/, войдите в свой аккаунт, нажмите на свою группу, "
            "скопируйе ссылку на группу и отправте сюда в чат"
            "После этого бот проверит доступность бота и сообщит вам после проверки. "
            "Если у вас будут какие-то вопросы или проблемы, то пишите /help"
        )
    else:
        keyboard.add(types.InlineKeyboardButton(
            "🔗 Канал закрытый",
            callback_data=f"project_help_channel_{chat_id}",
        ))
        text = (
            "<b>Шаг 2 из 2 · Канал</b>\n\n"
            "1) Перейдите по ссылке https://web.max.ru/ и войдите в свой аккаунт"
            "2) Нажмите на канал, который вы хотите подключить и скопируйте ссылку."
            "3) Пришлите ссылку сюда в чат. После этого бот проверить доступность этого канала."
            "<b>Если возникли вопросы или что-то не получилось, то пишите</b> /help"
        )
    keyboard.add(types.InlineKeyboardButton(
        "🔙 Назад к выбору чата",
        callback_data=f"project_back_type_{chat_id}",
    ))
    keyboard.add(_setup_cancel_button(chat_id))
    return text, keyboard


def _project_cannot_add_prompt(owner_id, chat_id):
    setup_state = customer_setup_state.setdefault(
        int(owner_id), {"chat_id": int(chat_id), "mode": "project"}
    )
    setup_state.update({"source_type": "group", "screen": "cannot_add"})
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton(
        "💬 Написать в поддержку",
        callback_data=f"project_help_support_{chat_id}",
    ))
    keyboard.add(types.InlineKeyboardButton(
        "🔙 Назад к шагу группы",
        callback_data=f"project_back_group_{chat_id}",
    ))
    keyboard.add(_setup_cancel_button(chat_id))
    text = (
        "Не получается добавить готовый аккаунт в чат?\n\n"
        "Напишите в поддержку — поможем выбрать способ подключения. Если MAX отправит запрос на вступление, "
        "его должен одобрить администратор. После вступления понадобится обычная ссылка на чат.\n\n"
        "В закрытую группу готовый аккаунт сможет войти, только если администратор добавит его "
        "либо разрешит вступить по ссылке. Без такого доступа MAX не покажет сообщения для пересылки."
    )
    return text, keyboard


def _project_invite_prompt(owner_id, chat_id):
    setup_state = customer_setup_state.setdefault(
        int(owner_id), {"chat_id": int(chat_id), "mode": "project"}
    )
    source_type = setup_state.get("source_type") or "group"
    setup_state.update({"source_type": source_type, "screen": "invite"})
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton(
        "💬 Написать в поддержку",
        callback_data=f"project_help_support_{chat_id}",
    ))
    back_action = "project_back_channel" if source_type == "channel" else "project_back_group"
    back_label = "🔙 Назад к шагу канала" if source_type == "channel" else "🔙 Назад к шагу группы"
    keyboard.add(types.InlineKeyboardButton(back_label, callback_data=f"{back_action}_{chat_id}"))
    keyboard.add(_setup_cancel_button(chat_id))
    return (
        f"Отправьте ссылку-приглашение на MAX {('группу' if source_type == 'group' else 'канал')}. "
        "После этого администратор сам все настроит, если будут проблемы, то напишет вам.",
        keyboard,
    )


def _forward_project_invite_to_admin(owner_id, chat_id, invite_url):
    """Send an invite to the project admin for manual joining, never auto-join it."""
    owner_id = int(owner_id)
    chat_id = int(chat_id)
    chat = get_chat_by_id(chat_id)
    state = customer_setup_state.get(owner_id) or {}
    if (
        not chat
        or chat.get("account_mode") != "project"
        or chat.get("is_active")
        or int(chat.get("owner_id") or 0) != owner_id
        or int(state.get("chat_id") or 0) != chat_id
    ):
        return False

    request_id = uuid.uuid4().hex[:10]
    user = None
    try:
        user = bot.get_chat(owner_id)
    except Exception:
        pass
    username = f"@{escape_html(user.username)}" if user and user.username else "никнейм не указан"
    destination_title = escape_html(chat.get("title") or f"Telegram-группа {chat_id}")
    max_source_label = "закрытый канал" if state.get("source_type") == "channel" else "закрытая группа или чат"
    safe_url = escape_html(invite_url).replace('"', "&quot;")

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton("🔗 Открыть приглашение", url=invite_url))
    keyboard.add(types.InlineKeyboardButton(
        "✅ Вступил — отправить ссылку MAX",
        callback_data=f"project_invite_admin_joined_{chat_id}_{request_id}",
    ))
    keyboard.add(types.InlineKeyboardButton(
        "⚠️ Не удалось вступить",
        callback_data=f"project_invite_admin_failed_{chat_id}_{request_id}",
    ))
    admin_text = (
        "🔐 <b>Нужно вручную вступить в MAX</b>\n\n"
        f"Пользователь: <code>{owner_id}</code> · {username}\n"
        f"Группа получения: <b>{destination_title}</b>\n"
        f"Чат MAX: {max_source_label}\n\n"
        f"Ссылка-приглашение: <a href=\"{safe_url}\">открыть в MAX</a>\n"
        f"<code>{safe_url}</code>\n\n"
        "Откройте ссылку и вступите или подпишитесь вручную с готового аккаунта. "
        "Если MAX отправит заявку, дождитесь её одобрения. Затем нажмите кнопку ниже."
    )
    try:
        admin_message = bot.send_message(
            ADMIN_ID,
            admin_text,
            reply_markup=keyboard,
            parse_mode="HTML",
            **NO_PREVIEW,
        )
    except Exception as error:
        debug_logger.warning(
            "PROJECT_INVITE_FORWARD_FAILED owner_id=%s chat_id=%s error_type=%s",
            owner_id, chat_id, type(error).__name__,
        )
        return False

    project_invite_pending[chat_id] = {
        "owner_id": owner_id,
        "request_id": request_id,
        "invite_url": invite_url,
        "admin_message_id": admin_message.message_id,
        "awaiting_source_url": False,
    }
    try:
        # Do not allow the scheduler or post-login checks to reuse an older
        # source URL while the admin is still joining from the invitation.
        update_chat_url(chat_id, None)
    except Exception as error:
        project_invite_pending.pop(chat_id, None)
        try:
            bot.edit_message_reply_markup(
                ADMIN_ID, admin_message.message_id, reply_markup=None
            )
        except Exception:
            pass
        debug_logger.error(
            "PROJECT_INVITE_CLEAR_SOURCE_FAILED owner_id=%s chat_id=%s error_type=%s",
            owner_id, chat_id, type(error).__name__,
        )
        return False

    state.update({"screen": "invite_forwarded", "status_message_id": None})
    prompt_id = state.get("prompt_message_id")
    if prompt_id:
        try:
            bot.edit_message_reply_markup(owner_id, int(prompt_id), reply_markup=None)
        except Exception:
            pass
    status_keyboard = types.InlineKeyboardMarkup(row_width=1)
    status_keyboard.add(_setup_cancel_button(chat_id))
    source_label = "канал" if state.get("source_type") == "channel" else "группу"
    try:
        sent = bot.send_message(
            owner_id,
            "✅ Ссылка-приглашение передана! "
            f"Пожалуйста, ожидайте!",
            reply_markup=status_keyboard,
            **NO_PREVIEW,
        )
        state["status_message_id"] = sent.message_id
    except Exception as error:
        debug_logger.warning(
            "PROJECT_INVITE_USER_STATUS_FAILED owner_id=%s chat_id=%s error_type=%s",
            owner_id, chat_id, type(error).__name__,
        )
    return True


def _send_project_admin_source_prompt(chat_id, pending):
    """Ask the admin for the ordinary MAX URL, tied to this customer's request."""
    chat_id = int(chat_id)
    owner_id = int(pending.get("owner_id") or 0)
    chat = get_chat_by_id(chat_id) or {}
    old_prompt_id = pending.get("source_prompt_id")
    if old_prompt_id:
        project_admin_source_prompts.pop(int(old_prompt_id), None)
    source_label = "канала" if (customer_setup_state.get(owner_id) or {}).get("source_type") == "channel" else "группы или чата"
    text = (
        "✅ После ручного вступления отправьте обычную ссылку MAX ответом на это сообщение.\n\n"
        f"Чат MAX: {source_label} · запрос пользователя <code>{owner_id}</code>.\n"
        f"Группа получения: <b>{escape_html(chat.get('title') or str(chat_id))}</b>.\n"
        f"ID подключения: <code>{chat_id}</code> · запрос: <code>{pending.get('request_id') or '—'}</code>.\n\n"
        "Нужна ссылка на сам чат/канал (например, https://web.max.ru/…). "
        "Ссылку-приглашение повторно не отправляйте."
    )
    prompt = bot.send_message(
        ADMIN_ID,
        text,
        reply_markup=types.ForceReply(),
        parse_mode="HTML",
        **NO_PREVIEW,
    )
    pending["source_prompt_id"] = int(prompt.message_id)
    pending["awaiting_source_url"] = True
    project_admin_source_prompts[int(prompt.message_id)] = {
        "chat_id": chat_id,
        "request_id": pending.get("request_id"),
        "owner_id": owner_id,
    }
    return prompt


def _is_project_admin_source_message(message):
    if (
        message.chat.type != "private"
        or not is_admin(message.from_user.id)
        or not message.text
        or message.text.startswith("/")
    ):
        return False
    reply = getattr(message, "reply_to_message", None)
    if reply and int(reply.message_id) in project_admin_source_prompts:
        return True
    if message.from_user.id in customer_setup_state or not _is_max_source_url(message.text.strip()):
        return False
    waiting = [
        pending for pending in project_invite_pending.values()
        if pending.get("awaiting_source_url")
    ]
    return len(waiting) == 1


def _project_access_retry_keyboard(chat_id, allow_edit=True):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton(
        "🔁 Проверить доступ ещё раз",
        callback_data=f"project_retry_{chat_id}",
    ))
    if allow_edit:
        keyboard.add(types.InlineKeyboardButton(
            "✏️ Исправить ссылку",
            callback_data=f"project_edit_source_{chat_id}",
        ))
    keyboard.add(types.InlineKeyboardButton(
        "💬 Написать в поддержку",
        callback_data="support_start",
    ))
    return keyboard


def _edit_setup_prompt(owner_id, setup_state, text, reply_markup=None, parse_mode=None):
    """Reuse one setup message so old step keyboards cannot remain active."""
    prompt_id = setup_state.get("prompt_message_id")
    if prompt_id:
        try:
            bot.edit_message_text(
                text=text,
                chat_id=int(owner_id),
                message_id=int(prompt_id),
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                **NO_PREVIEW,
            )
            return int(prompt_id)
        except Exception as error:
            debug_logger.info(
                "CUSTOMER_SETUP_PROMPT_EDIT_FAILED user_id=%s error_type=%s",
                owner_id, type(error).__name__,
            )
    sent = bot.send_message(
        int(owner_id),
        text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
        **NO_PREVIEW,
    )
    setup_state["prompt_message_id"] = sent.message_id
    return sent.message_id


def _verify_project_chat_access(
    chat_id, notify_chat_id=None, progress_message_id=None, refresh_list_message_id=None
):
    chat = get_chat_by_id(chat_id)
    owner_id = int(chat.get("owner_id") or 0) if chat else 0
    destination = int(notify_chat_id or owner_id or ADMIN_ID)
    pending_state = customer_setup_state.get(owner_id) or {}
    setup_prompt_id = None
    status_message_id = None
    if int(pending_state.get("chat_id") or 0) == int(chat_id):
        try:
            setup_prompt_id = int(pending_state.get("prompt_message_id") or 0) or None
        except (TypeError, ValueError):
            setup_prompt_id = None
        try:
            status_message_id = int(pending_state.get("status_message_id") or 0) or None
        except (TypeError, ValueError):
            status_message_id = None
    # Keep the setup instructions intact. Progress and result belong in a
    # separate status message, including automatic checks after login.
    report_message_id = progress_message_id or status_message_id

    def remember_status_message(message_id):
        messages = project_access_message_ids.setdefault(int(chat_id), set())
        messages.add((int(destination), int(message_id)))
        while len(messages) > 25:
            messages.pop()

    def refresh_chat_list():
        if not refresh_list_message_id or owner_id <= 0:
            return
        try:
            _show_my_chats(owner_id, owner_id, refresh_list_message_id)
        except Exception as error:
            debug_logger.info(
                "PROJECT_ACCESS_LIST_REFRESH_FAILED chat_id=%s error_type=%s",
                chat_id, type(error).__name__,
            )

    def report(text, reply_markup=None):
        if not destination:
            return False
        if report_message_id:
            try:
                bot.edit_message_text(
                    text=text,
                    chat_id=destination,
                    message_id=report_message_id,
                    reply_markup=reply_markup,
                    **NO_PREVIEW,
                )
                if reply_markup is not None:
                    remember_status_message(report_message_id)
                return True
            except Exception as error:
                debug_logger.info(
                    "PROJECT_ACCESS_STATUS_EDIT_FAILED chat_id=%s error_type=%s",
                    chat_id, type(error).__name__,
                )
        try:
            sent = bot.send_message(
                destination,
                text,
                reply_markup=reply_markup,
                **NO_PREVIEW,
            )
            if reply_markup is not None:
                remember_status_message(sent.message_id)
            return True
        except Exception as error:
            debug_logger.warning(
                "PROJECT_ACCESS_STATUS_SEND_FAILED chat_id=%s destination=%s error_type=%s",
                chat_id, destination, type(error).__name__,
            )
            return False

    def clear_project_access_buttons():
        tracked = project_access_message_ids.pop(int(chat_id), set())
        if setup_prompt_id:
            tracked.add((int(owner_id), int(setup_prompt_id)))
        for target_chat_id, target_message_id in tracked:
            try:
                bot.edit_message_reply_markup(
                    target_chat_id, target_message_id, reply_markup=None
                )
            except Exception:
                pass

    if not chat or chat.get("account_mode") != "project" or not chat.get("max_url"):
        if progress_message_id:
            report("Проверка остановлена: это подключение удалено или ссылка на чат MAX отсутствует.")
        refresh_chat_list()
        return False
    session_path = _session_path_for_chat(chat)
    if not os.path.exists(session_path):
        report(
            "Ссылка сохранена. Готовый аккаунт ещё не подключён. "
            "После его авторизации бот автоматически проверит доступ к этому чату.",
            reply_markup=_project_access_retry_keyboard(
                chat_id, allow_edit=not bool(chat.get("is_active"))
            ),
        )
        refresh_chat_list()
        return False
    try:
        verification = parse_max_group_media(
            group_url=chat["max_url"],
            chat_id=chat_id,
            session_path=session_path,
            verify_only=True,
            navigation_timeout_ms=12000,
            navigation_attempts=2,
        )
        with CHAT_LIFECYCLE_LOCK:
            current = get_chat_by_id(chat_id)
            if (
                not current
                or current.get("account_mode") != "project"
                or current.get("max_url") != chat.get("max_url")
                or int(current.get("owner_id") or 0) != owner_id
            ):
                report("Проверка остановлена: подключение было удалено или изменено.")
                return False
            # A successful retry must enable parsing; a failed retry never turns
            # an already active chat off.
            toggle_chat(chat_id, True)
            current = get_chat_by_id(chat_id)
            if not current or not current.get("is_active"):
                raise RuntimeError("CHAT_ACTIVATION_NOT_PERSISTED")
        if int(pending_state.get("chat_id") or 0) == int(chat_id):
            customer_setup_state.pop(owner_id, None)
        if setup_prompt_id and setup_prompt_id != report_message_id:
            try:
                bot.edit_message_reply_markup(owner_id, setup_prompt_id, reply_markup=None)
            except Exception:
                pass
        report("✅ Проверка завершена. MAX открыл чат, пересылка включена.")
        try:
            bot.send_message(
                owner_id,
                "🎉 Подключение завершено!\n\n"
                "MAX открыл чат, и пересылка включена. Бот будет отправлять сообщения "
                "в выбранную Telegram-группу.",
                **NO_PREVIEW,
            )
        except Exception as notify_error:
            debug_logger.error(
                "PROJECT_ACCESS_SUCCESS_NOTIFY_FAILED chat_id=%s owner_id=%s error_type=%s",
                chat_id, owner_id, type(notify_error).__name__,
            )
        clear_project_access_buttons()
        refresh_chat_list()
        if owner_id > 0:
            try:
                _offer_review_after_connection(
                    owner_id,
                    f"{chat_id}:{chat.get('account_mode') or 'project'}",
                )
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
        latest_chat = get_chat_by_id(chat_id)
        keyboard = _project_access_retry_keyboard(
            chat_id,
            allow_edit=not bool(latest_chat and latest_chat.get("is_active")),
        )
        if "MAX_INVITE_MANUAL_REQUIRED" in str(error):
            access_message = (
                "Не удалось подтвердить, что готовый аккаунт видит сообщения в этом чате. "
                "Проверьте ссылку и доступ аккаунта, затем нажмите «Проверить доступ ещё раз». "
                "Если аккаунт нельзя добавить в группу или чат, напишите в поддержку через /help."
            )
        elif "MAX_INVITE_APPROVAL_PENDING" in str(error):
            access_message = (
                "Заявка готового аккаунта отправлена. Когда администратор одобрит её, "
                "нажмите «Проверить доступ ещё раз»."
            )
        elif "MAX_INVITE_JOIN_REQUIRED" in str(error):
            access_message = (
                "Готовому аккаунту пока не удалось вступить в этот чат. "
                "Если есть ссылка-приглашение, вернитесь к шагу группы и нажмите "
                "«Ссылка приглашения», чтобы отправить ее."
            )
        elif "MAX_CHAT_NO_MESSAGES" in str(error):
            access_message = (
                "MAX открыл ссылку, но не показал доступных сообщений. Пересылку не включил. "
                "Проверьте, что готовый аккаунт действительно добавлен в группу или вступил по приглашению."
            )
        elif "CHAT_ACTIVATION_NOT_PERSISTED" in str(error):
            access_message = (
                "MAX подтвердил доступ, но не удалось сохранить включённый парсинг. "
                "Нажмите «Проверить доступ ещё раз» или напишите в поддержку."
            )
        elif "SESSION_EXPIRED" in str(error):
            access_message = (
                "Сессия готового аккаунта завершилась. Администратору нужно повторно войти в MAX, "
                "после этого проверку можно запустить ещё раз."
            )
        else:
            access_message = (
                "Не удалось подтвердить, что готовый аккунт видит сообщения в этой группе. "
                "Проверьте ссылку и доступ аккаунта, затем нажмите «Проверить доступ ещё раз». "
                "Если возникли проблемы и вопросы, то жмите /help."
            )
        report("⚠️ " + access_message, reply_markup=keyboard)
        refresh_chat_list()
        if setup_prompt_id and setup_prompt_id != report_message_id:
            try:
                bot.edit_message_reply_markup(owner_id, setup_prompt_id, reply_markup=None)
            except Exception:
                pass
        return False


def _verify_project_chats_after_login(notify_chat_id):
    for chat in get_all_chats():
        if chat.get("account_mode") == "project" and chat.get("max_url"):
            _verify_project_chat_access(
                chat["chat_id"],
                int(chat.get("owner_id") or notify_chat_id),
            )


def _begin_customer_group_setup(chat_id, owner_id, account_mode):
    if _user_needs_subscription(owner_id, account_mode):
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
        bot.send_message(owner_id, _chat_limit_notice(get_customer_chat_limit(owner_id)), **NO_PREVIEW)
        return False
    if not claimed:
        pending_setup_modes.pop(owner_id, None)
        bot.send_message(
            owner_id,
            "Эта Telegram-группа уже подключена к другому аккаунту. Выберите свою группу и повторите /bind.",
            **NO_PREVIEW,
        )
        return False
    _clear_project_invite_pending(int(chat_id), delete_admin_messages=True)
    pending_setup_modes.pop(owner_id, None)
    customer_setup_state[owner_id] = {
        "chat_id": chat_id,
        "mode": account_mode,
        "screen": "source_type" if account_mode == "project" else "source_url",
    }
    if account_mode == "project":
        sent = bot.send_message(
            owner_id,
            _project_source_type_text(chat_id),
            parse_mode="HTML",
            reply_markup=_project_source_type_keyboard(chat_id),
            **NO_PREVIEW,
        )
        customer_setup_state[owner_id]["prompt_message_id"] = sent.message_id
        return True

    setup_keyboard = types.InlineKeyboardMarkup(row_width=1)
    setup_keyboard.add(_setup_cancel_button(chat_id))
    sent = bot.send_message(
        owner_id,
        f"✅ Группа <b>{escape_html(title)}</b> закреплена за вами.\n\n"
        f"<b>Шаг 2 из {2 if account_mode == 'project' else 3}.</b> Пришлите ссылку на чат, группу или канал MAX, откуда получать сообщения.\n"
        "Как скопировать ссылку: откройте <a href=\"https://web.max.ru\">web.max.ru</a>, "
        "откройте нужный чат, группу или канал и скопируйте ссылку из меню.\n\n"
        + (f"Для закрытого чата MAX отправьте ссылку-приглашение — бот перешлёт её администратору, "
           f"который вручную вступит с аккаунта <code>{escape_html(MAX_PROJECT_ACCOUNT_PHONE)}</code>. "
           "Если MAX отправит заявку, администратор должен её одобрить. "
           "После вступления администратор отправит боту обычную ссылку на чат MAX для проверки.\n\n"
           if account_mode == "project" else "")
        + "Чтобы остановить настройку, нажмите «❌ Отмена».",
        parse_mode="HTML",
        reply_markup=setup_keyboard,
        **NO_PREVIEW,
    )
    customer_setup_state[owner_id]["prompt_message_id"] = sent.message_id
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


@bot.message_handler(func=_is_project_admin_source_message, content_types=["text"])
def project_admin_source_url(message):
    reply = getattr(message, "reply_to_message", None)
    prompt_id = int(reply.message_id) if reply else 0
    route = project_admin_source_prompts.get(prompt_id) if prompt_id else None
    if route:
        chat_id = int(route.get("chat_id") or 0)
        pending = project_invite_pending.get(chat_id) or {}
        if (
            pending.get("request_id") != route.get("request_id")
            or not pending.get("awaiting_source_url")
        ):
            project_admin_source_prompts.pop(prompt_id, None)
            bot.send_message(message.chat.id, "Этот запрос уже завершён. Используйте актуальное сообщение с просьбой о ссылке.", **NO_PREVIEW)
            return
    else:
        waiting = [
            (int(chat_id), pending)
            for chat_id, pending in project_invite_pending.items()
            if pending.get("awaiting_source_url")
        ]
        if len(waiting) != 1 or message.from_user.id in customer_setup_state:
            bot.send_message(
                message.chat.id,
                "Чтобы не перепутать подключения, отправьте ссылку ответом на сообщение с данными нужного запроса.",
                **NO_PREVIEW,
            )
            return
        chat_id, pending = waiting[0]
        prompt_id = int(pending.get("source_prompt_id") or 0)

    owner_id = int(pending.get("owner_id") or 0)
    state = customer_setup_state.get(owner_id) or {}
    chat = get_chat_by_id(chat_id)
    if (
        not chat
        or chat.get("account_mode") != "project"
        or int(chat.get("owner_id") or 0) != owner_id
        or int(state.get("chat_id") or 0) != chat_id
        or state.get("screen") != "awaiting_admin_source_url"
    ):
        _clear_project_invite_pending(chat_id, delete_admin_messages=True)
        bot.send_message(message.chat.id, "Настройка уже завершена или отменена. Новая ссылка не сохранена.", **NO_PREVIEW)
        return
    if _user_needs_subscription(owner_id, "project"):
        _clear_project_invite_pending(chat_id, delete_admin_messages=True)
        customer_setup_state.pop(owner_id, None)
        bot.send_message(
            owner_id,
            "Срок подписки истёк до завершения настройки. Продлите её командой /subscription, затем повторите подключение.",
            reply_markup=subscription_renewal_keyboard(),
            **NO_PREVIEW,
        )
        return

    url = (message.text or "").strip()
    if not _is_max_source_url(url) or _max_source_link_kind(url) == "invite":
        if prompt_id:
            project_admin_source_prompts.pop(prompt_id, None)
        bot.send_message(
            message.chat.id,
            "Нужна обычная ссылка на сам чат или канал MAX, а не ссылка-приглашение. Отправьте её ответом на новое сообщение ниже.",
            **NO_PREVIEW,
        )
        try:
            _send_project_admin_source_prompt(chat_id, pending)
        except Exception as error:
            debug_logger.warning("PROJECT_ADMIN_SOURCE_PROMPT_FAILED chat_id=%s error_type=%s", chat_id, type(error).__name__)
        return

    try:
        update_chat_url(chat_id, url)
    except Exception as error:
        debug_logger.exception("PROJECT_ADMIN_SOURCE_SAVE_FAILED chat_id=%s error=%s", chat_id, log_preview(error, 220))
        bot.send_message(message.chat.id, "Не удалось сохранить ссылку. Попробуйте отправить её ещё раз ответом на сообщение ниже.", **NO_PREVIEW)
        if prompt_id:
            project_admin_source_prompts.pop(prompt_id, None)
        try:
            _send_project_admin_source_prompt(chat_id, pending)
        except Exception:
            pass
        return

    if prompt_id:
        project_admin_source_prompts.pop(prompt_id, None)
    project_invite_pending.pop(chat_id, None)
    state["screen"] = "verifying"
    status_id = state.get("status_message_id")
    progress_text = "⏳ Администратор отправил обычную ссылку MAX. Проверяю, что готовый аккаунт видит сообщения этой группы."
    progress_message_id = None
    if status_id:
        try:
            bot.edit_message_text(
                progress_text,
                owner_id,
                int(status_id),
                reply_markup=None,
                **NO_PREVIEW,
            )
            progress_message_id = int(status_id)
        except Exception:
            pass
    if progress_message_id is None:
        progress_message_id = bot.send_message(owner_id, progress_text, **NO_PREVIEW).message_id
    state["status_message_id"] = progress_message_id
    bot.send_message(message.chat.id, "✅ Обычная ссылка сохранена. Бот проверяет доступ; результат придёт пользователю.", **NO_PREVIEW)
    threading.Thread(
        target=_verify_project_chat_access,
        args=(chat_id, owner_id, progress_message_id),
        daemon=True,
    ).start()


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
    if _user_needs_subscription(message.from_user.id, state.get("mode")):
        _clear_project_invite_pending(int(state.get("chat_id") or 0), delete_admin_messages=True)
        customer_setup_state.pop(message.from_user.id, None)
        bot.send_message(
            message.chat.id,
            "Срок подписки истёк до завершения настройки. Продлите её командой /subscription, затем повторите подключение.",
            reply_markup=subscription_renewal_keyboard(),
            **NO_PREVIEW,
        )
        return
    url = (message.text or "").strip()
    mode = state.get("mode")
    if mode == "project" and state.get("screen") == "awaiting_admin_source_url":
        bot.send_message(
            message.chat.id,
            "Администратор уже вручную вступил. Теперь он сам отправляет обычную ссылку MAX боту. Вам ничего присылать не нужно.",
            **NO_PREVIEW,
        )
        return
    if mode == "project" and state.get("source_type") not in {"channel", "group"}:
        _edit_setup_prompt(
            message.chat.id,
            state,
            "Сначала выберите тип чата MAX кнопкой в этом сообщении.",
            reply_markup=_project_source_type_keyboard(state["chat_id"]),
            parse_mode="HTML",
        )
        return
    if not _is_max_source_url(url):
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        if mode == "project":
            if state.get("screen") == "invite":
                back_action = "project_back_channel" if state.get("source_type") == "channel" else "project_back_group"
                back_label = "🔙 Назад к шагу канала" if state.get("source_type") == "channel" else "🔙 Назад к шагу группы"
            else:
                back_action = "project_back_type"
                back_label = "🔙 Назад к выбору чата"
            keyboard.add(types.InlineKeyboardButton(back_label, callback_data=f"{back_action}_{state['chat_id']}"))
        keyboard.add(_setup_cancel_button(state["chat_id"]))
        bot.send_message(
            message.chat.id,
            "Не удалось распознать ссылку MAX. Проверьте адрес и отправьте ссылку на канал, группу или чат с домена max.ru.",
            reply_markup=keyboard,
            parse_mode="HTML",
            **NO_PREVIEW,
        )
        return
    chat_id = state["chat_id"]
    link_kind = _max_source_link_kind(url)
    if mode == "project":
        if state.get("screen") == "verifying":
            bot.send_message(
                message.chat.id,
                "Ссылка уже получена, бот проверяет доступ. Дождитесь результата проверки.",
                **NO_PREVIEW,
            )
            return
        if state.get("screen") == "invite_forwarded":
            bot.send_message(
                message.chat.id,
                "Ссылка уже передана администратору. Дождитесь, пока он вручную вступит "
                "и отправит боту обычную ссылку на чат.",
                **NO_PREVIEW,
            )
            return
        if state.get("screen") == "invite" and link_kind != "invite":
            bot.send_message(
                message.chat.id,
                "На этом шаге нужна именно ссылка-приглашение. После ручного вступления администратор "
                "сам отправит боту обычную ссылку на чат.",
                **NO_PREVIEW,
            )
            return
        if link_kind == "invite":
            if _forward_project_invite_to_admin(message.from_user.id, chat_id, url):
                return
            bot.send_message(
                message.chat.id,
                "Не удалось передать приглашение администратору. Попробуйте ещё раз чуть позже "
                "или напишите в поддержку через /help.",
                **NO_PREVIEW,
            )
            return

    update_chat_url(chat_id, url)
    if mode == "self":
        customer_setup_state.pop(message.from_user.id, None)
        try:
            bot.edit_message_text(
                "✅ Ссылка сохранена. Перехожу к авторизации вашего аккаунта MAX.",
                message.chat.id,
                int(state.get("prompt_message_id")),
                reply_markup=None,
                **NO_PREVIEW,
            )
        except Exception:
            pass
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

    state["screen"] = "verifying"
    progress = bot.send_message(
        message.chat.id,
        "⏳ Обычная ссылка на MAX получена. Проверяю, видит ли готовый аккаунт сообщения этой группы.",
        **NO_PREVIEW,
    )
    state["status_message_id"] = progress.message_id
    threading.Thread(
        target=_verify_project_chat_access,
        args=(chat_id, message.chat.id, progress.message_id),
        daemon=True,
    ).start()


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
    review_id = awaiting_review_text.get(user_id)
    if review_id is None:
        awaiting_review_text.pop(user_id, None)
        return
    review_text = (message.text or "").strip()
    if review_text.startswith("/"):
        bot.send_message(
            message.chat.id,
            "Отправьте отзыв обычным текстом или нажмите кнопку «❌ Отмена».",
            reply_markup=get_cancel_keyboard(),
            **NO_PREVIEW,
        )
        return
    if not review_text:
        bot.send_message(
            message.chat.id,
            "Отзыв пустой. Напишите пару слов или нажмите кнопку «❌ Отмена».",
            reply_markup=get_cancel_keyboard(),
            **NO_PREVIEW,
        )
        return
    if len(review_text) > 500:
        bot.send_message(message.chat.id, "Отзыв слишком длинный. Сократите его до 500 символов.", **NO_PREVIEW)
        return
    if not save_pending_review_entry(review_id, user_id, review_text):
        awaiting_review_text.pop(user_id, None)
        bot.send_message(message.chat.id, "Не удалось сохранить отзыв. Откройте приглашение и попробуйте ещё раз.", **NO_PREVIEW)
        return
    awaiting_review_text.pop(user_id, None)
    try:
        _send_review_for_moderation(review_id, user_id, review_text)
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
    and is_admin(m.from_user.id)
    and m.reply_to_message is not None
    and (m.chat.id, m.reply_to_message.message_id) in support_message_routes
    and (
        not m.text
        or not m.text.startswith("/")
        or m.text.strip().lower() == "/close"
    ),
    content_types=["text", "photo", "video", "document", "audio", "voice", "animation", "sticker", "video_note", "location", "contact", "venue", "poll"],
)
def support_admin_reply(message):
    route_key = (int(message.chat.id), int(message.reply_to_message.message_id))
    user_id = support_message_routes.get(route_key)
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
    title = "готового аккаунта" if target_chat_id == "project_account" else (chat["title"] if chat else "MAX")
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
@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_") or call.data.startswith("qr_") or call.data.startswith("support_close_") or call.data.startswith("review_") or call.data.startswith("reviews_") or call.data.startswith("project_retry_") or call.data.startswith("project_edit_source_") or call.data.startswith("project_source_channel_") or call.data.startswith("project_source_group_") or call.data.startswith("project_add_help_") or call.data.startswith("project_help_invite_") or call.data.startswith("project_invite_admin_") or call.data.startswith("project_help_channel_") or call.data.startswith("project_help_support_") or call.data.startswith("project_back_") or call.data.startswith("setup_cancel_") or call.data in ["delete_this_msg", "cancel_step"])
def admin_callbacks(call):
    track_user(call)
    action = call.data
    if action.startswith(("project_invite_admin_joined_", "project_invite_admin_failed_")):
        if not is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "⛔️ Нет прав", show_alert=True)
            return
        joined_prefix = "project_invite_admin_joined_"
        failed_prefix = "project_invite_admin_failed_"
        prefix = joined_prefix if action.startswith(joined_prefix) else failed_prefix
        try:
            raw_chat_id, request_id = action[len(prefix):].rsplit("_", 1)
            invite_chat_id = int(raw_chat_id)
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Некорректная заявка", show_alert=True)
            return
        pending = project_invite_pending.get(invite_chat_id) or {}
        if pending.get("request_id") != request_id:
            bot.answer_callback_query(call.id, "Эта ссылка уже обработана или заменена")
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                pass
            return
        owner_id = int(pending.get("owner_id") or 0)
        invite_state = customer_setup_state.get(owner_id) or {}
        if int(invite_state.get("chat_id") or 0) != invite_chat_id:
            _clear_project_invite_pending(invite_chat_id, delete_admin_messages=True)
            bot.answer_callback_query(call.id, "Настройка пользователя уже завершена")
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                pass
            return
        if action.startswith(joined_prefix) and pending.get("awaiting_source_url"):
            bot.answer_callback_query(call.id, "Ожидается обычная ссылка от администратора")
            return
        if action.startswith(failed_prefix):
            _clear_project_invite_pending(invite_chat_id, delete_admin_messages=False)
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        status_id = invite_state.get("status_message_id")
        if status_id:
            try:
                bot.delete_message(owner_id, int(status_id))
            except Exception:
                pass
            invite_state["status_message_id"] = None
        if action.startswith(joined_prefix):
            invite_state["screen"] = "awaiting_admin_source_url"
            prompt_id = invite_state.get("prompt_message_id")
            if prompt_id:
                try:
                    bot.edit_message_reply_markup(owner_id, int(prompt_id), reply_markup=None)
                except Exception:
                    pass
            chat = get_chat_by_id(invite_chat_id) or {}
            source_label = "канал" if invite_state.get("source_type") == "channel" else "группу или чат"
            keyboard = types.InlineKeyboardMarkup(row_width=1)
            keyboard.add(_setup_cancel_button(invite_chat_id))
            sent = bot.send_message(
                owner_id,
                "✅ Администратор подтвердил, что готовый аккаунт вступил в группу.\n\n"
                "Я проверю доступ и сообщу вам результат. От вас больше ничего присылать не нужно.",
                reply_markup=keyboard,
                **NO_PREVIEW,
            )
            invite_state["status_message_id"] = sent.message_id
            try:
                _send_project_admin_source_prompt(invite_chat_id, pending)
            except Exception as error:
                debug_logger.exception("PROJECT_ADMIN_SOURCE_PROMPT_FAILED chat_id=%s error=%s", invite_chat_id, log_preview(error, 220))
                bot.send_message(
                    call.message.chat.id,
                    "Не удалось запросить ссылку MAX. Повторите нажатие «Вступил» после проверки подключения бота.",
                    **NO_PREVIEW,
                )
        else:
            invite_state["screen"] = "invite"
            _, invite_keyboard = _project_invite_prompt(owner_id, invite_chat_id)
            prompt_id = invite_state.get("prompt_message_id")
            if prompt_id:
                try:
                    bot.edit_message_reply_markup(owner_id, int(prompt_id), reply_markup=invite_keyboard)
                except Exception:
                    pass
            retry_keyboard = types.InlineKeyboardMarkup(row_width=1)
            retry_keyboard.add(types.InlineKeyboardButton(
                "🔁 Подключить ещё раз",
                callback_data=f"project_help_invite_{invite_chat_id}",
            ))
            bot.send_message(
                owner_id,
                "⚠️ Администратору не удалось вступить по этой ссылке. Проверьте, что ссылка ещё действует "
                "и разрешает вступление, затем отправьте новую.",
                reply_markup=retry_keyboard,
                **NO_PREVIEW,
            )
        return
    is_allowed_customer_qr = False
    is_allowed_customer_review = action == "reviews_show" or action.startswith("reviews_page_")
    is_allowed_project_retry = False
    is_allowed_project_setup = False
    is_allowed_setup_cancel = False
    is_allowed_customer_cancel = False
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
            review_id = int(action.rsplit("_", 1)[1])
            is_allowed_customer_review = get_review_entry_owner(review_id) == int(call.from_user.id)
        except (ValueError, IndexError):
            is_allowed_customer_review = False
    if not is_admin(call.from_user.id) and action.startswith(("project_retry_", "project_edit_source_")):
        try:
            retry_chat = get_chat_by_id(int(action.rsplit("_", 1)[1]))
            is_allowed_project_retry = bool(
                retry_chat
                and retry_chat.get("account_mode") == "project"
                and int(retry_chat.get("owner_id") or 0) == int(call.from_user.id)
            )
        except (ValueError, IndexError, TypeError):
            is_allowed_project_retry = False
    if not is_admin(call.from_user.id) and action.startswith(("project_source_channel_", "project_source_group_", "project_add_help_", "project_help_invite_", "project_help_channel_", "project_help_support_", "project_back_")):
        try:
            setup_chat_id = int(action.rsplit("_", 1)[1])
            setup_chat = get_chat_by_id(setup_chat_id)
            setup_state = customer_setup_state.get(int(call.from_user.id)) or {}
            is_allowed_project_setup = bool(
                setup_chat
                and setup_chat.get("account_mode") == "project"
                and int(setup_chat.get("owner_id") or 0) == int(call.from_user.id)
                and setup_state.get("mode") == "project"
                and int(setup_state.get("chat_id") or 0) == setup_chat_id
            )
        except (ValueError, IndexError, TypeError):
            is_allowed_project_setup = False
    if not is_admin(call.from_user.id) and action.startswith("setup_cancel_"):
        try:
            cancel_chat_id = int(action.rsplit("_", 1)[1])
            cancel_chat = get_chat_by_id(cancel_chat_id)
            cancel_state = customer_setup_state.get(int(call.from_user.id)) or {}
            is_allowed_setup_cancel = bool(
                cancel_chat
                and int(cancel_chat.get("owner_id") or 0) == int(call.from_user.id)
                and int(cancel_state.get("chat_id") or 0) == cancel_chat_id
            )
        except (ValueError, IndexError, TypeError):
            is_allowed_setup_cancel = False
    if not is_admin(call.from_user.id) and action == "cancel_step":
        customer_id = int(call.from_user.id)
        is_allowed_customer_cancel = (
            customer_id in pending_setup_modes
            or customer_id in customer_setup_state
            or customer_id in awaiting_review_text
        )
        if not is_allowed_customer_cancel:
            try:
                is_allowed_customer_cancel = get_user_writing_review_id(customer_id) is not None
            except Exception:
                is_allowed_customer_cancel = False
    if not is_admin(call.from_user.id) and action.startswith((
        "project_source_channel_", "project_source_group_", "project_add_help_",
        "project_help_invite_", "project_help_channel_", "project_help_support_",
        "project_back_", "setup_cancel_",
    )):
        try:
            stale_chat_id = int(action.rsplit("_", 1)[1])
            stale_chat = get_chat_by_id(stale_chat_id)
            if (
                stale_chat
                and stale_chat.get("is_active")
                and int(stale_chat.get("owner_id") or 0) == int(call.from_user.id)
            ):
                bot.answer_callback_query(call.id, "Подключение уже завершено")
                try:
                    bot.edit_message_reply_markup(
                        call.message.chat.id, call.message.message_id, reply_markup=None
                    )
                except Exception:
                    pass
                return
        except (ValueError, IndexError, TypeError):
            pass
    if not is_admin(call.from_user.id) and not is_allowed_customer_qr and not is_allowed_customer_review and not is_allowed_project_retry and not is_allowed_project_setup and not is_allowed_setup_cancel and not is_allowed_customer_cancel:
        bot.answer_callback_query(call.id, "⛔️ Нет прав", show_alert=True)
        return
    if action.startswith("setup_cancel_"):
        try:
            cancel_chat_id = int(action.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Некорректное подключение", show_alert=True)
            return
        cancel_state = customer_setup_state.get(int(call.from_user.id)) or {}
        cancel_chat = get_chat_by_id(cancel_chat_id)
        if cancel_chat and cancel_chat.get("is_active"):
            bot.answer_callback_query(call.id, "Подключение уже завершено")
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                pass
            return
        if (
            not cancel_chat
            or int(cancel_chat.get("owner_id") or 0) != int(call.from_user.id)
            or int(cancel_state.get("chat_id") or 0) != cancel_chat_id
        ):
            bot.answer_callback_query(call.id, "Этот шаг уже завершён")
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                pass
            return
        bot.answer_callback_query(call.id, "Настройка отменена")
        _cancel_customer_connection(
            call.from_user.id,
            cancel_chat_id,
            callback_message=call.message,
        )
        return
    if action.startswith("support_close_"):
        try:
            user_id = int(action.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Не удалось определить обращение", show_alert=True)
            return
        route_key = (int(call.message.chat.id), int(call.message.message_id))
        if support_message_routes.get(route_key) != user_id:
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
        user_id = int(call.from_user.id)
        review_is_writing = user_id in awaiting_review_text
        review_id = awaiting_review_text.get(user_id)
        if not review_is_writing:
            try:
                review_id = get_user_writing_review_id(user_id)
                review_is_writing = review_id is not None
            except Exception:
                review_is_writing = False
        notice = None
        try:
            bot.clear_step_handler(call.message)
            if review_is_writing:
                cancel_review_entry(review_id, user_id)
                awaiting_review_text.pop(user_id, None)
                notice = "Отзыв отменён. Спасибо, что подключились!"
            elif user_id in customer_setup_state or user_id in pending_setup_modes:
                setup_state = customer_setup_state.get(user_id) or {}
                setup_chat_id = setup_state.get("chat_id")
                if setup_chat_id is not None:
                    try:
                        setup_chat_id = int(setup_chat_id)
                    except (TypeError, ValueError):
                        setup_chat_id = None
                bot.answer_callback_query(call.id, "Настройка отменена")
                _cancel_customer_connection(
                    user_id,
                    setup_chat_id,
                    callback_message=call.message,
                )
                return
            elif is_admin(call.from_user.id):
                pending_chats = getattr(process_add_chat_step1, "pending_chats", None)
                if isinstance(pending_chats, dict):
                    pending_chats.pop(user_id, None)
            else:
                bot.answer_callback_query(call.id, "Сейчас нечего отменять")
                return
            bot.delete_message(call.message.chat.id, call.message.message_id)
            bot.answer_callback_query(call.id, "❌ Отменено")
            if notice:
                bot.send_message(call.message.chat.id, notice, **NO_PREVIEW)
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
    if action.startswith(("project_source_channel_", "project_source_group_", "project_add_help_", "project_help_invite_", "project_help_channel_", "project_help_support_", "project_back_")):
        try:
            setup_chat_id = int(action.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Некорректное подключение", show_alert=True)
            return
        setup_state = customer_setup_state.get(int(call.from_user.id)) or {}
        setup_chat = get_chat_by_id(setup_chat_id)
        if setup_chat and setup_chat.get("is_active"):
            bot.answer_callback_query(call.id, "Подключение уже завершено")
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                pass
            return
        if (
            not setup_chat
            or setup_chat.get("account_mode") != "project"
            or int(setup_chat.get("owner_id") or 0) != int(call.from_user.id)
            or setup_state.get("mode") != "project"
            or int(setup_state.get("chat_id") or 0) != setup_chat_id
        ):
            bot.answer_callback_query(call.id, "Этот шаг настройки уже завершён", show_alert=True)
            return
        if action.startswith(("project_source_channel_", "project_source_group_")):
            source_type = "channel" if action.startswith("project_source_channel_") else "group"
            prompt, keyboard = _project_source_instructions(
                call.from_user.id, source_type, setup_chat_id
            )
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                prompt,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                parse_mode="HTML",
                **NO_PREVIEW,
            )
            return
        if action.startswith("project_add_help_"):
            if setup_state.get("source_type") == "group":
                prompt, keyboard = _project_cannot_add_prompt(
                    call.from_user.id, setup_chat_id
                )
                bot.answer_callback_query(call.id)
                bot.edit_message_text(
                    prompt,
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=keyboard,
                    **NO_PREVIEW,
                )
                return
            keyboard = types.InlineKeyboardMarkup(row_width=1)
            keyboard.add(types.InlineKeyboardButton(
                "📢 Это канал — отправлю приглашение",
                callback_data=f"project_help_channel_{setup_chat_id}",
            ))
            keyboard.add(types.InlineKeyboardButton(
                "💬 Это группа или чат — поддержка",
                callback_data=f"project_help_support_{setup_chat_id}",
            ))
            keyboard.add(types.InlineKeyboardButton(
                "🔙 Назад к выбору чата",
                callback_data=f"project_back_type_{setup_chat_id}",
            ))
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "Не получается добавить готовый аккаунт?\n\n"
                "Если место, откуда хотите пересылать сообщения — канал, нажмите первую кнопку и отправьте ссылку-приглашение. "
                "Если это группа или чат и добавить аккаунт нельзя, нажмите «Поддержка».",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                **NO_PREVIEW,
            )
            return
        if action.startswith("project_help_invite_"):
            prompt, keyboard = _project_invite_prompt(call.from_user.id, setup_chat_id)
            bot.answer_callback_query(call.id)
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass
            sent = bot.send_message(
                call.message.chat.id,
                prompt,
                reply_markup=keyboard,
                **NO_PREVIEW,
            )
            setup_state["prompt_message_id"] = sent.message_id
            return
        if action.startswith("project_help_channel_"):
            setup_state["source_type"] = "channel"
            prompt, keyboard = _project_invite_prompt(call.from_user.id, setup_chat_id)
            bot.answer_callback_query(call.id)
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass
            sent = bot.send_message(
                call.message.chat.id,
                prompt,
                reply_markup=keyboard,
                **NO_PREVIEW,
            )
            setup_state["prompt_message_id"] = sent.message_id
            return
        if action.startswith("project_back_type_"):
            setup_state.pop("source_type", None)
            setup_state["screen"] = "source_type"
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                _project_source_type_text(setup_chat_id),
                call.message.chat.id,
                call.message.message_id,
                reply_markup=_project_source_type_keyboard(setup_chat_id),
                parse_mode="HTML",
                **NO_PREVIEW,
            )
            return
        if action.startswith("project_back_group_"):
            prompt, keyboard = _project_source_instructions(
                call.from_user.id, "group", setup_chat_id
            )
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                prompt,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                parse_mode="HTML",
                **NO_PREVIEW,
            )
            return
        if action.startswith("project_back_channel_"):
            prompt, keyboard = _project_source_instructions(
                call.from_user.id, "channel", setup_chat_id
            )
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                prompt,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                parse_mode="HTML",
                **NO_PREVIEW,
            )
            return
        if action.startswith("project_back_help_"):
            prompt, keyboard = _project_cannot_add_prompt(
                call.from_user.id, setup_chat_id
            )
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                prompt,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                **NO_PREVIEW,
            )
            return
        customer_setup_state.pop(int(call.from_user.id), None)
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                "Настройку приостановил. Поддержка поможет разобраться, как подключить этот чат.",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=None,
                **NO_PREVIEW,
            )
        except Exception:
            pass
        start_support(call.message.chat.id, call.from_user)
        return
    if action.startswith("project_edit_source_"):
        try:
            edit_chat_id = int(action.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Некорректное подключение", show_alert=True)
            return
        edit_chat = get_chat_by_id(edit_chat_id)
        if (
            not edit_chat
            or edit_chat.get("account_mode") != "project"
            or int(edit_chat.get("owner_id") or 0) != int(call.from_user.id)
        ):
            bot.answer_callback_query(call.id, "Изменить ссылку может владелец подключения", show_alert=True)
            return
        if edit_chat.get("is_active"):
            bot.answer_callback_query(call.id, "Подключение уже завершено; ссылку менять нельзя")
            try:
                if (call.message.text or "").startswith("📬 Ваши группы MAX_Parser"):
                    _show_my_chats(call.message.chat.id, call.from_user.id, call.message.message_id)
                else:
                    bot.edit_message_text(
                        "✅ Этот чат уже подключён, пересылка включена. Ссылку изменить нельзя.",
                        call.message.chat.id,
                        call.message.message_id,
                        reply_markup=None,
                        **NO_PREVIEW,
                    )
            except Exception:
                pass
            return
        toggle_chat(edit_chat_id, False)
        customer_setup_state[int(call.from_user.id)] = {
            "chat_id": edit_chat_id,
            "mode": "project",
            "screen": "source_type",
            "prompt_message_id": call.message.message_id,
        }
        bot.answer_callback_query(call.id, "Выберите тип чата")
        bot.edit_message_text(
            _project_source_type_text(edit_chat_id),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=_project_source_type_keyboard(edit_chat_id),
            parse_mode="HTML",
            **NO_PREVIEW,
        )
        return
    if action.startswith("project_retry_"):
        try:
            retry_chat_id = int(action.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Некорректный ID группы", show_alert=True)
            return
        retry_chat = get_chat_by_id(retry_chat_id)
        if retry_chat and retry_chat.get("is_active"):
            bot.answer_callback_query(call.id, "Пересылка уже включена")
            if (call.message.text or "").startswith("📬 Ваши группы MAX_Parser"):
                _show_my_chats(call.message.chat.id, call.from_user.id, call.message.message_id)
            else:
                try:
                    bot.edit_message_text(
                        "✅ Этот чат уже подключён, пересылка включена.",
                        call.message.chat.id,
                        call.message.message_id,
                        reply_markup=None,
                        **NO_PREVIEW,
                    )
                except Exception:
                    pass
            return
        bot.answer_callback_query(call.id, "Проверяю доступ к MAX")
        is_my_chats_listing = (call.message.text or "").startswith("📬 Ваши группы MAX_Parser")
        if is_my_chats_listing:
            progress = bot.send_message(
                call.from_user.id,
                "⏳ Проверяю доступ к чату MAX. Результат появится здесь.",
                **NO_PREVIEW,
            )
            progress_message_id = progress.message_id
        else:
            bot.edit_message_text(
                "⏳ Проверяю доступ к чату MAX. Результат появится здесь.",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=None,
                **NO_PREVIEW,
            )
            progress_message_id = call.message.message_id
        threading.Thread(
            target=_verify_project_chat_access,
            args=(
                retry_chat_id,
                call.from_user.id,
                progress_message_id,
                call.message.message_id if is_my_chats_listing else None,
            ),
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
            review_id = int(action.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Некорректное приглашение", show_alert=True)
            return
        target_user = int(call.from_user.id)
        if get_review_entry_owner(review_id) != target_user or not begin_review_entry(review_id, target_user):
            bot.answer_callback_query(call.id, "Приглашение уже использовано или недоступно", show_alert=True)
            return
        awaiting_review_text[target_user] = review_id
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "✍️ Напишите отзыв одним текстовым сообщением (до 500 символов).\n"
            "Чтобы отменить, нажмите кнопку «❌ Отмена».",
            reply_markup=get_cancel_keyboard(),
            **NO_PREVIEW,
        )
        return
    if action.startswith("review_skip_"):
        try:
            review_id = int(action.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Некорректное приглашение", show_alert=True)
            return
        target_user = int(call.from_user.id)
        if get_review_entry_owner(review_id) != target_user or not skip_review_entry(review_id, target_user):
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
            review_id = int(action.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Некорректный отзыв", show_alert=True)
            return
        decision = "approved" if action.startswith("review_approve_") else "rejected"
        review = moderate_review_entry(review_id, decision)
        if not review:
            bot.answer_callback_query(call.id, "Отзыв уже обработан или не найден", show_alert=True)
            return
        target_user = int(review["user_id"])
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
            "❌ Отмена", callback_data=f"qr_cancel_{target_id}"
        ))
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=cancel_keyboard)
        except Exception:
            pass
        link_keyboard = types.InlineKeyboardMarkup(row_width=1)
        link_keyboard.add(types.InlineKeyboardButton("🔐 Открыть MAX и войти", url=link))
        link_message = bot.send_message(
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
        _track_auth_message(target_id, link_message)
        _start_remote_login_monitor(target_id, session, call.message.chat.id)
    elif action.startswith("qr_ready_"):
        target_id = _qr_target_from_action(action)
        bot.answer_callback_query(call.id, "⏳ Проверяю вход...")
        session = active_logins.get(target_id)
        if not session:
            bot.send_message(call.message.chat.id, "❌ Сессия авторизации не найдена", **NO_PREVIEW)
            return
        chat = get_chat_by_id(target_id) if target_id != "project_account" else None
        title = "готового аккаунта" if target_id == "project_account" else chat["title"]
        status, data = session.check()
        if active_logins.get(target_id) is not session:
            return
        if status == "success":
            _complete_login(target_id, call.message.chat.id)
        elif status == "awaiting_password":
            _set_auth_waiter(awaiting_password_from, target_id, call.from_user.id)
            try:
                photo_obj = io.BytesIO(data)
                kb = _build_password_keyboard(target_id)
                auth_message = bot.send_photo(
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
                _track_auth_message(target_id, auth_message)
            except Exception as e:
                bot.send_message(call.message.chat.id, f"❌ Ошибка: {e}", **NO_PREVIEW)
        elif status == "awaiting_code":
            _set_auth_waiter(awaiting_code_from, target_id, call.from_user.id)
            auth_message = bot.send_message(
                call.message.chat.id,
                "📩 MAX перешёл к вводу кода. Проверьте SMS и чат «Коды авторизации» в MAX. "
                "Когда код придёт, отправьте его следующим сообщением; после обработки оно будет удалено.",
                reply_markup=_build_password_keyboard(target_id),
                **NO_PREVIEW,
            )
            _track_auth_message(target_id, auth_message)
        elif status == "awaiting_human":
            auth_message = bot.send_message(
                call.message.chat.id,
                _auth_human_check_text(session, data),
                reply_markup=_build_auth_check_keyboard(target_id),
                **NO_PREVIEW,
            )
            _track_auth_message(target_id, auth_message)
        elif status == "awaiting_phone":
            auth_message = bot.send_message(
                call.message.chat.id,
                "MAX показывает форму ввода номера. Чтобы пройти вход самостоятельно, выберите «Войти по номеру телефона» "
                "и откройте защищённую страницу. Номер и код вводите только там, не в Telegram.",
                reply_markup=_build_qr_keyboard(target_id),
                **NO_PREVIEW,
            )
            _track_auth_message(target_id, auth_message)
        elif status == "waiting":
            auth_message = bot.send_message(
                call.message.chat.id,
                "⚠️ MAX пока не подтвердил вход. Убедитесь, что QR-код отсканирован в приложении MAX, "
                "и нажмите кнопку ещё раз. Либо откройте защищённую страницу для входа по номеру телефона.",
                reply_markup=_build_qr_keyboard(target_id),
                **NO_PREVIEW,
            )
            _track_auth_message(target_id, auth_message)
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
        title = "готового аккаунта" if target_id == "project_account" else chat["title"]
        status, data = session.refresh()
        if active_logins.get(target_id) is not session:
            return
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
            qr_message = bot.send_photo(
                call.message.chat.id,
                photo_obj,
                caption=(
                    f" <b>QR-код обновлён</b>\n\n"
                    f"Отсканируйте новый код и нажмите <b>«✅ Готово»</b>"
                ),
                parse_mode='HTML',
                reply_markup=_build_qr_keyboard(target_id)
            )
            auth_qr_message_ids.setdefault(target_id, set()).add(
                (int(call.message.chat.id), int(qr_message.message_id))
            )
        except Exception as e:
            bot.send_message(call.message.chat.id, f"❌ Ошибка: {e}", **NO_PREVIEW)
    elif action.startswith("qr_cancel_"):
        target_id = _qr_target_from_action(action)
        bot.answer_callback_query(call.id, "Настройка отменена")
        _cancel_customer_connection(
            call.from_user.id,
            target_id,
            callback_message=call.message,
        )
    elif action == "admin_sent_messages":
        _show_sent_parser_chats(call.message.chat.id, call.message.message_id)
    elif action.startswith("admin_sent_chat_"):
        try:
            target_chat_id = int(action[len("admin_sent_chat_"):])
        except ValueError:
            bot.answer_callback_query(call.id, "Не удалось определить группу", show_alert=True)
            return
        _show_sent_parser_posts(call.message.chat.id, call.message.message_id, target_chat_id)
    elif action.startswith("admin_sent_post_"):
        record_id = action[len("admin_sent_post_"):]
        record = _find_sent_parser_record(record_id)
        if not record or not _sent_record_is_deletable(record):
            bot.answer_callback_query(call.id, "Эту отправку уже нельзя удалить", show_alert=True)
            _show_sent_parser_chats(call.message.chat.id, call.message.message_id)
            return
        _show_sent_parser_confirmation(call.message.chat.id, call.message.message_id, record)
    elif action.startswith("admin_sent_delete_"):
        record_id = action[len("admin_sent_delete_"):]
        deleted, remaining, error_text = _delete_sent_parser_record(record_id)
        if remaining:
            reason = re.sub(r"\s+", " ", error_text or "")[:75]
            detail = f" Причина: {reason}" if reason else ""
            bot.answer_callback_query(
                call.id,
                f"Удалено: {deleted}. Не удалено: {len(remaining)}.{detail}",
                show_alert=True,
            )
        elif deleted:
            bot.answer_callback_query(call.id, f"Удалено сообщений: {deleted}")
        else:
            bot.answer_callback_query(call.id, error_text or "Не удалось удалить", show_alert=True)
        record = _find_sent_parser_record(record_id)
        if record:
            _show_sent_parser_posts(call.message.chat.id, call.message.message_id, record.get("chat_id"))
        else:
            _show_sent_parser_chats(call.message.chat.id, call.message.message_id)
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
    elif action == "admin_review_delete":
        bot.answer_callback_query(call.id)
        show_admin_review_delete_list(call.message.chat.id, call.message.message_id)
    elif action.startswith("admin_review_delete_page_"):
        try:
            page = int(action.rsplit("_", 1)[1])
        except ValueError:
            page = 0
        bot.answer_callback_query(call.id)
        show_admin_review_delete_list(call.message.chat.id, call.message.message_id, page)
    elif action.startswith("admin_review_delete_confirm_"):
        parts = action.split("_")
        if len(parts) != 6:
            bot.answer_callback_query(call.id, "Некорректный отзыв", show_alert=True)
            return
        try:
            review_id, page = int(parts[4]), int(parts[5])
        except ValueError:
            bot.answer_callback_query(call.id, "Некорректный отзыв", show_alert=True)
            return
        review = get_review_entry(review_id)
        if not review or review.get("status") != "approved":
            bot.answer_callback_query(call.id, "Отзыв уже удалён или недоступен", show_alert=True)
            show_admin_review_delete_list(call.message.chat.id, call.message.message_id, page)
            return
        bot.answer_callback_query(call.id)
        show_admin_review_delete_confirmation(
            call.message.chat.id, call.message.message_id, review_id, page
        )
    elif action.startswith("admin_review_delete_yes_"):
        parts = action.split("_")
        if len(parts) != 6:
            bot.answer_callback_query(call.id, "Некорректный отзыв", show_alert=True)
            return
        try:
            review_id, page = int(parts[4]), int(parts[5])
        except ValueError:
            bot.answer_callback_query(call.id, "Некорректный отзыв", show_alert=True)
            return
        try:
            deleted = delete_approved_review_entry(review_id)
        except Exception:
            debug_logger.exception(
                "ADMIN_REVIEW_DELETE_FAILED admin_id=%s review_id=%s",
                call.from_user.id, review_id,
            )
            bot.answer_callback_query(call.id, "Не удалось удалить отзыв", show_alert=True)
            return
        if not deleted:
            bot.answer_callback_query(call.id, "Отзыв уже удалён или недоступен", show_alert=True)
        else:
            bot.answer_callback_query(call.id, "Отзыв удалён")
        show_admin_review_delete_list(call.message.chat.id, call.message.message_id, page)
    elif action == "admin_revoke_subscriptions":
        show_revoke_subscription_users(call.message.chat.id, call.message.message_id)
        bot.register_next_step_handler(call.message, process_revoke_subscription_user_id)
    elif action.startswith("admin_revoke_sub_page_"):
        show_revoke_subscription_users(
            call.message.chat.id, call.message.message_id,
            int(action.rsplit("_", 1)[1]),
        )
        bot.register_next_step_handler(call.message, process_revoke_subscription_user_id)
    elif action.startswith("admin_revoke_plan_"):
        parts = action.split("_")
        if len(parts) != 5 or parts[3] not in {"self", "project", "multi"}:
            bot.answer_callback_query(call.id, "Некорректный тариф", show_alert=True)
            return
        try:
            target_user_id = int(parts[4])
        except ValueError:
            bot.answer_callback_query(call.id, "Некорректный пользователь", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        show_revoke_subscription_confirmation(call.message.chat.id, target_user_id, parts[3])
    elif action.startswith("admin_revoke_confirm_"):
        parts = action.split("_")
        if len(parts) != 5 or parts[3] not in {"self", "project", "multi"}:
            bot.answer_callback_query(call.id, "Некорректный тариф", show_alert=True)
            return
        try:
            target_user_id = int(parts[4])
            plan_code = parts[3]
            revoked = revoke_subscription(target_user_id, plan_code)
        except (ValueError, TypeError, IndexError):
            bot.answer_callback_query(call.id, "Не удалось определить подписку", show_alert=True)
            return
        except Exception as error:
            bot.answer_callback_query(call.id, "Не удалось обновить доступ", show_alert=True)
            debug_logger.exception(
                "ADMIN_SUBSCRIPTION_REVOKE_FAILED admin_id=%s user_id=%s",
                call.from_user.id, target_user_id,
            )
            return
        if not revoked:
            bot.answer_callback_query(call.id, "Эта подписка уже не активна", show_alert=True)
            show_revoke_subscription_options(call.message.chat.id, target_user_id)
            return
        plan_label = SUBSCRIPTION_LABELS.get(plan_code, plan_code)
        delivery_note = ""
        try:
            bot.send_message(
                target_user_id,
                f"ℹ️ Доступ к MAX_Parser по тарифу «{plan_label}» отключён администратором.",
                **NO_PREVIEW,
            )
        except Exception:
            delivery_note = "\nУведомление пользователю не доставлено — возможно, он ещё не запускал бота."
        text = (
            "✅ <b>Доступ отключён</b>\n\n"
            f"Пользователь: <code>{target_user_id}</code>\n"
            f"Тариф: {escape_html(plan_label)}{delivery_note}"
        )
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_back"))
        bot.answer_callback_query(call.id, "Доступ отключён")
        safe_edit_message(text, call.message.chat.id, call.message.message_id, reply_markup=keyboard, **NO_PREVIEW)
    elif action == "admin_free_subscriptions":
        show_free_subscription_users(call.message.chat.id, call.message.message_id)
        bot.register_next_step_handler(call.message, process_free_subscription_user_id)
    elif action.startswith("admin_free_sub_page_"):
        show_free_subscription_users(
            call.message.chat.id, call.message.message_id,
            int(action.rsplit("_", 1)[1]),
        )
        bot.register_next_step_handler(call.message, process_free_subscription_user_id)
    elif action.startswith("admin_gift_separate_"):
        try:
            target_user_id = int(action.rsplit("_", 1)[1])
        except ValueError:
            bot.answer_callback_query(call.id, "Не удалось определить пользователя", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        show_free_subscription_separate_options(call.message.chat.id, target_user_id)
    elif action.startswith("admin_gift_multi_"):
        try:
            target_user_id = int(action.rsplit("_", 1)[1])
            bot.answer_callback_query(call.id)
            show_free_subscription_terms(call.message.chat.id, target_user_id, "multi")
        except (ValueError, TypeError, IndexError) as error:
            bot.answer_callback_query(call.id, "Некорректный пользователь", show_alert=True)
            debug_logger.warning("ADMIN_FREE_SUBSCRIPTION_INVALID admin_id=%s error=%s", call.from_user.id, log_preview(error, 120))
            return
    elif action.startswith("admin_gift_plan_"):
        parts = action.split("_")
        if len(parts) != 5 or parts[3] not in {"self", "project"}:
            bot.answer_callback_query(call.id, "Некорректный тариф", show_alert=True)
            return
        plan_code = parts[3]
        try:
            target_user_id = int(parts[4])
        except ValueError:
            bot.answer_callback_query(call.id, "Некорректный пользователь", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        show_free_subscription_terms(call.message.chat.id, target_user_id, plan_code)
    elif action.startswith("admin_gift_term_"):
        try:
            _, _, _, plan_code, months_value, user_id_value = action.split("_")
            target_user_id = int(user_id_value)
            months = int(months_value)
            if plan_code not in {"self", "project", "multi"} or months not in {1, 3, 12}:
                raise ValueError("invalid subscription choice")
            granted = grant_subscription(target_user_id, plan_code, months)
        except (ValueError, TypeError, IndexError) as error:
            bot.answer_callback_query(call.id, "Не удалось выдать подписку", show_alert=True)
            debug_logger.warning("ADMIN_FREE_SUBSCRIPTION_INVALID admin_id=%s error=%s", call.from_user.id, log_preview(error, 120))
            return
        except Exception as error:
            bot.answer_callback_query(call.id, "Ошибка базы данных", show_alert=True)
            debug_logger.exception("ADMIN_FREE_SUBSCRIPTION_FAILED admin_id=%s user_id=%s", call.from_user.id, target_user_id)
            return

        active_until = granted["active_until"].astimezone(timezone.utc)
        plan_label = SUBSCRIPTION_LABELS[plan_code]
        duration_label = SUBSCRIPTION_PERIOD_LABELS[months]
        delivery_note = ""
        try:
            bot.send_message(
                target_user_id,
                "🎁 <b>Вам подарили подписку MAX_Parser</b>\n\n"
                f"Тариф: <b>{escape_html(plan_label)}</b>\n"
                f"Срок: <b>{duration_label}</b> · действует до <b>{active_until:%d.%m.%Y %H:%M} UTC</b>.\n\n"
                "Подключите чат через /connect. Мульти-подписка включает оба способа подключения.",
                parse_mode="HTML",
                **NO_PREVIEW,
            )
        except Exception:
            delivery_note = "\nУведомление пользователю не доставлено — возможно, он ещё не запускал бота."
        text = (
            "✅ <b>Бесплатная подписка выдана</b>\n\n"
            f"Пользователь: <code>{target_user_id}</code>\n"
            f"Тариф: {escape_html(plan_label)}\n"
            f"Срок: {duration_label}\n"
            f"Действует до: <b>{active_until:%d.%m.%Y %H:%M} UTC</b>."
            f"{delivery_note}"
        )
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_back"))
        bot.answer_callback_query(call.id, "Подписка выдана")
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
        try:
            bot.clear_step_handler_by_chat_id(call.message.chat.id)
        except Exception:
            pass
        show_admin_menu(call.message.chat.id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
    elif action == "admin_broadcast":
        track_user(call.message)

        prompt = bot.send_message(
            call.message.chat.id,
            "💬 Введите текст рассылки:",
            reply_markup=get_cancel_keyboard(),
            **NO_PREVIEW,
        )

        bot.register_next_step_handler(prompt, process_broadcast_message)
    elif action == "admin_send_to_chat":
        bot.answer_callback_query(call.id)
        show_chat_send_selector(call.message.chat.id, call.message.message_id)
        prompt = bot.send_message(
            call.message.chat.id,
            "Отправьте ID чата из списка, затем отправьте сообщение для него.",
            reply_markup=get_cancel_keyboard(),
            **NO_PREVIEW,
        )
        bot.register_next_step_handler(prompt, process_send_to_chat_id)
    elif action.startswith("admin_send_chats_page_"):
        show_chat_send_selector(
            call.message.chat.id, call.message.message_id,
            int(action.rsplit("_", 1)[1]),
        )
    elif action == "admin_send_to_user":
        bot.answer_callback_query(call.id)
        show_user_send_selector(call.message.chat.id, call.message.message_id)
        prompt = bot.send_message(
            call.message.chat.id,
            "Отправьте ID пользователя из списка, затем отправьте сообщение для него.",
            reply_markup=get_cancel_keyboard(),
            **NO_PREVIEW,
        )
        bot.register_next_step_handler(prompt, process_send_to_user_id)
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
                    bot.answer_callback_query(call.id, "Проверяю доступ готового аккаунта…")
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
            else "Бот покинет Telegram-группу; её настройки и история удалятся."
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
        if call.data == "thanks_project":
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=(
                    '''📜 <b>История MAX_Parser</b>

Каждый проект начинается с идеи, но становится настоящим благодаря людям, которые поддерживают её.

<b>MAX_Parser</b> благодарит:

@novelly1338
@voidknight009  
@T1M1Ktg  

<b>Спасибо за поддержку, доверие и участие в развитии проекта.  
Ваш вклад навсегда останется частью его истории. ✨</b>'''
                ),
                reply_markup=return_start(),
                parse_mode="HTML",
                **NO_PREVIEW,
            )
        elif call.data == "about_bot":
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
• <b>Ваш аккаунт MAX:</b> вы входите в MAX, а бот читает и пересылает в выбранные вами чаты.\n
• <b>Готовый аккаунт:</b> Здесь уже подготовленный аккаунт, который будет читать и пересылать в выбранные вами чаты.\n
В обоих случаях бот пересылает сообщения только из чатов, к которым у выбранного аккаунта есть доступ.</blockquote>

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
                    "<blockquote><b>1️⃣ Ваш аккаунт MAX\n</b>Бот будет пересылать сообщения из вашего аккаунта MAX в выбранную вами группу.</blockquote>\n"
                    "<blockquote><b>2️⃣ Через готовый аккаунт MAX\n</b>Здесь вам не понадобится подключать ваш аккаунт, вы уже используете подготовленный аккаунт MAX</blockquote>\n"
                    "<blockquote><b>✨ Мульти-подписка\n</b>Мульти-подписка открывает оба способа: свой MAX-аккаунт и готовый аккаунт MAX.</blockquote>\n\n"
                    "<i>Пошаговый порядок подключения — в инструкции ниже.</i>"
                ),
                reply_markup=parser_keyboard(),
                parse_mode="HTML",
                **NO_PREVIEW,
            )
        elif call.data == "compare_modes":
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "<b>Чем отличаются способы подключения</b>\n\n"
                "<blockquote><b>1️⃣ Свой MAX-аккаунт</b>\n"
                "1 месяц — 249 ₽ · 3 месяца — 599 ₽ · 1 год — 2 499 ₽.\n"
                "Вы входите в свой MAX. Парсер читает выбранные вами чаты, которые доступны вашему аккаунту.\n"
                "Подходит, если не хотите добавлять готовый аккаунт в чат.</blockquote>\n\n"
                "<blockquote><b>2️⃣ Через готовый аккаунт</b>\n"
                "1 месяц — 499 ₽ · 3 месяца — 1 199 ₽ · 1 год — 5 499 ₽.\n"
                f"Используется готовый аккаунт <code>{escape_html(MAX_PROJECT_ACCOUNT_PHONE)}</code>. "
                "Для группы его нужно добавить или пригласить, для канала — просто скинуть ссылку.\n"
                "Вы не вводите данные своего MAX.</blockquote>\n\n"
                "<blockquote><b>✨ Мульти-подписка</b>\n"
                "1 месяц — 599 ₽ · 3 месяца — 1 499 ₽ · 1 год — 6 499 ₽. Включает оба способа, "
                "чтобы выбирать удобный вариант для каждого чата.</blockquote>",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=parser_keyboard(),
                parse_mode="HTML",
                **NO_PREVIEW,
            )
        elif call.data == "subscription_products":
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "💳 <b>Выберите тариф</b>\n\n"
                "Все тарифы действуют на весь ваш аккаунт MAX_Parser и включают до 5 чатов. "
                "Мульти-подписка открывает оба способа подключения.",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=subscription_products_keyboard(),
                parse_mode="HTML",
                **NO_PREVIEW,
            )
        elif call.data.startswith("subscription_product_"):
            plan_code = call.data.rsplit("_", 1)[1]
            if plan_code not in SUBSCRIPTION_PRICES:
                bot.answer_callback_query(call.id, "Не удалось определить тариф", show_alert=True)
                return
            bot.answer_callback_query(call.id)
            show_subscription_terms(
                call.message.chat.id,
                call.message.message_id,
                plan_code,
                back_callback="subscription_products",
            )
        elif call.data.startswith("subscription_buy_"):
            parts = call.data.split("_")
            try:
                plan_code = parts[2]
                months = int(parts[3])
                if plan_code not in SUBSCRIPTION_PRICES or months not in (1, 3, 12):
                    raise ValueError("invalid product")
            except (ValueError, IndexError):
                bot.answer_callback_query(call.id, "Не удалось определить тариф", show_alert=True)
                return
            if call.message.chat.type != "private":
                bot.answer_callback_query(call.id, "Оплатить можно в личном чате с ботом.", show_alert=True)
                return
            track_user(call)
            bot.answer_callback_query(call.id, "Готовлю счёт")
            _send_subscription_invoice(call.from_user.id, plan_code, months)
        elif call.data in ("connect_self", "connect_project", "connect_multi"):
            if not call.message or call.message.chat.type != "private":
                bot.answer_callback_query(
                    call.id,
                    "Продление доступно в личном чате с ботом.",
                    show_alert=True,
                )
                return
            plan_code = call.data.removeprefix("connect_")
            track_user(call)
            bot.answer_callback_query(call.id)
            _begin_connection_for_plan(
                call.from_user.id,
                plan_code,
                call.message.chat.id,
                call.message.message_id,
            )
        elif call.data in ("setup_mode_self", "setup_mode_project"):
            account_mode = "self" if call.data.endswith("self") else "project"
            if call.message.chat.type != "private":
                bot.answer_callback_query(call.id, "Откройте личный чат с ботом.", show_alert=True)
                return
            if PAYMENTS_ENABLED and not _user_has_product_access(call.from_user.id, "multi"):
                bot.answer_callback_query(call.id, "Для мульти-подписки сначала завершите оплату.", show_alert=True)
                return
            bot.answer_callback_query(call.id)
            _start_customer_setup_prompt(
                call.from_user.id,
                account_mode,
                call.message.chat.id,
                call.message.message_id,
            )
        elif call.data in ("renew_self", "renew_project"):
            # Backward compatibility for old renewal buttons still in chat history.
            plan_code = "self" if call.data == "renew_self" else "project"
            bot.answer_callback_query(call.id)
            show_subscription_terms(
                call.message.chat.id, call.message.message_id, plan_code,
                back_callback="subscription_products",
            )
        elif call.data in ("my_acc_250", "not_my_acc_250"):
            # Backward compatibility for old connection buttons.
            plan_code = "self" if call.data == "my_acc_250" else "project"
            bot.answer_callback_query(call.id)
            _begin_connection_for_plan(
                call.from_user.id, plan_code, call.message.chat.id, call.message.message_id
            )
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
        elif call.data == "video_instruction":
            bot.answer_callback_query(call.id)
            video_keyboard = types.InlineKeyboardMarkup(row_width=1)
            video_keyboard.add(types.InlineKeyboardButton(
                "1️⃣ 1 вариант · QR-код", callback_data="video_instruction_qr"
            ))
            video_keyboard.add(types.InlineKeyboardButton(
                "2️⃣ 1 вариант · телефон", callback_data="video_instruction_phone"
            ))
            video_keyboard.add(types.InlineKeyboardButton(
                "3️⃣ 2 вариант · готовый аккаунт", callback_data="video_instruction_project"
            ))
            video_keyboard.add(types.InlineKeyboardButton(
                "🔙 Назад к подключению", callback_data="parse_setup"
            ))
            bot.edit_message_text(
                "🎬 <b>Видео инструкция</b>\n\nВыберите способ подключения:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=video_keyboard,
                parse_mode="HTML",
                **NO_PREVIEW,
            )
        elif call.data.startswith("video_instruction_"):
            if call.data not in {
                "video_instruction_qr",
                "video_instruction_phone",
                "video_instruction_project",
            }:
                bot.answer_callback_query(call.id, "Не удалось определить способ", show_alert=True)
                return
            bot.answer_callback_query(call.id)
            video_keyboard = types.InlineKeyboardMarkup(row_width=1)
            video_keyboard.add(types.InlineKeyboardButton(
                "🔙 К выбору способа", callback_data="video_instruction"
            ))
            bot.edit_message_text(
                "🎬 <b>Видео инструкция</b>\n\nВидео для этого способа пока не добавлено.",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=video_keyboard,
                parse_mode="HTML",
                **NO_PREVIEW,
            )
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
                    "<b>Шаг 2 · Выберите чат MAX</b>\n"
                    "Во втором способе нажмите «Канал» или «Группа или чат». Затем откройте <a href=\"https://web.max.ru\">web.max.ru</a>, "
                    "перейдите к чату и скопируйте ссылку из меню.\n\n"
                    "<b>Вариант 1 · Ваш аккаунт MAX</b>\n"
                    "<blockquote><b>Через QR-код:</b>\n\nПосле отправки ссылки бот пришлёт QR-код. Отсканируйте его в приложении MAX и нажмите "
                    "«Готово, я отсканировал». Если MAX запросит пароль двухэтапной проверки, отправьте его боту в этой "
                    "личной переписке; он нужен только для входа в MAX. После успешного входа парсер включится автоматически.\n\n"
                    "<b>Через номер телефона:</b>\nВ том месте, где вам пришлют QR-код нажмите на войти по номеру телефона. "
                    "Далее нажмите на Открыть MAX и войти и перейдите по ссылке. Там будет краткая инструкция как действовать дальше, ознакомтесь и подключите MAX_Parser!</blockquote>\n\n"
                    "<b>Вариант 2 · Через готовый аккаунт MAX</b>\n"
                    f"<blockquote>Если выбрали группу, добавьте в неё аккаунт <code>{MAX_PROJECT_ACCOUNT_PHONE}</code>, а затем отправьте боту ссылку. "
                    "Если MAX попросит одобрить вступление, это должен сделать администратор. Для канала отправьте ссылку, а "
                    "для закрытого канала может понадобиться приглашение. Бот проверит доступ и включит пересылку только после проверки.\n"
                    "Если готовый аккаунт нельзя добавить и для закрытой группы нет приглашения, этот способ не сработает. "
                    "Используйте свой аккаунт MAX или обратитесь в поддержку.</blockquote>\n\n"
                    "<b>Управление и помощь</b>\n"
                    "<blockquote>• /mychats — посмотреть свои подключения.\n"
                    "• Чтобы отменить текущий шаг, нажмите «❌ Отмена» в сообщении бота.\n"
                    "• /help — открыть анонимный чат с поддержкой.</blockquote>\n\n"
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
