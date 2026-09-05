import re
import time
import requests
import os
import json
import hashlib
import shutil
import io
import queue
import threading
from typing import List, Dict
from playwright.sync_api import sync_playwright

SESSIONS_DIR = "sessions"
SEEN_MESSAGES_FILE = "seen_messages.json"
PHOTO_CACHE_FILE = "seen_images.json"

os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs("downloads", exist_ok=True)

message_cache_by_chat = {}
photo_cache_by_chat = {}


#! ==========================================
#! 1. QR-АВТОРИЗАЦИЯ
#! ==========================================

def _is_logged_in(page) -> bool:
    """
    Проверяет, авторизован ли пользователь.
    Логика: если страница содержит контент и нет элементов логина — авторизован.
    """
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
    """Ищет поле для ввода пароля на странице"""
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
    """Нажимает кнопку подтверждения/входа после ввода пароля"""
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
    """
    Управляет браузером в выделенном потоке для QR-логина.
    Скриншоты хранятся в памяти (bytes), не на диске.
    """

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
    """Отменяет процесс входа и закрывает браузер"""
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
    text = text.replace('👤', '').strip()
    return " ".join(text.split())


def get_message_hash(post: dict) -> str:
    clean_text = normalize_for_hash(post.get('text', ''))
    content = clean_text[-100:] if len(clean_text) > 100 else clean_text
    return hashlib.md5(content.encode('utf-8')).hexdigest()


def get_media_hash(url: str) -> str:
    return hashlib.md5(url.encode('utf-8')).hexdigest()


def is_new_message(post: dict, chat_id: int = 0) -> bool:
    if not isinstance(chat_id, int):
        print(f"⚠️ Некорректный chat_id в is_new_message: {chat_id}")
        return False
        
    msg_hash = get_message_hash(post)
    if chat_id not in message_cache_by_chat:
        message_cache_by_chat[chat_id] = set()
    if msg_hash in message_cache_by_chat[chat_id]:
        return False
    message_cache_by_chat[chat_id].add(msg_hash)
    return True


def is_new_media(url: str, chat_id: int = 0) -> bool:
    media_hash = get_media_hash(url)
    if chat_id not in photo_cache_by_chat:
        photo_cache_by_chat[chat_id] = set()
    if media_hash in photo_cache_by_chat[chat_id]:
        return False
    photo_cache_by_chat[chat_id].add(media_hash)
    return True


def clear_chat_cache(chat_id: int):
    if chat_id in message_cache_by_chat:
        del message_cache_by_chat[chat_id]
    if chat_id in photo_cache_by_chat:
        del photo_cache_by_chat[chat_id]
    save_message_cache()
    save_photo_cache()


def clear_all_caches():
    global message_cache_by_chat, photo_cache_by_chat
    message_cache_by_chat.clear()
    photo_cache_by_chat.clear()

    for f in [SEEN_MESSAGES_FILE, PHOTO_CACHE_FILE]:
        if os.path.exists(f):
            os.remove(f)

    if os.path.exists("downloads"):
        shutil.rmtree("downloads")
    os.makedirs("downloads", exist_ok=True)


#! ==========================================
#! 3. ЗАГРУЗКА МЕДИА
#! ==========================================

def get_media_bytes(url: str, media_type: str = 'image', chat_id: int = 0) -> dict:
    if not isinstance(chat_id, int):
        print(f"⚠️ Некорректный chat_id в get_media_bytes: {chat_id}")
        return None
        
    if not is_new_media(url, chat_id=chat_id):
        return None

    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        resp = requests.get(url, timeout=30, headers=headers)
        if resp.status_code == 200:
            data = resp.content
            if media_type == 'image' and len(data) < 5000:
                return None
            return {'bytes': data, 'type': media_type}
    except Exception as e:
        print(f"❌ Ошибка скачивания {media_type}: {e}")
    return None


