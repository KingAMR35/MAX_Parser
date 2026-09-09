import re
import time
import requests
import os
import json
import hashlib
import shutil
import io
import base64
from urllib.parse import unquote, urlparse
import queue
import threading
from typing import List, Dict
from playwright.sync_api import sync_playwright

SESSIONS_DIR = "sessions"

SEEN_MESSAGES_FILE = "seen_messages.json"

PHOTO_CACHE_FILE = "seen_images.json"

os.makedirs(SESSIONS_DIR, exist_ok=True)

message_cache_by_chat = {}

photo_cache_by_chat = {}

#! ==========================================
#! 1. QR-АВТОРИЗАЦИЯ
#! ==========================================

def _is_logged_in(page) -> bool:
    try:
        page_content = page.content()

        if len(page_content) < 1000:
            return False

        current_url = page.url.lower()
        if 'web.max.ru' in current_url and current_url.count('/') <= 3:
            login_indicators = [
                "input[type='password']",
                "div[class*='qr' i]",
                "div[class*='QR' i]",
                "canvas[class*='qr' i]",
                "img[class*='qr' i]",
                "button:has-text('Войти')"
            ]

            for sel in login_indicators:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=500):
                        return False
                except:
                    continue

        return True

    except Exception as e:
        print(f"️ Ошибка проверки авторизации: {e}")
        return False

def _get_password_input(page):
    password_selectors = [
        "input[type='password']",
        "input[name='password']",
        "input[name='passwd']",
        "input[placeholder*='пароль' i]",
        "input[placeholder*='password' i]",
        "input[placeholder*='Password' i]",
        "input[autocomplete='current-password']",
        "input[autocomplete='current-password' i]"
    ]

    for sel in password_selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=1000):
                return el
        except:
            continue
    return None

def _click_submit_button(page):
    submit_selectors = [
        "button[type='submit']",
        "button:has-text('Войти')",
        "button:has-text('Подтвердить')",
        "button:has-text('Продолжить')",
        "button:has-text('Sign in')",
        "button:has-text('Submit')",
        "button:has-text('Continue')",
        "button:has-text('Login')",
        "[data-testid='submit-button']",
        "input[type='submit']"
    ]

    for sel in submit_selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=1000):
                el.click()
                return True
        except:
            continue
    try:
        page.keyboard.press("Enter")
        return True
    except:
        return False

class QRLoginSession:

    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.command_queue = queue.Queue()
        self.result_queue = queue.Queue()
        self.thread = None
        self.browser = None
        self.context = None
        self.page = None
        self.pw = None
        self.screenshot_bytes = None
        self.status = "init"

    def start(self):
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()
        self.command_queue.put(("init", None))

        try:
            result = self.result_queue.get(timeout=30)
            return result
        except queue.Empty:
            return ("error", "Таймаут инициализации браузера")

    def _worker(self):
        try:
            while True:
                cmd, args = self.command_queue.get()
                if cmd == "init":
                    self._do_init()

                elif cmd == "refresh":
                    self._do_refresh()

                elif cmd == "check":
                    self._do_check()

                elif cmd == "password":
                    self._do_enter_password(args)

                elif cmd == "close":
                    self._do_close()
                    break
        except Exception as e:
            print(f" Ошибка в рабочем потоке QR: {e}")
            try:
                self.result_queue.put(("error", str(e)))
            except:
                pass

    def _do_init(self):
        print(f"🔐 [Поток {self.chat_id}] Инициализация браузера...")
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-blink-features=AutomationControlled']
        )
        self.context = self.browser.new_context(viewport={'width': 1280, 'height': 800})
        self.page = self.context.new_page()
        self.page.goto("https://web.max.ru", timeout=30000)
        self.page.wait_for_timeout(5000)

        if _is_logged_in(self.page):
            session_file = f"sessions/{self.chat_id}.json"
            self.context.storage_state(path=session_file)
            self._do_close()
            self.result_queue.put(("already_logged_in", session_file))
            return

        self.screenshot_bytes = self.page.screenshot(full_page=True)
        self.status = "waiting"
        self.result_queue.put(("waiting", self.screenshot_bytes))

    def _do_refresh(self):
        if self.status not in ("waiting", "awaiting_password"):
            self.result_queue.put(("error", "Сессия не активна"))
            return

        self.page.reload()
        self.page.wait_for_timeout(4000)

        if _is_logged_in(self.page):
            session_file = f"sessions/{self.chat_id}.json"
            self.context.storage_state(path=session_file)
            self._do_close()
            self.result_queue.put(("success", session_file))
            return

        self.screenshot_bytes = self.page.screenshot(full_page=True)
        self.status = "waiting"
        self.result_queue.put(("waiting", self.screenshot_bytes))

    def _do_check(self):
        if self.status not in ("waiting", "awaiting_password"):
            self.result_queue.put(("error", "Сессия не активна"))
            return

        if _is_logged_in(self.page):
            session_file = f"sessions/{self.chat_id}.json"
            self.context.storage_state(path=session_file)
            self._do_close()
            self.result_queue.put(("success", session_file))
            return
        
        password_input = _get_password_input(self.page)
        if password_input:
            self.status = "awaiting_password"
            self.screenshot_bytes = self.page.screenshot(full_page=True)
            self.result_queue.put(("awaiting_password", self.screenshot_bytes))
            return
        self.result_queue.put(("waiting", None))

    def _do_enter_password(self, password: str):
        """Вводит пароль в поле и нажимает кнопку подтверждения"""
        if self.status != "awaiting_password":
            self.result_queue.put(("error", "Сейчас не требуется ввод пароля"))
            return
        try:
            password_input = _get_password_input(self.page)
            if not password_input:
                self.result_queue.put(("error", "Поле для ввода пароля не найдено"))
                return

            password_input.click()
            password_input.fill(password)
            self.page.wait_for_timeout(300)
            submit_clicked = _click_submit_button(self.page)
            if not submit_clicked:
                print(f"⚠️ [Поток {self.chat_id}] Кнопка не найдена, пробуем Enter")
                self.page.keyboard.press("Enter")

            self.page.wait_for_timeout(7000)
            current_url = self.page.url.lower()
            url_changed = 'auth' not in current_url and 'login' not in current_url
            logged_in = _is_logged_in(self.page)
            password_gone = _get_password_input(self.page) is None
            success_signals = sum([url_changed, logged_in, password_gone])
            if success_signals >= 2:
                session_file = f"sessions/{self.chat_id}.json"
                self.context.storage_state(path=session_file)
                self._do_close()
                self.result_queue.put(("success", session_file))
                return

            if not password_gone:
                print(f" [Поток {self.chat_id}] Поле пароля всё ещё видно — неверный пароль")
                self.screenshot_bytes = self.page.screenshot(full_page=True)
                self.status = "awaiting_password"
                self.result_queue.put(("wrong_password", self.screenshot_bytes))
                return

            self.screenshot_bytes = self.page.screenshot(full_page=True)
            print(f"️ [Поток {self.chat_id}] Неоднозначный результат (сигналов: {success_signals}/3)")
            self.status = "waiting"
            self.result_queue.put(("waiting", self.screenshot_bytes))
        except Exception as e:
            print(f"❌ [Поток {self.chat_id}] Ошибка ввода пароля: {e}")
            self.result_queue.put(("error", str(e)))

    def _do_close(self):
        try:
            if self.browser:
                self.browser.close()
            if self.pw:
                self.pw.stop()
        except Exception as e:
            print(f"️ Ошибка при закрытии браузера: {e}")
        finally:
            self.browser = None
            self.context = None
            self.page = None
            self.pw = None
            self.screenshot_bytes = None
            self.status = "closed"

    def refresh(self):
        self.command_queue.put(("refresh", None))
        try:
            return self.result_queue.get(timeout=30)
        except queue.Empty:
            return ("error", "Таймаут обновления")

    def check(self):
        self.command_queue.put(("check", None))
        try:
            return self.result_queue.get(timeout=15)
        except queue.Empty:
            return ("error", "Таймаут проверки")

    def enter_password(self, password: str):
        self.command_queue.put(("password", password))
        try:
            return self.result_queue.get(timeout=20)
        except queue.Empty:
            return ("error", "Таймаут ввода пароля")

    def close(self):
        self.command_queue.put(("close", None))
        try:
            self.result_queue.get(timeout=5)
        except:
            pass

