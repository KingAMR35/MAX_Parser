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
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit
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
BOT_PUBLIC_USERNAME = os.getenv("BOT_PUBLIC_USERNAME", "max_parserBot").strip().lstrip("@")
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
awaiting_admin_group_private_reply = {}
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
support_delivery_locks = {}
support_delivery_locks_guard = threading.Lock()
PARSE_DIAGNOSTICS = {}
PARSE_DIAGNOSTICS_LOCK = threading.RLock()
CHAT_LIFECYCLE_LOCK = threading.RLock()
pending_setup_modes = {}
customer_setup_state = {}
max_send_source_locks = {}
max_send_browser_semaphore = threading.Semaphore(1)
admin_max_send_flows = {}
admin_max_send_flow_lock = threading.RLock()
public_max_send_flows = {}
public_max_send_flow_lock = threading.RLock()
public_max_send_albums = {}
_send_to_max_command_menu_state = {}
_send_to_max_command_menu_lock = threading.RLock()
max_outbound_suppressions = {}
max_outbound_suppressions_lock = threading.RLock()
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

_BASE_BOT_COMMANDS = [
    types.BotCommand("start", "Запустить бота"),
    types.BotCommand("help", "Связаться с тех поддержкой"),
    types.BotCommand("connect", "Подключить чат MAX"),
    types.BotCommand("mychats", "Мои группы парсинга"),
    types.BotCommand("subscription", "Подписка и продление"),
    types.BotCommand("admin", "Админ-панель")
]
_SEND_TO_MAX_BOT_COMMAND = types.BotCommand("send_to_max", "Отправить сообщение в MAX")
BOT_SHORT_DESCRIPTION = (
    "Пересылает сообщения из MAX в Telegram. Отправляйте текст и вложения обратно командой /send_to_max."
)
BOT_PROFILE_DESCRIPTION = (
    "MAX_Parser пересылает новые сообщения из MAX в Telegram: текст, фото, видео, документы и другие вложения. "
    "Подключайте доступные группы, каналы и личные чаты, выбирайте получение в Telegram-группу или личные сообщения. "
    "Из подключённой Telegram-группы можно отправлять текст и вложения обратно в MAX командой /send_to_max, "
    "если источник не является каналом. Пошаговая инструкция и поддержка — в боте."
)


def _sync_bot_profile_descriptions():
    for language_code in (None, "ru"):
        try:
            bot.set_my_short_description(
                BOT_SHORT_DESCRIPTION, language_code=language_code
            )
            bot.set_my_description(
                BOT_PROFILE_DESCRIPTION, language_code=language_code
            )
        except Exception as error:
            debug_logger.warning(
                "BOT_PROFILE_DESCRIPTION_SYNC_FAILED language=%s error_type=%s",
                language_code or "default", type(error).__name__,
            )


_sync_bot_profile_descriptions()

for _command_scope in (
    None,
    types.BotCommandScopeAllPrivateChats(),
    types.BotCommandScopeAllGroupChats(),
):
    try:
        if _command_scope is None:
            bot.set_my_commands(_BASE_BOT_COMMANDS)
        else:
            bot.set_my_commands(_BASE_BOT_COMMANDS, scope=_command_scope)
    except Exception as _command_error:
        debug_logger.warning(
            "BASE_COMMAND_MENU_SYNC_FAILED scope=%s error_type=%s",
            getattr(_command_scope, "type", "default"), type(_command_error).__name__,
        )

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
    media_hash = media.get("dedupe_hash") or media.get("hash") or get_media_hash(data=data)
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
        media_hash = str(
            media.get("dedupe_hash") or media.get("hash") or media.get("content_hash") or ""
        ).strip()
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


def _append_sent_parser_record(record):
    chat_id = int(record.get("chat_id") or 0)
    with _sent_parser_messages_lock:
        _sent_parser_messages.append(record)
        per_chat = []
        for item in _sent_parser_messages:
            try:
                if int(item.get("chat_id") or 0) == chat_id:
                    per_chat.append(item)
            except (TypeError, ValueError):
                continue
        keep_ids = {item.get("record_id") for item in per_chat[-500:]}
        retained = []
        for item in _sent_parser_messages:
            try:
                same_chat = int(item.get("chat_id") or 0) == chat_id
            except (TypeError, ValueError):
                same_chat = False
            if not same_chat or item.get("record_id") in keep_ids:
                retained.append(item)
        _sent_parser_messages[:] = retained[-10000:]
        _save_sent_parser_messages_locked()


def _record_bot_sent_group_messages(destination_chat_id, message_ids, preview, has_media=False):
    try:
        destination_chat_id = int(destination_chat_id)
        chat = bot.get_chat(destination_chat_id)
        if getattr(chat, "type", None) not in {"group", "supergroup"}:
            return
    except Exception:
        return
    ids = []
    for value in message_ids or []:
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    if not ids:
        return
    preview = re.sub(r"\s+", " ", str(preview or "")).strip()[:100]
    record = {
        "record_id": uuid.uuid4().hex[:12],
        "chat_id": destination_chat_id,
        "destination_chat_id": destination_chat_id,
        "message_ids": ids,
        "source_id": f"bot:{ids[0]}",
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "preview": preview or ("Сообщение с вложением" if has_media else "Сообщение бота"),
        "partial": False,
        "captioned": True,
        "text_sent": True,
        "has_media": bool(has_media),
        "media_hashes": [],
        "source_id_is_index": False,
        "name": "MAX_Parser",
        "destination_title": str(getattr(chat, "title", "") or "")[:120],
        "date": "",
        "time": "",
        "origin": "bot",
    }
    incoming_ids = set(ids)
    with _sent_parser_messages_lock:
        for index, existing in enumerate(_sent_parser_messages):
            try:
                same_chat = int(existing.get("chat_id") or 0) == destination_chat_id
                same_ids = incoming_ids.intersection(
                    int(value) for value in existing.get("message_ids") or []
                )
            except (TypeError, ValueError):
                continue
            if same_chat and same_ids and existing.get("origin") == "bot":
                updated = dict(existing)
                updated["preview"] = record["preview"]
                updated["has_media"] = bool(has_media)
                updated["destination_title"] = record["destination_title"]
                _sent_parser_messages[index] = updated
                _save_sent_parser_messages_locked()
                return
    _append_sent_parser_record(record)


def _record_parser_sent_post(
    chat_id: int, post: dict, message_ids, partial=False, captioned=False,
    text_sent=False, destination_chat_id=None,
):
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
        "destination_chat_id": int(destination_chat_id or chat_id),
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
    _append_sent_parser_record(record)


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
            captioned = item.get("captioned")
            if captioned is True or item.get("text_sent") is True:
                continue
            if captioned is None and item.get("preview") != "Медиа или вложение":
                continue
            record_is_index = bool(item.get("source_id_is_index"))
            if not item.get("has_media") and not item.get("media_hashes"):
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

def _split_numbered_poll_choices(value: str) -> list[str]:
    """Recover numbered choices when MAX exposes the whole list as one text node."""
    text = str(value or "").strip()
    markers = list(re.finditer(r"(?<!\S)(\d{1,2})[.)]?\s+", text))
    if len(markers) < 3 or len(markers) > 30:
        return []
    numbers = [int(match.group(1)) for match in markers]
    if numbers != list(range(1, len(numbers) + 1)):
        return []
    choices = [
        text[match.start():markers[index + 1].start() if index + 1 < len(markers) else len(text)].strip()
        for index, match in enumerate(markers)
    ]
    return choices if all(choices) else []

def _format_poll_message(poll: dict, comment: str = "", is_closed: bool = False) -> str:
    question = str(poll.get("question") or "Опрос").strip()
    if re.match(r"^(?:poll ended|опрос заверш[её]н|total votes?|всего голосов?)\b", question, re.I):
        question = "Опрос"
    rows = []
    discarded_meta_votes = 0
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
        voter_meta = re.fullmatch(
            r"(?:(\d+)\s+voted|voted\s+(\d+)|(\d+)\s+(?:person|people)\s+voted)(?:\s*:\s*(\d+))?",
            label,
            re.I,
        )
        if voter_meta:
            votes = max(votes, *(int(value or 0) for value in voter_meta.groups()))
            discarded_meta_votes += votes
            continue
        suffix = re.match(r"^(.*?)(?:\s*[.,]\s*|\s+)(\d+)\s*(?:votes?|голос(?:а|ов)?)$", label, re.I)
        if suffix:
            label = suffix.group(1).strip()
            votes = max(votes, int(suffix.group(2)))
        result = re.match(r"^(.*?)\s*[.,]\s*(\d+)\s*[,;]\s*\d+(?:[.,]\d+)?\s*%.*$", label, re.I)
        if result:
            label = result.group(1).strip()
            votes = max(votes, int(result.group(2)))
        label = re.sub(r"\s*[,;·|]?\s*\d+(?:[.,]\d+)?\s*%.*$", "", label).strip(" -–—.,;:")
        inline_count = re.match(r"^(.*?)\s*:\s*(\d+)\s*$", label)
        if inline_count:
            label = inline_count.group(1).strip()
            votes = max(votes, int(inline_count.group(2)))
        if not label or re.match(r"^(?:poll ended|опрос заверш[её]н|total votes?|всего голосов?)\b", label, re.I):
            continue
        numbered_choices = _split_numbered_poll_choices(label)
        if numbered_choices:
            rows.extend(
                (choice, votes if index == len(numbered_choices) - 1 else 0)
                for index, choice in enumerate(numbered_choices)
            )
        else:
            rows.append((label, votes))

    # Some MAX builds flatten three zero-vote options into one text node.
    # Recover the observed single-word shape without splitting ordinary phrases.
    if not is_closed and len(rows) == 1 and rows[0][1] == 0:
        flat = rows[0][0]
        split_choices = re.fullmatch(
            r"([a-zа-яё][a-zа-яё0-9_-]*)\s+([a-zа-яё][a-zа-яё0-9_-]*)\s+([a-zа-яё][a-zа-яё0-9_-]*)",
            flat,
            re.I,
        )
        if split_choices:
            rows = [(label, 0) for label in split_choices.groups()]

    total_votes = sum(votes for _, votes in rows)
    try:
        reported_total = max(0, int(poll.get("total_votes") or 0) - discarded_meta_votes)
        total_votes = max(total_votes, reported_total)
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

def _remember_sent_poll(chat_id: int, post: dict, message_id, destination_chat_id=None):
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
        entry["destination_chat_id"] = int(destination_chat_id or chat_id)
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
        destination_chat_id = int(entry.get("destination_chat_id") or chat_id)
        edit = lambda: _telegram_call(
            bot.edit_message_caption, chat_id=destination_chat_id, message_id=int(message_id),
            caption=text, parse_mode="HTML",
        )
    else:
        destination_chat_id = int(entry.get("destination_chat_id") or chat_id)
        edit = lambda: _telegram_call(
            bot.edit_message_text, text, chat_id=destination_chat_id, message_id=int(message_id),
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
    if destination is None and (method_name.startswith("send_") or method_name in {"copy_message", "forward_message"}) and args:
        destination = args[0]
    try:
        destination_key = int(destination) if destination is not None else None
    except (TypeError, ValueError):
        destination_key = None
    ctx = getattr(_send_context, "current", None)
    injected_inline_button = False
    if (
        ctx is not None
        and destination_key is not None
        and destination_key < 0
        and method_name in {
            "send_message", "send_photo", "send_video", "send_video_note",
            "send_document", "send_voice", "send_audio", "send_animation",
        }
        and not isinstance(kwargs.get("reply_markup"), types.InlineKeyboardMarkup)
    ):
        # Never recreate the legacy MAX ReplyKeyboard. Remove it from existing
        # chats on the next parsed post, while preserving any inline controls.
        kwargs["reply_markup"] = types.ReplyKeyboardRemove()
    if (
        ctx is not None
        and ctx.get("button_keyboard") is not None
        and destination_key == int(ctx.get("destination_chat_id") or 0)
        and method_name in {
            "send_message", "send_photo", "send_video", "send_video_note",
            "send_document", "send_voice", "send_audio", "send_animation",
        }
    ):
        # Put the button on the message as Telegram creates it. The final sync
        # below moves it to the last message in a multi-part MAX post.
        kwargs["reply_markup"] = ctx["button_keyboard"]
        injected_inline_button = True
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
            if injected_inline_button:
                ctx["inline_button_message_ids"].extend(ids)
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


def _attach_caption_to_prior_media(chat_id: int, post: dict, destination_chat_id=None) -> bool:
    destination_chat_id = int(destination_chat_id or chat_id)
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
            chat_id=destination_chat_id,
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
            fallback = _telegram_call(bot.send_message, destination_chat_id, caption, **NO_PREVIEW)
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

def _send_post_impl(chat_id: int, post: dict, destination_chat_id=None) -> None:
    destination_chat_id = int(destination_chat_id or chat_id)
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
                _telegram_call(bot.send_photo,destination_chat_id,obj,caption=caption,parse_mode="HTML")
            except Exception as exc:
                print(f"⚠️ [{chat_id}] Не удалось отправить карту с caption: {exc}")
                _telegram_call(bot.send_message,destination_chat_id,caption,parse_mode="HTML",**NO_PREVIEW)
            send_media=[]
        else:
            _telegram_call(bot.send_message,destination_chat_id,caption,parse_mode="HTML",**NO_PREVIEW)
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
        _telegram_call(bot.send_message,destination_chat_id,text,parse_mode="HTML",**NO_PREVIEW)
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
                _telegram_call(bot.send_photo, destination_chat_id, image_file, caption=poll_text, parse_mode="HTML")
            else:
                _telegram_call(bot.send_photo, destination_chat_id, image_file)
                _telegram_call(bot.send_message, destination_chat_id, poll_text, parse_mode="HTML", **NO_PREVIEW)
        else:
            _telegram_call(bot.send_message, destination_chat_id, poll_text, parse_mode="HTML", **NO_PREVIEW)
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
            _telegram_call(bot.send_message,destination_chat_id,msg_text,**NO_PREVIEW)
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
                _telegram_call(bot.send_media_group,destination_chat_id,album)
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
                if caption_in_album:
                    try:
                        retry_album = [_telegram_media_input(media, post) for media in chunk]
                        _telegram_call(bot.send_media_group, destination_chat_id, retry_album)
                        album_sent_ids.update(id(media) for media in chunk)
                        if caption_pending:
                            try:
                                _telegram_call(bot.send_message, destination_chat_id, msg_text, **NO_PREVIEW)
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
                    _telegram_call(bot.send_photo,destination_chat_id,obj,caption=msg_text,parse_mode="HTML")
                    caption_pending=False
                else:
                    _telegram_call(bot.send_photo,destination_chat_id,obj)
            elif media_type=="video_note":
                try:
                    _telegram_call(bot.send_video_note,destination_chat_id,obj)
                except Exception as note_error:
                    debug_logger.warning(
                        "TELEGRAM_VIDEO_NOTE_FALLBACK source_id=%s error=%s",
                        post.get("source_id") or post.get("idx") or "?",
                        log_preview(note_error, 220),
                    )
                    obj.seek(0)
                    try:
                        _telegram_call(
                            bot.send_video, destination_chat_id, obj,
                            supports_streaming=True,
                        )
                    except Exception:
                        obj.seek(0)
                        _telegram_call(
                            bot.send_document, destination_chat_id, obj,
                            visible_file_name="video_note.mp4",
                        )
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
                        _telegram_call(bot.send_video, destination_chat_id, obj, **video_options)
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
                        _telegram_call(bot.send_document, destination_chat_id, obj, **document_options)
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
                            bot.send_document, destination_chat_id, obj, caption=msg_text,
                            parse_mode="HTML", visible_file_name=filename,
                        )
                        caption_pending=False
                    else:
                        _telegram_call(bot.send_document, destination_chat_id, obj, visible_file_name=filename)
            elif media_type=="voice":
                try:
                    _telegram_call(bot.send_voice,destination_chat_id,obj)
                except Exception as voice_error:
                    print(f"⚠️ [{chat_id}] send_voice не принят Telegram: {voice_error}; пробуем audio")
                    obj.seek(0)
                    _telegram_call(bot.send_audio,destination_chat_id,obj,title=filename)
                caption_pending=False
            elif media_type=="audio":
                _telegram_call(bot.send_audio,destination_chat_id,obj,title=filename)
                caption_pending=False
            elif media_type=="animation":
                _telegram_call(bot.send_animation,destination_chat_id,obj)
            elif media_type=="sticker":
                _telegram_call(bot.send_photo,destination_chat_id,obj)
            else:
                if caption_pending and len(msg_text)<=1024:
                    _telegram_call(bot.send_document,destination_chat_id,obj,caption=msg_text,visible_file_name=filename)
                    caption_pending=False
                else:
                    _telegram_call(bot.send_document,destination_chat_id,obj,visible_file_name=filename)
        except Exception as error:
            print(f"⚠️ [{chat_id}] Ошибка отправки {media_type}: {error}")
            if media_type == "video":
                raise
            if media_type in {"image", "sticker", "voice", "audio", "animation"}:
                try:
                    obj.seek(0)
                    document_options = {"visible_file_name": filename}
                    if media_type == "image" and caption_pending and len(msg_text) <= 1024:
                        document_options.update(caption=msg_text, parse_mode="HTML")
                    _telegram_call(bot.send_document, destination_chat_id, obj, **document_options)
                    if media_type == "image" and caption_pending and len(msg_text) <= 1024:
                        caption_pending = False
                except Exception as fallback_error:
                    raise RuntimeError(
                        f"Не отправилось медиа {media_type}; fallback send_document также завершился ошибкой: {fallback_error}"
                    ) from error
            elif media_type not in {"voice", "video_note", "image", "sticker"}:
                try:
                    obj.seek(0)
                    _telegram_call(bot.send_document, destination_chat_id, obj, visible_file_name=filename)
                except Exception as fallback_error:
                    raise RuntimeError(
                        f"Не отправилось медиа {media_type}; fallback send_document также завершился ошибкой: {fallback_error}"
                    ) from error
            else:
                raise
        time.sleep(0.5)

    if caption_pending:
        _telegram_call(bot.send_message,destination_chat_id,msg_text,**NO_PREVIEW)