def is_human_message(text: str) -> bool:
    text = text.strip().lower()
    if not text:
        return False
    bot_phrases = [
        'теперь в max', 'now on max', 'напишите что-нибудь', 'write something', 'сферум',
        'удалил', 'удалила', 'изменил', 'изменила',
        'вошел', 'вошла', 'покинул', 'покинула',
        'добавил', 'добавила', 'исключил', 'исключила',
        'пригласил', 'пригласила', 'системное',
        'создал чат', 'создала чат', 'вернулся', 'вернулась',
        'скачать видео', 'ютуб', 'тикток', 'подарок', 'исчезнет'
    ]
    if any(phrase in text for phrase in bot_phrases):
        return False
    return 10 < len(text) < 2000

def get_parse_debug_screenshot(chat_id: int) -> str:
    """Возвращает путь к диагностическому скриншоту последней ошибки парсинга"""
    debug_path = f"{SESSIONS_DIR}/parse_debug_{chat_id}.png"
    if os.path.exists(debug_path):
        return debug_path
    return None


def clear_parse_debug_screenshot(chat_id: int):
    """Удаляет диагностический скриншот"""
    debug_path = f"{SESSIONS_DIR}/parse_debug_{chat_id}.png"
    if os.path.exists(debug_path):
        try:
            os.remove(debug_path)
        except:
            pass

def get_chat_cache_count(chat_id: int) -> int:
    """Возвращает количество сообщений в кэше для конкретного чата"""
    return len(message_cache_by_chat.get(chat_id, set()))

#! ==========================================
#! 4. ПАРСИНГ С ИСПОЛЬЗОВАНИЕМ JSON-СЕССИИ
#! ==========================================