def cancel_qr_login(login_session):
    try:
        if hasattr(login_session, 'close'):
            login_session.close()
    except:
        pass

#! ==========================================
#! 2. КЭШИ СООБЩЕНИЙ И МЕДИА (по чатам)
#! ==========================================

def load_message_cache():
    global message_cache_by_chat
    message_cache_by_chat.clear()
    if os.path.exists(SEEN_MESSAGES_FILE):
        try:
            with open(SEEN_MESSAGES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for chat_id_str, hashes in data.get('by_chat', {}).items():
                    message_cache_by_chat[int(chat_id_str)] = set(hashes)
                message_cache_by_chat[0] = set(data.get('message_hashes', []))
        except Exception:
            message_cache_by_chat = {}

def save_message_cache():
    try:
        with open(SEEN_MESSAGES_FILE, "w", encoding="utf-8") as f:
            json.dump({
                'by_chat': {str(k): list(v) for k, v in message_cache_by_chat.items()}
            }, f)
    except Exception:
        pass

def load_photo_cache():
    global photo_cache_by_chat
    photo_cache_by_chat.clear()
    if os.path.exists(PHOTO_CACHE_FILE):
        try:
            with open(PHOTO_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for chat_id_str, hashes in data.get('by_chat', {}).items():
                    photo_cache_by_chat[int(chat_id_str)] = set(hashes)
                photo_cache_by_chat[0] = set(data.get('photo_hashes', []))
        except Exception:
            photo_cache_by_chat = {}

def save_photo_cache():
    try:
        with open(PHOTO_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                'by_chat': {str(k): list(v) for k, v in photo_cache_by_chat.items()}
            }, f)
    except Exception:
        pass

def normalize_for_hash(text: str) -> str:
    if not text:
        return ""
    text = text.replace("👤", "").strip()
    return " ".join(text.split())

def get_message_hash(post: dict) -> str:
    source_id = str(post.get("source_id") or "").strip()
    if source_id:
        return hashlib.md5(f"max-message:{source_id}".encode("utf-8")).hexdigest()

    name = normalize_for_hash(post.get("name", ""))
    text = normalize_for_hash(post.get("text", ""))
    msg_time = normalize_for_hash(post.get("time", ""))
    media = post.get("media_files", []) or []
    media_hashes = []
    for item in media:
        value = item.get("hash") or item.get("content_hash") or item.get("url") or ""
        if value:
            media_hashes.append(str(value))
    content = "|".join([name, text, msg_time, "|".join(sorted(media_hashes))])
    return hashlib.md5(content.encode("utf-8")).hexdigest()

def get_media_hash(data=None, url=None) -> str:
    if isinstance(data, (bytes, bytearray)):
        return hashlib.md5(bytes(data)).hexdigest()
    if isinstance(data, str):
        return hashlib.md5(data.encode("utf-8")).hexdigest()
    if url is not None:
        return hashlib.md5(str(url).encode("utf-8")).hexdigest()
    return hashlib.md5(str(data).encode("utf-8")).hexdigest()

def is_new_message(post: dict, chat_id: int = 0) -> bool:
    if not isinstance(chat_id, int):
        print(f"⚠️ Некорректный chat_id в is_new_message: {chat_id}")
        return False
    msg_hash = get_message_hash(post)
    return msg_hash not in message_cache_by_chat.get(chat_id, set())

def mark_message_seen(post: dict, chat_id: int = 0) -> None:
    if chat_id not in message_cache_by_chat:
        message_cache_by_chat[chat_id] = set()
    msg_hash = get_message_hash(post)
    if msg_hash:
        message_cache_by_chat[chat_id].add(msg_hash)

def is_new_media(data=None, chat_id: int = 0, url=None, media_hash=None) -> bool:
    if not isinstance(chat_id, int):
        print(f"⚠️ Некорректный chat_id в is_new_media: {chat_id}")
        return False
    if media_hash is None:
        media_hash = get_media_hash(data=data, url=url)
    return media_hash not in photo_cache_by_chat.get(chat_id, set())

def mark_media_seen(data=None, chat_id: int = 0, url=None, media_hash=None) -> None:
    if chat_id not in photo_cache_by_chat:
        photo_cache_by_chat[chat_id] = set()
    if media_hash is None:
        media_hash = get_media_hash(data=data, url=url)
    if media_hash:
        photo_cache_by_chat[chat_id].add(media_hash)

def clear_chat_cache(chat_id: int):
    message_cache_by_chat.pop(chat_id, None)
    photo_cache_by_chat.pop(chat_id, None)
    save_message_cache()
    save_photo_cache()

def clear_all_caches():
    global message_cache_by_chat, photo_cache_by_chat
    message_cache_by_chat.clear()
    photo_cache_by_chat.clear()
    for filename in [SEEN_MESSAGES_FILE, PHOTO_CACHE_FILE]:
        if os.path.exists(filename):
            os.remove(filename)

#! ==========================================
#! 3. ЗАГРУЗКА МЕДИА
#! ==========================================

def _browser_cookies_as_dict(context) -> dict:
    try:
        return {cookie["name"]: cookie["value"] for cookie in context.cookies()}
    except Exception:
        return {}

def _looks_like_image(data: bytes, content_type: str = "") -> bool:
    if not data:
        return False
    ct = (content_type or "").lower()
    if ct.startswith("image/"):
        return True
    signatures = (
        b"\xff\xd8\xff",      #! JPEG
        b"\x89PNG\r\n\x1a\n", #! PNG
        b"RIFF",              #! WEBP
        b"GIF8",              #! GIF
        b"BM",                #! BMP
        b"<svg",
        b"<?xml",
    )
    return any(data.startswith(signature) for signature in signatures)

def get_media_bytes(
    url: str,
    media_type: str = "image",
    chat_id: int = 0,
    context=None,
    page=None,
) -> dict | None:
    if not isinstance(chat_id, int):
        print(f"⚠️ Некорректный chat_id в get_media_bytes: {chat_id}")
        return None
    if not url:
        return None

    for attempt in range(1, 4):
        try:
            if url.startswith("data:"):
                header, encoded = url.split(",", 1)
                data = base64.b64decode(encoded)
                content_type = header[5:].split(";", 1)[0] if header.startswith("data:") else ""
            elif url.startswith("blob:"):
                if page is None:
                    print(f"[{chat_id}] ⚠️ blob URL требует активную страницу MAX")
                    return None
                result = page.evaluate(
                    """
                    async url => {
                        const response = await fetch(url, {credentials: "include"});
                        if (!response.ok) throw new Error(`HTTP ${response.status}`);
                        const blob = await response.blob();
                        const buffer = await blob.arrayBuffer();
                        return {
                            bytes: Array.from(new Uint8Array(buffer)),
                            contentType: blob.type || ""
                        };
                    }
                    """,
                    url,
                )
                data = bytes(result.get("bytes", []))
                content_type = result.get("contentType", "")
            elif url.startswith(("http://", "https://")):
                cookies = _browser_cookies_as_dict(context) if context is not None else {}
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/140.0 Safari/537.36"
                    ),
                    "Referer": "https://web.max.ru/",
                    "Accept": "image/avif,image/webp,image/apng,image/jpeg,image/png,image/*,*/*;q=0.8",
                }
                response = requests.get(
                    url,
                    headers=headers,
                    cookies=cookies,
                    timeout=(15, 60),
                )
                response.raise_for_status()
                data = response.content
                content_type = response.headers.get("content-type", "")
            else:
                print(f"[{chat_id}] ⚠️ Неизвестный URL медиа: {url[:120]}")
                return None

            if not data:
                raise RuntimeError("MAX вернул пустой файл")

            if media_type == "image":
                if len(data) < 500:
                    print(f"[{chat_id}] ⛔ Слишком маленькое изображение: {len(data)} байт")
                    return None
                if not _looks_like_image(data, content_type):
                    raise RuntimeError(
                        f"ответ не похож на изображение: content-type={content_type!r}, size={len(data)}"
                    )

            media_hash = get_media_hash(data=data)

            return {
                "url": url,
                "bytes": data,
                "type": media_type,
                "hash": media_hash,
                "content_type": content_type,
            }

        except Exception as error:
            print(f"[{chat_id}] ⚠️ Попытка {attempt}/3 скачать {media_type}: {error}")
            if attempt < 3:
                time.sleep(1.5)

    print(f"[{chat_id}] ❌ Не удалось скачать {media_type}")
    return None