def _send_post(chat_id: int, post: dict, destination_chat_id=None) -> tuple[bool, int | None]:
    destination_chat_id = int(destination_chat_id or chat_id)
    post_id = str(post.get("source_id") or post.get("idx") or "?")
    button_chat = get_chat_by_id(int(chat_id))
    button_keyboard = (
        _public_max_send_button_keyboard(int(chat_id))
        if _max_send_button_is_eligible(button_chat)
        else None
    )
    context = {
        "chat_id": chat_id,
        "destination_chat_id": destination_chat_id,
        "button_keyboard": button_keyboard,
        "inline_button_message_ids": [],
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
        _send_post_impl(chat_id, post, destination_chat_id=destination_chat_id)
        if context["message_ids"]:
            _record_parser_sent_post(
                chat_id, post, context["message_ids"],
                captioned=context["caption_sent"], text_sent=context["text_sent"],
                destination_chat_id=destination_chat_id,
            )
            sent_record_saved = True
        if post.get("poll") and context["message_ids"]:
            _remember_sent_poll(
                chat_id, post, context["message_ids"][0],
                destination_chat_id=destination_chat_id,
            )
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
        latest_message_id = int(context["message_ids"][-1]) if context["message_ids"] else None
        for message_id in context["inline_button_message_ids"]:
            if latest_message_id and int(message_id) == latest_message_id:
                continue
            try:
                bot.edit_message_reply_markup(
                    destination_chat_id, int(message_id), reply_markup=None
                )
            except Exception as button_cleanup_error:
                if "message is not modified" not in str(button_cleanup_error).lower():
                    debug_logger.info(
                        "MAX_SEND_BUTTON_INTERMEDIATE_CLEANUP_FAILED chat_id=%s message_id=%s error_type=%s",
                        chat_id, message_id, type(button_cleanup_error).__name__,
                    )
        return result != "no_telegram_payload", latest_message_id
    except Exception as exc:
        if context["message_ids"] and not sent_record_saved:
            _record_parser_sent_post(
                chat_id, post, context["message_ids"], partial=True,
                captioned=context["caption_sent"], text_sent=context["text_sent"],
                destination_chat_id=destination_chat_id,
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


def _max_outbound_fingerprint(text):
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    normalized = normalized.replace("\u200b", "").replace("\ufeff", "")
    normalized = re.sub(r"\s+", " ", normalized).strip().casefold()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _max_outbound_post_fingerprints(post):
    """Match outbound echoes whether MAX exposes them as body text or sender label."""
    post = post if isinstance(post, dict) else {}
    text = str(post.get("text") or "").strip()
    name = str(post.get("name") or "").strip()
    fingerprints = []
    candidates = [text, f"{name} {text}".strip()]
    if not text:
        candidates.append(name)
    for candidate in candidates:
        fingerprint = _max_outbound_fingerprint(candidate)
        if fingerprint and fingerprint not in fingerprints:
            fingerprints.append(fingerprint)
    return fingerprints


def _outbound_video_time_fingerprint(value):
    """Stable one-use key for MAX's outgoing video when no message ID is exposed."""
    value = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    match = re.search(r"(?<!\d)(\d{1,2}:\d{2}(?:\s*(?:am|pm))?)(?!\d)", value)
    if not match:
        return ""
    return f"outgoing:video:{match.group(1)}"


MAX_OUTBOUND_KIND_ECHO_TTL = 10 * 60


def _max_outbound_media_kind(item):
    item = item if isinstance(item, dict) else {}
    kind = str(item.get("kind") or item.get("type") or "").casefold()
    aliases = {
        "photo": "image", "sticker": "image", "sticker_image": "image",
        "video_note": "video", "animation": "video", "voice": "audio",
        "file": "document", "unknown": "document",
    }
    if kind:
        return aliases.get(kind, kind)
    mime = str(item.get("mime_type") or item.get("content_type") or "").casefold()
    return (
        "image" if mime.startswith("image/") else
        "video" if mime.startswith("video/") else
        "audio" if mime.startswith("audio/") else "document"
    )


def _remember_max_outbound_kind_echoes(chat_ids, media):
    media_items = media if isinstance(media, list) else ([media] if media else [])
    counts = {}
    for item in media_items:
        kind = _max_outbound_media_kind(item)
        # MAX may re-encode a video note, so its downloaded bytes can differ
        # from Telegram's original. Text and other media already have exact
        # fingerprints; use this short-lived fallback for videos only.
        if kind != "video":
            continue
        counts[kind] = counts.get(kind, 0) + 1
    for chat_id in set(int(value) for value in chat_ids):
        for kind, count in counts.items():
            for _ in range(count):
                marker = f"outgoing-kind:{kind}:{uuid.uuid4().hex}"
                _remember_max_outbound_echo(
                    chat_id, marker, ttl_seconds=MAX_OUTBOUND_KIND_ECHO_TTL
                )


def _max_outbound_post_media_kinds(post):
    post = post if isinstance(post, dict) else {}
    kinds = set()
    if post.get("video_note_hint"):
        kinds.add("video")
    if post.get("voice_hint"):
        kinds.add("audio")
    for item in post.get("media_files") or []:
        kind = _max_outbound_media_kind(item)
        if kind:
            kinds.add(kind)
    return kinds


def _load_max_outbound_suppressions():
    now = time.time()
    try:
        rows = get_pending_max_outbound_messages()
    except Exception as error:
        debug_logger.warning("MAX_OUTBOUND_SUPPRESSIONS_LOAD_FAILED error_type=%s", type(error).__name__)
        return
    with max_outbound_suppressions_lock:
        for row in rows:
            created_at = row.get("created_at")
            try:
                fingerprint = str(row["fingerprint"])
                ttl_seconds = (
                    MAX_OUTBOUND_KIND_ECHO_TTL
                    if fingerprint.startswith("outgoing-kind:")
                    else 2 * 24 * 60 * 60
                )
                expires_at = float(created_at.timestamp()) + ttl_seconds
                if expires_at > now:
                    max_outbound_suppressions[(int(row["chat_id"]), fingerprint)] = expires_at
            except (AttributeError, TypeError, ValueError):
                continue


def _remember_max_outbound_echo(chat_id, fingerprint, ttl_seconds=None):
    key = (int(chat_id), str(fingerprint))
    with max_outbound_suppressions_lock:
        remember_max_outbound_message(*key)
        ttl_seconds = float(ttl_seconds or 2 * 24 * 60 * 60)
        max_outbound_suppressions[key] = time.time() + ttl_seconds


def _consume_max_outbound_echo(chat_id, fingerprint):
    key = (int(chat_id), str(fingerprint))
    now = time.time()
    with max_outbound_suppressions_lock:
        expires_at = max_outbound_suppressions.get(key)
        if not expires_at or expires_at <= now:
            max_outbound_suppressions.pop(key, None)
            return False
        max_outbound_suppressions.pop(key, None)
        try:
            consume_max_outbound_message(*key)
        except Exception as error:
            debug_logger.warning(
                "MAX_OUTBOUND_SUPPRESSION_CONSUME_FAILED chat_id=%s error_type=%s",
                chat_id, type(error).__name__,
            )
        return True


def _consume_max_outbound_kind_echo(chat_id, kind):
    prefix = f"outgoing-kind:{str(kind or '').casefold()}:"
    now = time.time()
    with max_outbound_suppressions_lock:
        candidates = [
            (key, expires_at)
            for key, expires_at in max_outbound_suppressions.items()
            if key[0] == int(chat_id) and key[1].startswith(prefix)
        ]
        expired_keys = [key for key, expires_at in candidates if expires_at <= now]
        for expired_key in expired_keys:
            max_outbound_suppressions.pop(expired_key, None)
            try:
                consume_max_outbound_message(*expired_key)
            except Exception:
                pass
        candidates = [(key, expiry) for key, expiry in candidates if expiry > now]
        if not candidates:
            return False
        key, _ = min(candidates, key=lambda item: item[1])
        max_outbound_suppressions.pop(key, None)
        try:
            consume_max_outbound_message(*key)
        except Exception as error:
            debug_logger.warning(
                "MAX_OUTBOUND_SUPPRESSION_CONSUME_FAILED chat_id=%s error_type=%s",
                chat_id, type(error).__name__,
            )
        return True


def _process_posts(
    chat_id: int, title: str, posts: list, record_diagnostics=True,
    diagnostics_out=None, destination_chat_id=None,
) -> tuple:
    if posts is None:
        raise TypeError(f"Парсер MAX для чата {title} вернул None вместо списка сообщений")
    if not isinstance(posts, list):
        raise TypeError(
            f"Парсер MAX для чата {title} вернул {type(posts).__name__} вместо списка сообщений"
        )
    new_count = 0
    skipped_count = 0
    failed_count = 0
    latest_sent_message_id = None
    diagnostic_posts = []
    destination_chat_id = int(destination_chat_id or chat_id)
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
            and _attach_caption_to_prior_media(chat_id, post, destination_chat_id)
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
            sent, sent_message_id = _send_post(chat_id, post, destination_chat_id)
            if not sent:
                skipped_count += 1
                diagnostic_posts.append(_diagnostic_post(post, "skipped", "нет поддерживаемого содержимого"))
                debug_logger.warning(
                    "MESSAGE_NOT_SENT chat_id=%s source_id=%s reason=no_payload text=%s",
                    chat_id, source_id or "?", log_preview(post.get("text")) or "<empty>"
                )
                continue
            if sent_message_id:
                latest_sent_message_id = int(sent_message_id)
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

    button_chat = get_chat_by_id(int(chat_id))
    # Recheck the single inline action every parse cycle so it is restored if
    # Telegram or another client removed the keyboard, and moved beside the
    # newest parser post without leaving copies on older posts.
    should_sync_button = bool(
        button_chat
        and button_chat.get("is_active")
        and not (
            button_chat.get("max_source_type") == "channel"
            and (
                bool(button_chat.get("max_source_type_verified"))
                or bool(button_chat.get("max_source_type_manual"))
            )
        )
    )
    if should_sync_button:
        try:
            _sync_max_send_button(
                int(chat_id),
                anchor_message_id=latest_sent_message_id,
                destination_chat_id=destination_chat_id,
            )
        except Exception as error:
            debug_logger.info(
                "MAX_SEND_BUTTON_REFRESH_FAILED chat_id=%s error_type=%s",
                chat_id, type(error).__name__,
            )
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
                destination_chat_id=(
                    get_chat_by_id(chat_id) or {}
                ).get("destination_chat_id") or chat_id,
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


def _update_detected_source_type(chat_id, source_type):
    if source_type not in {"group", "channel", "private"}:
        return
    try:
        chat = get_chat_by_id(int(chat_id))
        if not chat:
            return
        # A user-selected source type is authoritative: MAX's header can be
        # ambiguous (especially for private chats). A successful navigation
        # verifies access, but must not overwrite the user's group/private
        # choice. A detected channel is the safety exception: MAX channels do
        # not support the outbound-send action, even if the user picked a group.
        if bool(chat.get("max_source_type_manual")):
            if source_type == "channel" and chat.get("max_source_type") != "channel":
                update_chat_source_type(int(chat_id), "channel", verified=True, manual=True)
                debug_logger.info(
                    "MAX_SOURCE_TYPE_CHANNEL_SAFETY_OVERRIDE chat_id=%s selected=%s",
                    chat_id, chat.get("max_source_type"),
                )
                _sync_max_send_button(int(chat_id))
            elif chat.get("max_source_type") and not bool(chat.get("max_source_type_verified")):
                update_chat_source_type(
                    int(chat_id), chat["max_source_type"], verified=True, manual=True
                )
                _sync_max_send_button(int(chat_id))
            return
        if (
            chat.get("max_source_type") == source_type
            and bool(chat.get("max_source_type_verified"))
        ):
            return
        update_chat_source_type(int(chat_id), source_type, verified=True)
        debug_logger.info(
            "MAX_SOURCE_TYPE_DETECTED chat_id=%s source_type=%s",
            chat_id, source_type,
        )
        _sync_max_send_button(int(chat_id))
    except Exception as error:
        debug_logger.warning(
            "MAX_SOURCE_TYPE_PERSIST_FAILED chat_id=%s error_type=%s",
            chat_id, type(error).__name__,
        )


def _verify_manual_source_type(chat_id):
    """Mark the user's selected source kind verified after MAX opens the URL."""
    try:
        chat = get_chat_by_id(int(chat_id))
        if (
            chat
            and bool(chat.get("max_source_type_manual"))
            and chat.get("max_source_type") in {"group", "channel", "private"}
            and not bool(chat.get("max_source_type_verified"))
        ):
            update_chat_source_type(
                int(chat_id), chat["max_source_type"], verified=True, manual=True
            )
            _sync_max_send_button(int(chat_id))
    except Exception as error:
        debug_logger.warning(
            "MAX_MANUAL_SOURCE_VERIFY_FAILED chat_id=%s error_type=%s",
            chat_id, type(error).__name__,
        )


def run_one_parse_cycle():
    for configured_chat in get_all_chats():
        try:
            _sync_send_to_max_command_menu(
                int(configured_chat["chat_id"]), configured_chat
            )
        except Exception as command_menu_error:
            debug_logger.warning(
                "SEND_TO_MAX_COMMAND_MENU_CYCLE_SYNC_FAILED chat_id=%s error_type=%s",
                configured_chat.get("chat_id"), type(command_menu_error).__name__,
            )
    active_chats = _get_payable_active_chats()
    results = []
    if not active_chats:
        return "⚠️ Нет активных чатов для парсинга"
    for chat in active_chats:
        chat_id = chat["chat_id"]
        max_url = chat["max_url"]
        title = chat["title"]
        try:
            # Reconcile existing connections on every cycle, even when MAX
            # yielded no new posts. This also repairs old chats in place.
            _sync_max_send_button(
                int(chat_id),
                destination_chat_id=(chat.get("destination_chat_id") or chat_id),
            )
        except Exception as button_error:
            debug_logger.warning(
                "MAX_SEND_BUTTON_CYCLE_SYNC_FAILED chat_id=%s error_type=%s",
                chat_id, type(button_error).__name__,
            )
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
                source_type_callback=lambda detected, cid=chat_id: _update_detected_source_type(cid, detected),
            )
            _verify_manual_source_type(chat_id)
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
            source_type_callback=lambda detected, cid=chat_id: _update_detected_source_type(cid, detected),
        )
        _verify_manual_source_type(chat_id)
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

def _escape_html_attribute(value):
    return escape_html(str(value or "")).replace('"', "&quot;").replace("'", "&#x27;")


def _telegram_entities_to_html(text, entities=None):
    """Convert Telegram's UTF-16 entity offsets into escaped Telegram HTML."""
    text = str(text or "")
    if not text:
        return ""

    utf16_to_index = {0: 0}
    utf16_offset = 0
    for index, char in enumerate(text):
        utf16_offset += len(char.encode("utf-16-le")) // 2
        utf16_to_index[utf16_offset] = index + 1

    tag_by_type = {
        "bold": ("<b>", "</b>"),
        "italic": ("<i>", "</i>"),
        "underline": ("<u>", "</u>"),
        "strikethrough": ("<s>", "</s>"),
        "spoiler": ("<tg-spoiler>", "</tg-spoiler>"),
        "code": ("<code>", "</code>"),
        "blockquote": ("<blockquote>", "</blockquote>"),
        "expandable_blockquote": ("<blockquote expandable>", "</blockquote>"),
    }
    starts = {}
    ends = {}
    for sequence, entity in enumerate(entities or []):
        try:
            kind = str(entity.type)
            start_units = int(entity.offset)
            end_units = start_units + int(entity.length)
            start = utf16_to_index[start_units]
            end = utf16_to_index[end_units]
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if start < 0 or end > len(text) or end <= start:
            continue
        opening, closing = tag_by_type.get(kind, ("", ""))
        if kind == "text_link" and getattr(entity, "url", None):
            opening = f'<a href="{_escape_html_attribute(entity.url)}">'
            closing = "</a>"
        elif kind == "text_mention" and getattr(entity, "user", None):
            opening = f'<a href="tg://user?id={int(entity.user.id)}">'
            closing = "</a>"
        elif kind == "custom_emoji" and getattr(entity, "custom_emoji_id", None):
            opening = f'<tg-emoji emoji-id="{_escape_html_attribute(entity.custom_emoji_id)}">'
            closing = "</tg-emoji>"
        elif kind == "pre":
            language = str(getattr(entity, "language", "") or "").strip()
            if language:
                opening = f'<pre><code class="language-{_escape_html_attribute(language)}">'
                closing = "</code></pre>"
            else:
                opening, closing = "<pre>", "</pre>"
        if not opening:
            continue
        item = {"start": start, "end": end, "opening": opening, "closing": closing, "sequence": sequence}
        starts.setdefault(start, []).append(item)
        ends.setdefault(end, []).append(item)

    output = []
    for position in range(len(text) + 1):
        for item in sorted(ends.get(position, ()), key=lambda value: (value["start"], -value["sequence"]), reverse=True):
            output.append(item["closing"])
        for item in sorted(starts.get(position, ()), key=lambda value: (-value["end"], value["sequence"])):
            output.append(item["opening"])
        if position < len(text):
            output.append(escape_html(text[position]))
    return "".join(output)


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


def _build_phone_login_keyboard(target_chat_id, link):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🔐 Открыть MAX и войти", url=link))
    kb.add(types.InlineKeyboardButton(
        "🔄 Обновить страницу", callback_data=f"qr_refreshpage_{target_chat_id}"
    ))
    kb.add(types.InlineKeyboardButton(
        "🔙 Назад к QR-коду", callback_data=f"qr_back_{target_chat_id}"
    ))
    kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data=f"qr_cancel_{target_chat_id}"))
    return kb


def _phone_login_message_text():
    return (
        "📱 <b>Вход в MAX по номеру</b>\n\n"
        "Откройте защищённую страницу и проходите вход прямо в окне MAX: выберите вход по номеру, "
        "введите номер, код и при необходимости пароль там же. Не отправляйте их боту.\n\n"
        "Ссылка одноразовая и действует 10 минут. Не пересылайте её. После успешного входа браузер "
        "закроется, а MAX-сессия сохранится на сервере. Сервер браузера обрабатывает ввод, "
        "но бот не получает номер/код через Telegram и не записывает текст ввода."
    )


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


def _build_auth_human_keyboard(target_chat_id, reason):
    if reason == "device_password_setup":
        # The next action is on the MAX screen, not another Telegram check.
        return _build_password_keyboard(target_chat_id)
    return _build_auth_check_keyboard(target_chat_id)