def parse_max_group_media(group_url: str, chat_id: int) -> List[Dict]:
    """Парсит чат, используя легкую JSON-сессию"""
    if not isinstance(chat_id, int):
        raise Exception(f"Некорректный chat_id: {chat_id} (должен быть int)")
    
    session_file = f"{SESSIONS_DIR}/{chat_id}.json"

    if not os.path.exists(session_file):
        raise Exception("NO_SESSION")

    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=True,
        args=['--no-sandbox', '--disable-blink-features=AutomationControlled']
    )

    context = browser.new_context(
        storage_state=session_file,
        viewport={'width': 1280, 'height': 800}
    )
    page = context.new_page()

    try:
        print(f"[{chat_id}] Переход на главную MAX...")
        page.goto("https://web.max.ru", timeout=30000)
        page.wait_for_timeout(3000)

        if not _is_logged_in(page):
            debug_path = f"{SESSIONS_DIR}/parse_debug_{chat_id}.png"
            page.screenshot(path=debug_path, full_page=True)
            context.close()
            browser.close()
            pw.stop()
            raise Exception(f"SESSION_EXPIRED|{debug_path}")

        page.goto(group_url, timeout=60000)
        page.wait_for_timeout(4000)

        current_url = page.url
        if group_url not in current_url:
            print(f"⚠️ [{chat_id}] ОШИБКА: URL не совпадает! Мы не в целевом чате.")
            print(f"   Ожидалось: {group_url}")
            print(f"   Текущий URL: {current_url}")
            
            debug_path = f"{SESSIONS_DIR}/wrong_chat_debug_{chat_id}.png"
            page.screenshot(path=debug_path, full_page=True)
            context.close()
            browser.close()
            pw.stop()
            raise Exception(f"WRONG_CHAT_REDIRECT|{debug_path}")

        page_content = page.content()
        if len(page_content) < 1000:
            debug_path = f"{SESSIONS_DIR}/parse_debug_{chat_id}.png"
            page.screenshot(path=debug_path, full_page=True)
            print(f"[{chat_id}] ⚠️ Страница слишком короткая ({len(page_content)} символов)")
            context.close()
            browser.close()
            pw.stop()
            raise Exception(f"EMPTY_PAGE|{debug_path}")

        for i in range(30):
            page.keyboard.press("End")
            page.wait_for_timeout(200)
        page.wait_for_timeout(3000)

        raw_messages = page.evaluate(r"""() => {
            const results = [];
            const containers = new Set();
            
            const selectors = [
                '[class*="message"]', '[class*="Message"]',
                '[class*="bubble"]', '[class*="Bubble"]',
                '[data-testid*="message"]', 'article', 'div[class*="item"]'
            ];
            
            for (const sel of selectors) {
                try {
                    const elements = document.querySelectorAll(sel);
                    elements.forEach(el => {
                        if (el.innerText && el.innerText.trim().length > 10) {
                            containers.add(el);
                        }
                    });
                } catch(e) {}
            }
            
            const sorted = Array.from(containers).sort((a, b) => {
                const pos = a.compareDocumentPosition(b);
                if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
                if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
                return 0;
            });
            
            console.log('[MAX Parser] Найдено ' + sorted.length + ' контейнеров');
            
            sorted.forEach((container, idx) => {
                if (container.closest('.sidebar, .chat-list, .ChatList, .SuggestedChats, .suggested, .LeftPanel')) {
                    return;
                }

                const fullText = container.innerText.trim();
                if (fullText.length < 10 || fullText.length > 2000) return;
                
                let name = '';
                let role = '';
                let cleanText = fullText;
                
                const nameSelectors = ['[class*="name"]', '[class*="author"]', '[class*="sender"]', '[class*="user-name"]'];
                
                for (const sel of nameSelectors) {
                    const nameEl = container.querySelector(sel);
                    if (nameEl) {
                        name = nameEl.innerText.trim();
                        name = name.replace(/\u{1F464}/gu, '').trim();
                        name = name.replace(/\s+/g, ' ');
                        
                        const authorBlock = nameEl.parentElement;
                        if (authorBlock) {
                            const authorText = authorBlock.innerText.trim();
                            if (authorText.startsWith(name)) {
                                role = authorText.substring(name.length).trim();
                                if (role.length > 40) role = '';
                            }
                        }
                        break;
                    }
                }
                
                // === ОЧИСТКА ТЕКСТА ===
                
                // 1. Убираем служебные слова времени в начале
                cleanText = cleanText.replace(/^(Today|Yesterday|Сегодня|Вчера)\s+/i, '').trim();
                cleanText = cleanText.replace(/^(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}\s+/i, '').trim();
                cleanText = cleanText.replace(/^\d{1,2}\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}\s+/i, '').trim();
                cleanText = cleanText.replace(/^(Январь|Февраль|Март|Апрель|Май|Июнь|Июль|Август|Сентябрь|Октябрь|Ноябрь|Декабрь)\s+\d{1,2},?\s+\d{4}\s+/i, '').trim();
                cleanText = cleanText.replace(/^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+\d{1,2},?\s+\d{4}\s+/i, '').trim();

                // 2. Убираем эмодзи 👤
                cleanText = cleanText.replace(/\u{1F464}/gu, '').trim();
                
                // 3. Убираем имя и роль из начала текста
                if (name) {
                    const escapeRegExp = (str) => str.replace(/[.*+?^${}()|[\]\\]/g, '\\\\$&');
                    const nameEsc = escapeRegExp(name.trim());
                    const roleEsc = role ? escapeRegExp(role.trim()) : '';
                    
                    if (roleEsc) {
                        const regex = new RegExp('^\\s*' + nameEsc + '\\s+' + roleEsc + '\\s*', 'i');
                        cleanText = cleanText.replace(regex, '').trim();
                    }
                    
                    if (cleanText === fullText || cleanText.toLowerCase().startsWith(name.trim().toLowerCase())) {
                        const regexName = new RegExp('^\\s*' + nameEsc + '\\s*', 'i');
                        cleanText = cleanText.replace(regexName, '').trim();
                    }
                }
                
                // 4. Нормализуем пробелы
                cleanText = cleanText.replace(/\s+/g, ' ').trim();
                
                // 5. Извлекаем время с конца (поддержка AM/PM)
                let msgTime = '';
                const timeMatch = cleanText.match(/(\d{1,2}:\d{2})\s*(AM|PM|am|pm)?\s*$/i);
                if (timeMatch) {
                    msgTime = timeMatch[0].trim();
                    cleanText = cleanText.substring(0, cleanText.length - msgTime.length).trim();
                }
                
                // === ИЗОБРАЖЕНИЯ ===
                const images = [];
                const imgs = container.querySelectorAll('img');
                imgs.forEach(img => {
                    const src = img.src || (img.dataset ? img.dataset.src : '') || '';
                    if (src.length < 50) return;
                    const srcLower = src.toLowerCase();
                    if (srcLower.includes('avatar') || srcLower.includes('userpic') || 
                        srcLower.includes('profile') || srcLower.includes('icon') ||
                        srcLower.includes('emoji') || srcLower.includes('sticker')) return;
                    
                    const rect = img.getBoundingClientRect();
                    if (rect.width < 100 || rect.height < 100) return;
                    images.push(src);
                });
                
                // === ДОКУМЕНТЫ ===
                const documents = [];
                const links = container.querySelectorAll('a');
                links.forEach(a => {
                    const href = a.href || '';
                    const downloadAttr = a.getAttribute('download') || '';
                    if (href && href.startsWith('http')) {
                        const hrefLower = href.toLowerCase();
                        if (hrefLower.match(/\.(jpg|jpeg|png|gif|webp|svg|bmp)$/)) return;
                        
                        const hasExtension = href.match(/\.[a-zA-Z0-9]+$/);
                        const className = a.className || '';
                        const isFileElement = downloadAttr || hasExtension || className.match(/file|document|attachment/i);
                        
                        if (isFileElement) {
                            let filename = downloadAttr || href.split('/').pop().split('?')[0] || 'document';
                            let exists = documents.some(doc => doc.url === href);
                            if (!exists) {
                                documents.push({ url: href, filename: filename });
                            }
                        }
                    }
                });
                
                let fullName = role ? (name + ' ' + role) : name;
                if (!fullName) fullName = 'Аноним';
                
                results.push({
                    idx: idx,
                    name: fullName,
                    text: cleanText,
                    time: msgTime,
                    images: images,
                    documents: documents
                });
            });
            
            return results;
        }""")

        seen_hashes = set()
        unique = []
        for msg in raw_messages:
            if not is_human_message(msg['text']):
                continue

            clean_text = normalize_for_hash(msg.get('text', ''))
            content = clean_text[-100:] if len(clean_text) > 100 else clean_text
            msg_hash = hashlib.md5(content.encode('utf-8')).hexdigest()

            if msg_hash in seen_hashes:
                continue
            seen_hashes.add(msg_hash)
            unique.append(msg)

        last_10 = unique[-10:]
        print(f"[{chat_id}] После фильтрации: {len(last_10)} сообщений")

        human_posts = []
        for msg in last_10:
            media_files = []

            for img_url in msg['images']:
                res = get_media_bytes(img_url, media_type='image', chat_id=chat_id)
                if res:
                    media_files.append({
                        'url': img_url,
                        'bytes': res['bytes'],
                        'type': 'image'
                    })

            for doc in msg.get('documents', []):
                res = get_media_bytes(doc['url'], media_type='document', chat_id=chat_id)
                if res:
                    media_files.append({
                        'url': doc['url'],
                        'bytes': res['bytes'],
                        'type': 'document',
                        'filename': doc['filename']
                    })

            human_posts.append({
                'name': msg['name'],
                'text': msg['text'],
                'time': msg['time'],
                'media_files': media_files
            })

        context.close()
        browser.close()
        pw.stop()

        return human_posts

    except Exception as e:
        try:
            context.close()
            browser.close()
            pw.stop()
        except:
            pass
        raise e