def is_human_message(msg: dict) -> bool:
    text = (msg.get("text") or "").strip().lower()
    has_media = bool(msg.get("images") or msg.get("documents"))

    if not text and not has_media:
        return False
    if any(phrase in text for phrase in [
        "теперь в max", "now on max", "напишите что-нибудь", "write something",
        "сферум", "удалил", "удалила", "изменил", "изменила", "вошел", "вошла",
        "покинул", "покинула", "добавил", "добавила", "исключил", "исключила",
        "пригласил", "пригласила", "системное", "создал чат", "создала чат",
        "вернулся", "вернулась", "скачать видео", "ютуб", "тикток", "подарок", "исчезнет",
    ]):
        return False

    if has_media:
        return len(text) < 2000

    return 5 < len(text) < 2000

def get_parse_debug_screenshot(chat_id: int) -> str:
    debug_path = f"{SESSIONS_DIR}/parse_debug_{chat_id}.png"
    return debug_path if os.path.exists(debug_path) else None

def clear_parse_debug_screenshot(chat_id: int):
    debug_path = f"{SESSIONS_DIR}/parse_debug_{chat_id}.png"
    if os.path.exists(debug_path):
        try:
            os.remove(debug_path)
        except OSError:
            pass

def get_chat_cache_count(chat_id: int) -> int:
    return len(message_cache_by_chat.get(chat_id, set()))