def _auth_human_check_text(session, reason):
    if reason == "device_password_setup":
        return (
            "⚠️ MAX пока не завершил вход.\n\n"
            "1. На телефоне или другом устройстве, где ваш аккаунт MAX уже открыт, зайдите в "
            "«Настройки» → «Безопасность» → «Пароль для входа» и задайте пароль.\n\n"
            "2. Вернитесь в открытую защищённую страницу входа. На самом экране MAX нажмите "
            "«Попробовать войти снова» (если надпись на английском — «Try signing in again»).\n\n"
            "3. Продолжите шаги, которые покажет MAX. Не нажимайте «Проверить вход» в Telegram: "
            "повторная проверка сама по себе не завершает вход.\n\n"
            "Бот сохранит сессию и включит парсинг только после того, как MAX действительно откроет аккаунт."
        )
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
            _sync_max_send_button(int(target_chat_id))
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
    device_password_notice_sent = False
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
            if (
                status == "awaiting_human"
                and data == "device_password_setup"
                and not device_password_notice_sent
            ):
                device_password_notice_sent = True
                try:
                    bot.send_message(
                        output_chat_id,
                        _auth_human_check_text(session, data),
                        reply_markup=_build_auth_human_keyboard(target_chat_id, data),
                        **NO_PREVIEW,
                    )
                except Exception as error:
                    debug_logger.warning(
                        "REMOTE_MAX_PASSWORD_SETUP_NOTICE_FAILED target=%s error_type=%s",
                        target_chat_id, type(error).__name__,
                    )
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
            _sync_max_send_button(int(target_chat_id))
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
    kb.add(types.InlineKeyboardButton("🗑 Удалить сообщения бота", callback_data="admin_sent_messages"))
    kb.row(
        types.InlineKeyboardButton("👑 Админы", callback_data="admin_admins"),
        types.InlineKeyboardButton("👤 Пользователи", callback_data="admin_users")
    )
    kb.row(types.InlineKeyboardButton("📢 Рассылка всем", callback_data="admin_broadcast"))
    kb.row(types.InlineKeyboardButton("💬 Отправить в чат", callback_data="admin_send_to_chat"))
    kb.row(types.InlineKeyboardButton("👤 Отправить пользователю", callback_data="admin_send_to_user"))
    if int(chat_id) == ADMIN_ID:
        kb.add(types.InlineKeyboardButton("✉️ Отправить сообщение в MAX", callback_data="admin_send_to_max"))
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
    for record in reversed(_sent_parser_records_snapshot()):
        try:
            if int(record.get("destination_chat_id") or record.get("chat_id")) == int(target_chat_id):
                title = str(record.get("destination_title") or "").strip()
                if title:
                    return title
        except (TypeError, ValueError):
            continue
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
            "🗑 <b>Удаление сообщений бота</b>\n\n"
            "Выберите Telegram-группу. Здесь доступны сообщения парсера и другие сообщения, отправленные ботом, за последние 47 часов.\n\n"
            "Отметьте сообщения кнопками и подтвердите удаление."
        )
    else:
        text = (
            "🗑 <b>Удаление сообщений бота</b>\n\n"
            "Новые сообщения парсера и другие отправки бота появятся здесь. Удалять их можно кнопками в этом разделе.\n\n"
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

def _show_sent_parser_posts(chat_id, message_id, target_chat_id, page=0):
    records = [
        record for record in _sent_parser_records_snapshot()
        if str(record.get("chat_id")) == str(target_chat_id) and _sent_record_is_deletable(record)
    ]
    records.sort(key=lambda record: str(record.get("sent_at") or ""), reverse=True)
    page_size = 10
    page_count = max(1, (len(records) + page_size - 1) // page_size)
    page = max(0, min(int(page or 0), page_count - 1))
    page_records = records[page * page_size:(page + 1) * page_size]
    title = _sent_parser_chat_title(target_chat_id)
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for record in page_records:
        preview = " ".join(str(record.get("preview") or "Сообщение").split())[:34]
        label = f"🗑 {_sent_record_time_label(record)} · {preview}"
        keyboard.add(types.InlineKeyboardButton(
            label, callback_data=f"admin_sent_post_{record.get('record_id')}"
        ))
    if page_count > 1:
        navigation = []
        if page > 0:
            navigation.append(types.InlineKeyboardButton(
                "◀️ Предыдущие", callback_data=f"admin_sent_page_{int(target_chat_id)}_{page - 1}"
            ))
        if page + 1 < page_count:
            navigation.append(types.InlineKeyboardButton(
                "Следующие ▶️", callback_data=f"admin_sent_page_{int(target_chat_id)}_{page + 1}"
            ))
        keyboard.row(*navigation)
    keyboard.add(types.InlineKeyboardButton("🔙 К группам", callback_data="admin_sent_messages"))
    text = (
        f"🗑 <b>{escape_html(title)}</b>\n\n"
        "Выберите отправку, которую нужно удалить. Если у неё было несколько фото или файлов, удалится весь набор."
        + (f"\n\nСтраница {page + 1} из {page_count}." if page_count > 1 else "")
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
    target_chat_id = int(record.get("destination_chat_id") or record.get("chat_id"))
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
    destination_chat_id = int(chat.get("destination_chat_id") or target_chat_id)
    identifier_line = (
        f"🆔 <b>ID Telegram-получателя:</b> <code>{destination_chat_id}</code>\n"
        if chat.get("destination_type") == "private"
        else f"🆔 <b>ID:</b> <code>{chat['chat_id']}</code>\n"
    )
    account_mode_text = "Готовый аккаунт" if chat.get("account_mode") == "project" else "Аккаунт пользователя"
    phone_line = (
        f"📱 <b>Телефон:</b> {escape_html(chat['phone'] or '❌ не задан')}\n"
        if chat.get("account_mode") != "project" else ""
    )

    telegram_type = ""
    group_info_lines = ""
    if chat.get("destination_type") == "private":
        telegram_type = "private"
        group_info_lines = "📍 <b>Куда отправляются сообщения:</b> личные сообщения владельца\n\n"
    else:
        try:
            telegram_chat = bot.get_chat(destination_chat_id)
            telegram_type = str(getattr(telegram_chat, "type", "") or "")
            type_labels = {
                "private": "личный чат",
                "group": "группа",
                "supergroup": "супергруппа",
                "channel": "канал",
            }
            type_label = type_labels.get(telegram_type, "чат")
            member_label = "Подписчиков" if telegram_type == "channel" else "Участников"
            try:
                member_count = bot.get_chat_member_count(destination_chat_id)
                count_line = f"👥 <b>{member_label}:</b> {int(member_count):,}\n".replace(",", " ")
            except Exception:
                count_line = f"👥 <b>{member_label}:</b> временно недоступно\n"
            username = getattr(telegram_chat, "username", None)
            username_line = (
                f"🔗 <b>Публичная ссылка:</b> https://t.me/{escape_html(username)}\n"
                if username else ""
            )
            description = str(getattr(telegram_chat, "description", "") or "").strip()
            description_line = (
                f"📝 <b>Описание:</b> {escape_html(description[:400])}\n"
                if description else ""
            )
            group_info_lines = (
                f"📍 <b>Тип Telegram-чата:</b> {type_label}\n"
                f"{count_line}{username_line}{description_line}\n"
            )
        except Exception as error:
            debug_logger.info(
                "ADMIN_CHAT_INFO_UNAVAILABLE chat_id=%s error_type=%s",
                target_chat_id, type(error).__name__,
            )
            group_info_lines = "📍 <b>Сведения о Telegram-чате:</b> временно недоступны\n\n"

    text = (
        f"📋 <b>{escape_html(chat['title'] or 'Без названия')}</b>\n\n"
        f"{identifier_line}"
        f"🔗 <b>URL:</b> {escape_html(chat['max_url'] or ' не задан')}\n"
        f"{phone_line}"
        f"👤 <b>MAX-аккаунт:</b> {account_mode_text}\n"
        f"🧑‍💻 <b>Владелец Telegram:</b> <code>{chat.get('owner_id') or 'не назначен'}</code>\n"
        f"🔑 <b>Сессия:</b> {session_status}\n"
        f"⚙️ <b>Парсинг:</b> {status_text}\n\n"
        f"{group_info_lines}"
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
    if telegram_type in ("group", "supergroup"):
        kb.add(types.InlineKeyboardButton(
            "💬 Написать в группу", callback_data=f"admin_group_message_{target_chat_id}"
        ))
    kb.add(types.InlineKeyboardButton("🔗 Задать URL группы MAX", callback_data=f"admin_set_url_{target_chat_id}"))
    if chat.get("account_mode") != "project":
        kb.add(types.InlineKeyboardButton("📱 Задать номер телефона", callback_data=f"admin_set_phone_{target_chat_id}"))
    kb.add(types.InlineKeyboardButton("🔑 Авторизоваться (QR)", callback_data=f"admin_qr_login_{target_chat_id}"))
    kb.add(types.InlineKeyboardButton("🗑 Очистить кэш чата", callback_data=f"admin_clear_chat_cache_{target_chat_id}"))
    kb.add(types.InlineKeyboardButton("❌ Удалить чат", callback_data=f"admin_ask_delete_{target_chat_id}", style="danger"))
    kb.add(types.InlineKeyboardButton("🔙 К списку чатов", callback_data="admin_chats", style="primary"))
    safe_edit_message(text, chat_id, message_id, reply_markup=kb, parse_mode="HTML", **NO_PREVIEW)

def process_admin_group_question(message, target_chat_id):
    admin_id = int(message.from_user.id)
    if not is_admin(admin_id):
        return
    if message.content_type != "text" or not (message.text or "").strip() or message.text.strip().startswith("/"):
        prompt = bot.send_message(
            message.chat.id,
            "Отправьте вопрос обычным текстом или нажмите «Отмена».",
            reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton(
                "❌ Отмена", callback_data=f"admin_group_message_cancel_{int(target_chat_id)}"
            )),
            **NO_PREVIEW,
        )
        bot.register_next_step_handler(prompt, process_admin_group_question, int(target_chat_id))
        return
    question = message.text.strip()
    if len(question) > 3800:
        prompt = bot.send_message(
            message.chat.id,
            "Сообщение слишком длинное. Сократите его до 3800 символов или нажмите «Отмена».",
            reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton(
                "❌ Отмена", callback_data=f"admin_group_message_cancel_{int(target_chat_id)}"
            )),
            **NO_PREVIEW,
        )
        bot.register_next_step_handler(prompt, process_admin_group_question, int(target_chat_id))
        return
    try:
        telegram_chat = bot.get_chat(int(target_chat_id))
        if getattr(telegram_chat, "type", None) not in ("group", "supergroup"):
            bot.send_message(message.chat.id, "Этот чат больше не является группой.", **NO_PREVIEW)
            return
        question_html = _telegram_entities_to_html(question, getattr(message, "entities", None))
        sent = bot.send_message(
            int(target_chat_id),
            "📨 <b>Сообщение от админа</b>\n\n"
            f"{question_html}\n\n"
            "Ответьте на это сообщение в группе или нажмите кнопку, чтобы написать администраторам лично.",
            parse_mode="HTML",
            **NO_PREVIEW,
        )
        _record_bot_sent_group_messages(
            target_chat_id,
            [sent.message_id],
            question,
            has_media=False,
        )
        save_admin_group_thread_message(target_chat_id, sent.message_id, admin_id)
        reply_token = uuid.uuid4().hex
        save_admin_group_reply_link_token(reply_token, target_chat_id, sent.message_id)
        reply_keyboard = types.InlineKeyboardMarkup(row_width=1)
        reply_keyboard.add(types.InlineKeyboardButton(
            "✉️ Ответить",
            url=f"https://t.me/{BOT_PUBLIC_USERNAME}?start=reply_{reply_token}",
        ))
        bot.edit_message_reply_markup(
            int(target_chat_id), sent.message_id, reply_markup=reply_keyboard
        )
        bot.send_message(
            message.chat.id,
            f"✅ Сообщение отправлено в группу «{escape_html(telegram_chat.title or str(target_chat_id))}». Ответы на сообщение бота будут приходить всем администраторам.",
            reply_markup=admin_send_again_keyboard("group", target_chat_id),
            parse_mode="HTML",
            **NO_PREVIEW,
        )
    except Exception as error:
        debug_logger.warning(
            "ADMIN_GROUP_QUESTION_SEND_FAILED group_id=%s admin_id=%s error_type=%s",
            target_chat_id, admin_id, type(error).__name__,
        )
        bot.send_message(
            message.chat.id,
            "Не удалось отправить сообщение. Проверьте, что бот состоит в группе и может писать в неё.",
            **NO_PREVIEW,
        )


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
    kb.add(types.InlineKeyboardButton(
        "🗑 Удалить пользователя",
        callback_data=f"admin_delete_user_{page}",
        style="danger",
    ))
    kb.add(types.InlineKeyboardButton("🔙 Назад в меню", callback_data="admin_back"))
    safe_edit_message(text, chat_id, message_id, reply_markup=kb, **NO_PREVIEW)


def process_admin_delete_user(message, list_message_id, page):
    admin_id = int(message.from_user.id)
    if not is_admin(admin_id):
        return

    raw_user_id = (message.text or "").strip()
    if not re.fullmatch(r"\d{1,20}", raw_user_id):
        prompt = bot.send_message(
            message.chat.id,
            "Пришлите только числовой user ID из таблицы пользователей.",
            reply_markup=get_cancel_keyboard(),
            **NO_PREVIEW,
        )
        bot.register_next_step_handler(
            prompt, process_admin_delete_user, list_message_id, page
        )
        return

    user_id = int(raw_user_id)
    if not get_user_profile(user_id):
        prompt = bot.send_message(
            message.chat.id,
            "Пользователь с таким ID не найден. Проверьте ID и отправьте его ещё раз.",
            reply_markup=get_cancel_keyboard(),
            **NO_PREVIEW,
        )
        bot.register_next_step_handler(
            prompt, process_admin_delete_user, list_message_id, page
        )
        return

    had_referral_record = has_referral_invite(user_id)
    if not delete_user(user_id):
        bot.send_message(
            message.chat.id,
            "Не удалось удалить пользователя. Обновите список и попробуйте ещё раз.",
            **NO_PREVIEW,
        )
        return

    result = f"✅ Пользователь <code>{user_id}</code> удалён из списка пользователей."
    if had_referral_record:
        result += (
            "\n\nУ него уже есть запись об участии в реферальной программе. "
            "Удаление профиля не сбрасывает её, поэтому повторно привязать его к другой ссылке не получится."
        )
    else:
        result += (
            "\n\nДля проверки реферальной ссылки попросите его сразу открыть её. "
            "Бот добавит профиль заново после перехода."
        )
    bot.send_message(message.chat.id, result, parse_mode="HTML", **NO_PREVIEW)
    show_users_list(message.chat.id, list_message_id, page)


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


def send_content_to_user(user_id, message, sent_message_ids=None):
    try:
        collected_ids = []

        def send_and_collect(send_method, *args, **kwargs):
            result = send_method(*args, **kwargs)
            collected_ids.extend(_telegram_result_ids(result))
            return result

        header_html = "📨 <b>Сообщение от админа</b>\n\n"
        header_plain = "📨 Сообщение от админа\n\n"
        caption = str(getattr(message, "caption", None) or "")
        caption_html = _telegram_entities_to_html(
            caption, getattr(message, "caption_entities", None)
        )

        def send_captioned(send_method, file_id):
            if caption and len(header_plain) + len(caption) <= 1024:
                send_and_collect(
                    send_method,
                    user_id,
                    file_id,
                    caption=header_html + caption_html,
                    parse_mode="HTML",
                )
            else:
                send_and_collect(bot.send_message, user_id, header_html, parse_mode="HTML", **NO_PREVIEW)
                if caption and len(caption) <= 1024:
                    send_and_collect(send_method, user_id, file_id, caption=caption_html, parse_mode="HTML")
                elif caption:
                    send_and_collect(send_method, user_id, file_id)
                    send_and_collect(bot.send_message, user_id, caption_html, parse_mode="HTML", **NO_PREVIEW)
                else:
                    send_and_collect(send_method, user_id, file_id)

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
            send_and_collect(bot.send_message, user_id, header_html, parse_mode="HTML", **NO_PREVIEW)
            send_and_collect(bot.send_sticker, user_id, message.sticker.file_id)
        elif message.video_note:
            send_and_collect(bot.send_message, user_id, header_html, parse_mode="HTML", **NO_PREVIEW)
            send_and_collect(bot.send_video_note, user_id, message.video_note.file_id)
        elif message.text is not None:
            message_text = str(message.text)
            text_html = _telegram_entities_to_html(
                message_text, getattr(message, "entities", None)
            )
            if len(header_plain) + len(message_text) <= 4096:
                send_and_collect(
                    bot.send_message,
                    user_id,
                    header_html + text_html,
                    parse_mode="HTML",
                    **NO_PREVIEW,
                )
            else:
                send_and_collect(bot.send_message, user_id, header_html, parse_mode="HTML", **NO_PREVIEW)
                send_and_collect(bot.send_message, user_id, text_html, parse_mode="HTML", **NO_PREVIEW)
        else:
            return False, "Тип сообщения не поддерживается"

        if sent_message_ids is not None:
            sent_message_ids.extend(collected_ids)
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


def admin_send_again_keyboard(target_type, target_id):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton(
        "✉️ Отправить ещё",
        callback_data=f"admin_send_again_{target_type}_{int(target_id)}",
    ))
    return keyboard


def process_send_to_one(message, target_user_id):
    ok, error = send_content_to_user(target_user_id, message)

    if ok:
        bot.send_message(
            message.chat.id,
            f"✅ Сообщение отправлено пользователю {target_user_id}",
            reply_markup=admin_send_again_keyboard("user", target_user_id),
        )
    else:
        bot.send_message(
            message.chat.id,
            f"❌ Не удалось отправить пользователю {target_user_id}.\n"
            f"Причина: {error[:300]}"
        )

def process_send_to_chat(message, target_chat_id):
    sent_ids = []
    ok, error = send_content_to_user(target_chat_id, message, sent_message_ids=sent_ids)

    if ok:
        _record_bot_sent_group_messages(
            target_chat_id,
            sent_ids,
            getattr(message, "text", None) or getattr(message, "caption", None) or message.content_type,
            has_media=message.content_type != "text",
        )
        bot.send_message(
            message.chat.id,
            f"✅ Сообщение отправлено в чат {target_chat_id}",
            reply_markup=admin_send_again_keyboard("chat", target_chat_id),
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
            f"✅ Сообщение отправлено пользователю {target_user_id}",
            reply_markup=admin_send_again_keyboard("user", target_user_id),
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
    sent_ids = []
    ok, error = send_content_to_user(target_chat_id, message, sent_message_ids=sent_ids)

    if ok:
        _record_bot_sent_group_messages(
            target_chat_id,
            sent_ids,
            getattr(message, "text", None) or getattr(message, "caption", None) or message.content_type,
            has_media=message.content_type != "text",
        )
        bot.send_message(
            message.chat.id,
            f"✅ Сообщение отправлено в чат {target_chat_id}",
            reply_markup=admin_send_again_keyboard("chat", target_chat_id),
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
    keyboard.add(types.InlineKeyboardButton("🎁 Пригласить друга", callback_data="referral_program"))
    keyboard.add(types.InlineKeyboardButton("👑 Благодарности", callback_data="thanks_project"))
    keyboard.add(types.InlineKeyboardButton("⭐ Отзывы", callback_data="reviews_show"))
    return keyboard


def _referral_start_link(user_id):
    return f"https://t.me/{BOT_PUBLIC_USERNAME}?start=ref_{int(user_id)}"


def show_referral_program(chat_id, user_id, message_id=None, stats=None):
    if stats is None:
        stats = get_referral_stats(user_id)
    referral_link = _referral_start_link(user_id)
    share_link = "https://t.me/share/url?" + urlencode({
        "url": referral_link,
        "text": "Попробуй MAX_Parser — получай сообщения из MAX в Telegram.",
    })
    text = (
        "🎁 <b>Пригласи друга — получи 7 дней MAX_Parser</b>\n\n"
        "За каждого нового пользователя, который перейдёт по твоей ссылке и ознакомится с возможностями бота,"
        " тебе начислится 7 дней мульти-подписки. Она открывает оба способа и позволяет подключить до 10 чатов.\n\n"
        "<b>Что должен сделать друг:</b>\n"
        "1. Впервые открыть бота по твоей ссылке.\n"
        "2. Посмотреть разницу способов подключения.\n"
        "3. Открыть инструкцию.\n\n"
        f"📨 <b>Твоя ссылка:</b>\n<code>{escape_html(referral_link)}</code>\n\n"
        f"📊 <b>Приглашено:</b> {stats['total']} · <b>Наград получено:</b> {stats['rewarded']}\n"
        f"⏳ <b>Ещё выполняют условия:</b> {stats['in_progress']}"
    )
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton("📨 Поделиться ссылкой", url=share_link))
    keyboard.add(types.InlineKeyboardButton("🔄 Обновить прогресс", callback_data="referral_refresh"))
    keyboard.add(types.InlineKeyboardButton("🔙 В главное меню", callback_data="start_home"))
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


def _record_referral_engagement(user_id, activity):
    try:
        reward = record_referral_activity(int(user_id), activity)
    except Exception as error:
        debug_logger.warning(
            "REFERRAL_ACTIVITY_SAVE_FAILED user_id=%s activity=%s error_type=%s",
            user_id, activity, type(error).__name__,
        )
        return
    if not reward or not reward.get("rewarded"):
        return
    invited = get_user_profile(user_id) or {}
    invited_name = " ".join(
        part for part in (invited.get("first_name"), invited.get("last_name")) if part
    ).strip() or "Друг"
    active_until = reward["active_until"].astimezone(timezone.utc)
    try:
        bot.send_message(
            reward["referrer_id"],
            "🎉 <b>Друг выполнил условия приглашения!</b>\n\n"
            f"👤 {escape_html(invited_name)}\n"
            "🎁 Тебе добавлены 7 дней мульти-подписки.\n"
            f"📅 Подписка действует до {active_until:%d.%m.%Y %H:%M} UTC.",
            parse_mode="HTML",
            **NO_PREVIEW,
        )
    except Exception as error:
        debug_logger.warning(
            "REFERRAL_REWARD_NOTIFY_FAILED referrer_id=%s error_type=%s",
            reward["referrer_id"], type(error).__name__,
        )

def return_start():
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(types.InlineKeyboardButton("✔ Подключить MAX_Parser", callback_data="parse_setup"))
    keyboard.add(types.InlineKeyboardButton("🔙 Назад", callback_data="start_comeback"))
    return keyboard

def parser_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton(
        _subscription_product_button_label("self"), callback_data="connect_self"
    ))
    keyboard.add(types.InlineKeyboardButton(
        _subscription_product_button_label("project"), callback_data="connect_project"
    ))
    keyboard.add(types.InlineKeyboardButton(
        _subscription_product_button_label("multi"), callback_data="connect_multi"
    ))
    keyboard.add(types.InlineKeyboardButton("ℹ️ Разница способов", callback_data="compare_modes"))
    keyboard.add(types.InlineKeyboardButton("🤔 Не знаю, что выбрать", callback_data="method_choice_help"))
    keyboard.add(types.InlineKeyboardButton("📖 Инструкция", callback_data="instruction"))
    keyboard.add(types.InlineKeyboardButton("🎬 Видео инструкция", callback_data="video_instruction"))
    keyboard.add(types.InlineKeyboardButton("🏠 В главное меню", callback_data="start_home"))
    return keyboard


SUBSCRIPTION_PRICES = {
    "self": {1: 249, 3: 599, 12: 2499},
    "project": {1: 499, 3: 1199, 12: 5499},
    "multi": {1: 599, 3: 1499, 12: 6499},
}
SUBSCRIPTION_DISCOUNT_PERCENT = 50
SUBSCRIPTION_LABELS = {
    "self": "Подключение своего MAX-аккаунта",
    "project": "Подключение аккаунта MAX_Parser",
    "multi": "Мульти-подписка",
}
SUBSCRIPTION_SHORT_LABELS = {
    "self": "1️⃣ Свой",
    "project": "2️⃣ Готовый",
    "multi": "✨ Мульти",
}
SUBSCRIPTION_PERIOD_LABELS = {1: "1 месяц", 3: "3 месяца", 12: "1 год"}
SUBSCRIPTION_SHORT_PERIOD_LABELS = {1: "1 мес", 3: "3 мес", 12: "1 год"}


def _format_rubles(amount_kopecks, include_currency=True):
    rubles, kopecks = divmod(int(amount_kopecks), 100)
    formatted = f"{rubles:,}".replace(",", " ")
    if kopecks:
        formatted += f",{kopecks:02d}"
    return f"{formatted} ₽" if include_currency else formatted


def _discounted_price_kopecks(plan_code, months):
    regular_rubles = SUBSCRIPTION_PRICES[plan_code][months]
    return regular_rubles * (100 - SUBSCRIPTION_DISCOUNT_PERCENT)


def _strikethrough_button_text(text):
    return "".join(
        f"{character}\u0336" if not character.isspace() else character
        for character in text
    )


def _subscription_product_button_label(plan_code):
    regular_price = SUBSCRIPTION_PRICES[plan_code][1] * 100
    discounted_price = _discounted_price_kopecks(plan_code, 1)
    return (
        f"{SUBSCRIPTION_SHORT_LABELS[plan_code]}: "
        f"{_strikethrough_button_text(_format_rubles(regular_price, include_currency=False))}→"
        f"{_format_rubles(discounted_price)}"
    )


def _subscription_discount_text_html(plan_code):
    prices = []
    for months in (1, 3, 12):
        period = SUBSCRIPTION_PERIOD_LABELS[months]
        regular_price = _format_rubles(SUBSCRIPTION_PRICES[plan_code][months] * 100)
        discounted_price = _format_rubles(_discounted_price_kopecks(plan_code, months))
        prices.append(
            f"{period} — <s>{regular_price}</s> <b>{discounted_price}</b>"
        )
    return " · ".join(prices)


def subscription_products_keyboard(prefix="subscription_product"):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton(
        _subscription_product_button_label("self"), callback_data=f"{prefix}_self"
    ))
    keyboard.add(types.InlineKeyboardButton(
        _subscription_product_button_label("project"), callback_data=f"{prefix}_project"
    ))
    keyboard.add(types.InlineKeyboardButton(
        _subscription_product_button_label("multi"), callback_data=f"{prefix}_multi"
    ))
    return keyboard


def subscription_period_keyboard(plan_code, back_callback="subscription_products"):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for months in (1, 3, 12):
        regular_amount = SUBSCRIPTION_PRICES[plan_code][months] * 100
        discounted_amount = _discounted_price_kopecks(plan_code, months)
        period = SUBSCRIPTION_SHORT_PERIOD_LABELS[months]
        keyboard.add(types.InlineKeyboardButton(
            f"{period} · {_strikethrough_button_text(_format_rubles(regular_amount, include_currency=False))}→"
            f"{_format_rubles(discounted_amount)}",
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
    customer_chats = get_user_chats(user_id)
    charged_chat_count = sum(
        1 for chat in customer_chats
        if not (
            chat.get("destination_type") == "private"
            and not chat.get("is_active")
            and (
                chat.get("setup_pending")
                or (
                    not chat.get("max_url")
                    and int(chat.get("message_count") or 0) == 0
                )
            )
        )
    )
    if charged_chat_count >= chat_limit:
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
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton(
        "👥 В Telegram-группу", callback_data=f"connectdest_group_{account_mode}"
    ))
    keyboard.add(types.InlineKeyboardButton(
        "👤 В личные сообщения", callback_data=f"connectdest_private_{account_mode}"
    ))
    keyboard.add(types.InlineKeyboardButton("❌ Отмена", callback_data="cancel_step"))
    text = (
        f"Вы выбрали: <b>{label}</b>.\n\n"
        "Куда отправлять новые сообщения из MAX?"
    )
    if message_id:
        try:
            bot.edit_message_text(
                text, chat_id, message_id, reply_markup=keyboard,
                parse_mode="HTML", **NO_PREVIEW,
            )
            return
        except Exception:
            pass
    bot.send_message(
        chat_id, text, reply_markup=keyboard, parse_mode="HTML", **NO_PREVIEW,
    )


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
    amount = _discounted_price_kopecks(plan_code, duration_months)
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


def _support_keyboard(waiting=False):
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


def _support_delivery_lock(user_id):
    """Serialize acceptance and message routing for one support conversation."""
    user_id = int(user_id)
    with support_delivery_locks_guard:
        return support_delivery_locks.setdefault(user_id, threading.RLock())


def _notify_support_user_accepted(user_id, admin):
    admin_name, admin_username = _support_user_label(admin)
    bot.send_message(
        int(user_id),
        "✅ <b>Администратор принял ваше обращение</b>\n\n"
        f"👤 {admin_name}\n🔎 Никнейм: {admin_username}\n\n"
        "Теперь вы переписываетесь напрямую с этим администратором.",
        parse_mode="HTML",
        reply_markup=_support_keyboard(),
        **NO_PREVIEW,
    )


def _notify_support_admin_one(admin_id, user, body_message=None):
    full_name, username = _support_user_label(user)
    header_text = (
        "🆘 <b>Обращение в тех поддержку</b>\n"
        f"👤 {full_name}\n"
        f"🔎 Никнейм: {username}\n"
        f"🆔 ID: <code>{user.id}</code>"
    )
    assigned_admin_id = get_support_session_assignee(user.id)
    support_keyboard = (
        _support_admin_keyboard(user.id)
        if assigned_admin_id == int(admin_id) else None
    )
    if body_message is None:
        header = bot.send_message(
            admin_id,
            header_text,
            parse_mode="HTML",
            reply_markup=support_keyboard,
            **NO_PREVIEW,
        )
        _map_support_message(admin_id, header.message_id, user.id)
        return

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
            "⚠️ <b>Сообщение не удалось скопировать</b>\n"
            f"<blockquote>{escape_html(fallback[:3000])}</blockquote>\n\n"
            f"Причина: <code>{escape_html(str(error)[:500])}</code>",
            parse_mode="HTML",
            **NO_PREVIEW,
        )
        _map_support_message(admin_id, copied.message_id, user.id)


def _notify_support_admin(user, body_message=None):
    delivered = 0
    assigned_admin_id = get_support_session_assignee(user.id)
    admin_ids = (
        [assigned_admin_id]
        if assigned_admin_id is not None
        else _support_admin_ids()
    )
    for admin_id in admin_ids:
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