def _filename_from_headers(headers: dict, fallback: str = "") -> str:
    """Extract a filename from Content-Disposition without writing anything to disk."""
    disposition = (headers or {}).get("content-disposition", "") or ""

    match = re.search(r"filename\*\s*=\s*(?:UTF-8''|utf-8'')?([^;]+)", disposition, re.I)
    if match:
        value = match.group(1).strip().strip('"')
        return unquote(value)

    match = re.search(r'filename\s*=\s*"([^"]+)"', disposition, re.I)
    if not match:
        match = re.search(r"filename\s*=\s*([^;]+)", disposition, re.I)
    if match:
        return unquote(match.group(1).strip().strip('"'))

    return fallback

def _normalize_document_filename(filename: str, url: str = "", content_type: str = "") -> str:
    filename = unquote((filename or "").strip())
    filename = filename.replace("\\", "_").replace("/", "_")
    filename = re.sub(r"[\r\n\t]+", " ", filename).strip(" .")

    if not filename or filename.lower() in {"document", "document.file", "document.blob"}:
        try:
            candidate = unquote(urlparse(url).path.rsplit("/", 1)[-1])
            if candidate and "." in candidate:
                filename = candidate
        except Exception:
            pass

    if not filename:
        ct = (content_type or "").lower()
        if "pdf" in ct:
            filename = "document.pdf"
        elif "word" in ct:
            filename = "document.docx"
        elif "spreadsheet" in ct or "excel" in ct:
            filename = "document.xlsx"
        else:
            filename = "document.bin"

    return filename[:240]

def _download_document_to_memory(page, context, url: str, filename: str = "", chat_id: int = 0) -> dict | None:
    """
    Downloads a MAX attachment directly into RAM.

    - blob: URLs are read inside the authenticated MAX page via fetch + FileReader.
    - http(s): URLs are fetched through browser_context.request, which shares the
      browser context's cookie storage with the logged-in MAX session.
    - No project/download file is created.
    """
    if not url:
        return None

    try:
        if url.startswith("data:"):
            header, encoded = url.split(",", 1)
            file_bytes = base64.b64decode(encoded)
            content_type = header[5:].split(";", 1)[0] if header.startswith("data:") else ""
            final_name = _normalize_document_filename(filename, url, content_type)

        elif url.startswith("blob:"):
            data_url = page.evaluate(
                r"""
                async (url) => {
                    const response = await fetch(url, { credentials: 'include' });
                    if (!response.ok) {
                        throw new Error(`HTTP ${response.status}`);
                    }
                    const blob = await response.blob();
                    return await new Promise((resolve, reject) => {
                        const reader = new FileReader();
                        reader.onload = () => resolve(reader.result);
                        reader.onerror = () => reject(reader.error || new Error('FileReader failed'));
                        reader.readAsDataURL(blob);
                    });
                }
                """,
                url,
            )
            if not data_url or "," not in data_url:
                raise RuntimeError("blob вернул пустые данные")

            meta, encoded = data_url.split(",", 1)
            file_bytes = base64.b64decode(encoded)
            content_type = meta[5:].split(";", 1)[0] if meta.startswith("data:") else ""
            final_name = _normalize_document_filename(filename, url, content_type)

        elif url.startswith(("http://", "https://")):
            response = context.request.get(url, timeout=60_000, fail_on_status_code=False)
            if not response.ok:
                raise RuntimeError(f"HTTP {response.status}: {response.status_text}")

            file_bytes = response.body()
            headers = response.headers
            content_type = headers.get("content-type", "")
            header_name = _filename_from_headers(headers, filename)
            final_name = _normalize_document_filename(header_name, url, content_type)
        else:
            print(f"[{chat_id}] ⚠️ Неизвестный URL документа: {url[:120]}")
            return None

        if not file_bytes:
            raise RuntimeError("получен пустой файл")

        return {
            "url": url,
            "bytes": file_bytes,
            "type": "document",
            "filename": final_name,
        }

    except Exception as e:
        print(f"[{chat_id}] ❌ Не удалось получить документ в память: {e}")
        return None

def _download_document_from_message_card(page, context, msg: dict, chat_id: int = 0) -> dict | None:
    """Find the visible MAX file card and obtain its attachment without saving it to disk."""
    dom_id = (msg.get("dom_id") or "").strip()
    if not dom_id:
        return None

    filename_hint = ""
    text = msg.get("text", "") or ""
    m = re.search(
        r"([^<>:\"/\\|?*]{1,220}\.(?:pdf|doc|docx|xls|xlsx|ppt|pptx|zip|rar|txt|rtf|odt|ods|odp))",
        text,
        re.I,
    )
    if m:
        filename_hint = m.group(1).strip()
        filename_hint = re.sub(
            r"^(?:PDF|DOCX?|XLSX?|PPTX?|ZIP|RAR|TXT|RTF|ODT|ODS|ODP)\s+",
            "",
            filename_hint,
            flags=re.I,
        ).strip()

    try:
        click_id = (msg.get("download_click_id") or "").strip()
        if click_id:
            candidate = page.locator(f'[data-max-parser-download-id="{click_id}"]').first
            if candidate.count() > 0:
                try:
                    with page.expect_download(timeout=7000) as download_info:
                        candidate.click(timeout=4000)
                    download = download_info.value
                    download_url = download.url
                    suggested = download.suggested_filename or filename_hint or "document.pdf"
                    if download_url:
                        media = _download_document_to_memory(
                            page=page,
                            context=context,
                            url=download_url,
                            filename=filename_hint or suggested,
                            chat_id=chat_id,
                        )
                        if media and media.get("bytes"):
                            return media
                except Exception as e:
                    print(f"[{chat_id}] ℹ️ Не удалось получить download event от кнопки: {e}")

                try:
                    attrs = candidate.evaluate(
                        """el => ({
                            href: el.getAttribute('href') || '',
                            url: el.getAttribute('data-url') || el.getAttribute('data-href') ||
                                el.getAttribute('data-download-url') || el.getAttribute('data-file-url') ||
                                el.getAttribute('data-src') || el.getAttribute('data-link') || ''
                        })"""
                    )
                    for url in [attrs.get('href', ''), attrs.get('url', '')]:
                        if url:
                            media = _download_document_to_memory(
                                page=page,
                                context=context,
                                url=url,
                                filename=filename_hint or msg.get('download_click_filename', ''),
                                chat_id=chat_id,
                            )
                            if media and media.get("bytes"):
                                return media
                except Exception:
                    pass

        card = page.locator(f'[data-max-parser-message-id="{dom_id}"]').first
        if card.count() == 0:
            return None

        download_candidates = card.locator(
            'a, button, [role="button"], [data-testid*="download" i], '
            '[aria-label*="скачать" i], [aria-label*="download" i], '
            '[class*="download" i]'
        )

        candidate_count = min(download_candidates.count(), 20)
        for i in range(candidate_count - 1, -1, -1):
            candidate = download_candidates.nth(i)
            try:
                if not candidate.is_visible(timeout=300):
                    continue
            except Exception:
                continue

            try:
                with page.expect_download(timeout=5000) as download_info:
                    candidate.click(timeout=3000)
                download = download_info.value
                download_url = download.url
                suggested = download.suggested_filename or filename_hint or "document.pdf"
                if download_url:
                    media = _download_document_to_memory(
                        page=page,
                        context=context,
                        url=download_url,
                        filename=filename_hint or suggested,
                        chat_id=chat_id,
                    )
                    if media and media.get("bytes"):
                        return media
            except Exception as e:
                print(f"[{chat_id}] ℹ️ Клик по элементу вложения без download event: {e}")

        candidates = card.locator(
            '[href], [src], [data-url], [data-href], [data-download-url], '
            '[data-file-url], [data-src], [data-link]'
        )
        values = candidates.evaluate_all(
            """
            els => els.map(el => ({
                url: el.getAttribute('href') || el.getAttribute('data-url') ||
                    el.getAttribute('data-href') || el.getAttribute('data-download-url') ||
                    el.getAttribute('data-file-url') || el.getAttribute('data-src') ||
                    el.getAttribute('data-link') || el.src || '',
                download: el.getAttribute('download') || '',
                text: el.innerText || el.textContent || ''
            })).filter(x => x.url)
            """
        )

        for item in values:
            url = (item.get("url") or "").strip()
            hint = " ".join([filename_hint, item.get("download", ""), item.get("text", "")]).strip()
            if not re.search(r"\.(?:pdf|doc|docx|xls|xlsx|ppt|pptx|zip|rar|txt|rtf|odt|ods|odp)(?:[?#]|$)", hint, re.I) and not re.search(
                r"скачать|download|pdf|document|file|attachment", hint, re.I
            ):
                continue
            media = _download_document_to_memory(
                page=page,
                context=context,
                url=url,
                filename=filename_hint or item.get("download", "") or item.get("text", ""),
                chat_id=chat_id,
            )
            if media and media.get("bytes"):
                return media

        try:
            with page.expect_download(timeout=5000) as download_info:
                card.click(timeout=3000)
            download = download_info.value
            download_url = download.url
            suggested = download.suggested_filename or filename_hint or "document.pdf"
            if download_url:
                return _download_document_to_memory(
                    page=page,
                    context=context,
                    url=download_url,
                    filename=filename_hint or suggested,
                    chat_id=chat_id,
                )
        except Exception as e:
            print(f"[{chat_id}] ℹ️ Карточка файла не дала download event: {e}")

    except Exception as e:
        print(f"[{chat_id}] ❌ Ошибка поиска карточки документа: {e}")

    return None