def start_support(chat_id, user, source_message=None):
    if chat_id != user.id:
        bot.send_message(
            chat_id,
            "Чтобы написать в тех поддержку, откройте со мной личный чат и отправьте /help.",
            **NO_PREVIEW,
        )
        return
    track_user(type("SupportUserEvent", (), {"from_user": user})())
    with support_state_lock:
        already_open = user.id in support_sessions
        if not already_open:
            support_sessions.add(user.id)
            open_support_session(user.id)
    if already_open:
        assigned_admin_id = get_support_session_assignee(user.id)
        text = (
            "✅ Обращение уже принято администратором. Можете продолжать переписку."
            if assigned_admin_id is not None
            else "🆘 Обращение уже открыто. Напишите сообщение — его получат администраторы."
        )
        bot.send_message(
            chat_id,
            text + " Чтобы завершить чат, нажмите кнопку ниже.",
            reply_markup=_support_keyboard(),
            **NO_PREVIEW,
        )
        return

    try:
        bot.send_message(
            chat_id,
            "🆘 <b>Обращение открыто</b>\n\n"
            "Напишите, чем мы можем помочь. Ваше сообщение получат администраторы.\n\n"
            "Чтобы завершить чат, нажмите кнопку ниже.",
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


def _close_support(user_id, notify_user=True, closed_by_id=None):
    with support_state_lock:
        was_open = user_id in support_sessions
        support_sessions.discard(user_id)
        clear_support_pending_reply(user_id)
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
                        "Я — <b>MAX_Parser</b>. Меня добавили в этот чат.\n\n"
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
                        "Я — <b>MAX_Parser</b>. Меня добавили в этот чат.\n\n"
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

@bot.message_handler(
    func=lambda m: m.chat.type in ("group", "supergroup")
    and m.reply_to_message is not None
    and m.from_user is not None
    and not getattr(m.from_user, "is_bot", False)
    and get_admin_group_thread_message(m.chat.id, m.reply_to_message.message_id) is not None,
    content_types=["text", "photo", "video", "document", "audio", "voice", "animation", "sticker", "video_note", "location", "contact", "venue", "poll"],
)
def handle_admin_group_thread_reply(message):
    source_id = int(message.message_id)
    group_id = int(message.chat.id)
    parent_route = get_admin_group_thread_message(group_id, message.reply_to_message.message_id)
    if not parent_route:
        return

    sender = message.from_user
    sender_name = " ".join(part for part in (sender.first_name, sender.last_name) if part).strip() or "Участник"
    username = f"@{escape_html(sender.username)}" if sender.username else "не указан"
    body = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    if not body:
        body = {
            "photo": "Фото",
            "video": "Видео",
            "document": "Файл",
            "audio": "Аудио",
            "voice": "Голосовое сообщение",
            "animation": "Анимация",
            "sticker": "Стикер",
            "video_note": "Видеосообщение",
            "location": "Геопозиция",
            "contact": "Контакт",
            "venue": "Место",
            "poll": "Опрос",
        }.get(message.content_type, "Сообщение без текста")
    body = body[:2600] + ("…" if len(body) > 2600 else "")
    group_title = escape_html(message.chat.title or str(group_id))
    card_text = (
        "💬 <b>Ответ в подключённую группу</b>\n"
        f"📍 {group_title}\n"
        f"🆔 ID группы: <code>{group_id}</code>\n"
        f"👤 {escape_html(sender_name)}\n"
        f"🔎 Никнейм: {username}\n"
        f"🆔 ID: <code>{int(sender.id)}</code>\n"
        f"💬 <b>Сообщение:</b>\n\n<blockquote>{escape_html(body)}</blockquote>"
    )
    delivered = 0
    for admin_id in _support_admin_ids():
        try:
            card = bot.send_message(admin_id, card_text, parse_mode="HTML", **NO_PREVIEW)
            save_admin_group_reply_route(admin_id, card.message_id, group_id, source_id)
            if message.content_type != "text":
                copied = bot.copy_message(
                    admin_id, group_id, source_id,
                    reply_to_message_id=card.message_id,
                    caption="",
                )
                save_admin_group_reply_route(admin_id, copied.message_id, group_id, source_id)
            delivered += 1
        except Exception as error:
            debug_logger.warning(
                "ADMIN_GROUP_REPLY_NOTIFY_FAILED group_id=%s admin_id=%s error_type=%s",
                group_id, admin_id, type(error).__name__,
            )
    if not delivered:
        debug_logger.warning(
            "ADMIN_GROUP_REPLY_NO_RECIPIENTS group_id=%s message_id=%s",
            group_id, source_id,
        )


@bot.message_handler(commands=['start'])
def start_bot(message):
    if message.chat.type == "private":
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) > 1 and parts[1].startswith("ref_"):
            try:
                referrer_id = int(parts[1][4:])
                register_referral(referrer_id, message.from_user.id)
            except (ValueError, TypeError):
                pass
            except Exception as error:
                debug_logger.warning(
                    "REFERRAL_REGISTER_FAILED user_id=%s error_type=%s",
                    message.from_user.id, type(error).__name__,
                )
    track_user(message)
    if message.chat.type == "private":
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) > 1 and parts[1].startswith("maxsend_"):
            match = re.fullmatch(r"maxsend_(-?\d+)(?:_(\d+))?", parts[1])
            if not match:
                bot.send_message(message.chat.id, "Не удалось определить чат MAX.", reply_markup=start_keyboard(), **NO_PREVIEW)
                return
            source_chat_id = int(match.group(1))
            allowed_user_id = int(match.group(2)) if match.group(2) else None
            _start_public_max_send(
                SimpleNamespace(id=None, from_user=message.from_user, message=message),
                source_chat_id,
                allowed_private_user_id=allowed_user_id,
            )
            return
        if len(parts) > 1 and parts[1].startswith("reply_"):
            token = parts[1][6:]
            route = None
            try:
                route = get_admin_group_reply_link_token(token)
            except Exception as error:
                debug_logger.warning(
                    "ADMIN_GROUP_REPLY_LINK_LOOKUP_FAILED user_id=%s error_type=%s",
                    message.from_user.id, type(error).__name__,
                )
            if not route:
                bot.send_message(
                    message.chat.id,
                    "Эта ссылка для ответа уже не активна. Попросите администратора отправить новый вопрос.",
                    reply_markup=start_keyboard(),
                    **NO_PREVIEW,
                )
                return
            awaiting_admin_group_private_reply[int(message.from_user.id)] = {
                "group_chat_id": int(route["group_chat_id"]),
                "group_message_id": int(route["group_message_id"]),
            }
            try:
                telegram_chat = bot.get_chat(int(route["group_chat_id"]))
                group_name = escape_html(telegram_chat.title or str(route["group_chat_id"]))
            except Exception:
                group_name = escape_html(str(route["group_chat_id"]))
            keyboard = types.InlineKeyboardMarkup(row_width=1)
            keyboard.add(types.InlineKeyboardButton("❌ Не отвечать", callback_data="group_private_reply_cancel"))
            bot.send_message(
                message.chat.id,
                f"✉️ Напишите ответ администраторам по группе «{group_name}».\n\n"
                "Сообщение увидят администраторы <b>MAX_Parser</b>. Ответ можно отправить текстом или вложением.",
                reply_markup=keyboard,
                **NO_PREVIEW,
            )
            return
    text = (
        "👋 <b>Привет!</b>\n\n"
        "Меня зовут <b>MAX_Parser</b>.\n\n"
        "Я помогаю автоматически пересылать сообщения из выбранной группы "
        "<b>мессенджера MAX в Telegram</b> , а также <b>из Telegram в MAX</b>.\n\n"
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


@bot.message_handler(
    func=lambda m: m.chat.type == "private"
    and m.from_user is not None
    and int(m.from_user.id) in awaiting_admin_group_private_reply,
    content_types=["text", "photo", "video", "document", "audio", "voice", "animation", "sticker", "video_note", "location", "contact", "venue", "poll"],
)
def handle_admin_group_private_reply(message):
    user_id = int(message.from_user.id)
    state = awaiting_admin_group_private_reply.get(user_id)
    if not state:
        return
    if message.content_type == "text" and (message.text or "").strip().startswith("/"):
        bot.send_message(
            message.chat.id,
            "Напишите ответ обычным сообщением или нажмите «❌ Не отвечать».",
            **NO_PREVIEW,
        )
        return

    group_id = int(state["group_chat_id"])
    group_message_id = int(state["group_message_id"])
    sender = message.from_user
    sender_name = " ".join(part for part in (sender.first_name, sender.last_name) if part).strip() or "Участник"
    username = f"@{escape_html(sender.username)}" if sender.username else "не указан"
    body = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    if not body:
        body = {
            "photo": "Фото", "video": "Видео", "document": "Файл", "audio": "Аудио",
            "voice": "Голосовое сообщение", "animation": "Анимация", "sticker": "Стикер",
            "video_note": "Видеосообщение", "location": "Геопозиция", "contact": "Контакт",
            "venue": "Место", "poll": "Опрос",
        }.get(message.content_type, "Сообщение без текста")
    body = body[:2400] + ("…" if len(body) > 2400 else "")
    try:
        group_chat = bot.get_chat(group_id)
        group_title = escape_html(group_chat.title or str(group_id))
    except Exception:
        group_title = escape_html(str(group_id))
    card = (
        "💬 <b>Ответ на сообщение в группе</b>\n"
        f"📍 {group_title}\n"
        f"🆔 ID группы: <code>{group_id}</code>\n"
        f"👤 {escape_html(sender_name)}\n"
        f"🔎 Никнейм: {username}\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"💬 <b>Сообщение:</b>\n\n<blockquote>{escape_html(body)}</blockquote>"
    )
    delivered = 0
    for admin_id in _support_admin_ids():
        try:
            admin_message = bot.send_message(admin_id, card, parse_mode="HTML", **NO_PREVIEW)
            save_admin_group_reply_route(admin_id, admin_message.message_id, group_id, group_message_id)
            if message.content_type != "text":
                copied = bot.copy_message(
                    admin_id, message.chat.id, message.message_id,
                    reply_to_message_id=admin_message.message_id,
                    caption="",
                )
                save_admin_group_reply_route(admin_id, copied.message_id, group_id, group_message_id)
            delivered += 1
        except Exception as error:
            debug_logger.warning(
                "ADMIN_GROUP_PRIVATE_REPLY_NOTIFY_FAILED group_id=%s admin_id=%s error_type=%s",
                group_id, admin_id, type(error).__name__,
            )
    if delivered:
        awaiting_admin_group_private_reply.pop(user_id, None)
        bot.send_message(message.chat.id, "✅ Ответ отправлен администраторам.", **NO_PREVIEW)
    else:
        bot.send_message(
            message.chat.id,
            "Не удалось доставить ответ. Попробуйте ещё раз немного позже.",
            **NO_PREVIEW,
        )


@bot.message_handler(commands=["referral"])
def referral_program_command(message):
    track_user(message)
    if message.chat.type != "private":
        bot.reply_to(message, "Откройте личный чат со мной и отправьте /referral.")
        return
    show_referral_program(message.chat.id, message.from_user.id)


@bot.message_handler(commands=['help', 'help1'])
def help_command(message):
    start_support(message.chat.id, message.from_user, source_message=message)


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
        order = get_payment_order(query.invoice_payload)
        expected_amount = None
        if (
            order
            and order["plan_code"] in SUBSCRIPTION_PRICES
            and int(order["duration_months"]) in (1, 3, 12)
        ):
            expected_amount = _discounted_price_kopecks(
                order["plan_code"], int(order["duration_months"])
            )
        accepted = (
            expected_amount == int(query.total_amount)
            and claim_payment_order_for_checkout(
                payload=query.invoice_payload,
                user_id=query.from_user.id,
                currency=query.currency,
                total_amount=query.total_amount,
            )
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
    try:
        reserved_chat_id = int(state.get("chat_id") or 0)
        if (
            reserved_chat_id
            and state.get("destination_type") == "private"
            and not bool((get_chat_by_id(reserved_chat_id) or {}).get("is_active"))
        ):
            _delete_chat_everywhere(reserved_chat_id)
    except Exception as error:
        debug_logger.info(
            "PRIVATE_SETUP_CANCEL_CLEANUP_FAILED user_id=%s error_type=%s",
            user_id, type(error).__name__,
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
        text = "У вас пока нет подключённых чатов MAX. Начните настройку командой /connect."
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
        _sync_send_to_max_command_menu(chat_id, chat, force_disabled=True)
        destination_chat_id = int(chat.get("destination_chat_id") or chat_id)
        button_message_id = chat.get("max_send_button_message_id")
        if button_message_id:
            try:
                bot.delete_message(destination_chat_id, int(button_message_id))
            except Exception:
                pass
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
    if chat.get("destination_type") != "private":
        try:
            bot.leave_chat(destination_chat_id)
        except Exception as error:
            debug_logger.warning(
                "CHAT_DELETE_LEAVE_FAILED chat_id=%s destination=%s error=%s",
                chat_id, destination_chat_id, log_preview(error),
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
    keyboard.add(types.InlineKeyboardButton(
        "👤 ЛС - личный чат",
        callback_data=f"project_source_private_{chat_id}",
    ))
    keyboard.add(_setup_cancel_button(chat_id))
    return keyboard


def _project_source_type_text(chat_id):
    chat = get_chat_by_id(chat_id)
    title = escape_html((chat or {}).get("title") or f"Группа {chat_id}")
    if chat and chat.get("destination_type") == "private":
        destination = "✅ Пересылка в личные сообщения выбрана."
    else:
        destination = f"✅ Telegram-группа <b>{title}</b> выбрана для получения сообщений."
    return (
        f"{destination}\n\n"
        "<b>Шаг 2 из 2.</b> Что хотите подключить в MAX?"
    )


def _customer_source_type_keyboard(chat_id):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton(
        "📢 Канал", callback_data=f"customer_source_channel_{int(chat_id)}"
    ))
    keyboard.add(types.InlineKeyboardButton(
        "👥 Группа или чат", callback_data=f"customer_source_group_{int(chat_id)}"
    ))
    keyboard.add(types.InlineKeyboardButton(
        "👤 ЛС - личный чат", callback_data=f"customer_source_private_{int(chat_id)}"
    ))
    keyboard.add(_setup_cancel_button(chat_id))
    return keyboard


def _customer_source_type_text(chat_id):
    chat = get_chat_by_id(int(chat_id)) or {}
    title = escape_html(chat.get("title") or f"Группа {chat_id}")
    if chat.get("destination_type") == "private":
        destination = "✅ Пересылка в личные сообщения выбрана."
    else:
        destination = f"✅ Telegram-группа <b>{title}</b> выбрана для получения сообщений."
    step_total = 2 if chat.get("account_mode") == "project" else 3
    return (
        f"{destination}\n\n"
        f"<b>Шаг 2 из {step_total}.</b> Что хотите подключить в MAX?"
    )


def _customer_source_url_prompt(owner_id, chat_id):
    chat = get_chat_by_id(int(chat_id)) or {}
    title = escape_html(chat.get("title") or "Группа")
    source_type = chat.get("max_source_type") or "group"
    source_label = {
        "private": "личный чат",
        "channel": "канал",
        "group": "группу или чат",
    }.get(source_type, "чат MAX")
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton(
        "🔙 Назад к выбору чата", callback_data=f"customer_source_back_{int(chat_id)}"
    ))
    keyboard.add(_setup_cancel_button(chat_id))
    if chat.get("destination_type") == "private":
        text = (
            "✅ Новые сообщения будут приходить в ваши личные сообщения с ботом.\n\n"
            f"<b>Шаг 2 из 3.</b> Пришлите ссылку на {source_label} MAX, откуда получать сообщения.\n\n"
            "<b>Как скопировать ссылку</b>: откройте <a href=\"https://web.max.ru\">web.max.ru</a>, "
            "откройте выбранный чат и скопируйте ссылку из меню.\n\n"
            "<i>Чтобы остановить настройку, нажмите «❌ Отмена».</i>"
        )
    else:
        text = (
            f"✅ Группа <b>{title}</b> закреплена за вами.\n\n"
            f"<b>Шаг 2 из 3.</b>\n\nПришлите ссылку на {source_label} MAX, откуда получать сообщения.\n\n"
            "<b>Как скопировать ссылку</b>: откройте <a href=\"https://web.max.ru\">web.max.ru</a>, "
            "откройте выбранный чат и скопируйте ссылку из меню.\n\n"
            "<i>Чтобы остановить настройку, нажмите «❌ Отмена».</i>"
        )
    return text, keyboard


def _max_source_is_private(chat):
    return bool(
        chat
        and chat.get("max_source_type") == "private"
        and chat.get("max_source_type_verified")
        and chat.get("max_source_type_manual")
    )


def _start_admin_max_send(call, source_chat_id):
    admin_id = int(call.from_user.id)
    if admin_id != ADMIN_ID or call.message.chat.type != "private" or int(call.message.chat.id) != ADMIN_ID:
        bot.answer_callback_query(call.id, "Отправка в MAX доступна только владельцу в личном чате с ботом.", show_alert=True)
        return
    try:
        source_chat_id = int(source_chat_id)
        chat = get_chat_by_id(source_chat_id)
    except (TypeError, ValueError):
        chat = None
    if (
        not _admin_may_send_through_chat(chat)
        or not chat.get("is_active")
        or not chat.get("max_url")
        or not os.path.exists(_session_path_for_chat(chat))
    ):
        bot.answer_callback_query(call.id, "Для этого чата нет активного подключения MAX.", show_alert=True)
        return
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    nonce = uuid.uuid4().hex[:8]
    is_private_source = _max_source_is_private(chat)
    with admin_max_send_flow_lock:
        current = admin_max_send_flows.get(admin_id)
        if current and current.get("stage") == "sending":
            bot.answer_callback_query(call.id, "Дождитесь завершения текущей отправки.", show_alert=True)
            return
        admin_max_send_flows[admin_id] = {
            "source_chat_id": source_chat_id,
            "nonce": nonce,
            "stage": "body" if is_private_source else "name",
            "sender_name": "",
            "expires_at": time.time() + 15 * 60,
        }
    bot.answer_callback_query(call.id)
    _admin_max_send_prompt(
        admin_id, source_chat_id, nonce, "body" if is_private_source else "name"
    )


def _admin_may_send_through_chat(chat):
    if not chat:
        return False
    if chat.get("max_source_type") == "channel" and chat.get("max_source_type_verified"):
        return False
    if chat.get("account_mode") == "project":
        return True
    owner_id = chat.get("owner_id")
    return owner_id is None or int(owner_id) == ADMIN_ID


def _admin_max_send_keyboard(chat_id, nonce, include_skip=False):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    if include_skip:
        keyboard.add(types.InlineKeyboardButton(
            "⏭️ Пропустить этот шаг", callback_data=f"admin_max_skipname_{nonce}"
        ))
    keyboard.add(types.InlineKeyboardButton(
        "❌ Отмена", callback_data=f"admin_max_cancel_{nonce}"
    ))
    return keyboard


def _admin_max_send_source_list(admin_chat_id, message_id=None):
    if int(admin_chat_id) != ADMIN_ID:
        return
    eligible = []
    for chat in get_all_chats():
        try:
            if (
                _admin_may_send_through_chat(chat)
                and
                chat.get("is_active")
                and chat.get("max_url")
                and os.path.exists(_session_path_for_chat(chat))
            ):
                eligible.append(chat)
        except (TypeError, KeyError, OSError):
            continue

    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for chat in eligible:
        title = str(chat.get("title") or "Чат MAX")
        keyboard.add(types.InlineKeyboardButton(
            f"{title[:38]} · {chat['chat_id']}",
            callback_data=f"admin_max_source_{int(chat['chat_id'])}",
        ))
    keyboard.add(types.InlineKeyboardButton("❌ Отмена", callback_data="admin_max_cancel"))
    text = (
        "✉️ <b>Отправка сообщения в MAX</b>\n\nВыберите подключённый чат MAX."
        if eligible else
        "✉️ <b>Отправка сообщения в MAX</b>\n\nНет активных подключений с доступной сессией."
    )
    if message_id:
        safe_edit_message(text, admin_chat_id, message_id, reply_markup=keyboard, **NO_PREVIEW)
    else:
        bot.send_message(admin_chat_id, text, reply_markup=keyboard, **NO_PREVIEW)


def _admin_max_send_prompt(admin_id, source_chat_id, nonce, stage):
    flow = admin_max_send_flows.get(int(admin_id))
    if not flow or flow.get("nonce") != nonce or flow.get("source_chat_id") != int(source_chat_id):
        return
    chat = get_chat_by_id(int(source_chat_id))
    if not chat or not chat.get("is_active") or not chat.get("max_url"):
        admin_max_send_flows.pop(int(admin_id), None)
        bot.send_message(admin_id, "Подключение MAX больше не активно.", **NO_PREVIEW)
        return
    flow["stage"] = stage
    flow["expires_at"] = time.time() + 15 * 60
    prompt_text = (
        f"👤 Кто отправляет сообщение в MAX?"
        if stage == "name" else
        "✍️ Отправьте текст, фото, видео, кружок, голосовое, стикер, файл, геопозицию или опрос для MAX."
    )
    prompt = bot.send_message(
        admin_id,
        prompt_text if stage != "name" else prompt_text + "\n\nИли пропустите этот шаг.",
        reply_markup=_admin_max_send_keyboard(
            source_chat_id, nonce, include_skip=(stage == "name")
        ),
        **NO_PREVIEW,
    )
    flow["prompt_message_id"] = int(prompt.message_id)
    if stage == "name":
        bot.register_next_step_handler(prompt, _receive_admin_max_sender_name, source_chat_id, nonce)
    else:
        bot.register_next_step_handler(prompt, _receive_admin_max_body, source_chat_id, nonce)


def _valid_admin_max_input(message, source_chat_id, nonce, stage):
    if (
        not message
        or not getattr(message, "from_user", None)
        or int(message.from_user.id) != ADMIN_ID
        or int(message.chat.id) != ADMIN_ID
        or message.chat.type != "private"
    ):
        return None
    with admin_max_send_flow_lock:
        flow = admin_max_send_flows.get(ADMIN_ID)
        if (
            not flow
            or flow.get("nonce") != nonce
            or int(flow.get("source_chat_id") or 0) != int(source_chat_id)
            or flow.get("stage") != stage
            or float(flow.get("expires_at") or 0) <= time.time()
        ):
            return None
        return flow


def _receive_admin_max_sender_name(message, source_chat_id, nonce):
    flow = _valid_admin_max_input(message, source_chat_id, nonce, "name")
    if not flow:
        return
    value = str(message.text or "").strip()
    if not value or value.startswith("/") or len(value) > 80:
        bot.send_message(message.chat.id, "Укажите имя длиной до 80 символов.", **NO_PREVIEW)
        _admin_max_send_prompt(ADMIN_ID, source_chat_id, nonce, "name")
        return
    flow["sender_name"] = value
    _admin_max_send_prompt(ADMIN_ID, source_chat_id, nonce, "body")


def _receive_admin_max_body(message, source_chat_id, nonce):
    flow = _valid_admin_max_input(message, source_chat_id, nonce, "body")
    if not flow:
        return
    payload = _public_max_input(message)
    if not payload:
        bot.send_message(
            message.chat.id,
            "Этот тип сообщения пока нельзя отправить в MAX. Пришлите текст, фото, видео, кружок, голосовое, стикер или файл.",
            **NO_PREVIEW,
        )
        _admin_max_send_prompt(ADMIN_ID, source_chat_id, nonce, "body")
        return
    value = str(payload.get("body") or "").strip()
    media = payload.get("media")
    if (
        (not value and not media)
        or (message.content_type == "text" and value.startswith("/"))
        or len(value) > 3500
    ):
        bot.send_message(
            message.chat.id,
            "Отправьте текст до 3500 символов или прикрепите медиафайл.",
            **NO_PREVIEW,
        )
        _admin_max_send_prompt(ADMIN_ID, source_chat_id, nonce, "body")
        return
    flow["body"] = value
    flow["media"] = media
    flow["payload_label"] = payload.get("label") or "Текст"
    flow["stage"] = "confirm"
    flow["expires_at"] = time.time() + 15 * 60
    chat = get_chat_by_id(source_chat_id) or {}
    body_preview = escape_html(value) if value else f"<i>{escape_html(flow['payload_label'])}</i>"
    sender_preview = (
        f"<b>{escape_html(flow['sender_name'])}</b> пишет:\n\n"
        if str(flow.get("sender_name") or "").strip()
        else ""
    )
    preview = (
        f"Проверьте сообщение для MAX «{escape_html(chat.get('title') or 'Чат')}»:\n\n"
        f"{sender_preview}{body_preview}"
    )
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.row(
        types.InlineKeyboardButton(
            "✅ Отправить", callback_data=f"admin_max_confirm_{int(source_chat_id)}_{nonce}"
        ),
        types.InlineKeyboardButton("❌ Отмена", callback_data=f"admin_max_cancel_{nonce}"),
    )
    sent = bot.send_message(
        message.chat.id, preview, parse_mode="HTML", reply_markup=keyboard, **NO_PREVIEW
    )
    flow["preview_message_id"] = int(sent.message_id)


def _send_max_payload_with_sender(
    group_url,
    body,
    sender_name,
    media,
    chat_id,
    session_path,
    on_before_send=None,
    on_send_confirmed=None,
    source_type_callback=None,
):
    """Send sender attribution and message text together with any attachment."""
    body = str(body or "").strip()
    sender_name = str(sender_name or "").strip()
    header = f"{sender_name} пишет:" if sender_name else ""
    common = {
        "group_url": group_url,
        "chat_id": int(chat_id),
        "session_path": session_path,
        "on_before_send": on_before_send,
        "on_send_confirmed": on_send_confirmed,
        "source_type_callback": source_type_callback,
    }
    text = f"{header}\n\n{body}".strip() if header else body
    send_message_to_max(
        text=text,
        media=media,
        bold_sender_name=sender_name if header else "",
        bold_first_line=False,
        **common,
    )


def _max_send_failure_text(error, heading=None):
    detail = escape_html(log_preview(f"{type(error).__name__}: {error}", 700))
    heading = heading or "⚠️ Не удалось отправить сообщение в MAX."
    return f"{heading}\n\nДиагностика: <code>{detail}</code>"


def _print_max_send_failure(context, source_chat_id, error):
    formatted = traceback.format_exception(type(error), error, error.__traceback__)
    safe_traceback = "\n".join(log_preview(line, 900) for line in formatted)
    print(
        f"{context} source_chat_id={source_chat_id} "
        f"error_type={type(error).__name__} error={log_preview(error, 900)}\n"
        f"{safe_traceback}",
        flush=True,
    )


def _send_admin_max_message(admin_id, source_chat_id, nonce, preview_message_id):
    result_text = None
    result_keyboard = None
    try:
        with admin_max_send_flow_lock:
            flow = admin_max_send_flows.get(int(admin_id))
            if not flow or flow.get("nonce") != nonce or flow.get("stage") != "sending":
                return
            name = str(flow.get("sender_name") or "").strip()
            body = str(flow.get("body") or "").strip()
            media = flow.get("media") or None
        chat = get_chat_by_id(int(source_chat_id))
        if (
            not _admin_may_send_through_chat(chat)
            or not chat.get("is_active")
            or not chat.get("max_url")
        ):
            raise RuntimeError("CONNECTION_NO_LONGER_ACTIVE")
        if media:
            media_items = media if isinstance(media, list) else [media]
            downloaded_items = []
            total_size = 0
            for item in media_items[:10]:
                item = dict(item)
                telegram_file = bot.get_file(item["file_id"])
                if _is_public_max_file_oversize(getattr(telegram_file, "file_size", None)):
                    raise RuntimeError("TELEGRAM_FILE_TOO_LARGE")
                file_bytes = bot.download_file(telegram_file.file_path)
                total_size += len(file_bytes)
                if total_size > 20 * 1024 * 1024:
                    raise RuntimeError("TELEGRAM_FILE_TOO_LARGE")
                item["bytes"] = file_bytes
                item = _prepare_public_max_media(item)
                downloaded_items.append(item)
            media = downloaded_items
            if len(media) == 1:
                media = media[0]
        source_lock = max_send_source_locks.setdefault(int(source_chat_id), threading.Lock())
        with source_lock:
            _send_max_payload_with_sender(
                group_url=chat["max_url"],
                body=body,
                sender_name=name,
                media=media,
                chat_id=int(source_chat_id),
                session_path=_session_path_for_chat(chat),
                source_type_callback=lambda detected: _update_detected_source_type(
                    source_chat_id, detected
                ),
            )
        result_text = "✅ Сообщение отправлено в MAX."
        result_keyboard = types.InlineKeyboardMarkup(row_width=1)
        result_keyboard.add(types.InlineKeyboardButton(
            "✍️ Написать в MAX", callback_data=f"admin_max_source_{int(source_chat_id)}"
        ))
        debug_logger.info("ADMIN_MAX_MESSAGE_SENT admin_id=%s source_chat_id=%s", admin_id, source_chat_id)
    except Exception as error:
        _print_max_send_failure("ADMIN_MAX_MESSAGE_SEND_FAILED", source_chat_id, error)
        debug_logger.warning(
            "ADMIN_MAX_MESSAGE_SEND_FAILED source_chat_id=%s error_type=%s error=%s",
            source_chat_id, type(error).__name__, log_preview(error, 900),
        )
        result_text = _max_send_failure_text(error)
        if "SESSION_EXPIRED" in str(error) or "NO_SESSION" in str(error):
            result_text = _max_send_failure_text(error, "⚠️ Сессия MAX завершилась. Подключите аккаунт заново.")
        elif "MAX_PHOTO_UPLOAD_FAILED" in str(error):
            result_text = _max_send_failure_text(error, "⚠️ MAX не принял фотографию. Попробуйте отправить её как файл или уменьшите размер.")
        elif "TELEGRAM_FILE_TOO_LARGE" in str(error):
            result_text = _max_send_failure_text(error, "⚠️ Файл больше 20 МБ. Отправьте файл поменьше.")
        elif "MAX_UPLOAD_CONTROL_NOT_FOUND" in str(error):
            result_text = _max_send_failure_text(error, "⚠️ MAX не открыл загрузку этого типа файла. Попробуйте другой формат.")
        elif "MAX_UPLOAD_NOT_READY" in str(error):
            result_text = _max_send_failure_text(error, "⚠️ MAX не успел подготовить вложение. Попробуйте отправить файл ещё раз.")
        elif "MAX_SEND_NOT_CONFIRMED" in str(error):
            result_text = _max_send_failure_text(error, "⚠️ MAX не подтвердил отправку. Проверьте чат MAX перед повторной попыткой.")
    finally:
        with admin_max_send_flow_lock:
            current = admin_max_send_flows.get(int(admin_id))
            if current and current.get("nonce") == nonce:
                admin_max_send_flows.pop(int(admin_id), None)
        if result_text is not None:
            try:
                bot.edit_message_text(
                    result_text, int(admin_id), int(preview_message_id),
                    reply_markup=result_keyboard, **NO_PREVIEW
                )
            except Exception:
                try:
                    bot.send_message(
                        int(admin_id), result_text, reply_markup=result_keyboard, **NO_PREVIEW
                    )
                except Exception:
                    pass


def _finish_admin_max_send(admin_id, source_chat_id, nonce, preview_message_id):
    try:
        _send_admin_max_message(admin_id, source_chat_id, nonce, preview_message_id)
    finally:
        max_send_browser_semaphore.release()


def _max_send_anchor_is_parser_post(chat_id, message_id):
    try:
        chat_id = int(chat_id)
        message_id = int(message_id)
    except (TypeError, ValueError):
        return False
    for record in _sent_parser_records_snapshot():
        if str(record.get("origin") or "") == "bot":
            continue
        try:
            if int(record.get("chat_id") or 0) == chat_id and message_id in {
                int(value) for value in record.get("message_ids") or []
            }:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _clear_max_send_anchor(chat_id, destination_chat_id, message_id):
    is_parser_post = _max_send_anchor_is_parser_post(chat_id, message_id)
    try:
        if is_parser_post:
            bot.edit_message_reply_markup(
                int(destination_chat_id), int(message_id), reply_markup=None
            )
        else:
            bot.delete_message(int(destination_chat_id), int(message_id))
            with _sent_parser_messages_lock:
                before = len(_sent_parser_messages)
                _sent_parser_messages[:] = [
                    record for record in _sent_parser_messages
                    if not (
                        record.get("origin") == "bot"
                        and int(record.get("destination_chat_id") or record.get("chat_id") or 0)
                        == int(destination_chat_id)
                        and int(message_id) in {
                            int(value) for value in record.get("message_ids") or []
                        }
                    )
                ]
                if len(_sent_parser_messages) != before:
                    _save_sent_parser_messages_locked()
    except Exception:
        try:
            bot.edit_message_reply_markup(
                int(destination_chat_id), int(message_id), reply_markup=None
            )
        except Exception:
            pass


def _latest_parser_send_anchor(chat_id, destination_chat_id):
    candidates = []
    for record in _sent_parser_records_snapshot():
        if str(record.get("origin") or "") == "bot":
            continue
        try:
            if int(record.get("chat_id") or 0) != int(chat_id):
                continue
            if int(record.get("destination_chat_id") or record.get("chat_id")) != int(destination_chat_id):
                continue
            message_ids = [int(value) for value in record.get("message_ids") or []]
            if not message_ids:
                continue
            sent_at = datetime.fromisoformat(
                str(record.get("sent_at") or "").replace("Z", "+00:00")
            )
            candidates.append((sent_at, message_ids[-1]))
        except (TypeError, ValueError):
            continue
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _remove_max_send_button(chat_id, preserve_anchor=False):
    chat = get_chat_by_id(int(chat_id))
    if not chat:
        return False
    _sync_send_to_max_command_menu(int(chat_id), chat)
    if not chat.get("max_send_button_message_id"):
        return False
    destination_chat_id = int(chat.get("destination_chat_id") or chat_id)
    _clear_max_send_anchor(
        int(chat_id), destination_chat_id, int(chat["max_send_button_message_id"])
    )
    if not preserve_anchor:
        set_max_send_button_message_id(int(chat_id), None)
    return True


def _public_max_send_button_keyboard(source_chat_id, private_user_id=None):
    payload = f"maxsend_{int(source_chat_id)}"
    if private_user_id is not None:
        payload += f"_{int(private_user_id)}"
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(types.InlineKeyboardButton(
        "✍️ Написать в MAX",
        url=f"https://t.me/{BOT_PUBLIC_USERNAME}?start={payload}",
    ))
    return keyboard


def _answer_public_max_send_action(call, *args, **kwargs):
    callback_id = getattr(call, "id", None)
    if callback_id:
        bot.answer_callback_query(callback_id, *args, **kwargs)


def _max_send_button_is_eligible(chat):
    source_is_confirmed_channel = bool(
        chat
        and chat.get("max_source_type") == "channel"
        and (
            chat.get("max_source_type_verified")
            or chat.get("max_source_type_manual")
        )
    )
    if (
        not chat
        or not chat.get("is_active")
        or not chat.get("max_url")
        or source_is_confirmed_channel
        or not os.path.exists(_session_path_for_chat(chat))
    ):
        return False
    owner_id = int(chat.get("owner_id") or 0)
    if owner_id and _user_needs_subscription(owner_id, chat.get("account_mode") or "self"):
        return False
    destination_type = str(chat.get("destination_type") or "group")
    try:
        destination_chat_id = int(chat.get("destination_chat_id") or chat["chat_id"])
    except (KeyError, TypeError, ValueError):
        return False
    # The persisted destination type is established when the user connects the
    # Telegram target. Avoid hiding the button when a transient getChat request
    # fails; actual access is rechecked when the button is used.
    if destination_type == "private":
        return bool(owner_id and destination_chat_id == owner_id)
    return destination_type in {"group", "supergroup"}


def _sync_send_to_max_command_menu(chat_id, chat=None, force_disabled=False):
    """Expose /send_to_max only in connected Telegram groups with sendable MAX sources."""
    chat_id = int(chat_id)
    chat = chat or get_chat_by_id(chat_id)
    if not chat:
        return False
    destination_type = str(chat.get("destination_type") or "group")
    if destination_type not in {"group", "supergroup"}:
        return False
    try:
        destination_chat_id = int(chat.get("destination_chat_id") or chat["chat_id"])
    except (KeyError, TypeError, ValueError):
        return False
    should_show = bool(not force_disabled and _max_send_button_is_eligible(chat))
    with _send_to_max_command_menu_lock:
        if _send_to_max_command_menu_state.get(destination_chat_id) is should_show:
            return True
        scope = types.BotCommandScopeChat(chat_id=destination_chat_id)
        try:
            if should_show:
                bot.set_my_commands(
                    [
                        _BASE_BOT_COMMANDS[0],
                        _SEND_TO_MAX_BOT_COMMAND,
                        *_BASE_BOT_COMMANDS[1:],
                    ],
                    scope=scope,
                )
            else:
                # Chat scope overrides AllGroupChats. Delete it so the group
                # inherits the base menu, which deliberately excludes this command.
                bot.delete_my_commands(scope=scope)
        except Exception as error:
            debug_logger.warning(
                "SEND_TO_MAX_COMMAND_MENU_SYNC_FAILED chat_id=%s destination_chat_id=%s enabled=%s error_type=%s",
                chat_id, destination_chat_id, should_show, type(error).__name__,
            )
            return False
        _send_to_max_command_menu_state[destination_chat_id] = should_show
    return True


def _sync_max_send_button(chat_id, anchor_message_id=None, destination_chat_id=None):
    """Keep one MAX-send link beside the newest parser post for each connection."""
    chat = get_chat_by_id(int(chat_id))
    _sync_send_to_max_command_menu(int(chat_id), chat)
    if not _max_send_button_is_eligible(chat):
        _remove_max_send_button(chat_id)
        return False
    destination_chat_id = int(
        destination_chat_id or chat.get("destination_chat_id") or chat["chat_id"]
    )
    existing_id = chat.get("max_send_button_message_id")
    keyboard = _public_max_send_button_keyboard(chat_id)

    target_id = anchor_message_id or _latest_parser_send_anchor(
        int(chat_id), destination_chat_id
    ) or chat.get("max_send_button_message_id")
    if not target_id:
        debug_logger.info(
            "MAX_SEND_BUTTON_NO_ANCHOR chat_id=%s destination_chat_id=%s",
            chat_id, destination_chat_id,
        )
        return False
    target_id = int(target_id)
    if existing_id and int(existing_id) != target_id:
        existing_id = int(existing_id)
        try:
            bot.edit_message_reply_markup(destination_chat_id, existing_id, reply_markup=None)
        except Exception as error:
            error_text = str(error).lower()
            if "message is not modified" not in error_text and not any(marker in error_text for marker in (
                "message to edit not found", "message_id_invalid", "message can't be edited",
            )):
                debug_logger.warning(
                    "MAX_SEND_BUTTON_MOVE_FAILED chat_id=%s message_id=%s error=%s",
                    chat_id, existing_id, log_preview(error, 180),
                )
            else:
                set_max_send_button_message_id(int(chat_id), None)

    try:
        bot.edit_message_reply_markup(
            destination_chat_id, target_id, reply_markup=keyboard
        )
        set_max_send_button_message_id(int(chat_id), target_id)
        debug_logger.info(
            "MAX_SEND_BUTTON_ATTACHED chat_id=%s destination_chat_id=%s message_id=%s",
            chat_id, destination_chat_id, target_id,
        )
        return True
    except Exception as error:
        if "message is not modified" in str(error).lower():
            set_max_send_button_message_id(int(chat_id), target_id)
            debug_logger.info(
                "MAX_SEND_BUTTON_ATTACHED chat_id=%s destination_chat_id=%s message_id=%s already_present=true",
                chat_id, destination_chat_id, target_id,
            )
            return True
        debug_logger.warning(
            "MAX_SEND_BUTTON_ATTACH_FAILED chat_id=%s message_id=%s error=%s",
            chat_id, target_id, log_preview(error, 180),
        )
        return False


def _cleanup_legacy_max_send_buttons():
    for chat in get_all_chats():
        try:
            _sync_max_send_button(int(chat["chat_id"]))
        except Exception as error:
            debug_logger.warning(
                "MAX_SEND_BUTTON_SYNC_FAILED chat_id=%s error_type=%s",
                chat.get("chat_id"), type(error).__name__,
            )


def _public_max_send_keyboard(nonce, include_skip=False):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    if include_skip:
        keyboard.add(types.InlineKeyboardButton(
            "⏭️ Пропустить этот шаг", callback_data=f"maxsend_skipname_{nonce}"
        ))
    keyboard.add(types.InlineKeyboardButton(
        "❌ Отмена", callback_data=f"maxsend_cancel_{nonce}"
    ))
    return keyboard


def _start_public_max_send(call, source_chat_id, allowed_private_user_id=None):
    user = call.from_user
    destination_chat_id = int(call.message.chat.id)
    try:
        source_chat_id = int(source_chat_id)
        chat = get_chat_by_id(source_chat_id)
    except (TypeError, ValueError):
        chat = None
    expected_destination = int((chat or {}).get("destination_chat_id") or (chat or {}).get("chat_id") or 0)
    is_private_owner_chat = bool(
        chat
        and chat.get("destination_type") == "private"
        and call.message.chat.type == "private"
        and destination_chat_id == int(user.id)
        and destination_chat_id == expected_destination
    )
    is_authorized_private_resume = bool(
        call.message.chat.type == "private"
        and destination_chat_id == int(user.id)
        and (
            allowed_private_user_id is None
            or int(allowed_private_user_id) == int(user.id)
        )
        and (
            (chat or {}).get("destination_type") != "private"
            or expected_destination == int(user.id)
        )
    )
    is_group_chat = (
        call.message.chat.type in {"group", "supergroup"}
        and destination_chat_id == expected_destination
    )
    if (
        not chat
        or not (is_group_chat or is_private_owner_chat or is_authorized_private_resume)
        or not _max_send_button_is_eligible(chat)
    ):
        _remove_max_send_button(source_chat_id)
        error_text = "Отправка доступна только в подключённом чате MAX."
        if getattr(call, "id", None):
            _answer_public_max_send_action(call, error_text, show_alert=True)
        else:
            bot.send_message(destination_chat_id, error_text, reply_markup=start_keyboard(), **NO_PREVIEW)
        return
    if getattr(user, "is_bot", False):
        error_text = "Эту кнопку может использовать пользователь."
        if getattr(call, "id", None):
            _answer_public_max_send_action(call, error_text, show_alert=True)
        else:
            bot.send_message(destination_chat_id, error_text, **NO_PREVIEW)
        return
    # Collect the sender name and message in the clicker's private chat. Telegram
    # Privacy Mode hides ordinary group messages from bots, so waiting for input
    # in the destination group silently drops valid submissions.
    input_chat_id = int(user.id)
    flow_key = (input_chat_id, int(user.id))
    nonce = uuid.uuid4().hex[:10]
    is_private_source = _max_source_is_private(chat)
    initial_stage = "body" if is_private_source else "name"
    with public_max_send_flow_lock:
        current = public_max_send_flows.get(flow_key)
        if current and float(current.get("expires_at") or 0) > time.time():
            error_text = "Сначала завершите или отмените текущую отправку."
            if getattr(call, "id", None):
                _answer_public_max_send_action(call, error_text, show_alert=True)
            else:
                bot.send_message(destination_chat_id, error_text, **NO_PREVIEW)
            return
        public_max_send_flows[flow_key] = {
            "source_chat_id": source_chat_id,
            "destination_chat_id": expected_destination,
            "input_chat_id": input_chat_id,
            "user_id": int(user.id),
            "destination_type": str(chat.get("destination_type") or "group"),
            "nonce": nonce,
            "stage": initial_stage,
            "sender_name": "",
            "expires_at": time.time() + 15 * 60,
        }
    try:
        prompt_text = (
            "💬 Отправьте текст или вложение для этого личного чата MAX."
            if is_private_source else
            f"👤 Кто отправляет сообщение в MAX?\n\n"
            "Напишите имя в личном чате с ботом или нажмите «Пропустить этот шаг»."
        )
        prompt_options = {
            "reply_markup": _public_max_send_keyboard(
                nonce, include_skip=(initial_stage == "name")
            ),
            **NO_PREVIEW,
        }
        prompt = bot.send_message(input_chat_id, prompt_text, **prompt_options)
    except Exception as error:
        with public_max_send_flow_lock:
            current = public_max_send_flows.get(flow_key)
            if current and current.get("nonce") == nonce:
                public_max_send_flows.pop(flow_key, None)
        open_bot_text = "Сначала откройте @max_parserBot и нажмите /start, затем нажмите кнопку ещё раз."
        if getattr(call, "id", None):
            _answer_public_max_send_action(call, open_bot_text, show_alert=True)
        else:
            bot.send_message(destination_chat_id, "Не удалось открыть отправку в MAX. " + open_bot_text, **NO_PREVIEW)
        if is_group_chat:
            try:
                bot.send_message(
                    destination_chat_id,
                    "Чтобы написать в MAX, сначала откройте @max_parserBot и нажмите /start. Затем нажмите эту кнопку ещё раз.",
                    **NO_PREVIEW,
                )
            except Exception:
                pass
        debug_logger.info(
            "PUBLIC_MAX_PRIVATE_PROMPT_FAILED user=%s error_type=%s",
            user.id, type(error).__name__,
        )
        return
    if getattr(call, "id", None):
        _answer_public_max_send_action(call, "Откройте личный чат с ботом для продолжения.")
    with public_max_send_flow_lock:
        current = public_max_send_flows.get(flow_key)
        if current and current.get("nonce") == nonce:
            current["prompt_message_id"] = int(prompt.message_id)


@bot.message_handler(commands=["send_to_max"], chat_types=["group", "supergroup"])
def send_to_max_group_command(message):
    if not message or not getattr(message, "from_user", None):
        return
    if getattr(message.from_user, "is_bot", False):
        return
    destination_chat_id = int(message.chat.id)
    chat = None
    for candidate in get_active_chats():
        try:
            candidate_destination = int(
                candidate.get("destination_chat_id") or candidate["chat_id"]
            )
        except (KeyError, TypeError, ValueError):
            continue
        if (
            candidate_destination == destination_chat_id
            and str(candidate.get("destination_type") or "group") in {"group", "supergroup"}
            and _max_send_button_is_eligible(candidate)
        ):
            chat = candidate
            break
    if not chat:
        bot.send_message(
            destination_chat_id,
            "Отправка в MAX доступна в подключённой группе, если источник MAX — группа или личный чат. Для канала эта функция недоступна.",
            **NO_PREVIEW,
        )
        return
    _start_public_max_send(
        SimpleNamespace(id=None, from_user=message.from_user, message=message),
        int(chat["chat_id"]),
    )


def _ask_public_max_body(flow_key, nonce):
    with public_max_send_flow_lock:
        flow = public_max_send_flows.get(flow_key)
        if not flow or flow.get("nonce") != nonce or flow.get("stage") != "body":
            return False
    text = "💬 Отправьте текст или вложение для MAX."
    prompt = bot.send_message(
        int(flow_key[0]),
        text,
        reply_markup=_public_max_send_keyboard(nonce),
        **NO_PREVIEW,
    )
    with public_max_send_flow_lock:
        current = public_max_send_flows.get(flow_key)
        if current and current.get("nonce") == nonce and current.get("stage") == "body":
            current["prompt_message_id"] = int(prompt.message_id)
    return True


def _is_public_max_send_reply(message):
    if not message or not getattr(message, "from_user", None) or message.chat.type != "private":
        return False
    if int(message.chat.id) != int(message.from_user.id):
        return False
    key = (int(message.chat.id), int(message.from_user.id))
    with public_max_send_flow_lock:
        flow = public_max_send_flows.get(key)
        if (
            not flow
            or flow.get("stage") not in {"name", "body"}
            or float(flow.get("expires_at") or 0) <= time.time()
        ):
            return False
        return int(flow.get("input_chat_id") or flow.get("destination_chat_id") or 0) == int(message.chat.id)


def _public_max_input(message):
    """Extract one Telegram text/media item while retaining its native file type."""
    content_type = str(getattr(message, "content_type", "") or "")
    caption = str(getattr(message, "caption", None) or "").strip()
    if content_type == "text":
        return {"body": str(message.text or "").strip(), "media": None, "label": "Текст"}

    media = None
    label = "Вложение"
    if content_type == "photo":
        photos = message.photo or []
        photo = max(photos, key=lambda item: int(getattr(item, "file_size", 0) or 0)) if any(
            getattr(item, "file_size", None) for item in photos
        ) else photos[-1]
        media = {"file_id": photo.file_id, "filename": "photo.jpg", "mime_type": "image/jpeg", "kind": "photo"}
        label = "Фото"
    elif content_type in {"video", "video_note", "animation"}:
        item = getattr(message, content_type, None)
        if content_type == "animation":
            filename = str(getattr(item, "file_name", None) or "animation.mp4")
            mime_type = str(getattr(item, "mime_type", None) or "video/mp4")
            label = "Анимация"
            kind = "video"
        elif content_type == "video_note":
            # MAX Web has no Telegram-style round video-note composer. Upload
            # the original square clip through MAX's regular video picker.
            filename, mime_type, label = "video.mp4", "video/mp4", "Видеосообщение"
            kind = "video"
        else:
            filename = str(getattr(item, "file_name", None) or "video.mp4")
            mime_type = str(getattr(item, "mime_type", None) or "video/mp4")
            label = "Видео"
            kind = "video"
        media = {"file_id": item.file_id, "filename": filename, "mime_type": mime_type, "kind": kind}
    elif content_type in {"document", "audio", "voice", "sticker"}:
        item = getattr(message, content_type, None)
        if content_type == "sticker":
            if getattr(item, "is_video", False):
                filename, mime_type = "sticker.webm", "video/webm"
                kind = "video"
                file_id = item.file_id
            elif getattr(item, "is_animated", False):
                thumbnail = getattr(item, "thumbnail", None) or getattr(item, "thumb", None)
                emoji = str(getattr(item, "emoji", None) or "").strip()
                if not thumbnail or not getattr(thumbnail, "file_id", None):
                    return {
                        "body": caption or emoji or "Стикер",
                        "media": None,
                        "label": f"Стикер {emoji}".strip(),
                    }
                filename, mime_type = "sticker.webp", "image/webp"
                kind = "sticker_image"
                file_id = thumbnail.file_id
            else:
                filename, mime_type = "sticker.webp", "image/webp"
                kind = "sticker_image"
                file_id = item.file_id
            emoji = str(getattr(item, "emoji", None) or "").strip()
            label = f"Стикер {emoji}".strip()
        elif content_type == "voice":
            filename, mime_type, label = "voice.ogg", "audio/ogg", "Голосовое сообщение"
            kind = "voice"
            file_id = item.file_id
        elif content_type == "audio":
            filename = str(getattr(item, "file_name", None) or "audio.mp3")
            mime_type = str(getattr(item, "mime_type", None) or "audio/mpeg")
            label = "Аудио"
            kind = "audio"
            file_id = item.file_id
        else:
            filename = str(getattr(item, "file_name", None) or "document.bin")
            mime_type = str(getattr(item, "mime_type", None) or "application/octet-stream")
            label = "Файл"
            kind = "document"
            file_id = item.file_id
        if content_type == "sticker":
            caption = caption or emoji
        media = {"file_id": file_id, "filename": filename, "mime_type": mime_type, "kind": kind}
    elif content_type == "contact":
        contact = message.contact
        full_name = " ".join(filter(None, [contact.first_name, contact.last_name]))
        body = "Контакт: " + (full_name or "без имени")
        if contact.phone_number:
            body += f"\nТелефон: {contact.phone_number}"
        return {"body": body, "media": None, "label": "Контакт"}
    elif content_type == "location":
        location = message.location
        latitude = float(location.latitude)
        longitude = float(location.longitude)
        body = (
            "📍 Геопозиция\n"
            f"Координаты: {latitude:.6f}, {longitude:.6f}\n"
            f"Карта: https://maps.google.com/?q={latitude:.6f},{longitude:.6f}"
        )
        live_period = int(getattr(location, "live_period", 0) or 0)
        if live_period:
            body += (
                "\nЭто геопозиция в реальном времени."
                if live_period == 2147483647
                else f"\nПериод трансляции: до {live_period // 60 or 1} мин."
            )
        return {
            "body": body,
            "media": None,
            "label": "Геопозиция",
        }
    elif content_type == "venue":
        venue = message.venue
        latitude = float(venue.location.latitude)
        longitude = float(venue.location.longitude)
        body = (
            f"📍 {venue.title}\n{venue.address}\n"
            f"Координаты: {latitude:.6f}, {longitude:.6f}\n"
            f"Карта: https://maps.google.com/?q={latitude:.6f},{longitude:.6f}"
        )
        return {"body": body, "media": None, "label": "Место"}
    elif content_type == "poll":
        poll = message.poll
        options = "\n".join(f"• {option.text}" for option in poll.options or [])
        body = f"📊 Опрос: {poll.question}"
        if options:
            body += f"\n{options}"
        poll_type = str(getattr(poll, "type", "regular") or "regular")
        if poll_type == "quiz":
            body += "\nФормат: викторина"
            correct_id = getattr(poll, "correct_option_id", None)
            if correct_id is not None and 0 <= int(correct_id) < len(poll.options or []):
                body += f"\nПравильный ответ: {(poll.options or [])[int(correct_id)].text}"
            explanation = str(getattr(poll, "explanation", None) or "").strip()
            if explanation:
                body += f"\nПояснение: {explanation}"
        if getattr(poll, "allows_multiple_answers", False):
            body += "\nМожно выбрать несколько вариантов."
        if getattr(poll, "is_closed", False):
            body += "\nОпрос завершён."
        close_date = getattr(poll, "close_date", None)
        if close_date:
            try:
                body += f"\nЗавершится: {datetime.fromtimestamp(int(close_date), timezone.utc).astimezone().strftime('%d.%m.%Y %H:%M')}"
            except (TypeError, ValueError, OSError):
                pass
        return {"body": body, "media": None, "label": "Опрос"}
    elif content_type == "dice":
        dice = message.dice
        return {
            "body": f"{dice.emoji} Результат: {dice.value}",
            "media": None,
            "label": "Эмодзи-кубик",
        }
    elif content_type == "game":
        game = message.game
        details = str(getattr(game, "text", None) or getattr(game, "description", None) or "").strip()
        body = str(getattr(game, "title", None) or "Игра")
        if details:
            body += f"\n{details}"
        return {"body": body, "media": None, "label": "Игра"}
    elif content_type == "web_app_data":
        return {
            "body": str(getattr(message.web_app_data, "data", None) or "Данные приложения"),
            "media": None,
            "label": "Данные приложения",
        }
    else:
        return None
    return {"body": caption, "media": media, "label": label}


def _public_max_preview_text(flow):
    name = escape_html(flow.get("sender_name") or "")
    body = escape_html(str(flow.get("body") or ""))
    label = escape_html(str(flow.get("payload_label") or "Текст"))
    description = body[:1800] if body else f"<i>{label}</i>"
    if len(body) > 1800:
        description += "…"
    sender_line = f"<b>{name}</b> пишет:\n\n" if name else ""
    return f"Проверьте сообщение для MAX:\n\n{sender_line}{description}"


def _public_max_send_confirm_keyboard(source_chat_id, nonce):
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.row(
        types.InlineKeyboardButton(
            "✅ Отправить", callback_data=f"maxsend_confirm_{int(source_chat_id)}_{nonce}"
        ),
        types.InlineKeyboardButton("❌ Отмена", callback_data=f"maxsend_cancel_{nonce}"),
    )
    return keyboard


def _is_public_max_file_oversize(file_size):
    try:
        return int(file_size or 0) > 20 * 1024 * 1024
    except (TypeError, ValueError):
        return False


def _prepare_public_max_media(item):
    """Use formats MAX Web can preview for Telegram stickers and voice notes."""
    data = item.get("bytes")
    if not isinstance(data, (bytes, bytearray)) or not data:
        return item
    kind = str(item.get("kind") or "")
    try:
        if kind == "sticker_image":
            from PIL import Image
            with Image.open(io.BytesIO(bytes(data))) as source:
                source.seek(0)
                converted = io.BytesIO()
                source.convert("RGBA").save(converted, format="PNG", optimize=True)
            item["bytes"] = converted.getvalue()
            item["filename"] = "sticker.png"
            item["mime_type"] = "image/png"
            item["kind"] = "photo"
            return item
    except Exception as error:
        debug_logger.warning(
            "PUBLIC_MAX_STICKER_IMAGE_CONVERSION_FAILED error_type=%s",
            type(error).__name__,
        )

    needs_video_conversion = kind == "video" and str(item.get("filename") or "").lower().endswith(".webm")
    needs_voice_conversion = kind == "voice" and (
        str(item.get("mime_type") or "").lower().startswith("audio/ogg")
        or bytes(data).startswith(b"OggS")
    )
    if not (needs_video_conversion or needs_voice_conversion):
        return item
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            ffmpeg = None
    if not ffmpeg:
        return item
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0"]
    if needs_video_conversion:
        command += [
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-pix_fmt", "yuv420p", "-movflags", "+frag_keyframe+empty_moov", "-f", "mp4", "pipe:1",
        ]
    else:
        command += [
            "-vn", "-c:a", "aac", "-b:a", "96k", "-movflags",
            "+frag_keyframe+empty_moov", "-f", "mp4", "pipe:1",
        ]
    try:
        converted = subprocess.run(
            command,
            input=bytes(data),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=25,
            check=False,
        )
        output = converted.stdout
        valid_container = (
            b"ftyp" in output[:64]
            if needs_video_conversion
            else output[:8].startswith(b"\x00\x00\x00") or b"ftyp" in output[:64]
        )
        if converted.returncode == 0 and output and valid_container:
            item["bytes"] = output
            if needs_video_conversion:
                item["filename"] = "sticker.mp4"
                item["mime_type"] = "video/mp4"
            else:
                item["filename"] = "voice.m4a"
                item["mime_type"] = "audio/mp4"
    except Exception as error:
        debug_logger.warning(
            "PUBLIC_MAX_MEDIA_CONVERSION_FAILED kind=%s error_type=%s",
            kind, type(error).__name__,
        )
    return item


def _send_public_max_message(flow_key, nonce, preview_message_id):
    result_text = None
    result_keyboard = None
    try:
        with public_max_send_flow_lock:
            flow = public_max_send_flows.get(flow_key)
            if not flow or flow.get("nonce") != nonce or flow.get("stage") != "sending":
                return
            flow = dict(flow)
        source_chat_id = int(flow["source_chat_id"])
        chat = get_chat_by_id(source_chat_id)
        if not _max_send_button_is_eligible(chat):
            raise RuntimeError("CONNECTION_NO_LONGER_ACTIVE")
        body = str(flow.get("body") or "").strip()
        name = str(flow.get("sender_name") or "").strip()
        media = flow.get("media") or None
        if media:
            media_items = media if isinstance(media, list) else [media]
            downloaded_items = []
            total_size = 0
            for item in media_items[:10]:
                item = dict(item)
                telegram_file = bot.get_file(item["file_id"])
                if _is_public_max_file_oversize(getattr(telegram_file, "file_size", None)):
                    raise RuntimeError("TELEGRAM_FILE_TOO_LARGE")
                file_bytes = bot.download_file(telegram_file.file_path)
                total_size += len(file_bytes)
                if total_size > 20 * 1024 * 1024:
                    raise RuntimeError("TELEGRAM_FILE_TOO_LARGE")
                item["bytes"] = file_bytes
                item = _prepare_public_max_media(item)
                downloaded_items.append(item)
            media = downloaded_items
            if len(media) == 1:
                media = media[0]

        source_lock = max_send_source_locks.setdefault(source_chat_id, threading.Lock())
        with source_lock:
            _send_max_payload_with_sender(
                group_url=chat["max_url"],
                body=body,
                sender_name=name,
                media=media,
                chat_id=source_chat_id,
                session_path=_session_path_for_chat(chat),
                source_type_callback=lambda detected: _update_detected_source_type(source_chat_id, detected),
            )
        result_text = "✅ Сообщение отправлено в MAX."
        result_keyboard = _public_max_send_button_keyboard(
            source_chat_id, private_user_id=int(flow.get("user_id") or flow_key[1])
        )
    except Exception as error:
        _print_max_send_failure("PUBLIC_MAX_SEND_FAILED", int(flow.get("source_chat_id") or flow_key[0]), error)
        debug_logger.warning(
            "PUBLIC_MAX_SEND_FAILED user_chat=%s error_type=%s error=%s",
            flow_key, type(error).__name__, log_preview(error, 900),
        )
        result_text = _max_send_failure_text(error)
        if "MAX_SOURCE_IS_CHANNEL" in str(error):
            result_text = _max_send_failure_text(error, "⚠️ Это канал MAX. Отправлять туда сообщения нельзя; кнопка для этого источника скрыта.")
        elif "MAX_PHOTO_UPLOAD_FAILED" in str(error):
            result_text = _max_send_failure_text(error, "⚠️ MAX не принял фотографию. Попробуйте отправить её как файл или уменьшите размер.")
        elif "TELEGRAM_FILE_TOO_LARGE" in str(error):
            result_text = _max_send_failure_text(error, "⚠️ Файл больше 20 МБ. Отправьте файл поменьше.")
        elif "SESSION_EXPIRED" in str(error) or "NO_SESSION" in str(error):
            result_text = _max_send_failure_text(error, "⚠️ Сессия MAX завершилась. Владелец подключения должен авторизовать аккаунт заново.")
        elif "MAX_UPLOAD_CONTROL_NOT_FOUND" in str(error):
            result_text = _max_send_failure_text(error, "⚠️ MAX не открыл загрузку этого типа файла. Попробуйте отправить его как документ или выберите другой формат.")
        elif "MAX_UPLOAD_NOT_READY" in str(error):
            result_text = _max_send_failure_text(error, "⚠️ MAX не успел подготовить вложение. Проверьте чат MAX и попробуйте отправить файл ещё раз.")
        elif "MAX_SEND_NOT_CONFIRMED" in str(error):
            result_text = _max_send_failure_text(error, "⚠️ MAX не подтвердил отправку. Проверьте чат MAX перед повторной попыткой.")
    finally:
        with public_max_send_flow_lock:
            current = public_max_send_flows.get(flow_key)
            if current and current.get("nonce") == nonce:
                public_max_send_flows.pop(flow_key, None)
        if result_text is not None:
            try:
                bot.edit_message_text(
                    result_text,
                    int(flow_key[0]),
                    int(preview_message_id),
                    reply_markup=result_keyboard,
                    **NO_PREVIEW,
                )
            except Exception:
                pass


def _finish_public_max_send(flow_key, nonce, preview_message_id):
    try:
        _send_public_max_message(flow_key, nonce, preview_message_id)
    finally:
        max_send_browser_semaphore.release()


def _finalize_public_max_album(flow_key, nonce, media_group_id):
    album_key = (flow_key, str(media_group_id))
    with public_max_send_flow_lock:
        album = public_max_send_albums.pop(album_key, None)
        flow = public_max_send_flows.get(flow_key)
        if (
            not album
            or not flow
            or flow.get("nonce") != nonce
            or flow.get("stage") != "body"
        ):
            return
        messages = list(album.get("messages") or [])[:10]
    items = []
    captions = []
    for message in messages:
        payload = _public_max_input(message)
        if payload and payload.get("media"):
            items.append(payload["media"])
            if payload.get("body"):
                captions.append(str(payload["body"]).strip())
    if not items:
        return
    body = captions[0] if captions else ""
    with public_max_send_flow_lock:
        current = public_max_send_flows.get(flow_key)
        sender_name = str((current or {}).get("sender_name") or "")
    if len(body) + len(sender_name) + 2 > 3500:
        prompt = bot.send_message(
            int(flow_key[0]),
            "Подпись к альбому слишком длинная. Отправьте более короткий текст или альбом без подписи.",
            reply_markup=_public_max_send_keyboard(nonce),
            **NO_PREVIEW,
        )
        with public_max_send_flow_lock:
            current = public_max_send_flows.get(flow_key)
            if current and current.get("nonce") == nonce:
                current["prompt_message_id"] = int(prompt.message_id)
        return
    with public_max_send_flow_lock:
        current = public_max_send_flows.get(flow_key)
        if not current or current.get("nonce") != nonce or current.get("stage") != "body":
            return
        current["body"] = body
        current["media"] = items
        current["payload_label"] = f"Альбом · {len(items)} вложений"
        current["stage"] = "confirm"
        current["expires_at"] = time.time() + 10 * 60
        source_chat_id = int(current["source_chat_id"])
        preview_flow = dict(current)
    preview = bot.send_message(
        int(flow_key[0]),
        _public_max_preview_text(preview_flow),
        parse_mode="HTML",
        reply_markup=_public_max_send_confirm_keyboard(source_chat_id, nonce),
        **NO_PREVIEW,
    )
    with public_max_send_flow_lock:
        current = public_max_send_flows.get(flow_key)
        if current and current.get("nonce") == nonce:
            current["preview_message_id"] = int(preview.message_id)


@bot.message_handler(
    func=_is_public_max_send_reply,
    content_types=[
        "text", "photo", "video", "animation", "document", "audio", "voice",
        "video_note", "sticker", "contact", "location", "venue", "poll", "dice", "game", "web_app_data",
    ],
)
def handle_public_max_send_reply(message):
    flow_key = (int(message.chat.id), int(message.from_user.id))
    with public_max_send_flow_lock:
        flow = public_max_send_flows.get(flow_key)
        if not flow or flow.get("stage") not in {"name", "body"}:
            return
        flow = dict(flow)

    if flow.get("stage") == "name":
        if message.content_type != "text":
            sent = bot.send_message(
                message.chat.id,
                "Сначала отправьте имя текстом или нажмите «⏭️ Пропустить этот шаг».",
                reply_markup=_public_max_send_keyboard(flow["nonce"], include_skip=True),
                **NO_PREVIEW,
            )
            _record_bot_sent_group_messages(
                message.chat.id, [sent.message_id], "Подсказка: имя нужно указать текстом"
            )
            with public_max_send_flow_lock:
                current = public_max_send_flows.get(flow_key)
                if current and current.get("nonce") == flow.get("nonce"):
                    current["prompt_message_id"] = int(sent.message_id)
            return
        sender_name = str(message.text or "").strip()
        if sender_name == "⏭️ Пропустить этот шаг":
            sender_name = ""
        elif not sender_name or sender_name.startswith("/") or len(sender_name) > 80:
            sent = bot.send_message(
                message.chat.id,
                "Укажите имя длиной до 80 символов или нажмите «⏭️ Пропустить этот шаг».",
                reply_markup=_public_max_send_keyboard(flow["nonce"], include_skip=True),
                **NO_PREVIEW,
            )
            _record_bot_sent_group_messages(
                message.chat.id, [sent.message_id], "Подсказка о длине имени"
            )
            with public_max_send_flow_lock:
                current = public_max_send_flows.get(flow_key)
                if current and current.get("nonce") == flow.get("nonce"):
                    current["prompt_message_id"] = int(sent.message_id)
            return
        with public_max_send_flow_lock:
            current = public_max_send_flows.get(flow_key)
            if not current or current.get("nonce") != flow.get("nonce"):
                return
            current["sender_name"] = sender_name
            current["stage"] = "body"
        _ask_public_max_body(flow_key, flow["nonce"])
        return

    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id and message.content_type in {"photo", "video", "animation"}:
        album_key = (flow_key, str(media_group_id))
        with public_max_send_flow_lock:
            current = public_max_send_albums.get(album_key)
            if current is None or current.get("nonce") != flow.get("nonce"):
                current = {"nonce": flow.get("nonce"), "messages": [], "timer": None}
                public_max_send_albums[album_key] = current
            if all(int(item.message_id) != int(message.message_id) for item in current["messages"]):
                current["messages"].append(message)
            old_timer = current.get("timer")
            if old_timer:
                old_timer.cancel()
            timer = threading.Timer(
                1.0,
                _finalize_public_max_album,
                args=(flow_key, flow.get("nonce"), str(media_group_id)),
            )
            timer.daemon = True
            current["timer"] = timer
            timer.start()
        return

    payload = _public_max_input(message)
    if not payload:
        sent = bot.send_message(
            message.chat.id,
            "Этот тип сообщения пока не поддерживается. Пришлите текст, фото, видео, кружок, голосовое, стикер, файл, геопозицию, контакт или опрос.",
            reply_markup=_public_max_send_keyboard(flow["nonce"]),
            **NO_PREVIEW,
        )
        _record_bot_sent_group_messages(
            message.chat.id, [sent.message_id], "Подсказка о типе сообщения"
        )
        with public_max_send_flow_lock:
            current = public_max_send_flows.get(flow_key)
            if current and current.get("nonce") == flow.get("nonce"):
                current["prompt_message_id"] = int(sent.message_id)
        return
    body = str(payload.get("body") or "").strip()
    if len(body) > 3500 or len(str(flow.get("sender_name") or "")) + len(body) + 2 > 3500:
        sent = bot.send_message(
            message.chat.id,
            "Текст слишком длинный. Отправьте текст до 3500 символов или прикрепите его как файл.",
            reply_markup=_public_max_send_keyboard(flow["nonce"]),
            **NO_PREVIEW,
        )
        _record_bot_sent_group_messages(
            message.chat.id, [sent.message_id], "Подсказка о длине сообщения"
        )
        with public_max_send_flow_lock:
            current = public_max_send_flows.get(flow_key)
            if current and current.get("nonce") == flow.get("nonce"):
                current["prompt_message_id"] = int(sent.message_id)
        return
    with public_max_send_flow_lock:
        current = public_max_send_flows.get(flow_key)
        if not current or current.get("nonce") != flow.get("nonce"):
            return
        current["body"] = body
        current["media"] = payload.get("media")
        current["payload_label"] = payload.get("label") or "Текст"
        current["stage"] = "confirm"
        current["expires_at"] = time.time() + 10 * 60
        source_chat_id = int(current["source_chat_id"])
        nonce = current["nonce"]
    preview = bot.send_message(
        message.chat.id,
        _public_max_preview_text(current),
        parse_mode="HTML",
        reply_markup=_public_max_send_confirm_keyboard(source_chat_id, nonce),
        **NO_PREVIEW,
    )
    _record_bot_sent_group_messages(
        message.chat.id,
        [preview.message_id],
        f"Предпросмотр отправки в MAX · {payload.get('label') or 'Текст'}",
        has_media=bool(payload.get("media")),
    )
    with public_max_send_flow_lock:
        current = public_max_send_flows.get(flow_key)
        if current and current.get("nonce") == nonce:
            current["preview_message_id"] = int(preview.message_id)


def _project_source_instructions(owner_id, source_type, chat_id):
    setup_state = customer_setup_state.setdefault(
        int(owner_id), {"chat_id": int(chat_id), "mode": "project"}
    )
    setup_state["source_type"] = source_type
    setup_state["screen"] = "source_instructions"
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    if source_type == "private":
        text = (
            "<b>Шаг 2 из 2 · Личный чат</b>\n\n"
            "Пришлите ссылку на личный чат MAX. Готовый аккаунт сможет читать его, только если этот чат уже доступен аккаунту.\n\n"
            "Если это переписка с другим человеком, сначала откройте её в MAX на аккаунте проекта и убедитесь, что сообщения видны. "
            "После ссылки бот проверит доступ и включит пересылку."
        )
    elif source_type == "group":
        keyboard.add(types.InlineKeyboardButton(
            "🔗 Ссылка приглашения",
            callback_data=f"project_help_invite_{chat_id}",
        ))
        text = (
            "<b>Шаг 2 из 2 · Группа или чат</b>\n\n"
            f"1) Добавьте в вашу MAX группу готовый аккаунт <code>{escape_html(MAX_PROJECT_ACCOUNT_PHONE)}</code>.\n"
            "2) Затем перейдите по ссылке https://web.max.ru/, войдите в свой аккаунт, нажмите на свою группу, "
            "скопируйте ссылку на группу и отправьте сюда в чат.\n\n"
            "После этого бот проверит доступность бота и сообщит вам после проверки.\n\n"
            "<i>Если у вас будут какие-то вопросы или проблемы, то пишите /help</i>"
        )
    else:
        keyboard.add(types.InlineKeyboardButton(
            "🔗 Канал закрытый",
            callback_data=f"project_help_channel_{chat_id}",
        ))
        text = (
            "<b>Шаг 2 из 2 · Канал</b>\n\n"
            "1) Перейдите по ссылке https://web.max.ru/ и войдите в свой аккаунт.\n"
            "2) Нажмите на канал, который вы хотите подключить и скопируйте ссылку.\n"
            "3) Пришлите ссылку сюда в чат. После этого бот проверить доступность этого канала.\n\n"
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
        f"<b>Отправьте ссылку-приглашение на MAX {('группу' if source_type == 'group' else 'канал')}.</b>\n\n"
        "После этого <b>администратор сам все настроит</b>, если будут проблемы, то напишет вам.",
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
        if (
            str(pending_state.get("chat_id") or "") == str(chat_id)
            and pending_state.get("screen") == "verifying"
        ):
            pending_state["screen"] = "invite"
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
        detected_source_type = (verification or {}).get("source_type") if isinstance(verification, dict) else None
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
            toggle_chat(chat_id, True)
            if (
                bool(current.get("max_source_type_manual"))
                and detected_source_type == "channel"
                and current.get("max_source_type") != "channel"
            ):
                update_chat_source_type(chat_id, "channel", verified=True, manual=True)
            elif bool(current.get("max_source_type_manual")) and current.get("max_source_type"):
                update_chat_source_type(
                    chat_id, current["max_source_type"], verified=True, manual=True
                )
            elif detected_source_type in {"group", "channel", "private"}:
                update_chat_source_type(chat_id, detected_source_type, verified=True)
            current = get_chat_by_id(chat_id)
            if not current or not current.get("is_active"):
                raise RuntimeError("CHAT_ACTIVATION_NOT_PERSISTED")
        _sync_max_send_button(chat_id)
        if int(pending_state.get("chat_id") or 0) == int(chat_id):
            customer_setup_state.pop(owner_id, None)
        if setup_prompt_id and setup_prompt_id != report_message_id:
            try:
                bot.edit_message_reply_markup(owner_id, setup_prompt_id, reply_markup=None)
            except Exception:
                pass
        status_reported = report("✅ Доступ к MAX подтверждён. Пересылка включена.")
        clear_project_access_buttons()
        refresh_chat_list()
        review_offered = False
        if owner_id > 0:
            try:
                connection_generation = get_or_create_chat_connection_generation(chat_id)
                if connection_generation:
                    review_offered = _offer_review_after_connection(
                        owner_id,
                        f"{chat_id}:{chat.get('account_mode') or 'project'}:{connection_generation}",
                    )
            except Exception as notify_error:
                debug_logger.warning(
                    "REVIEW_INVITATION_SEND_FAILED user_id=%s error_type=%s",
                    owner_id, type(notify_error).__name__,
                )
        if owner_id > 0 and not status_reported and not review_offered:
            try:
                bot.send_message(
                    owner_id,
                    "<b>🎉 Подключение завершено!</b>\n\n"
                    "MAX открыл чат, пересылка включена. Новые сообщения будут приходить в выбранное место.",
                    **NO_PREVIEW,
                )
            except Exception as notify_error:
                debug_logger.error(
                    "PROJECT_ACCESS_SUCCESS_NOTIFY_FAILED chat_id=%s owner_id=%s error_type=%s",
                    chat_id, owner_id, type(notify_error).__name__,
                )
        return True
    except Exception as error:
        debug_logger.warning(
            "PROJECT_GROUP_ACCESS_CHECK_FAILED chat_id=%s owner_id=%s error_type=%s",
            chat_id, owner_id, type(error).__name__,
        )
        if (
            str(pending_state.get("chat_id") or "") == str(chat_id)
            and pending_state.get("screen") == "verifying"
        ):
            pending_state["screen"] = "invite"
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
    previous_chat = get_chat_by_id(int(chat_id))
    if previous_chat and previous_chat.get("max_send_button_message_id"):
        _remove_max_send_button(int(chat_id))
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
    _sync_send_to_max_command_menu(int(chat_id), get_chat_by_id(int(chat_id)))
    _clear_project_invite_pending(int(chat_id), delete_admin_messages=True)
    pending_setup_modes.pop(owner_id, None)
    customer_setup_state[owner_id] = {
        "chat_id": chat_id,
        "mode": account_mode,
        "destination_type": "group",
        "screen": "source_type",
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
    sent = bot.send_message(
        owner_id,
        _customer_source_type_text(chat_id),
        parse_mode="HTML",
        reply_markup=_customer_source_type_keyboard(chat_id),
        **NO_PREVIEW,
    )
    customer_setup_state[owner_id]["prompt_message_id"] = sent.message_id
    return True


def _begin_customer_private_setup(owner_id, account_mode, callback_message=None):
    owner_id = int(owner_id)
    if _user_needs_subscription(owner_id, account_mode):
        pending_setup_modes.pop(owner_id, None)
        bot.send_message(
            owner_id,
            "Для подключения сначала оформите подписку командой /subscription.",
            reply_markup=subscription_renewal_keyboard(),
            **NO_PREVIEW,
        )
        return False
    try:
        user = bot.get_chat(owner_id)
        user_name = " ".join(
            part for part in (user.first_name, user.last_name) if str(part or "").strip()
        ).strip() or str(owner_id)
    except Exception:
        user_name = str(owner_id)
    title = f"Личные сообщения · {user_name}"[:120]
    try:
        source_chat_id = claim_private_chat(owner_id, title, account_mode)
    except ChatLimitReached:
        pending_setup_modes.pop(owner_id, None)
        bot.send_message(
            owner_id,
            _chat_limit_notice(get_customer_chat_limit(owner_id)),
            **NO_PREVIEW,
        )
        return False
    try:
        for old_chat in get_user_chats(owner_id):
            old_id = int(old_chat.get("chat_id") or 0)
            if (
                old_id != int(source_chat_id)
                and old_chat.get("destination_type") == "private"
                and not old_chat.get("is_active")
                and (
                    old_chat.get("setup_pending")
                    or (
                        not old_chat.get("max_url")
                        and int(old_chat.get("message_count") or 0) == 0
                    )
                )
            ):
                _delete_chat_everywhere(old_id)
    except Exception as error:
        debug_logger.warning(
            "PRIVATE_SETUP_DUPLICATE_CLEANUP_FAILED user_id=%s error_type=%s",
            owner_id, type(error).__name__,
        )
    pending_setup_modes.pop(owner_id, None)
    customer_setup_state[owner_id] = {
        "chat_id": source_chat_id,
        "mode": account_mode,
        "destination_type": "private",
        "screen": "source_type",
    }

    if account_mode == "project":
        text = _project_source_type_text(source_chat_id)
        keyboard = _project_source_type_keyboard(source_chat_id)
    else:
        text = _customer_source_type_text(source_chat_id)
        keyboard = _customer_source_type_keyboard(source_chat_id)

    sent = None
    if callback_message:
        try:
            bot.edit_message_text(
                text,
                callback_message.chat.id,
                callback_message.message_id,
                reply_markup=keyboard,
                parse_mode="HTML",
                **NO_PREVIEW,
            )
            sent = callback_message
        except Exception:
            pass
    if sent is None:
        sent = bot.send_message(
            owner_id, text, reply_markup=keyboard, parse_mode="HTML", **NO_PREVIEW
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
    if state.get("source_type") not in {"channel", "group", "private"}:
        source_keyboard = (
            _project_source_type_keyboard(state["chat_id"])
            if mode == "project"
            else _customer_source_type_keyboard(state["chat_id"])
        )
        _edit_setup_prompt(
            message.chat.id,
            state,
            "Сначала выберите тип чата MAX кнопкой в этом сообщении.",
            reply_markup=source_keyboard,
            parse_mode="HTML",
        )
        return
    if not _is_max_source_url(url):
        keyboard = types.InlineKeyboardMarkup(row_width=1)
        if mode == "project":
            if state.get("screen") == "invite":
                if state.get("source_type") == "channel":
                    back_action, back_label = "project_back_channel", "🔙 Назад к шагу канала"
                elif state.get("source_type") == "private":
                    back_action, back_label = "project_back_type", "🔙 Назад к выбору чата"
                else:
                    back_action, back_label = "project_back_group", "🔙 Назад к шагу группы"
            else:
                back_action = "project_back_type"
                back_label = "🔙 Назад к выбору чата"
            keyboard.add(types.InlineKeyboardButton(back_label, callback_data=f"{back_action}_{state['chat_id']}"))
        else:
            keyboard.add(types.InlineKeyboardButton(
                "🔙 Назад к выбору чата",
                callback_data=f"customer_source_back_{state['chat_id']}",
            ))
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
        if link_kind == "invite" or state.get("screen") == "invite":
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
            "<b>Шаг 3 из 3.</b> Сейчас пришлю QR-код для входа в MAX.\n\nОтсканируйте его в приложении MAX "
            "и нажмите «✅ Готово, я отсканировал». Авторизация будет сохранена только для вашей группы. \n\n"
            "<b>Если удобнее входить по номеру телефона</b>, то нажмите кнопку «Войти по номеру телефона»",
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
    if _close_support(message.from_user.id, closed_by_id=message.from_user.id):
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
    and m.from_user is not None
    and is_admin(m.from_user.id)
    and m.reply_to_message is not None
    and get_admin_group_reply_route(m.chat.id, m.reply_to_message.message_id) is not None
    and (not m.text or not m.text.startswith("/")),
    content_types=["text", "photo", "video", "document", "audio", "voice", "animation", "sticker", "video_note", "location", "contact", "venue", "poll"],
)
def admin_group_thread_reply(message):
    route = get_admin_group_reply_route(message.chat.id, message.reply_to_message.message_id)
    if not route:
        return
    group_id = int(route["group_chat_id"])
    reply_to_id = int(route["group_message_id"])
    header_html = "📩 <b>Ответ от команды MAX_Parser</b>\n\n"
    header_plain = "📩 Ответ от команды MAX_Parser\n\n"
    sent_message_ids = []
    try:
        if message.content_type == "text":
            body_html = _telegram_entities_to_html(
                message.text or "", getattr(message, "entities", None)
            )
            if len(header_plain) + len(message.text or "") <= 4096:
                sent = bot.send_message(
                    group_id,
                    header_html + body_html,
                    parse_mode="HTML",
                    reply_to_message_id=reply_to_id,
                    **NO_PREVIEW,
                )
                sent_message_ids.append(sent.message_id)
            else:
                header = bot.send_message(
                    group_id, header_html, parse_mode="HTML",
                    reply_to_message_id=reply_to_id, **NO_PREVIEW,
                )
                sent_message_ids.append(header.message_id)
                body = bot.send_message(
                    group_id, body_html, parse_mode="HTML",
                    reply_to_message_id=header.message_id, **NO_PREVIEW,
                )
                sent_message_ids.append(body.message_id)
        else:
            caption = str(getattr(message, "caption", None) or "")
            caption_html = _telegram_entities_to_html(
                caption, getattr(message, "caption_entities", None)
            )
            if caption and len(header_plain) + len(caption) <= 1024:
                copied = bot.copy_message(
                    group_id, message.chat.id, message.message_id,
                    reply_to_message_id=reply_to_id,
                    caption=header_html + caption_html,
                    parse_mode="HTML",
                )
                sent_message_ids.append(copied.message_id)
            else:
                header = bot.send_message(
                    group_id, header_html, parse_mode="HTML",
                    reply_to_message_id=reply_to_id, **NO_PREVIEW,
                )
                sent_message_ids.append(header.message_id)
                copied = bot.copy_message(
                    group_id, message.chat.id, message.message_id,
                    reply_to_message_id=header.message_id,
                )
                sent_message_ids.append(copied.message_id)
        for sent_id in sent_message_ids:
            save_admin_group_thread_message(group_id, sent_id, int(message.from_user.id))
        bot.send_message(
            message.chat.id,
            "✅ Ответ отправлен в группу.",
            reply_markup=admin_send_again_keyboard("group", group_id),
            **NO_PREVIEW,
        )
    except Exception as error:
        debug_logger.warning(
            "ADMIN_GROUP_REPLY_SEND_FAILED group_id=%s admin_id=%s error_type=%s",
            group_id, message.from_user.id, type(error).__name__,
        )
        bot.reply_to(message, "Не удалось отправить ответ. Проверьте, что бот всё ещё состоит в группе.")


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
    admin_id = int(message.from_user.id)
    if message.content_type == "text" and message.text.strip().lower() == "/close":
        if get_support_session_assignee(user_id) == admin_id and _close_support(
            user_id, closed_by_id=admin_id
        ):
            bot.reply_to(message, "Диалог завершён. Пользователь получил уведомление.")
        else:
            bot.reply_to(message, "Этот диалог уже закрыт или его ведёт другой администратор.")
        return

    with _support_delivery_lock(user_id):
        if user_id not in support_sessions:
            bot.reply_to(message, "Диалог уже закрыт. Откройте новое обращение, если пользователь напишет снова.")
            return
        assigned_admin_id = get_support_session_assignee(user_id)
        if assigned_admin_id is None:
            try:
                claim_result = claim_support_session(user_id, admin_id)
            except Exception as error:
                debug_logger.warning(
                    "SUPPORT_AUTO_ASSIGN_FAILED user_id=%s admin_id=%s error_type=%s",
                    user_id, admin_id, type(error).__name__,
                )
                bot.reply_to(message, "Не удалось назначить обращение. Попробуйте ещё раз.")
                return
            if not claim_result:
                bot.reply_to(message, "Диалог уже закрыт.")
                return
            assigned_admin_id = int(claim_result.get("admin_id") or 0)
            if assigned_admin_id != admin_id:
                bot.reply_to(message, "Это обращение уже ведёт другой администратор.")
                return
            if claim_result.get("claimed"):
                try:
                    _notify_support_user_accepted(user_id, message.from_user)
                except Exception as error:
                    debug_logger.warning(
                        "SUPPORT_USER_ASSIGN_NOTICE_FAILED user_id=%s admin_id=%s error_type=%s",
                        user_id, admin_id, type(error).__name__,
                    )
        if assigned_admin_id != admin_id:
            bot.reply_to(message, "Это обращение уже ведёт другой администратор.")
            return
        try:
            _send_support_admin_message(user_id, message)
            bot.send_message(
                message.chat.id,
                f"✅ Ответ отправлен пользователю <code>{user_id}</code>.",
                parse_mode="HTML",
                **NO_PREVIEW,
            )
        except Exception as error:
            bot.reply_to(message, f"Не удалось доставить ответ: {escape_html(str(error)[:300])}")


def _send_support_admin_message(user_id, message):
    if message.content_type == "text" and len(message.text or "") <= 3900:
        bot.send_message(
            user_id,
            "🆘 <b>Ответ от тех поддержки</b>\n\n"
            + _telegram_entities_to_html(
                message.text or "",
                getattr(message, "entities", None),
            ),
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


def process_support_admin_followup(message, user_id):
    admin_id = int(message.from_user.id)
    user_id = int(user_id)
    if not is_admin(admin_id):
        return
    if message.content_type == "text" and (message.text or "").strip().startswith("/"):
        if (message.text or "").strip().lower() == "/close":
            if get_support_session_assignee(user_id) == admin_id and _close_support(user_id, closed_by_id=admin_id):
                bot.send_message(message.chat.id, "✅ Чат с пользователем завершён.", **NO_PREVIEW)
            else:
                bot.send_message(message.chat.id, "Этот диалог уже закрыт.", **NO_PREVIEW)
            return
        prompt = bot.send_message(
            message.chat.id,
            "Отправьте ответ обычным сообщением, нажмите «❌ Отмена» или введите /close, чтобы завершить чат.",
            reply_markup=get_cancel_keyboard(),
            **NO_PREVIEW,
        )
        bot.register_next_step_handler(prompt, process_support_admin_followup, user_id)
        return
    if get_support_session_assignee(user_id) != admin_id:
        bot.send_message(message.chat.id, "Это обращение уже закрыто или его ведёт другой администратор.", **NO_PREVIEW)
        return
    try:
        _send_support_admin_message(user_id, message)
        bot.send_message(
            message.chat.id,
            f"✅ Ответ отправлен пользователю <code>{user_id}</code>.",
            parse_mode="HTML",
            **NO_PREVIEW,
        )
    except Exception as error:
        bot.send_message(
            message.chat.id,
            f"Не удалось доставить ответ: {escape_html(str(error)[:300])}",
            **NO_PREVIEW,
        )


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
    user_id = int(message.from_user.id)
    with _support_delivery_lock(user_id):
        return _support_user_message_locked(message)


def _support_user_message_locked(message):
    if message.content_type == "text" and (message.text or "").startswith("/"):
        return
    user_id = int(message.from_user.id)
    try:
        _notify_support_admin(message.from_user, message)
        if get_support_session_assignee(user_id) is None:
            bot.send_message(
                message.chat.id,
                "✅ Сообщение отправлено администраторам. Можете написать ещё.",
                reply_markup=_support_keyboard(),
                **NO_PREVIEW,
            )
    except Exception as error:
        bot.send_message(
            message.chat.id,
            f"Не удалось доставить сообщение в поддержку: {escape_html(str(error)[:200])}",
            **NO_PREVIEW,
        )

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
            reply_markup=_build_auth_human_keyboard(target_chat_id, data),
            **NO_PREVIEW,
        )
    elif status == "waiting":
        session = active_logins.get(target_chat_id)
        if session:
            bot.send_message(
                user_id,
                "MAX пока не подтвердил вход. Отсканируйте QR-код и нажмите «✅ Готово, я отсканировал». "
                "Также можно выбрать вход по номеру телефона.",
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
@bot.callback_query_handler(func=lambda call: call.data.startswith("maxsend_") or call.data.startswith("admin_") or call.data.startswith("qr_") or call.data.startswith("support_close_") or call.data.startswith("support_accept_") or call.data.startswith("review_") or call.data.startswith("reviews_") or call.data.startswith("project_retry_") or call.data.startswith("project_edit_source_") or call.data.startswith("project_source_channel_") or call.data.startswith("project_source_group_") or call.data.startswith("project_source_private_") or call.data.startswith("project_add_help_") or call.data.startswith("project_help_invite_") or call.data.startswith("project_invite_admin_") or call.data.startswith("project_help_channel_") or call.data.startswith("project_help_support_") or call.data.startswith("project_back_") or call.data.startswith("setup_cancel_") or call.data.startswith("connectdest_") or call.data.startswith("customer_source_") or call.data in ["delete_this_msg", "cancel_step"])
def admin_callbacks(call):
    track_user(call)
    action = call.data
    if action.startswith("maxsend_start_"):
        try:
            start_parts = action[len("maxsend_start_"):].split("_", 1)
            source_chat_id = int(start_parts[0])
            allowed_private_user_id = int(start_parts[1]) if len(start_parts) > 1 else None
        except ValueError:
            bot.answer_callback_query(call.id, "Не удалось определить чат MAX.", show_alert=True)
            return
        _start_public_max_send(
            call, source_chat_id, allowed_private_user_id=allowed_private_user_id
        )
        return
    if action.startswith("maxsend_skipname_"):
        nonce = action[len("maxsend_skipname_"):]
        flow_key = (int(call.message.chat.id), int(call.from_user.id))
        with public_max_send_flow_lock:
            flow = public_max_send_flows.get(flow_key)
            if (
                not flow
                or flow.get("nonce") != nonce
                or flow.get("stage") != "name"
                or int(flow.get("prompt_message_id") or 0) != int(call.message.message_id)
            ):
                flow = None
            else:
                flow["sender_name"] = ""
                flow["stage"] = "body"
        if not flow:
            bot.answer_callback_query(call.id, "Эта кнопка уже не действует.")
            return
        bot.answer_callback_query(call.id, "Имя пропущено")
        try:
            bot.edit_message_text(
                "Имя отправителя пропущено.",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=None,
                **NO_PREVIEW,
            )
        except Exception:
            pass
        try:
            _ask_public_max_body(flow_key, nonce)
        except Exception as error:
            debug_logger.warning(
                "PUBLIC_MAX_BODY_PROMPT_FAILED user=%s error_type=%s",
                call.from_user.id, type(error).__name__,
            )
        return
    if action.startswith("maxsend_cancel_"):
        nonce = action[len("maxsend_cancel_"):]
        flow_key = (int(call.message.chat.id), int(call.from_user.id))
        with public_max_send_flow_lock:
            flow = public_max_send_flows.get(flow_key)
            if not flow or flow.get("nonce") != nonce or flow.get("stage") == "sending":
                flow = None
            else:
                public_max_send_flows.pop(flow_key, None)
        if not flow:
            bot.answer_callback_query(call.id, "Эта кнопка уже не действует.")
            return
        bot.answer_callback_query(call.id, "Отправка отменена")
        try:
            bot.edit_message_text(
                "Отправка в MAX отменена.",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=None,
                **NO_PREVIEW,
            )
        except Exception:
            pass
        return
    if action.startswith("maxsend_confirm_"):
        try:
            source_chat_text, nonce = action[len("maxsend_confirm_"):].rsplit("_", 1)
            source_chat_id = int(source_chat_text)
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Кнопка устарела.", show_alert=True)
            return
        flow_key = (int(call.message.chat.id), int(call.from_user.id))
        with public_max_send_flow_lock:
            flow = public_max_send_flows.get(flow_key)
            if (
                not flow
                or flow.get("nonce") != nonce
                or int(flow.get("source_chat_id") or 0) != source_chat_id
                or flow.get("stage") != "confirm"
                or int(flow.get("preview_message_id") or 0) != int(call.message.message_id)
            ):
                flow = None
            elif not max_send_browser_semaphore.acquire(blocking=False):
                bot.answer_callback_query(call.id, "MAX сейчас занят. Попробуйте чуть позже.", show_alert=True)
                return
            else:
                flow["stage"] = "sending"
        if not flow:
            bot.answer_callback_query(call.id, "Эта кнопка уже не действует.", show_alert=True)
            return
        bot.answer_callback_query(call.id, "Отправляю сообщение в MAX")
        try:
            bot.edit_message_text(
                "⏳ Отправляю сообщение в MAX…",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=None,
                **NO_PREVIEW,
            )
        except Exception:
            pass
        threading.Thread(
            target=_finish_public_max_send,
            args=(flow_key, nonce, int(call.message.message_id)),
            daemon=True,
            name=f"public-max-send-{source_chat_id}-{call.from_user.id}",
        ).start()
        return
    if action.startswith("connectdest_"):
        try:
            _, destination_kind, account_mode = action.split("_", 2)
        except ValueError:
            bot.answer_callback_query(call.id, "Не удалось определить способ отправки", show_alert=True)
            return
        user_id = int(call.from_user.id)
        if call.message.chat.type != "private":
            bot.answer_callback_query(call.id, "Откройте настройку в личном чате с ботом.", show_alert=True)
            return
        if pending_setup_modes.get(user_id) != account_mode:
            bot.answer_callback_query(call.id, "Настройка устарела. Начните её через /connect.", show_alert=True)
            return
        if account_mode not in {"self", "project"}:
            bot.answer_callback_query(call.id, "Неизвестный способ подключения", show_alert=True)
            return
        if destination_kind not in {"group", "private"}:
            bot.answer_callback_query(call.id, "Неизвестное место отправки", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        if destination_kind == "private":
            _begin_customer_private_setup(user_id, account_mode, call.message)
            return
        label = "настройку своего аккаунта MAX" if account_mode == "self" else "настройку через готовый аккаунт"
        try:
            bot.edit_message_text(
                f"<b>Шаг 1 из {2 if account_mode == 'project' else 3} · Telegram-группа</b>\n\n"
                f"Вы выбрали: <b>{label}</b>.\n\n<b>Добавьте меня в Telegram-группу, в которую я буду присылать сообщения.</b> "
                "Если я уже состою в ней, то напишите в группе команду /bind.\n\n"
                "<i>Чтобы остановить настройку, нажмите «❌ Отмена».</i>",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=get_cancel_keyboard(),
                **NO_PREVIEW,
            )
        except Exception:
            bot.send_message(
                user_id,
                f"<b>Шаг 1 из {2 if account_mode == 'project' else 3} · Telegram-группа</b>\n\n"
                f"Вы выбрали: <b>{label}</b>.\n\n<b>Добавьте меня в Telegram-группу, в которую я буду присылать сообщения.</b> "
                "Если я уже состою в ней, то напишите в группе команду /bind.\n\n"
                "<i>Чтобы остановить настройку, нажмите «❌ Отмена».</i>",
                parse_mode="HTML",
                reply_markup=get_cancel_keyboard(),
                **NO_PREVIEW,
            )
        return
    if action.startswith("customer_source_"):
        owner_id = int(call.from_user.id)
        if call.message.chat.type != "private" or int(call.message.chat.id) != owner_id:
            bot.answer_callback_query(call.id, "Откройте настройку в личном чате с ботом.", show_alert=True)
            return
        state = customer_setup_state.get(owner_id) or {}
        try:
            source_chat_id = int(action.rsplit("_", 1)[1])
        except (TypeError, ValueError, IndexError):
            bot.answer_callback_query(call.id, "Настройка устарела.", show_alert=True)
            return
        chat = get_chat_by_id(source_chat_id)
        if (
            not chat
            or chat.get("account_mode") != "self"
            or int(chat.get("owner_id") or 0) != owner_id
            or int(state.get("chat_id") or 0) != source_chat_id
        ):
            bot.answer_callback_query(call.id, "Настройка уже завершена.", show_alert=True)
            return
        if chat.get("is_active"):
            bot.answer_callback_query(call.id, "Подключение уже завершено.", show_alert=True)
            return
        if action.startswith("customer_source_back_"):
            state.pop("source_type", None)
            state["screen"] = "source_type"
            update_chat_source_type(source_chat_id, None)
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                _customer_source_type_text(source_chat_id),
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=_customer_source_type_keyboard(source_chat_id),
                **NO_PREVIEW,
            )
            return
        if action.startswith("customer_source_channel_"):
            source_type = "channel"
        elif action.startswith("customer_source_private_"):
            source_type = "private"
        else:
            source_type = "group"
        state["source_type"] = source_type
        state["screen"] = "source_url"
        update_chat_source_type(source_chat_id, source_type, manual=True)
        text, keyboard = _customer_source_url_prompt(owner_id, source_chat_id)
        state["prompt_message_id"] = int(call.message.message_id)
        bot.answer_callback_query(call.id, "Выберите чат MAX")
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=keyboard,
            **NO_PREVIEW,
        )
        return
    if action == "admin_send_to_max":
        if int(call.from_user.id) != ADMIN_ID or call.message.chat.type != "private" or int(call.message.chat.id) != ADMIN_ID:
            bot.answer_callback_query(call.id, "Эта функция доступна только владельцу.", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        _admin_max_send_source_list(call.message.chat.id, call.message.message_id)
        return
    if action.startswith("admin_max_source_"):
        if int(call.from_user.id) != ADMIN_ID or call.message.chat.type != "private" or int(call.message.chat.id) != ADMIN_ID:
            bot.answer_callback_query(call.id, "Эта функция доступна только владельцу.", show_alert=True)
            return
        try:
            source_chat_id = int(action[len("admin_max_source_"):])
        except ValueError:
            bot.answer_callback_query(call.id, "Не удалось определить чат MAX.", show_alert=True)
            return
        _start_admin_max_send(call, source_chat_id)
        return
    if action.startswith("admin_max_skipname_"):
        if int(call.from_user.id) != ADMIN_ID or call.message.chat.type != "private" or int(call.message.chat.id) != ADMIN_ID:
            bot.answer_callback_query(call.id, "Эта функция доступна только владельцу.", show_alert=True)
            return
        nonce = action[len("admin_max_skipname_"):]
        with admin_max_send_flow_lock:
            flow = admin_max_send_flows.get(ADMIN_ID)
            if not flow or flow.get("nonce") != nonce or flow.get("stage") != "name":
                flow = None
            else:
                flow["sender_name"] = ""
                flow["stage"] = "body"
                source_chat_id = int(flow["source_chat_id"])
        if not flow:
            bot.answer_callback_query(call.id, "Эта кнопка уже не действует.")
            return
        try:
            bot.clear_step_handler_by_chat_id(ADMIN_ID)
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Имя пропущено")
        try:
            bot.edit_message_text(
                "Имя отправителя пропущено.",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=None,
                **NO_PREVIEW,
            )
        except Exception:
            pass
        _admin_max_send_prompt(ADMIN_ID, source_chat_id, nonce, "body")
        return
    if action.startswith("admin_max_cancel"):
        if int(call.from_user.id) != ADMIN_ID or call.message.chat.type != "private" or int(call.message.chat.id) != ADMIN_ID:
            bot.answer_callback_query(call.id, "Эта функция доступна только владельцу.", show_alert=True)
            return
        nonce = action[len("admin_max_cancel_"):] if action.startswith("admin_max_cancel_") else ""
        with admin_max_send_flow_lock:
            flow = admin_max_send_flows.get(ADMIN_ID)
            if not nonce and flow:
                bot.answer_callback_query(call.id, "Эта кнопка уже устарела.")
                return
            if flow and nonce and flow.get("nonce") != nonce:
                flow = None
            if flow and flow.get("stage") != "sending":
                admin_max_send_flows.pop(ADMIN_ID, None)
            elif flow and flow.get("stage") == "sending":
                bot.answer_callback_query(call.id, "Сообщение уже отправляется.")
                return
        try:
            bot.clear_step_handler_by_chat_id(ADMIN_ID)
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Отправка отменена")
        try:
            bot.edit_message_text(
                "Отправка сообщения отменена.", call.message.chat.id, call.message.message_id,
                reply_markup=None, **NO_PREVIEW,
            )
        except Exception:
            pass
        return
    if action.startswith("admin_max_confirm_"):
        if int(call.from_user.id) != ADMIN_ID or call.message.chat.type != "private" or int(call.message.chat.id) != ADMIN_ID:
            bot.answer_callback_query(call.id, "Эта функция доступна только владельцу.", show_alert=True)
            return
        try:
            raw_source_id, nonce = action[len("admin_max_confirm_"):].rsplit("_", 1)
            source_chat_id = int(raw_source_id)
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Кнопка устарела.", show_alert=True)
            return
        with admin_max_send_flow_lock:
            flow = admin_max_send_flows.get(ADMIN_ID)
            if (
                not flow
                or flow.get("nonce") != nonce
                or int(flow.get("source_chat_id") or 0) != source_chat_id
                or flow.get("stage") != "confirm"
                or int(flow.get("preview_message_id") or 0) != int(call.message.message_id)
            ):
                flow = None
            elif max_send_browser_semaphore.acquire(blocking=False):
                flow["stage"] = "sending"
            else:
                bot.answer_callback_query(call.id, "MAX сейчас занят. Попробуйте чуть позже.", show_alert=True)
                return
        if not flow:
            bot.answer_callback_query(call.id, "Эта кнопка устарела.", show_alert=True)
            return
        bot.answer_callback_query(call.id, "Отправляю сообщение в MAX")
        try:
            bot.edit_message_text(
                "⏳ Отправляю сообщение в MAX…", call.message.chat.id, call.message.message_id,
                reply_markup=None, **NO_PREVIEW,
            )
        except Exception:
            pass
        threading.Thread(
            target=_finish_admin_max_send,
            args=(ADMIN_ID, source_chat_id, nonce, int(call.message.message_id)),
            daemon=True,
            name=f"admin-max-send-{source_chat_id}",
        ).start()
        return
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
    if not is_admin(call.from_user.id) and action.startswith(("project_source_channel_", "project_source_group_", "project_source_private_", "project_add_help_", "project_help_invite_", "project_help_channel_", "project_help_support_", "project_back_")):
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
        "project_source_channel_", "project_source_group_", "project_source_private_", "project_add_help_",
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
    if action.startswith("support_accept_"):
        if not is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "⛔️ Нет прав", show_alert=True)
            return
        bot.answer_callback_query(call.id, "Теперь можно отвечать пользователю сразу сообщением.")
        try:
            bot.edit_message_reply_markup(
                call.message.chat.id,
                call.message.message_id,
                reply_markup=None,
            )
        except Exception:
            pass
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
        assigned_admin_id = get_support_session_assignee(user_id)
        if assigned_admin_id != int(call.from_user.id):
            bot.answer_callback_query(
                call.id,
                "Завершить чат может администратор, который принял обращение",
                show_alert=True,
            )
            return
        if _close_support(user_id, closed_by_id=call.from_user.id):
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
    if action.startswith(("project_source_channel_", "project_source_group_", "project_source_private_", "project_add_help_", "project_help_invite_", "project_help_channel_", "project_help_support_", "project_back_")):
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
        if action.startswith(("project_source_channel_", "project_source_group_", "project_source_private_")):
            if action.startswith("project_source_channel_"):
                source_type = "channel"
            elif action.startswith("project_source_private_"):
                source_type = "private"
            else:
                source_type = "group"
            update_chat_source_type(setup_chat_id, source_type, manual=True)
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
            update_chat_source_type(setup_chat_id, "channel", manual=True)
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
            update_chat_source_type(setup_chat_id, None)
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
    if action.startswith("qr_refreshpage_"):
        target_id = _qr_target_from_action(action)
        session = active_logins.get(target_id)
        if not session:
            bot.answer_callback_query(call.id, "Сессия авторизации не найдена", show_alert=True)
            return
        bot.answer_callback_query(call.id, "🔄 Обновляю страницу MAX…")
        remote_auth_service.revoke_session(session)
        try:
            session.remote_action("refresh", timeout=20)
        except Exception as error:
            bot.send_message(
                call.message.chat.id,
                "❌ Не удалось обновить страницу MAX. Попробуйте нажать кнопку ещё раз.",
                **NO_PREVIEW,
            )
            debug_logger.warning(
                "REMOTE_AUTH_PAGE_REFRESH_FAILED target=%s error_type=%s",
                target_id, type(error).__name__,
            )
            return
        if active_logins.get(target_id) is not session:
            return
        try:
            link, _record = remote_auth_service.create_link(
                session, target_id, call.from_user.id, call.message.chat.id
            )
        except RuntimeError as error:
            bot.send_message(
                call.message.chat.id,
                "❌ Не удалось обновить ссылку на страницу входа. Попробуйте позже.",
                **NO_PREVIEW,
            )
            debug_logger.warning(
                "REMOTE_AUTH_LINK_REFRESH_FAILED target=%s error_type=%s",
                target_id, type(error).__name__,
            )
            return
        try:
            bot.edit_message_text(
                _phone_login_message_text(),
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=_build_phone_login_keyboard(target_id, link),
                **NO_PREVIEW,
            )
            _track_auth_message(target_id, call.message)
        except Exception as error:
            debug_logger.warning(
                "REMOTE_AUTH_LINK_REFRESH_MESSAGE_FAILED target=%s error_type=%s",
                target_id, type(error).__name__,
            )
    elif action.startswith("qr_back_"):
        target_id = _qr_target_from_action(action)
        session = active_logins.get(target_id)
        if not session:
            bot.answer_callback_query(call.id, "Сессия авторизации не найдена", show_alert=True)
            return
        bot.answer_callback_query(call.id, "Возвращаю QR-код…")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        # Start a fresh QR session instead of reusing a partially completed
        # phone-login page; this also invalidates its one-time browser link.
        do_qr_login(target_id, call.message.chat.id)
        return
    elif action.startswith("qr_phone_"):
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
        link_message = bot.send_message(
            call.message.chat.id,
            _phone_login_message_text(),
            parse_mode="HTML",
            reply_markup=_build_phone_login_keyboard(target_id, link),
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
            if data == "device_password_setup":
                try:
                    bot.edit_message_text(
                        _auth_human_check_text(session, data),
                        call.message.chat.id,
                        call.message.message_id,
                        reply_markup=_build_auth_human_keyboard(target_id, data),
                        **NO_PREVIEW,
                    )
                except Exception:
                    try:
                        bot.edit_message_reply_markup(
                            call.message.chat.id,
                            call.message.message_id,
                            reply_markup=_build_auth_human_keyboard(target_id, data),
                        )
                    except Exception:
                        pass
                # Keep the existing notice in place; do not send the same
                # password-setup explanation again after a Telegram check.
                return
            auth_message = bot.send_message(
                call.message.chat.id,
                _auth_human_check_text(session, data),
                reply_markup=_build_auth_human_keyboard(target_id, data),
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
    elif action.startswith("admin_sent_page_"):
        try:
            target_raw, page_raw = action[len("admin_sent_page_"):].rsplit("_", 1)
            target_chat_id = int(target_raw)
            page = int(page_raw)
        except (TypeError, ValueError):
            bot.answer_callback_query(call.id, "Не удалось открыть страницу", show_alert=True)
            return
        _show_sent_parser_posts(
            call.message.chat.id, call.message.message_id, target_chat_id, page=page
        )
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
    elif action.startswith("admin_delete_user_"):
        try:
            page = int(action.rsplit("_", 1)[1])
        except ValueError:
            page = 0
        prompt = bot.send_message(
            call.message.chat.id,
            "Отправьте user ID пользователя, которого нужно удалить из списка.",
            reply_markup=get_cancel_keyboard(),
            **NO_PREVIEW,
        )
        bot.register_next_step_handler(
            prompt,
            process_admin_delete_user,
            call.message.message_id,
            page,
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
    elif action.startswith("admin_send_again_"):
        if not is_admin(call.from_user.id) or call.message.chat.type != "private":
            bot.answer_callback_query(call.id, "⛔️ Нет прав", show_alert=True)
            return
        try:
            target_type, raw_target_id = action[len("admin_send_again_"):].split("_", 1)
            target_id = int(raw_target_id)
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "Не удалось определить получателя", show_alert=True)
            return
        if target_type not in {"user", "chat", "group", "support"}:
            bot.answer_callback_query(call.id, "Неизвестный тип получателя", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        if target_type == "user":
            prompt_text = f"Отправьте ещё одно сообщение пользователю <code>{target_id}</code>."
            handler = process_send_to_user_content
        elif target_type == "support":
            prompt_text = f"Отправьте следующий ответ пользователю <code>{target_id}</code>."
            handler = process_support_admin_followup
        elif target_type == "group":
            prompt_text = "Напишите ещё одно сообщение для группы."
            handler = process_admin_group_question
        else:
            prompt_text = f"Отправьте ещё одно сообщение в чат <code>{target_id}</code>."
            handler = process_send_to_chat_content
        prompt = bot.send_message(
            call.message.chat.id,
            prompt_text,
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard(),
            **NO_PREVIEW,
        )
        bot.register_next_step_handler(prompt, handler, target_id)
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
    elif action.startswith("admin_group_message_cancel_"):
        if not is_admin(call.from_user.id) or call.message.chat.type != "private":
            bot.answer_callback_query(call.id, "⛔️ Нет прав", show_alert=True)
            return
        try:
            bot.clear_step_handler_by_chat_id(call.message.chat.id)
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Отменено")
    elif action.startswith("admin_group_message_"):
        if not is_admin(call.from_user.id) or call.message.chat.type != "private":
            bot.answer_callback_query(call.id, "⛔️ Нет прав", show_alert=True)
            return
        target_id = int(action.rsplit("_", 1)[1])
        try:
            telegram_chat = bot.get_chat(target_id)
        except Exception:
            bot.answer_callback_query(call.id, "Группа сейчас недоступна", show_alert=True)
            return
        if getattr(telegram_chat, "type", None) not in ("group", "supergroup"):
            bot.answer_callback_query(call.id, "Сообщения доступны только для групп", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(types.InlineKeyboardButton(
            "❌ Отмена", callback_data=f"admin_group_message_cancel_{target_id}"
        ))
        prompt = bot.send_message(
            call.message.chat.id,
            "💬 Напишите сообщение для группы. Чтобы получить ответы, попросите участников ответить именно на сообщение бота.",
            reply_markup=keyboard,
            **NO_PREVIEW,
        )
        bot.register_next_step_handler(prompt, process_admin_group_question, target_id)
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
            new_active = not bool(chat['is_active'])
            toggle_chat(target_id, new_active)
            if new_active:
                _sync_max_send_button(target_id)
            else:
                _remove_max_send_button(target_id, preserve_anchor=True)
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
        if call.data == "group_private_reply_cancel":
            awaiting_admin_group_private_reply.pop(int(call.from_user.id), None)
            bot.answer_callback_query(call.id, "Ответ отменён")
            try:
                bot.edit_message_reply_markup(
                    call.message.chat.id, call.message.message_id, reply_markup=None
                )
            except Exception:
                pass
            bot.send_message(call.message.chat.id, "Хорошо, отвечать не нужно.", **NO_PREVIEW)
        elif call.data == "referral_program":
            if call.message.chat.type != "private":
                bot.answer_callback_query(call.id, "Откройте программу приглашений в личном чате с ботом.", show_alert=True)
                return
            bot.answer_callback_query(call.id)
            show_referral_program(call.message.chat.id, call.from_user.id, call.message.message_id)
        elif call.data == "referral_refresh":
            if call.message.chat.type != "private":
                bot.answer_callback_query(call.id, "Откройте программу приглашений в личном чате с ботом.", show_alert=True)
                return
            try:
                stats = get_referral_stats(call.from_user.id)
            except Exception as error:
                debug_logger.warning(
                    "REFERRAL_REFRESH_FAILED user_id=%s error_type=%s",
                    call.from_user.id, type(error).__name__,
                )
                bot.answer_callback_query(
                    call.id,
                    "Не удалось обновить прогресс. Попробуйте ещё раз.",
                    show_alert=True,
                )
                return
            bot.answer_callback_query(
                call.id,
                f"📊 Наград: {stats['rewarded']} · выполняют условия: {stats['in_progress']}",
            )
            show_referral_program(
                call.message.chat.id,
                call.from_user.id,
                call.message.message_id,
                stats=stats,
            )
        elif call.data == "thanks_project":
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
• Отправлять сообщения обратно в подходящую группу MAX командой /send_to_max из подключённой Telegram-группы.
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
            _record_referral_engagement(call.from_user.id, "comparison")
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "<b>Чем отличаются способы подключения</b>\n\n"
                "<blockquote><b>1️⃣ Свой MAX-аккаунт</b>\n"
                f"Скидка {SUBSCRIPTION_DISCOUNT_PERCENT}%: {_subscription_discount_text_html('self')}.\n"
                "Вы входите в свой MAX. Парсер читает выбранные вами чаты, которые доступны вашему аккаунту.\n"
                "Подходит, если не хотите добавлять готовый аккаунт в чат.</blockquote>\n\n"
                "<blockquote><b>2️⃣ Через готовый аккаунт</b>\n"
                f"Скидка {SUBSCRIPTION_DISCOUNT_PERCENT}%: {_subscription_discount_text_html('project')}.\n"
                f"Используется готовый аккаунт <code>{escape_html(MAX_PROJECT_ACCOUNT_PHONE)}</code>. "
                "Для группы его нужно добавить или пригласить, для канала — просто скинуть ссылку.\n"
                "Вы не вводите данные своего MAX.</blockquote>\n\n"
                "<blockquote><b>✨ Мульти-подписка</b>\n"
                f"Скидка {SUBSCRIPTION_DISCOUNT_PERCENT}%: {_subscription_discount_text_html('multi')}. "
                "Включает оба способа, "
                "чтобы выбирать удобный вариант для каждого чата.</blockquote>",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=parser_keyboard(),
                parse_mode="HTML",
                **NO_PREVIEW,
            )
        elif call.data == "method_choice_help":
            bot.answer_callback_query(call.id)
            method_help_keyboard = types.InlineKeyboardMarkup(row_width=1)
            method_help_keyboard.add(types.InlineKeyboardButton(
                "🔙 К выбору способа", callback_data="parse_setup"
            ))
            bot.edit_message_text(
                "<b>🤔 Какой способ подойдёт?</b>\n\n"
                "<blockquote><b>1️⃣ Свой MAX-аккаунт</b> — выбирайте, если:\n"
                "• вы уже состоите в группе, но добавить аккаунт проекта нельзя;\n"
                "• закрытый чат не принимает новых участников и в него нельзя вступить по приглашению;\n"
                "• личную переписку в MAX видите только вы.</blockquote>\n\n"
                "<blockquote><b>2️⃣ Готовый аккаунт MAX_Parser</b> — выбирайте, если:\n"
                f"• аккаунт проекта <code>{escape_html(MAX_PROJECT_ACCOUNT_PHONE)}</code> можно добавить в группу;\n"
                "• канал открыт и аккаунту проекта доступны публикации;\n"
                "• готовый аккаунт уже видит нужную личную переписку.\n"
                "Для закрытой группы или канала отправьте ссылку-приглашение. Администратор проекта вручную попробует вступить. "
                "Если MAX пришлёт заявку, владелец или администратор должен её одобрить. После вступления отправьте обычную ссылку на чат.</blockquote>\n\n"
                "<blockquote><b>✨ Мульти-подписка</b> включает оба способа. Можно подключить свой аккаунт к личным и закрытым для добавления чатам, "
                "а готовый — к группам и каналам, куда ему можно получить доступ.</blockquote>\n\n"
                "<b>Простое правило:</b> если чат виден только вашему аккаунту — выбирайте свой. Если готовый аккаунт можно добавить, пригласить "
                "или он уже видит источник — выбирайте готовый. Если ни один вариант не подходит, напишите в поддержку через /help.",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=method_help_keyboard,
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
            _record_referral_engagement(call.from_user.id, "connection")
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
            _record_referral_engagement(call.from_user.id, "connection")
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
            plan_code = "self" if call.data == "renew_self" else "project"
            bot.answer_callback_query(call.id)
            show_subscription_terms(
                call.message.chat.id, call.message.message_id, plan_code,
                back_callback="subscription_products",
            )
        elif call.data in ("my_acc_250", "not_my_acc_250"):
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
                "2️⃣ 1 вариант · Номер телефона", callback_data="video_instruction_phone"
            ))
            video_keyboard.add(types.InlineKeyboardButton(
                "3️⃣ 2 вариант · Готовый аккаунт", callback_data="video_instruction_project"
            ))
            video_keyboard.add(types.InlineKeyboardButton(
                "🔙 Назад к подключению", callback_data="parse_setup"
            ))
            menu_text = "🎬 <b>Видео инструкция</b>\n\nВыберите способ подключения:"
            if getattr(call.message, "video", None):
                bot.send_message(
                    call.message.chat.id,
                    menu_text,
                    reply_markup=video_keyboard,
                    parse_mode="HTML",
                    **NO_PREVIEW,
                )
                try:
                    bot.delete_message(
                        call.message.chat.id, call.message.message_id
                    )
                except Exception:
                    pass
            else:
                bot.edit_message_text(
                    menu_text,
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
            _record_referral_engagement(call.from_user.id, "instruction")
            bot.answer_callback_query(call.id)
            video_keyboard = types.InlineKeyboardMarkup(row_width=1)
            video_keyboard.add(types.InlineKeyboardButton(
                "🔙 К выбору способа", callback_data="video_instruction"
            ))
            if call.data == "video_instruction_phone":
                video_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "media",
                    "phone_login_instruction.mp4",
                )
                try:
                    with open(video_path, "rb") as phone_video:
                        bot.send_video(
                            call.message.chat.id,
                            phone_video,
                            caption="🎬 <b>Видеоинструкция · вход по номеру телефона</b>",
                            reply_markup=video_keyboard,
                            supports_streaming=True,
                        )
                    try:
                        bot.delete_message(
                            call.message.chat.id, call.message.message_id
                        )
                    except Exception:
                        pass
                except Exception as error:
                    debug_logger.warning(
                        "PHONE_LOGIN_INSTRUCTION_VIDEO_SEND_FAILED error_type=%s",
                        type(error).__name__,
                    )
                    bot.send_message(
                        call.message.chat.id,
                        "Не удалось отправить видеоинструкцию. Попробуйте ещё раз позже.",
                        reply_markup=video_keyboard,
                        **NO_PREVIEW,
                    )
                return
            bot.edit_message_text(
                "🎬 <b>Видео инструкция</b>\n\nВидео для этого способа пока не добавлено.",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=video_keyboard,
                parse_mode="HTML",
                **NO_PREVIEW,
            )
        elif call.data == "instruction":
            _record_referral_engagement(call.from_user.id, "instruction")
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
                    "Во втором способе нажмите «Канал», «ЛС - личный чат» или «Группа или чат». Затем откройте <a href=\"https://web.max.ru\">web.max.ru</a>, "
                    "перейдите к чату и скопируйте ссылку из меню.\n\n"
                    "<b>Вариант 1 · Ваш аккаунт MAX</b>\n"
                    "<blockquote><b>Через QR-код:</b>\n\nПосле отправки ссылки бот пришлёт QR-код. Отсканируйте его в приложении MAX и нажмите "
                    "«✅ Готово, я отсканировал». Если MAX запросит пароль двухэтапной проверки, отправьте его боту в этой "
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
        _cleanup_legacy_max_send_buttons()
        threading.Thread(target=background_parser, daemon=True).start()
        if PAYMENTS_ENABLED:
            threading.Thread(target=subscription_reminder_loop, daemon=True).start()
        print("✅ Запуск polling...")
        bot.infinity_polling(none_stop=True, timeout=60)
    except KeyboardInterrupt:
        print(" Остановлен пользователем")
        save_message_cache()