def parse_max_group_media(group_url: str, chat_id: int) -> List[Dict]:
    """Парсит чат MAX через точные карточки .item[data-index]."""
    if not isinstance(chat_id, int):
        raise Exception(f"Некорректный chat_id: {chat_id} (должен быть int)")

    session_file = f"{SESSIONS_DIR}/{chat_id}.json"
    if not os.path.exists(session_file):
        raise Exception("NO_SESSION")

    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        storage_state=session_file,
        viewport={"width": 1280, "height": 800},
    )
    page = context.new_page()

    try:
        print(f"[{chat_id}] Переход на главную MAX...")
        page.goto("https://web.max.ru", timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        if not _is_logged_in(page):
            debug_path = f"{SESSIONS_DIR}/parse_debug_{chat_id}.png"
            page.screenshot(path=debug_path, full_page=True)
            raise Exception(f"SESSION_EXPIRED|{debug_path}")

        page.goto(group_url, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)

        current_url = page.url
        if group_url not in current_url:
            debug_path = f"{SESSIONS_DIR}/wrong_chat_debug_{chat_id}.png"
            page.screenshot(path=debug_path, full_page=True)
            raise Exception(f"WRONG_CHAT_REDIRECT|{debug_path}")

        if len(page.content()) < 1000:
            debug_path = f"{SESSIONS_DIR}/parse_debug_{chat_id}.png"
            page.screenshot(path=debug_path, full_page=True)
            raise Exception(f"EMPTY_PAGE|{debug_path}")

        print(f"[{chat_id}] Ожидание загрузки изображений...")
        for _ in range(40):
            page.keyboard.press("End")
            page.wait_for_timeout(200)

        page.wait_for_timeout(2000)

        raw_messages = page.evaluate(
            r"""
            () => {
                const results = [];
                const rows = Array.from(document.querySelectorAll('.item[data-index]'));

                function clean(value) {
                    return String(value || "")
                        .replace(/[\u200B\u200C\u200D\uFEFF]/g, "")
                        .replace(/\s+/g, " ")
                        .trim();
                }

                function safeAttr(el, attr) {
                    try {
                        const value = el.getAttribute(attr);
                        return typeof value === "string" ? value : "";
                    } catch (e) {
                        return "";
                    }
                }

                function getCandidateUrl(el) {
                    if (!el) return "";
                    const attrs = [
                        "href", "src", "currentSrc", "data-url", "data-href",
                        "data-download-url", "data-file-url", "data-src", "data-link"
                    ];

                    for (const attr of attrs) {
                        let value = "";
                        try {
                            value = attr === "currentSrc"
                                ? (typeof el.currentSrc === "string" ? el.currentSrc : "")
                                : safeAttr(el, attr);
                        } catch (e) {
                            value = "";
                        }

                        if (typeof value !== "string") continue;
                        value = value.trim();

                        if (
                            value.startsWith("http://") ||
                            value.startsWith("https://") ||
                            value.startsWith("blob:")
                        ) {
                            return value;
                        }
                    }

                    // MAX иногда отдаёт href как нестандартный объект.
                    const propertyHref = typeof el.href === "string" ? el.href : "";
                    if (
                        propertyHref.startsWith("http://") ||
                        propertyHref.startsWith("https://") ||
                        propertyHref.startsWith("blob:")
                    ) {
                        return propertyHref;
                    }

                    return "";
                }

                function getReplyBlock(row) {
                    const selectors = [
                        ".link button.mark",
                        '[class*="reply" i]',
                        '[class*="quoted" i]',
                        '[class*="quote" i]',
                        '[data-testid*="reply" i]',
                        '[data-testid*="quote" i]'
                    ];

                    for (const selector of selectors) {
                        try {
                            const element = row.querySelector(selector);
                            if (element) return element;
                        } catch (e) {}
                    }
                    return null;
                }

                function getReplyText(block) {
                    if (!block) return "";
                    return clean(block.textContent);
                }

                function getAuthor(row) {
                    // ВАЖНО: только реальные header/name-селекторы.
                    // Никаких широких [class*=name], которые могли принять текст сообщения за имя.
                    const selectors = [
                        ".bubbleContent .header .name .text",
                        ".bubbleContent .header .name",
                        ".header .name .text",
                        ".header .name",
                        ".name .text"
                    ];

                    for (const selector of selectors) {
                        try {
                            const element = row.querySelector(selector);
                            if (!element) continue;

                            let value = clean(element.textContent);
                            value = value.replace(/^👤\s*/u, "").trim();

                            if (!value) continue;
                            if (/^(сегодня|вчера|today|yesterday)$/i.test(value)) continue;
                            if (/\b\d{1,2}:\d{2}\b/.test(value)) continue;

                            return value;
                        } catch (e) {}
                    }

                    return "";
                }

                function getRole(row, author) {
                    if (!author) return "";
                    try {
                        const authorNode = row.querySelector(
                            ".bubbleContent .header .name .text, " +
                            ".bubbleContent .header .name, " +
                            ".header .name .text, " +
                            ".header .name"
                        );
                        if (!authorNode || !authorNode.parentElement) return "";

                        const parentText = clean(authorNode.parentElement.textContent);
                        if (!parentText.toLowerCase().startsWith(author.toLowerCase())) return "";

                        const role = parentText.substring(author.length).trim();
                        return role.length <= 60 ? role : "";
                    } catch (e) {
                        return "";
                    }
                }

                function getTime(row) {
                    const selectors = [
                        ".bubbleContent .meta .text",
                        ".meta .text",
                        '[class*="time" i]'
                    ];

                    for (const selector of selectors) {
                        try {
                            const element = row.querySelector(selector);
                            if (!element) continue;

                            const value = clean(element.textContent);
                            const match = value.match(/\b\d{1,2}:\d{2}\s*(?:AM|PM)?\b/i);
                            if (match) return match[0].trim();
                        } catch (e) {}
                    }

                    return "";
                }

                function getOwnText(row) {
                    // Самый надёжный вариант: только непосредственный .text внутри bubbleContent.
                    try {
                        const direct = row.querySelector(".bubbleContent > .text");
                        if (direct) return clean(direct.textContent);
                    } catch (e) {}

                    try {
                        const bubble = row.querySelector(".bubbleContent");
                        if (!bubble) return "";

                        const clone = bubble.cloneNode(true);
                        [
                            ".header", ".meta", ".link", "button.mark",
                            '[class*="reply" i]', '[class*="quoted" i]',
                            '[class*="quote" i]'
                        ].forEach(selector => {
                            try {
                                clone.querySelectorAll(selector).forEach(el => el.remove());
                            } catch (e) {}
                        });

                        return clean(clone.textContent);
                    } catch (e) {
                        return "";
                    }
                }

                function getImageUrls(row) {
                    const images = [];
                    const seen = new Set();

                    try {
                        row.querySelectorAll("img").forEach(img => {
                            let src = "";
                            try {
                                src =
                                    (typeof img.currentSrc === "string" && img.currentSrc) ||
                                    safeAttr(img, "src") ||
                                    safeAttr(img, "data-src") ||
                                    safeAttr(img, "data-original") ||
                                    safeAttr(img, "data-lazy-src") ||
                                    "";
                            } catch (e) {}

                            if (!src || src.startsWith("data:")) return;

                            const lower = src.toLowerCase();
                            if (
                                lower.includes("avatar") ||
                                lower.includes("userpic") ||
                                lower.includes("profile") ||
                                lower.includes("icon") ||
                                lower.includes("emoji") ||
                                lower.includes("sticker") ||
                                lower.includes("spacer") ||
                                lower.includes("blank")
                            ) return;

                            const rect = img.getBoundingClientRect();
                            const width = Math.max(img.naturalWidth || 0, rect.width || 0);
                            const height = Math.max(img.naturalHeight || 0, rect.height || 0);

                            if (width < 40 || height < 40) return;

                            if (!seen.has(src)) {
                                seen.add(src);
                                images.push(src);
                            }
                        });
                    } catch (e) {}

                    // Фотографии в MAX могут быть background-image.
                    try {
                        row.querySelectorAll("div, span, a, picture").forEach(el => {
                            try {
                                const bg = getComputedStyle(el).backgroundImage;
                                const match = bg && bg.match(/url\(["']?(.*?)["']?\)/);
                                if (!match) return;

                                const src = match[1];
                                if (!src || src.startsWith("data:") || src.length < 30) return;

                                const lower = src.toLowerCase();
                                if (
                                    lower.includes("avatar") ||
                                    lower.includes("userpic") ||
                                    lower.includes("profile") ||
                                    lower.includes("icon") ||
                                    lower.includes("emoji") ||
                                    lower.includes("sticker")
                                ) return;

                                if (!seen.has(src)) {
                                    seen.add(src);
                                    images.push(src);
                                }
                            } catch (e) {}
                        });
                    } catch (e) {}

                    return images;
                }

                function extractFilename(text) {
                    const match = clean(text).match(
                        /([^<>:"/\\|?*]{1,220}\.(?:pdf|doc|docx|xls|xlsx|ppt|pptx|zip|rar|txt|rtf|odt|ods|odp))/i
                    );
                    if (!match) return "";
                    return match[1].trim().replace(
                        /^(?:PDF|DOCX?|XLSX?|PPTX?|ZIP|RAR|TXT|RTF|ODT|ODS|ODP)\s+/i,
                        ""
                    ).trim();
                }

                function getFileNodes(row) {
                    return row.querySelectorAll(
                        "a[href], button, [role='button'], " +
                        "[data-url], [data-href], [data-download-url], " +
                        "[data-file-url], [data-src], [data-link]"
                    );
                }

                rows.forEach((row, index) => {
                    try {
                        const sourceId =
                            safeAttr(row, "data-message-id") ||
                            safeAttr(row, "data-mid") ||
                            safeAttr(row, "data-id") ||
                            safeAttr(row, "data-index") ||
                            String(index);

                        const block = row.querySelector(".block");
                        if (!block) return;

                        const bubble = block.querySelector(".bubbleContent");
                        if (!bubble) return;

                        const name = getAuthor(row);
                        const role = getRole(row, name);
                        let text = getOwnText(row);
                        const time = getTime(row);
                        const replyBlock = getReplyBlock(row);
                        const replyText = getReplyText(replyBlock);
                        const images = getImageUrls(block);
                        const documents = [];

                        const fullRowText = clean(row.innerText);
                        const filenameHint = extractFilename(fullRowText);

                        getFileNodes(block).forEach((el, nodeIndex) => {
                            try {
                                const href = getCandidateUrl(el);
                                const downloadAttr = safeAttr(el, "download");
                                const ownText = clean(el.textContent);
                                const aria = safeAttr(el, "aria-label");
                                const title = safeAttr(el, "title");
                                const className = typeof el.className === "string" ? el.className : "";
                                const hint = [ownText, aria, title, className, fullRowText].join(" ");

                                const looksLikeFile =
                                    Boolean(downloadAttr) ||
                                    /\.(pdf|docx?|xlsx?|pptx?|zip|rar|txt|rtf|odt|ods|odp)(?:[?#\s]|$)/i.test(hint) ||
                                    /скачать|download|файл|file|document|attachment/i.test(hint);

                                if (!looksLikeFile) return;

                                let filename = filenameHint || downloadAttr || "document.file";
                                if (href) {
                                    const pathName = decodeURIComponent(
                                        new URL(href, location.href).pathname.split("/").pop() || ""
                                    );
                                    if (/\.(pdf|docx?|xlsx?|pptx?|zip|rar|txt|rtf|odt|ods|odp)$/i.test(pathName)) {
                                        filename = pathName;
                                    }
                                }

                                if (!href && /скачать|download/i.test(hint)) {
                                    const clickId = `max-parser-download-${index}-${nodeIndex}`;
                                    try {
                                        el.setAttribute("data-max-parser-download-id", clickId);
                                    } catch (e) {}
                                }

                                if (href && !/\.(jpg|jpeg|png|gif|webp|svg|bmp)(?:[?#\s]|$)/i.test(href)) {
                                    if (!documents.some(doc => doc.url === href)) {
                                        documents.push({url: href, filename});
                                    }
                                }
                            } catch (e) {}
                        });

                        // Ключевой fallback: если у карточки нет URL, Python будет нажимать "Скачать".
                        let downloadClickId = "";
                        const clickControls = block.querySelectorAll(
                            "a, button, [role='button'], span, div"
                        );
                        for (let nodeIndex = 0; nodeIndex < clickControls.length; nodeIndex++) {
                            const el = clickControls[nodeIndex];
                            const ownText = clean(el.textContent);
                            const aria = safeAttr(el, "aria-label");
                            const title = safeAttr(el, "title");
                            const cls = typeof el.className === "string" ? el.className : "";

                            if (
                                /^(скачать|download)$/i.test(ownText) ||
                                /скачать|download/i.test(aria + " " + title + " " + cls)
                            ) {
                                downloadClickId = `max-parser-download-${index}-final-${nodeIndex}`;
                                try {
                                    el.setAttribute("data-max-parser-download-id", downloadClickId);
                                } catch (e) {}
                                break;
                            }
                        }

                        const hasSomething = Boolean(text || images.length || documents.length || filenameHint || downloadClickId);
                        if (!hasSomething) return;

                        // Защита от ситуации, когда MAX случайно записал текст сообщения в поле имени.
                        let finalName = clean(name);
                        if (
                            !finalName ||
                            finalName.toLowerCase() === text.toLowerCase() ||
                            finalName.toLowerCase() === fullRowText.toLowerCase() ||
                            /^(сегодня|вчера|today|yesterday)\b/i.test(finalName) ||
                            /\b\d{1,2}:\d{2}\b/.test(finalName)
                        ) {
                            finalName = "";
                        }

                        results.push({
                            idx: index,
                            source_id: String(sourceId),
                            dom_id: `max-parser-message-${index}`,
                            download_click_id: downloadClickId,
                            download_click_filename: filenameHint,
                            name: finalName || "Аноним",
                            text,
                            time,
                            images,
                            documents,
                            is_reply: Boolean(replyBlock),
                            reply_text: replyText,
                        });

                        try {
                            row.setAttribute("data-max-parser-message-id", `max-parser-message-${index}`);
                        } catch (e) {}
                    } catch (e) {
                        console.error("MAX Parser: ошибка обработки .item:", e);
                    }
                });

                return results;
            }
            """
        )

        cleaned = []
        seen_ids = set()

        for msg in raw_messages:
            text = " ".join((msg.get("text") or "").split()).strip()
            name = " ".join((msg.get("name") or "").split()).strip()
            source_id = str(msg.get("source_id") or "").strip()
            reply_text = " ".join((msg.get("reply_text") or "").split()).strip()

            if re.match(
                r"^(today|yesterday|сегодня|вчера|new messages?|новые сообщения)\b",
                name,
                re.I,
            ):
                name = ""

            if name and text and name.casefold() == text.casefold():
                name = ""

            if name and text:
                prefix = name + " "
                while text.casefold().startswith(prefix.casefold()):
                    text = text[len(prefix):].strip()

            if reply_text and text.casefold().startswith(reply_text.casefold()):
                text = text[len(reply_text):].strip()

            msg["name"] = name or "Аноним"
            msg["text"] = text
            msg["source_id"] = source_id

            if not is_human_message(msg):
                continue

            if source_id:
                if source_id in seen_ids:
                    continue
                seen_ids.add(source_id)

            cleaned.append(msg)

        last_10 = cleaned[-10:]
        print(f"[{chat_id}] После фильтрации: {len(last_10)} сообщений")

        human_posts = []

        for msg in last_10:
            media_files = []

            for img_url in msg.get("images", []):
                media = get_media_bytes(
                    img_url,
                    media_type="image",
                    chat_id=chat_id,
                    context=context,
                    page=page,
                )
                if media and media.get("bytes"):
                    media_files.append(media)

            for doc in msg.get("documents", []):
                doc_url = (doc.get("url") or "").strip()
                if not doc_url:
                    continue

                media = _download_document_to_memory(
                    page=page,
                    context=context,
                    url=doc_url,
                    filename=doc.get("filename", ""),
                    chat_id=chat_id,
                )
                if media and media.get("bytes"):
                    media_files.append(media)

            if not any(m.get("type") == "document" for m in media_files):
                has_file_hint = bool(
                    re.search(
                        r"\.(?:pdf|doc|docx|xls|xlsx|ppt|pptx|zip|rar|txt|rtf|odt|ods|odp)\b|скачать|download|\bpdf\b",
                        msg.get("text", "") or "",
                        re.I,
                    )
                    or msg.get("download_click_id")
                    or msg.get("download_click_filename")
                )

                if has_file_hint:
                    media = _download_document_from_message_card(
                        page=page,
                        context=context,
                        msg=msg,
                        chat_id=chat_id,
                    )
                    if media and media.get("bytes"):
                        media_files.append(media)

            unique_media = []
            seen_media_hashes = set()
            for media in media_files:
                data = media.get("bytes")
                if not isinstance(data, (bytes, bytearray)) or not data:
                    continue
                media_hash = get_media_hash(data=data)
                if media_hash in seen_media_hashes:
                    continue
                seen_media_hashes.add(media_hash)
                media["hash"] = media_hash
                unique_media.append(media)

            human_posts.append({
                "source_id": msg.get("source_id", ""),
                "name": msg.get("name", "Аноним"),
                "text": msg.get("text", ""),
                "time": msg.get("time", ""),
                "media_files": unique_media,
            })

        return human_posts

    except Exception:
        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
        raise
    finally:
        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
