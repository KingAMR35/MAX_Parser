import re
import time
import requests
import os
import json
import hashlib
import shutil
import io
import base64
import subprocess
from urllib.parse import unquote, urlparse
import queue
import threading
import traceback
from typing import List, Dict
from playwright.sync_api import sync_playwright
from debug_log import logger as debug_logger, preview as log_preview

SESSIONS_DIR = "sessions"
SEEN_MESSAGES_FILE = "seen_messages.json"
PHOTO_CACHE_FILE = "seen_images.json"
MAX_MESSAGES_TO_PARSE = 10  #! Тут менять кол-во сообщений
MAX_MESSAGES_TO_PARSE = max(0, int(MAX_MESSAGES_TO_PARSE))
MAX_TIMEZONE_ID = "Europe/Samara"

os.makedirs(SESSIONS_DIR, exist_ok=True)

message_cache_by_chat = {}
photo_cache_by_chat = {}
recent_post_hashes_by_chat = {}
message_signatures_by_chat = {}


def _strip_browser_video_fallback(text: str) -> str:
    """Remove text rendered inside an unsupported video element, keeping captions."""
    text = str(text or "")
    text = re.sub(
        r"(?:your browser does not support (?:this )?video|your browser does not support the video tag|"
        r"ваш браузер не поддерживает (?:это )?видео)(?:\s+\d{1,2}:\d{2})?\.?",
        " ", text, flags=re.I,
    )
    return _normalize_message_lines(text)


def _auth_browser_should_be_headless() -> bool:
    """Keep MAX authorization in the background on every host."""
    return True


def _has_auth_captcha(page) -> bool:
    """Detect MAX's human verification without attempting to solve it."""
    try:
        body_text = page.locator("body").inner_text(timeout=500).casefold()
    except Exception:
        body_text = ""
    if any(marker in body_text for marker in (
        "not a robot",
        "verify that you are human",
        "проверка, что вы не робот",
        "подтвердите, что вы не робот",
        "captcha",
    )):
        return True

    for selector in (
        "[class*='captcha-widget' i]",
        "[id*='captcha' i]",
        "iframe[src*='captcha' i]",
        "iframe[title*='captcha' i]",
    ):
        try:
            if page.locator(selector).first.is_visible(timeout=150):
                return True
        except Exception:
            continue

    # MAX renders its verification challenge in a visible iframe whose URL
    # may be an opaque one-time link without the word "captcha" in it.
    try:
        frames = page.locator("iframe")
        for index in range(frames.count()):
            if frames.nth(index).is_visible(timeout=150):
                return True
    except Exception:
        pass
    return False


def _normalize_message_lines(text: str) -> str:
    lines = [
        re.sub(r"[\t ]+", " ", line).strip()
        for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

#! ==========================================
#! 1. QR-АВТОРИЗАЦИЯ
#! ==========================================

def _is_logged_in(page) -> bool:
    try:
        page_content = page.content()
        if len(page_content) < 1000:
            return False
        current_url = page.url.lower()
        current_path = urlparse(current_url).path.lower()
        if re.search(r"/(?:auth|login|signin|sign-in)(?:/|$)", current_path):
            return False

        # Never treat an unfinished challenge as a successful login. MAX may
        # remove the phone form briefly while switching to its code/CAPTCHA step.
        if _get_password_input(page) or _get_login_code_input(page) or _get_phone_input(page):
            return False

        try:
            body_text = page.locator("body").inner_text(timeout=1000).casefold()
        except Exception:
            body_text = ""
        login_text_markers = (
            "sign in to max via qr code",
            "scan the qr code to sign in",
            "sign in with phone number",
            "enter your phone number",
            "enter the verification code",
            "enter code",
            "code was sent",
            "code has been sent",
            "we sent a code",
            "we've sent a code",
            "enter the code we sent",
            "check your phone",
            "text message",
            "sms code",
            "verify your phone",
            "войти по номеру телефона",
            "введите номер телефона",
            "введите код",
            "код подтверждения",
            "код отправлен",
            "мы отправили код",
            "выслали код",
            "проверьте телефон",
            "смс",
            "подтвердите номер",
        )
        if any(marker in body_text for marker in login_text_markers):
            return False

        login_indicators = (
            "input[type='password']",
            "input[type='tel']",
            "input[autocomplete='one-time-code']",
            "input[name*='otp' i]",
            "input[name*='code' i]",
            "div[class*='qr' i]",
            "canvas[class*='qr' i]",
            "img[class*='qr' i]",
            "button:has-text('Sign in')",
            "button:has-text('Войти')",
            "text=Sign in with phone number",
        )
        for selector in login_indicators:
            try:
                if page.locator(selector).first.is_visible(timeout=150):
                    return False
            except Exception:
                continue
        return True
    except Exception as e:
        print(f"️ Ошибка проверки авторизации: {e}")
        return False

def _goto_max_with_retry(page, url: str, chat_id: int, stage: str, timeout: int = 60000, max_attempts: int = 4):
    """Retry transient MAX network failures with a short exponential backoff."""
    retryable_errors = (
        "err_connection_closed",
        "err_connection_reset",
        "err_connection_aborted",
        "err_connection_refused",
        "err_timed_out",
        "err_network_changed",
        "err_name_not_resolved",
        "err_tunnel_connection_failed",
        "err_internet_disconnected",
        "err_ssl_protocol_error",
    )
    max_attempts = max(1, int(max_attempts))
    for attempt in range(1, max_attempts + 1):
        try:
            return page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        except Exception as exc:
            error_text = str(exc).lower()
            network_error = next(
                (marker.upper() for marker in retryable_errors if marker in error_text),
                type(exc).__name__,
            )
            if attempt >= max_attempts or not any(marker in error_text for marker in retryable_errors):
                debug_logger.error(
                    "MAX_NAVIGATION_FAILED chat_id=%s stage=%s attempts=%s error=%s",
                    chat_id, stage, attempt, network_error,
                )
                raise
            delay_seconds = min(2 ** attempt, 8)
            debug_logger.warning(
                "MAX_NAVIGATION_RETRY chat_id=%s stage=%s attempt=%s/%s delay_seconds=%s error=%s",
                chat_id, stage, attempt + 1, max_attempts, delay_seconds, network_error,
            )
            print(
                f"[{chat_id}] ⚠️ Сетевой сбой при этапе «{stage}». "
                f"Повтор {attempt + 1}/{max_attempts} через {delay_seconds} сек."
            )
            time.sleep(delay_seconds)

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
            if el.is_visible(timeout=150):
                return el
        except:
            continue
    return None

def _get_phone_input(page):
    selectors = [
        "input[type='tel']",
        "input[autocomplete='tel']",
        "input[autocomplete='tel-national']",
        "input[inputmode='tel']",
        "input[name*='phone' i]",
        "input[placeholder*='телефон' i]",
        "input[placeholder*='phone' i]",
        "input[aria-label*='телефон' i]",
        "input[aria-label*='phone' i]",
    ]
    for selector in selectors:
        try:
            element = page.locator(selector).first
            if element.is_visible(timeout=250):
                return element
        except Exception:
            continue
    return None


def _wait_for_phone_input(page, timeout_seconds: float = 15.0):
    deadline = time.monotonic() + timeout_seconds
    while True:
        phone_input = _get_phone_input(page)
        if phone_input:
            return phone_input
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        page.wait_for_timeout(min(250, max(1, int(remaining * 1000))))


def _get_login_code_input(page):
    try:
        body_text = page.locator("body").inner_text(timeout=500).casefold()
    except Exception:
        body_text = ""
    code_prompt_markers = (
        "verification code",
        "enter code",
        "enter the code",
        "code was sent",
        "code has been sent",
        "we sent a code",
        "we've sent a code",
        "enter the code we sent",
        "check your phone",
        "text message",
        "check your messages",
        "sms code",
        "код подтверждения",
        "введите код",
        "код отправлен",
        "мы отправили код",
        "код отправлен на",
        "проверьте телефон",
        "смс",
        "проверьте сообщения",
    )
    code_prompt_visible = any(marker in body_text for marker in code_prompt_markers)
    explicit_code_selectors = [
        "input[autocomplete='one-time-code']",
        "input[name*='otp' i]",
        "input[name*='verification' i]",
        "input[name*='code' i]",
        "input[placeholder*='код' i]",
        "input[placeholder*='code' i]",
        "input[aria-label*='код' i]",
        "input[aria-label*='code' i]",
    ]
    # A semantic OTP field is strong evidence that MAX advanced, even if the
    # UI keeps the phone number visible beside it during a transition.
    for selector in explicit_code_selectors:
        try:
            element = page.locator(selector).first
            if not element.is_visible(timeout=150):
                continue
            hints = " ".join(
                str(element.get_attribute(attribute) or "")
                for attribute in ("name", "placeholder", "aria-label", "autocomplete")
            )
            if re.search(r"phone|телефон|номер", hints, re.I):
                continue
            return element
        except Exception:
            continue

    # Do not mistake a generic/numeric phone field for the code form. MAX must
    # have removed the phone input and shown explicit code-step text first.
    if _get_phone_input(page):
        return None
    if code_prompt_visible:
        selectors = [
            "input[inputmode='numeric']",
            "input[maxlength='1']",
            "input[type='text']",
            "[role='textbox']",
            "[contenteditable='true']",
        ]
    else:
        selectors = []
    for selector in selectors:
        try:
            element = page.locator(selector).first
            if not element.is_visible(timeout=150):
                continue
            return element
        except Exception:
            continue
    return None


def _fill_login_code(page, code_input, digits: str) -> bool:
    try:
        if str(code_input.get_attribute("maxlength") or "") == "1":
            candidates = page.locator("input[maxlength='1']")
            fields = []
            for index in range(candidates.count()):
                field = candidates.nth(index)
                try:
                    if field.is_visible(timeout=150):
                        fields.append(field)
                except Exception:
                    continue
            if len(fields) >= len(digits):
                for field, digit in zip(fields, digits):
                    field.fill(digit, timeout=1500)
                return True
            return False
        code_input.fill(digits, timeout=5000)
        return True
    except Exception:
        return False


def _click_phone_login_option(page):
    selectors = [
        "button:has-text('Войти по номеру телефона')",
        "button:has-text('Войти по телефону')",
        "button:has-text('Вход по номеру телефона')",
        "button:has-text('Вход по телефону')",
        "text=Войти по номеру телефона",
        "text=Вход по номеру телефона",
        "text=Sign in with phone number",
        "text=Log in with phone number",
        "[aria-label*='phone number' i]",
        "[aria-label*='номер телефона' i]",
    ]
    for selector in selectors:
        try:
            element = page.locator(selector).first
            if element.is_visible(timeout=250) and element.is_enabled(timeout=250):
                try:
                    element.click(timeout=1500)
                except Exception:
                    # The click may already have been dispatched before a slow transition.
                    return True
                return True
        except Exception:
            continue
    return False


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
            if el.is_visible(timeout=250) and el.is_enabled(timeout=250):
                try:
                    el.click(timeout=1500)
                except Exception:
                    # The click may have been dispatched before navigation or
                    # a re-render timed out; do not submit the same form twice.
                    return True
                return True
        except:
            continue
    try:
        page.keyboard.press("Enter")
        return True
    except:
        return False


def _submit_phone_number(phone_input) -> bool:
    """Submit the phone field using its own form, never a page-wide button."""
    try:
        form = phone_input.locator("xpath=ancestor::form[1]")
        if form.count():
            for selector in (
                "button[type='submit']",
                "input[type='submit']",
                "[data-testid='submit-button']",
                "button:has-text('Continue')",
                "button:has-text('Next')",
                "button:has-text('Send code')",
                "button:has-text('Sign in')",
                "button:has-text('Продолжить')",
                "button:has-text('Далее')",
                "button:has-text('Отправить код')",
                "button:has-text('Войти')",
            ):
                try:
                    button = form.locator(selector).first
                    if not button.is_visible(timeout=200) or not button.is_enabled(timeout=200):
                        continue
                    try:
                        button.click(timeout=3000)
                    except Exception:
                        # A slow page transition can time out after the click
                        # has already reached MAX. Do not submit a second time.
                        return True
                    return True
                except Exception:
                    continue
            # Some MAX builds use a custom div[role=button]. Click it only
            # when it is the sole visible enabled action in the phone form.
            try:
                role_buttons = form.locator("[role='button']")
                visible_buttons = []
                for index in range(role_buttons.count()):
                    button = role_buttons.nth(index)
                    if button.is_visible(timeout=200) and button.is_enabled(timeout=200):
                        visible_buttons.append(button)
                if len(visible_buttons) == 1:
                    try:
                        visible_buttons[0].click(timeout=3000)
                    except Exception:
                        return True
                    return True
            except Exception:
                pass
    except Exception:
        pass

    # If the form uses a custom submit control, submit from the focused phone
    # field. This avoids accidentally clicking another visible page button.
    try:
        phone_input.press("Enter", timeout=3000)
        return True
    except Exception:
        return False

class QRLoginSession:
    def __init__(self, chat_id):
        self.chat_id = chat_id
        session_key = str(chat_id)
        if not re.fullmatch(r"-?\d+|project_account", session_key):
            raise ValueError("Некорректный идентификатор сессии MAX")
        self.session_file = os.path.join(SESSIONS_DIR, f"{session_key}.json")
        self.command_queue = queue.Queue()
        self.remote_command_queue = queue.Queue()
        self.result_queue = queue.Queue()
        self.thread = None
        self.browser = None
        self.context = None
        self.page = None
        self.pw = None
        self.screenshot_bytes = None
        self.status = "init"
        self._logged_in_confirmation_count = 0

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
                try:
                    remote_action, remote_args, response_queue = self.remote_command_queue.get_nowait()
                except queue.Empty:
                    remote_action = None
                if remote_action is not None:
                    try:
                        response_queue.put(self._do_remote_action(remote_action, remote_args))
                    except Exception as error:
                        response_queue.put(error)
                    continue
                try:
                    cmd, args = self.command_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if cmd == "init":
                    self._do_init()
                elif cmd == "refresh":
                    self._do_refresh()
                elif cmd == "check":
                    self._do_check()
                elif cmd == "password":
                    self._do_enter_password(args)
                elif cmd == "phone_login":
                    self._do_start_phone_login()
                elif cmd == "phone":
                    self._do_enter_phone(args)
                elif cmd == "code":
                    self._do_enter_code(args)
                elif cmd == "close":
                    self._do_close()
                    break
        except Exception as e:
            print(f" Ошибка в рабочем потоке QR: {e}")
            try:
                self.result_queue.put(("error", str(e)))
            except:
                pass

    def _do_remote_action(self, action, args):
        if not self.page or self.status == "closed":
            raise RuntimeError("Браузер авторизации уже закрыт")
        args = args if isinstance(args, dict) else {}
        if action == "screenshot":
            return self.page.screenshot(full_page=False, animations="disabled")
        if action == "click":
            x, y = int(args.get("x", -1)), int(args.get("y", -1))
            if not (0 <= x < 1280 and 0 <= y < 800):
                raise ValueError("Координаты клика вне окна")
            # Playwright Mouse.click does not accept a timeout keyword.
            # Passing one raises TypeError, which made every remote click fail.
            self.page.mouse.click(x, y)
            return True
        if action == "key":
            key = str(args.get("key", ""))
            allowed = {
                "Enter", "Tab", "Backspace", "Delete", "Escape", "ArrowUp",
                "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End",
                "PageUp", "PageDown", "Space",
            }
            if key not in allowed:
                raise ValueError("Клавиша не разрешена")
            self.page.keyboard.press(key)
            return True
        if action == "text":
            value = args.get("text")
            if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
                raise ValueError("Недопустимый текст ввода")
            focused = self.page.evaluate("""() => {
                const isVisible = el => {
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        style.visibility !== 'collapse';
                };
                const isEditable = el => {
                    if (!(el instanceof HTMLElement) || !isVisible(el)) return false;
                    if (el.tagName === 'INPUT') {
                        return !el.disabled && !el.readOnly &&
                            !['hidden', 'button', 'submit', 'reset', 'checkbox', 'radio', 'file', 'image', 'search'].includes(el.type);
                    }
                    return el.tagName === 'TEXTAREA'
                        ? !el.disabled && !el.readOnly
                        : el.isContentEditable || el.getAttribute('role') === 'textbox';
                };
                const score = el => {
                    if (!isEditable(el)) return -1000;
                    const details = [el.type, el.name, el.id, el.placeholder,
                        el.getAttribute('aria-label'), el.getAttribute('autocomplete'),
                        el.getAttribute('inputmode'), el.getAttribute('data-testid')].join(' ').toLowerCase();
                    if (/search|поиск/.test(details) && !/code|otp|код|phone|телефон|номер/.test(details)) return -1000;
                    let value = el.tagName === 'INPUT' ? 10 : 0;
                    if (/one-time-code|otp|verification|verify|sms|код|подтверж/.test(details)) value += 100;
                    if (/phone|телефон|номер/.test(details)) value += 80;
                    if (el.type === 'tel') value += 35;
                    if (/numeric|decimal|tel/.test(el.getAttribute('inputmode') || '')) value += 30;
                    if (el.type === 'password') value += 25;
                    if (el.type === 'text') value += 10;
                    return value;
                };
                const candidates = [...document.querySelectorAll('input, textarea, [contenteditable="true"], [role="textbox"]')];
                if (!candidates.length) return false;
                const active = document.activeElement;
                const activeScore = candidates.includes(active) ? score(active) : -1000;
                let best = null, bestScore = -1000;
                for (const candidate of candidates) {
                    const candidateScore = score(candidate);
                    if (candidateScore > bestScore) {
                        best = candidate;
                        bestScore = candidateScore;
                    }
                }
                if (activeScore >= bestScore && activeScore >= 0) return true;
                if (!best || bestScore < 0) return false;
                best.focus({preventScroll: true});
                return document.activeElement === best;
            }""")
            if not focused:
                raise ValueError("Поле ввода MAX не найдено; откройте форму телефона или кода")
            # Use real, ordered key events so MAX's code/phone inputs process
            # each character. A single insert_text call behaves like paste
            # and is ignored by some of MAX's segmented verification fields.
            self.page.keyboard.type(value, delay=8)
            return True
        if action == "scroll":
            delta_y = int(args.get("deltaY", 0))
            if not -900 <= delta_y <= 900:
                raise ValueError("Недопустимое значение прокрутки")
            self.page.mouse.wheel(0, delta_y)
            return True
        if action == "refresh":
            self.page.reload(wait_until="domcontentloaded", timeout=20000)
            self.page.wait_for_timeout(1500)
            return True
        raise ValueError("Неизвестное действие")

    def remote_action(self, action, args=None, timeout=8):
        response_queue = queue.Queue(maxsize=1)
        self.remote_command_queue.put((action, args or {}, response_queue))
        try:
            result = response_queue.get(timeout=timeout)
        except queue.Empty as error:
            raise TimeoutError("Таймаут управления удалённым браузером") from error
        if isinstance(result, Exception):
            raise result
        return result

    def _do_init(self):
        print(f"🔐 [Поток {self.chat_id}] Инициализация браузера...")
        self.pw = sync_playwright().start()
        self.auth_browser_headless = True
        print(f"🔐 [Поток {self.chat_id}] Браузер авторизации: headless")
        self.browser = self.pw.chromium.launch(
            headless=self.auth_browser_headless,
            args=['--no-sandbox', '--disable-blink-features=AutomationControlled']
        )
        self.context = self.browser.new_context(
            viewport={'width': 1280, 'height': 800},
            timezone_id=MAX_TIMEZONE_ID,
        )
        self.page = self.context.new_page()
        self.page.goto("https://web.max.ru", timeout=30000)
        self.page.wait_for_timeout(5000)
        if not _has_auth_captcha(self.page) and _is_logged_in(self.page):
            self.context.storage_state(path=self.session_file)
            self._do_close()
            self.result_queue.put(("already_logged_in", self.session_file))
            return
        self.screenshot_bytes = self.page.screenshot(full_page=True)
        self.status = "waiting"
        self.result_queue.put(("waiting", self.screenshot_bytes))

    def _do_start_phone_login(self):
        if self.status not in ("waiting", "awaiting_phone"):
            self.result_queue.put(("error", "Сессия входа не активна"))
            return
        if _wait_for_phone_input(self.page, timeout_seconds=1.0):
            self.status = "awaiting_phone"
            self.result_queue.put(("awaiting_phone", None))
            return
        if not _click_phone_login_option(self.page):
            self.result_queue.put(("error", "Кнопка входа по номеру телефона не найдена"))
            return
        if not _wait_for_phone_input(self.page):
            self.result_queue.put(("error", "MAX не показал поле номера телефона"))
            return
        self.status = "awaiting_phone"
        self.result_queue.put(("awaiting_phone", None))

    def _do_enter_phone(self, phone: str):
        if self.status not in ("waiting", "awaiting_phone"):
            self.result_queue.put(("error", "Сейчас номер телефона не запрашивается"))
            return
        try:
            if not _has_auth_captcha(self.page) and _is_logged_in(self.page):
                self.context.storage_state(path=self.session_file)
                self._do_close()
                self.result_queue.put(("success", self.session_file))
                return
            digits = re.sub(r"\D", "", str(phone or ""))
            if not 7 <= len(digits) <= 15:
                self.result_queue.put(("error", "Проверьте формат номера телефона"))
                return
            phone_input = _get_phone_input(self.page)
            if not phone_input:
                # MAX can finish rendering the phone form after the bot has
                # already prompted the user. Re-open it before reporting failure.
                _click_phone_login_option(self.page)
                phone_input = _wait_for_phone_input(self.page, timeout_seconds=12.0)
            if not phone_input:
                self.result_queue.put(("error", "Поле номера телефона не найдено"))
                return
            phone_input.fill("+" + digits, timeout=5000)
            if not _submit_phone_number(phone_input):
                self.result_queue.put(("error", "Не удалось отправить номер в форме MAX"))
                return
            deadline = time.monotonic() + 25
            while True:
                if _has_auth_captcha(self.page):
                    self.status = "awaiting_human"
                    self.result_queue.put(("awaiting_human", "captcha"))
                    return
                if _get_password_input(self.page):
                    self.status = "awaiting_password"
                    self.result_queue.put(("awaiting_password", None))
                    return
                if _get_login_code_input(self.page):
                    self.status = "awaiting_code"
                    self.result_queue.put(("awaiting_code", None))
                    return
                if _get_phone_input(self.page):
                    self.status = "awaiting_phone"
                    self.result_queue.put(("awaiting_phone", None))
                    return
                if _is_logged_in(self.page):
                    self.context.storage_state(path=self.session_file)
                    self._do_close()
                    self.result_queue.put(("success", self.session_file))
                    return
                if time.monotonic() >= deadline:
                    break
                self.page.wait_for_timeout(500)
            # Do not loop the user back to re-enter the number. A challenge,
            # rate limit, or changed MAX screen needs inspection first.
            self.status = "awaiting_human"
            self.result_queue.put(("awaiting_human", "transition_timeout"))
        except Exception as e:
            self.result_queue.put(("error", f"Не удалось отправить номер в MAX: {str(e)[:200]}"))

    def _do_enter_code(self, code: str):
        if self.status != "awaiting_code":
            self.result_queue.put(("error", "Сейчас код подтверждения не запрашивается"))
            return
        try:
            digits = re.sub(r"\D", "", str(code or ""))
            if not 4 <= len(digits) <= 8:
                self.result_queue.put(("error", "Код должен содержать от 4 до 8 цифр"))
                return
            code_input = _get_login_code_input(self.page)
            if not code_input:
                deadline = time.monotonic() + 10
                while not code_input and time.monotonic() < deadline:
                    self.page.wait_for_timeout(300)
                    code_input = _get_login_code_input(self.page)
                if not code_input:
                    self.result_queue.put(("error", "Поле кода подтверждения не найдено"))
                    return
            if not _fill_login_code(self.page, code_input, digits):
                self.result_queue.put(("error", "Не удалось заполнить поле кода MAX полностью"))
                return
            if not _click_submit_button(self.page):
                self.page.keyboard.press("Enter")
            deadline = time.monotonic() + 20
            while True:
                if _has_auth_captcha(self.page):
                    self.status = "awaiting_human"
                    self.result_queue.put(("awaiting_human", "captcha"))
                    return
                if _get_password_input(self.page):
                    self.status = "awaiting_password"
                    self.screenshot_bytes = self.page.screenshot(full_page=True)
                    self.result_queue.put(("awaiting_password", self.screenshot_bytes))
                    return
                if not _get_login_code_input(self.page) and not _get_phone_input(self.page) and _is_logged_in(self.page):
                    self.context.storage_state(path=self.session_file)
                    self._do_close()
                    self.result_queue.put(("success", self.session_file))
                    return
                if time.monotonic() >= deadline:
                    break
                self.page.wait_for_timeout(500)
            if _get_login_code_input(self.page):
                self.status = "awaiting_code"
                self.screenshot_bytes = self.page.screenshot(full_page=True)
                self.result_queue.put(("wrong_code", self.screenshot_bytes))
                return
            if _get_phone_input(self.page):
                self.status = "awaiting_phone"
                self.result_queue.put(("awaiting_phone", None))
                return
            self.status = "waiting"
            self.screenshot_bytes = self.page.screenshot(full_page=True)
            self.result_queue.put(("waiting", self.screenshot_bytes))
        except Exception as e:
            self.result_queue.put(("error", f"Не удалось подтвердить код в MAX: {str(e)[:200]}"))

    def _do_refresh(self):
        if self.status not in ("waiting", "awaiting_password", "awaiting_phone", "awaiting_code"):
            self.result_queue.put(("error", "Сессия не активна"))
            return
        self.page.reload()
        self.page.wait_for_timeout(4000)
        if not _has_auth_captcha(self.page) and _is_logged_in(self.page):
            self.context.storage_state(path=self.session_file)
            self._do_close()
            self.result_queue.put(("success", self.session_file))
            return
        self.screenshot_bytes = self.page.screenshot(full_page=True)
        self.status = "waiting"
        self.result_queue.put(("waiting", self.screenshot_bytes))

    def _do_check(self):
        if self.status not in ("waiting", "awaiting_password", "awaiting_phone", "awaiting_code", "awaiting_human"):
            self.result_queue.put(("error", "Сессия не активна"))
            return
        if _has_auth_captcha(self.page):
            self._logged_in_confirmation_count = 0
            self.status = "awaiting_human"
            self.result_queue.put(("awaiting_human", "captcha"))
            return
        password_input = _get_password_input(self.page)
        if password_input:
            self._logged_in_confirmation_count = 0
            self.status = "awaiting_password"
            self.screenshot_bytes = self.page.screenshot(full_page=True)
            self.result_queue.put(("awaiting_password", self.screenshot_bytes))
            return
        if _get_login_code_input(self.page):
            self._logged_in_confirmation_count = 0
            self.status = "awaiting_code"
            self.screenshot_bytes = self.page.screenshot(full_page=True)
            self.result_queue.put(("awaiting_code", self.screenshot_bytes))
            return
        if _get_phone_input(self.page):
            self._logged_in_confirmation_count = 0
            self.status = "awaiting_phone"
            self.screenshot_bytes = self.page.screenshot(full_page=True)
            self.result_queue.put(("awaiting_phone", self.screenshot_bytes))
            return
        if _is_logged_in(self.page):
            self._logged_in_confirmation_count += 1
            if self._logged_in_confirmation_count >= 3:
                self.context.storage_state(path=self.session_file)
                self._do_close()
                self.result_queue.put(("success", self.session_file))
                return
            self.result_queue.put(("waiting", None))
            return
        self._logged_in_confirmation_count = 0
        if self.status == "awaiting_human":
            self.result_queue.put(("awaiting_human", "transition_timeout"))
        else:
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
                self.page.keyboard.press("Enter")
            deadline = time.monotonic() + 20
            while True:
                if _has_auth_captcha(self.page):
                    self.status = "awaiting_human"
                    self.result_queue.put(("awaiting_human", "captcha"))
                    return
                if _get_login_code_input(self.page):
                    self.status = "awaiting_code"
                    self.result_queue.put(("awaiting_code", None))
                    return
                if not _get_password_input(self.page) and not _get_phone_input(self.page) and _is_logged_in(self.page):
                    self.context.storage_state(path=self.session_file)
                    self._do_close()
                    self.result_queue.put(("success", self.session_file))
                    return
                if time.monotonic() >= deadline:
                    break
                self.page.wait_for_timeout(500)
            if _get_password_input(self.page):
                self.screenshot_bytes = self.page.screenshot(full_page=True)
                self.status = "awaiting_password"
                self.result_queue.put(("wrong_password", self.screenshot_bytes))
                return
            if _get_phone_input(self.page):
                self.status = "awaiting_phone"
                self.result_queue.put(("awaiting_phone", None))
                return
            self.screenshot_bytes = self.page.screenshot(full_page=True)
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

    def start_phone_login(self):
        self.command_queue.put(("phone_login", None))
        try:
            return self.result_queue.get(timeout=30)
        except queue.Empty:
            return ("error", "Таймаут переключения на вход по номеру")

    def enter_phone(self, phone: str):
        self.command_queue.put(("phone", phone))
        try:
            return self.result_queue.get(timeout=60)
        except queue.Empty:
            return ("error", "Таймаут ввода номера телефона")

    def enter_code(self, code: str):
        self.command_queue.put(("code", code))
        try:
            return self.result_queue.get(timeout=20)
        except queue.Empty:
            return ("error", "Таймаут подтверждения кода")

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
    global message_cache_by_chat, message_signatures_by_chat
    message_cache_by_chat.clear()
    message_signatures_by_chat.clear()
    if os.path.exists(SEEN_MESSAGES_FILE):
        try:
            with open(SEEN_MESSAGES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for chat_id_str, hashes in data.get('by_chat', {}).items():
                    message_cache_by_chat[int(chat_id_str)] = set(hashes)
                message_cache_by_chat[0] = set(data.get('message_hashes', []))
                for chat_id_str, signatures in data.get('signatures_by_chat', {}).items():
                    message_signatures_by_chat[int(chat_id_str)] = set(signatures)
        except Exception:
            message_cache_by_chat = {}

def save_message_cache():
    try:
        with open(SEEN_MESSAGES_FILE, "w", encoding="utf-8") as f:
            json.dump({
                'by_chat': {str(k): list(v) for k, v in message_cache_by_chat.items()},
                'signatures_by_chat': {str(k): list(v) for k, v in message_signatures_by_chat.items()},
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

def _get_post_text_signature(post: dict) -> str:
    """Signature for message identity that stays stable when MAX loads its media later."""
    name = normalize_for_hash(post.get("name", ""))
    text = normalize_for_hash(post.get("text", ""))
    msg_time = normalize_for_hash(post.get("time", ""))
    special = dict(post.get("special")) if isinstance(post.get("special"), dict) else {}
    for key in ("location", "poll", "contact"):
        if key not in special and post.get(key):
            special[key] = post.get(key)
    try:
        special_key = json.dumps(special, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        special_key = str(special)
    return "|".join([
        name, text, msg_time, normalize_for_hash(post.get("date", "")),
        normalize_for_hash(special_key),
    ])


def _has_media_independent_content(post: dict) -> bool:
    if normalize_for_hash(post.get("text", "")):
        return True
    special = post.get("special") if isinstance(post.get("special"), dict) else {}
    return any(post.get(key) or special.get(key) for key in ("location", "poll", "contact"))


def _get_post_content_signature(post: dict) -> str:
    """Signature that also records media, retained for compatibility with existing caches."""
    media_keys = sorted(
        str(item.get("hash") or item.get("content_hash") or item.get("url") or "")
        for item in (post.get("media_files") or [])
        if isinstance(item, dict) and (item.get("hash") or item.get("content_hash") or item.get("url"))
    )
    return _get_post_text_signature(post) + "|" + "|".join(media_keys)

def get_message_hash(post: dict) -> str:
    """Primary duplicate key: message content, never data-index/date-of-today."""
    name = normalize_for_hash(post.get("name", ""))
    text = normalize_for_hash(post.get("text", ""))
    msg_time = normalize_for_hash(post.get("time", ""))
    media = post.get("media_files", []) or []
    media_hashes = []
    for item in media:
        value = item.get("hash") or item.get("content_hash") or item.get("url") or ""
        if value:
            media_hashes.append(str(value))
    content = "|".join([name, text, msg_time, normalize_for_hash(post.get("date", "")), "|".join(sorted(media_hashes))])
    if not any([name, text, msg_time, media_hashes]):
        source_id = str(post.get("source_id") or "").strip()
        content = f"max-empty:{source_id}"
    return hashlib.md5(content.encode("utf-8")).hexdigest()

def get_text_message_hash(post: dict) -> str:
    if not _has_media_independent_content(post):
        return ""
    signature = _get_post_text_signature(post)
    if not signature:
        return ""
    return hashlib.md5(f"max-text-message:{signature}".encode("utf-8")).hexdigest()

def get_legacy_message_hash(post: dict) -> str:
    source_id = str(post.get("source_id") or "").strip()
    # MAX data-index/fallback positions can be reused after loading older history.
    # They are not safe duplicate keys; content hashes above remain authoritative.
    if not source_id or post.get("source_id_is_index"):
        return ""
    return hashlib.md5(f"max-message:{source_id}".encode("utf-8")).hexdigest()

def get_media_hash(data=None, url=None) -> str:
    if isinstance(data, (bytes, bytearray)):
        return hashlib.md5(bytes(data)).hexdigest()
    if isinstance(data, str):
        return hashlib.md5(data.encode("utf-8")).hexdigest()
    if url is not None:
        return hashlib.md5(str(url).encode("utf-8")).hexdigest()
    return hashlib.md5(str(data).encode("utf-8")).hexdigest()


def _image_visual_hash(data: bytes):
    """Small perceptual hash used only to drop screenshot fallbacks of an image already fetched."""
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source).convert("L")
            aspect = image.width / max(1, image.height)
            image = image.resize((9, 8))
            pixels = list(image.getdata())
        bits = 0
        for y in range(8):
            for x in range(8):
                bits = (bits << 1) | int(pixels[y * 9 + x] > pixels[y * 9 + x + 1])
        return aspect, bits
    except Exception:
        return None


def _same_visual_image(first, second) -> bool:
    if not first or not second or abs(first[0] - second[0]) > 0.06:
        return False
    return (first[1] ^ second[1]).bit_count() <= 1

def _similar_visual_image(first, second) -> bool:
    if not first or not second or abs(first[0] - second[0]) > 0.06:
        return False
    return (first[1] ^ second[1]).bit_count() <= 4

def _visual_cache_key(visual) -> str:
    return f"visual:{visual[0]:.4f}:{visual[1]:016x}"

def _parse_visual_cache_key(value):
    try:
        prefix, aspect, bits = str(value).split(":", 2)
        if prefix != "visual":
            return None
        return float(aspect), int(bits, 16)
    except (TypeError, ValueError):
        return None

def is_new_message(post: dict, chat_id: int = 0) -> bool:
    if not isinstance(chat_id, int):
        print(f"⚠️ Некорректный chat_id в is_new_message: {chat_id}")
        return False
    cache = message_cache_by_chat.get(chat_id, set())
    signature = _get_post_content_signature(post)
    known_signatures = message_signatures_by_chat.get(chat_id, set())
    if signature and signature in known_signatures:
        return False
    text_signature = _get_post_text_signature(post)
    if _has_media_independent_content(post) and text_signature and text_signature in known_signatures:
        mark_message_seen(post, chat_id=chat_id)
        return False
    # Старые версии добавляли к подписи фотографии/видео. Ищем историческую
    # подпись с той же текстовой частью, чтобы обновившиеся вложения не создавали
    # новое сообщение. После совпадения записываем новый стабильный ключ.
    if _has_media_independent_content(post) and text_signature and any(
        cached_signature.startswith(text_signature + "|")
        for cached_signature in known_signatures
        if isinstance(cached_signature, str)
    ):
        mark_message_seen(post, chat_id=chat_id)
        return False
    special = post.get("special") if isinstance(post.get("special"), dict) else {}
    poll = post.get("poll") or special.get("poll") or {}
    msg_hash = get_message_hash(post)
    if msg_hash in cache:
        if poll and signature and signature not in known_signatures:
            return True
        return False
    text_hash = get_text_message_hash(post)
    if text_hash and text_hash in cache:
        if poll and signature and signature not in known_signatures:
            return True
        return False
    legacy_hash = get_legacy_message_hash(post)
    if legacy_hash and legacy_hash in cache:
        if poll and signature and signature not in known_signatures:
            return True
        # Старый кэш мог быть создан по data-index. Мигрируем его
        # на стабильный контентный ключ, чтобы дальнейшая переиндексация
        # после системных событий MAX не привела к повторной отправке.
        cache.add(msg_hash)
        if text_hash:
            cache.add(text_hash)
        if signature:
            message_signatures_by_chat.setdefault(chat_id, set()).add(signature)
        return False
    return True


def _accept_max_invite_if_available(page, source_url: str, chat_id: int):
    """Join a user-selected MAX source when MAX shows a clear join/subscribe button."""
    source_parts = urlparse(source_url)
    source_host = (source_parts.hostname or "").lower().rstrip(".")
    if source_host != "max.ru" and not source_host.endswith(".max.ru"):
        return
    is_invite_link = bool(re.match(r"^/join(?:/|$)", source_parts.path, re.I))

    invite_actions = re.compile(
        r"^(?:вступить(?: в (?:чат|группу|канал))?|присоединиться|подписаться|"
        r"принять(?: приглашение)?|отправить заявку|запросить вступление|"
        r"join(?: chat| group| channel)?|subscribe|request to join)$",
        re.I,
    )
    buttons = page.locator("button, [role='button']")
    clicked = False
    requested_approval = False
    for _ in range(2):
        matched = None
        matched_requests_approval = False
        try:
            for index in range(min(buttons.count(), 40)):
                button = buttons.nth(index)
                try:
                    if not button.is_visible(timeout=100):
                        continue
                    label = " ".join((button.inner_text(timeout=250) or "").split())
                    if not label:
                        label = " ".join((button.get_attribute("aria-label") or "").split())
                    if invite_actions.fullmatch(label):
                        matched = button
                        if re.fullmatch(r"отправить заявку|запросить вступление|request to join", label, re.I):
                            matched_requests_approval = True
                        break
                except Exception:
                    continue
        except Exception:
            matched = None
        if matched is None:
            break
        try:
            matched.click(timeout=2000)
            page.wait_for_timeout(1200)
            clicked = True
            requested_approval = requested_approval or matched_requests_approval
        except Exception as error:
            debug_logger.warning(
                "MAX_INVITE_ACTION_FAILED chat_id=%s error=%s",
                chat_id, log_preview(error, 180),
            )
            break

    if requested_approval:
        raise Exception("MAX_INVITE_APPROVAL_PENDING")
    current_path = urlparse(page.url).path
    if is_invite_link and re.match(r"^/join(?:/|$)", current_path, re.I):
        if clicked:
            try:
                body_text = page.locator("body").inner_text(timeout=500).casefold()
            except Exception:
                body_text = ""
            if re.search(
                r"(?:заявка.{0,30}(?:отправлена|ожидает|рассматривается)|"
                r"ожидайте.{0,20}(?:одобрения|подтверждения)|"
                r"request.{0,30}(?:sent|pending)|waiting for (?:approval|review))",
                body_text,
                re.I,
            ):
                raise Exception("MAX_INVITE_APPROVAL_PENDING")
        remaining_action = False
        try:
            for index in range(min(buttons.count(), 40)):
                button = buttons.nth(index)
                if not button.is_visible(timeout=100):
                    continue
                label = " ".join((button.inner_text(timeout=250) or "").split())
                if not label:
                    label = " ".join((button.get_attribute("aria-label") or "").split())
                if invite_actions.fullmatch(label):
                    remaining_action = True
                    break
        except Exception:
            remaining_action = True
        if remaining_action:
            raise Exception("MAX_INVITE_JOIN_REQUIRED")
        if not clicked:
            raise Exception("MAX_INVITE_JOIN_REQUIRED")
    if clicked:
        debug_logger.info("MAX_INVITE_ACCEPTED chat_id=%s route=%s", chat_id, current_path)

def mark_message_seen(post: dict, chat_id: int = 0) -> None:
    if chat_id not in message_cache_by_chat:
        message_cache_by_chat[chat_id] = set()
    if chat_id not in message_signatures_by_chat:
        message_signatures_by_chat[chat_id] = set()
    signature = _get_post_content_signature(post)
    if signature:
        message_signatures_by_chat[chat_id].add(signature)
    text_signature = _get_post_text_signature(post)
    if _has_media_independent_content(post) and text_signature:
        message_signatures_by_chat[chat_id].add(text_signature)
    msg_hash = get_message_hash(post)
    if msg_hash:
        message_cache_by_chat[chat_id].add(msg_hash)
    text_hash = get_text_message_hash(post)
    if text_hash:
        message_cache_by_chat[chat_id].add(text_hash)
    legacy_hash = get_legacy_message_hash(post)
    if legacy_hash:
        message_cache_by_chat[chat_id].add(legacy_hash)

def is_new_media(data=None, chat_id: int = 0, url=None, media_hash=None) -> bool:
    if not isinstance(chat_id, int):
        print(f"⚠️ Некорректный chat_id в is_new_media: {chat_id}")
        return False
    if media_hash is None:
        media_hash = get_media_hash(data=data, url=url)
    cache = photo_cache_by_chat.get(chat_id, set())
    target_visual = _parse_visual_cache_key(media_hash)
    if target_visual:
        for cached_hash in cache:
            cached_visual = _parse_visual_cache_key(cached_hash)
            if _similar_visual_image(target_visual, cached_visual):
                return False
    return media_hash not in cache

def mark_media_seen(data=None, chat_id: int = 0, url=None, media_hash=None) -> None:
    if chat_id not in photo_cache_by_chat:
        photo_cache_by_chat[chat_id] = set()
    if media_hash is None:
        media_hash = get_media_hash(data=data, url=url)
    if media_hash:
        photo_cache_by_chat[chat_id].add(media_hash)

def clear_chat_cache(chat_id: int):
    message_cache_by_chat.pop(chat_id, None)
    message_signatures_by_chat.pop(chat_id, None)
    photo_cache_by_chat.pop(chat_id, None)
    recent_post_hashes_by_chat.pop(chat_id, None)
    save_message_cache()
    save_photo_cache()

def clear_all_caches():
    global message_cache_by_chat, photo_cache_by_chat, message_signatures_by_chat
    message_cache_by_chat.clear()
    message_signatures_by_chat.clear()
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
    data = data.lstrip()
    signatures = (
        b"\xff\xd8\xff",      # JPEG
        b"\x89PNG\r\n\x1a\n", # PNG
        b"GIF8",              # GIF
        b"BM",                # BMP
        b"<svg",
        b"<?xml",
    )
    return (
        any(data.startswith(signature) for signature in signatures)
        or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
    )


def _sniff_rich_media_type(data: bytes, content_type: str = "") -> str:
    """Identify audio/video returned for an image candidate so it is not retried as a photo."""
    content_type = (content_type or "").split(";", 1)[0].strip().lower()
    if content_type.startswith("video/"):
        return "video"
    if content_type.startswith("audio/"):
        return "audio"
    if len(data) > 12 and data[4:8] == b"ftyp":
        return "video"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "video"
    if data.startswith(b"OggS") or data.startswith(b"ID3") or data.startswith(b"fLaC"):
        return "audio"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "audio"
    return ""

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
                headers = {
                    "Referer": "https://web.max.ru/",
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
                    if media_type == "image" else "*/*",
                }
                response = None
                # Для картинок MAX часто требует cookies/контекст браузера.
                # requests() в таком случае возвращает HTML shell (~250 KB),
                # из-за чего caption сохранялся, а фото терялось.
                if context is not None:
                    try:
                        response = context.request.get(url, headers=headers, timeout=60_000, fail_on_status_code=False)
                    except Exception:
                        response = None
                if response is not None and response.ok:
                    data = response.body()
                    content_type = response.headers.get("content-type", "")
                else:
                    cookies = _browser_cookies_as_dict(context) if context is not None else {}
                    fallback_headers = {
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/140.0 Safari/537.36"
                        ),
                        "Referer": "https://web.max.ru/",
                        "Accept": headers["Accept"],
                    }
                    response2 = requests.get(url, headers=fallback_headers, cookies=cookies, timeout=(15, 60))
                    response2.raise_for_status()
                    data = response2.content
                    content_type = response2.headers.get("content-type", "")
                # Последний browser-only fallback полезен для CORS/proxy URLs.
                if media_type == "image" and (not data or not _looks_like_image(data, content_type)) and page is not None:
                    try:
                        fetched = page.evaluate(
                            """async (url) => {
                                const r = await fetch(url, {credentials:'include', headers:{'Accept':'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8'}});
                                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                                const b = await r.blob();
                                const ab = await b.arrayBuffer();
                                return {bytes:Array.from(new Uint8Array(ab)), contentType:b.type||''};
                            }""",
                            url,
                        )
                        browser_bytes = bytes(fetched.get("bytes") or [])
                        browser_type = fetched.get("contentType") or ""
                        if browser_bytes:
                            data = browser_bytes
                            content_type = browser_type or content_type
                    except Exception:
                        pass
            else:
                print(f"[{chat_id}] ⚠️ Неизвестный URL медиа: {url[:120]}")
                return None
            if not data:
                raise RuntimeError("MAX вернул пустой файл")
            detected_type = _sniff_rich_media_type(data, content_type) if media_type == "image" else ""
            if detected_type:
                result = {
                    "url": url,
                    "bytes": data,
                    "type": detected_type,
                    "hash": get_media_hash(data=data),
                    "content_type": content_type,
                }
                result["filename"] = _media_filename(
                    "", url, content_type, data, detected_type
                )
                return result
            if media_type == "image":
                if len(data) < 500:
                    return None
                if not _looks_like_image(data, content_type):
                    # Если MAX вернул HTML shell вместо картинки, не повторяем
                    # бессмысленные запросы. Выходим в screenshot fallback ниже.
                    if str(content_type or "").lower().startswith("text/html") or data.lstrip().lower().startswith((b"<html", b"<!doctype")):
                        return None
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
            if attempt < 3:
                time.sleep(1.5)
    return None

def is_human_message(msg: dict) -> bool:
    """Принимает любой реальный тип сообщения MAX, включая сообщения без текста."""
    text = (msg.get("text") or "").strip()
    has_media = bool(
        msg.get("images") or msg.get("documents") or msg.get("attachments")
        or msg.get("emoji_image_urls") or msg.get("voice_hint")
        or msg.get("video_note_hint") or msg.get("emoji_texts")
        or msg.get("sticker_hint")
    )
    has_special = bool(
        msg.get("location") or msg.get("poll") or msg.get("contact")
    )
    has_sticker = bool(msg.get("stickers"))
    # Emoji-only и короткие сообщения тоже являются полноценными сообщениями.
    if not text and not has_media and not has_special and not has_sticker:
        return False

    if any(phrase in text.casefold() for phrase in [
        "теперь в max", "now on max", "напишите что-нибудь", "write something",
        "сферум", "удалил", "удалила", "изменил", "изменила", "вошел", "вошла",
        "покинул", "покинула", "добавил", "добавила", "исключил", "исключила",
        "пригласил", "пригласила", "системное", "создал чат", "создала чат",
        "вернулся", "вернулась",
    ]):
        return False

    # Не ограничиваем длину медиа/спецсообщений: подпись может быть большой.
    return len(text) < 10000 if text else True

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
    filename = filename.replace("\\\\", "_").replace("/", "_")
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


def _media_filename(filename: str, url: str, content_type: str, data: bytes, media_type: str) -> str:
    """Keep downloaded voice/video files recognizable to Telegram's media endpoints."""
    name = _normalize_document_filename(filename, url, content_type)
    suffix = os.path.splitext(name)[1].lower()
    valid_suffixes = {
        "voice": {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav"},
        "audio": {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav"},
        "video": {".mp4", ".m4v", ".mov", ".webm", ".gif", ".webp"},
        "video_note": {".mp4", ".m4v", ".webm"},
        "animation": {".gif", ".mp4", ".m4v"},
    }
    if media_type not in valid_suffixes:
        return name
    if suffix not in valid_suffixes[media_type]:
        content_type = (content_type or "").lower()
        path_suffix = os.path.splitext(urlparse(url).path)[1].lower()
        signature_suffix = ""
        if data.startswith(b"OggS"):
            signature_suffix = ".ogg"
        elif data.startswith(b"ID3"):
            signature_suffix = ".mp3"
        elif len(data) > 12 and data[4:8] == b"ftyp":
            signature_suffix = ".mp4"
        elif data.startswith(b"\x1a\x45\xdf\xa3"):
            signature_suffix = ".webm"
        elif data.startswith((b"GIF87a", b"GIF89a")):
            signature_suffix = ".gif"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            signature_suffix = ".webp"
        suffix_by_type = {
            "audio/ogg": ".ogg", "application/ogg": ".ogg", "audio/opus": ".opus",
            "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a",
            "audio/wav": ".wav", "audio/x-wav": ".wav", "video/mp4": ".mp4",
            "video/webm": ".webm", "video/quicktime": ".mov", "image/gif": ".gif",
            "image/webp": ".webp",
        }
        detected = signature_suffix or suffix_by_type.get(content_type) or path_suffix
        if detected not in valid_suffixes[media_type]:
            detected = ".ogg" if media_type == "voice" else ".mp4" if media_type in ("video", "video_note") else ".gif"
        name = os.path.splitext(name)[0] + detected
    return name[:240]


def _animated_image_kind(data: bytes) -> str:
    """Return the container name for animated GIF, APNG, or WebP payloads."""
    if data.startswith((b"GIF87a", b"GIF89a")) and data.count(b"\x21\xf9\x04") > 1:
        return "gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP" and b"ANIM" in data and b"ANMF" in data:
        return "webp"
    if data.startswith(b"\x89PNG\r\n\x1a\n") and b"acTL" in data:
        return "apng"
    return ""


def _prepare_static_emoji_image(media: dict, chat_id: int = 0) -> dict:
    """Use the first rendered frame as a photo when MAX exposes no emoji Unicode."""
    data = media.get("bytes")
    if not isinstance(data, (bytes, bytearray)) or not data:
        return media
    try:
        from PIL import Image
        with Image.open(io.BytesIO(bytes(data))) as source:
            source.seek(0)
            frame = source.convert("RGBA")
            output = io.BytesIO()
            frame.save(output, format="PNG")
            media["bytes"] = output.getvalue()
            media["type"] = "image"
            media["filename"] = "emoji.png"
            media["content_type"] = "image/png"
            return media
    except Exception:
        pass

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg and _media_payload_kind(media) == "video":
        try:
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-frames:v", "1",
                 "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
                input=bytes(data), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False,
            )
            if result.returncode == 0 and result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
                media["bytes"] = result.stdout
                media["type"] = "image"
                media["filename"] = "emoji.png"
                media["content_type"] = "image/png"
                return media
        except Exception as exc:
            debug_logger.warning("MAX_EMOJI_PREVIEW_FAILED chat_id=%s error=%s", chat_id, log_preview(exc, 180))
    media["type"] = "image"
    return media


def _prepare_static_sticker_image(media: dict, chat_id: int = 0) -> dict:
    data = media.get("bytes")
    if not isinstance(data, (bytes, bytearray)) or not data:
        return media
    try:
        from PIL import Image
        with Image.open(io.BytesIO(bytes(data))) as source:
            source.seek(0)
            frame = source.convert("RGBA")
            output = io.BytesIO()
            frame.save(output, format="PNG")
        media["bytes"] = output.getvalue()
        media["type"] = "image"
        media["filename"] = "sticker.png"
        media["content_type"] = "image/png"
        media["hash"] = get_media_hash(data=media["bytes"])
    except Exception as exc:
        debug_logger.warning("MAX_STICKER_CONVERSION_FAILED chat_id=%s error=%s", chat_id, log_preview(exc, 180))
    return media


def _encode_animated_image_frames(data: bytes, ffmpeg: str, chat_id: int = 0) -> bytes | None:
    """Decode animated WebP/APNG frames and encode one cycle as MP4 raw video."""
    try:
        from PIL import Image, ImageSequence

        source = Image.open(io.BytesIO(data))
        if int(getattr(source, "n_frames", 1)) < 2:
            return None
        width, height = source.size
        out_width = max(2, width + width % 2)
        out_height = max(2, height + height % 2)
        fps = 24
        raw_frames = bytearray()
        for index, frame in enumerate(ImageSequence.Iterator(source)):
            if index >= 900:
                raise ValueError("в анимированном WebP больше 900 кадров")
            rgba = frame.convert("RGBA")
            if rgba.size != (width, height):
                rgba = rgba.resize((width, height), Image.Resampling.LANCZOS)
            rgb = Image.new("RGB", (out_width, out_height), (0, 0, 0))
            rgb.paste(rgba, (0, 0), rgba.getchannel("A"))
            duration_ms = max(10, int(frame.info.get("duration") or source.info.get("duration") or 100))
            repeats = max(1, round(duration_ms * fps / 1000))
            frame_bytes = rgb.tobytes()
            raw_frames.extend(frame_bytes * repeats)
        if not raw_frames:
            return None
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{out_width}x{out_height}",
            "-r", str(fps), "-i", "pipe:0", "-map", "0:v:0", "-an",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", "pipe:1",
        ]
        converted = subprocess.run(
            command, input=bytes(raw_frames), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=45, check=False,
        )
        if converted.returncode == 0 and converted.stdout and b"ftyp" in converted.stdout[:64]:
            return converted.stdout
        detail = converted.stderr.decode("utf-8", "replace")[-300:]
        print(f"[{chat_id}] ⚠️ Не удалось собрать MP4 из кадров анимации: {detail}")
    except Exception as exc:
        print(f"[{chat_id}] ⚠️ Не удалось прочитать кадры анимации: {exc}")
    return None


def _media_payload_kind(media: dict) -> str:
    """Classify downloaded bytes by payload, independent of a stale DOM label."""
    data = media.get("bytes")
    if not isinstance(data, (bytes, bytearray)):
        return ""
    data = bytes(data)
    content_type = str(media.get("content_type") or "").lower().split(";", 1)[0].strip()
    if (content_type.startswith("video/") and not content_type.startswith("video/ogg")) or data[4:8] == b"ftyp" or data.startswith(b"\x1aE\xdf\xa3"):
        return "video"
    if content_type.startswith("audio/") or data.startswith((b"OggS", b"ID3", b"fLaC")):
        return "audio"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "audio"
    return ""


def _media_satisfies_hint(media: dict, msg: dict) -> bool:
    """A voice/circle hint is valid only when the bytes match the expected stream."""
    if msg.get("video_note_hint"):
        return _media_payload_kind(media) == "video"
    if msg.get("voice_hint"):
        return _media_payload_kind(media) == "audio"
    return True


_MEDIA_DURATION_CACHE = {}


def _probe_media_duration(data: bytes) -> float | None:
    """Read a media duration from memory so adjacent lazy voice responses can be matched."""
    if not data:
        return None
    media_hash = get_media_hash(data=data)
    if media_hash in _MEDIA_DURATION_CACHE:
        return _MEDIA_DURATION_CACHE[media_hash]
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            ffmpeg = None
    duration = None
    if data.startswith(b"OggS"):
        try:
            offset = 0
            last_granule = 0
            while True:
                page_start = data.find(b"OggS", offset)
                if page_start < 0 or page_start + 27 > len(data):
                    break
                segment_count = data[page_start + 26]
                lace_start = page_start + 27
                lace_end = lace_start + segment_count
                if lace_end > len(data):
                    break
                page_size = sum(data[lace_start:lace_end])
                granule = int.from_bytes(data[page_start + 6:page_start + 14], "little")
                if granule < 0xFFFFFFFFFFFFFFFF:
                    last_granule = max(last_granule, granule)
                offset = lace_end + page_size
                if offset <= page_start or offset >= len(data):
                    break
            opus_head = data.find(b"OpusHead")
            if last_granule and opus_head >= 0 and opus_head + 12 <= len(data):
                pre_skip = int.from_bytes(data[opus_head + 10:opus_head + 12], "little")
                duration = max(0.0, (last_granule - pre_skip) / 48000.0)
        except Exception:
            duration = None
    if ffmpeg:
        try:
            if duration is not None:
                _MEDIA_DURATION_CACHE[media_hash] = duration
                return duration
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-i", "pipe:0", "-f", "null", "-"],
                input=bytes(data), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=12, check=False,
            )
            text = result.stderr.decode("utf-8", "replace")
            match = re.search(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", text)
            if match:
                duration = int(match.group(1))*3600 + int(match.group(2))*60 + float(match.group(3))
        except Exception:
            duration = None
    _MEDIA_DURATION_CACHE[media_hash] = duration
    return duration


def _install_max_media_response_cache(page, chat_id: int) -> None:
    """Capture audio/video responses during history warmup, before lazy cards are clicked."""
    page._max_media_response_cache = []
    page._max_media_claimed_responses = set()

    def on_response(resp):
        try:
            content_type = str(resp.headers.get("content-type") or "").lower()
            if not any(kind in content_type for kind in ("audio/", "video/", "octet-stream", "binary")):
                return
            url = str(resp.url or "")
            parsed_url = urlparse(url)
            path = (parsed_url.netloc + parsed_url.path)[:180]
            # Ignore MAX's bundled call/ringtone sounds; only message media belongs here.
            if "/_app/immutable/assets/" in parsed_url.path:
                return
            body = resp.body()
            if not body or len(body) <= 300 or len(body) > 25 * 1024 * 1024:
                return
            media_kind = _media_payload_kind({"bytes": body, "content_type": content_type})
            if media_kind not in ("audio", "video"):
                return
            entry = {
                "url": url,
                "path": path,
                "bytes": body,
                "type": media_kind,
                "content_type": content_type,
                "content_range": str(resp.headers.get("content-range") or ""),
                "response_order": len(page._max_media_response_cache),
                "hash": get_media_hash(data=body),
            }
            page._max_media_response_cache.append(entry)
            debug_logger.info(
                "MAX_MEDIA_PRELOAD_CAPTURED chat_id=%s order=%s type=%s endpoint=%s content_type=%s bytes=%s content_range=%s",
                chat_id, entry["response_order"], media_kind, path, content_type,
                len(body), entry["content_range"] or "<none>"
            )
        except Exception:
            pass

    page._max_media_response_listener = on_response
    page.on("response", on_response)


def _take_preloaded_media(page, msg: dict, expected_type: str, chat_id: int) -> list[dict]:
    """Use a matching lazy-loaded MAX response when its card has no DOM URL."""
    wanted = "audio" if expected_type == "voice" else "video"
    claimed = getattr(page, "_max_media_claimed_responses", set())
    responses = getattr(page, "_max_media_response_cache", [])
    wanted_duration = float(msg.get("media_duration_seconds") or 0)
    candidates = []
    for entry in responses:
        order = entry.get("response_order")
        if order in claimed or entry.get("type") != wanted:
            continue
        path = str(entry.get("path") or "").lower()
        if expected_type == "video_note":
            if not any(marker in path for marker in ("videomsg", "video-message", "video_note", "video-note")):
                continue
        elif expected_type == "voice":
            if "/audio" not in path and "audio" not in path:
                continue
        data = bytes(entry.get("bytes") or b"")
        content_range = str(entry.get("content_range") or "")
        range_match = re.search(r"bytes\s+\d+-\d+/(\d+)", content_range, re.I)
        if expected_type == "video_note" and range_match and int(range_match.group(1)) > len(data):
            expected_size = int(range_match.group(1))
            try:
                full = page.context.request.get(
                    entry["url"], headers={"Referer": "https://web.max.ru/"},
                    timeout=60_000, fail_on_status_code=False,
                )
                full_data = full.body() if full.ok else b""
            except Exception as exc:
                debug_logger.warning(
                    "MAX_MEDIA_PRELOAD_RANGE_FAILED chat_id=%s source_id=%s response_order=%s error=%s",
                    chat_id, msg.get("source_id") or msg.get("idx"), order, log_preview(exc, 180),
                )
                continue
            if len(full_data) < expected_size:
                debug_logger.warning(
                    "MAX_MEDIA_PRELOAD_INCOMPLETE_RANGE chat_id=%s source_id=%s response_order=%s expected_bytes=%s actual_bytes=%s",
                    chat_id, msg.get("source_id") or msg.get("idx"), order,
                    expected_size, len(full_data),
                )
                continue
            data = full_data
            content_range = "full-response"
            debug_logger.info(
                "MAX_MEDIA_PRELOAD_RANGE_COMPLETED chat_id=%s source_id=%s response_order=%s bytes=%s",
                chat_id, msg.get("source_id") or msg.get("idx"), order, len(data),
            )
        actual_duration = _probe_media_duration(data) if wanted_duration else None
        difference = abs(actual_duration - wanted_duration) if actual_duration is not None else float("inf")
        candidates.append((difference, order, entry, data, actual_duration, content_range))

    if not candidates:
        return []
    # Nearby MAX cards may prefetch more than one audio stream at once. Match the
    # duration displayed on this exact card before falling back to response order.
    candidates.sort(key=lambda item: (item[0], item[1]))
    difference, order, entry, data, actual_duration, content_range = candidates[0]
    if difference != float("inf") and difference > max(1.25, wanted_duration * 0.75):
        debug_logger.warning(
            "MAX_MEDIA_PRELOAD_DURATION_MISMATCH chat_id=%s source_id=%s expected_type=%s wanted_seconds=%.2f candidate_seconds=%.2f",
            chat_id, msg.get("source_id") or msg.get("idx"), expected_type,
            wanted_duration, actual_duration,
        )
        return []
    range_match = re.search(r"bytes\s+\d+-\d+/(\d+)", content_range, re.I)
    if range_match and int(range_match.group(1)) > len(data):
        try:
            full = page.context.request.get(
                entry["url"], headers={"Referer": "https://web.max.ru/"},
                timeout=60_000, fail_on_status_code=False,
            )
            full_data = full.body() if full.ok else b""
            if len(full_data) >= int(range_match.group(1)):
                data = full_data
                content_range = "full-response"
        except Exception:
            pass
    if not _media_satisfies_hint({"bytes": data, "content_type": entry.get("content_type")}, msg):
        return []
    claimed.add(order)
    page._max_media_claimed_responses = claimed
    suffix = ".ogg" if wanted == "audio" and "ogg" in entry.get("content_type", "") else ".mp4" if wanted == "video" and "mp4" in entry.get("content_type", "") else ".bin"
    media = {
        "url": entry.get("url") or "",
        "bytes": data,
        "type": expected_type,
        "filename": ("voice" if wanted == "audio" else "video_note") + suffix,
        "content_type": entry.get("content_type") or "",
        "hash": get_media_hash(data=data),
    }
    debug_logger.info(
        "MAX_MEDIA_PRELOAD_USED chat_id=%s source_id=%s expected=%s response_order=%s bytes=%s content_type=%s duration_seconds=%s content_range=%s",
        chat_id, msg.get("source_id") or msg.get("idx"), expected_type,
        order, len(data), media["content_type"] or "<none>", actual_duration, content_range or "<none>"
    )
    return [media]


def _claim_preloaded_media(page, media_files: list[dict]) -> None:
    claimed = getattr(page, "_max_media_claimed_responses", set())
    hashes = {m.get("hash") or get_media_hash(data=m.get("bytes") or b"") for m in media_files}
    for entry in getattr(page, "_max_media_response_cache", []):
        if entry.get("hash") in hashes:
            claimed.add(entry.get("response_order"))
    page._max_media_claimed_responses = claimed


def _capture_video_canvas(page, msg: dict, chat_id: int = 0) -> list[dict]:
    """Record MAX's canvas-rendered circular video through visible browser frames."""
    try:
        card = _strict_message_card_locator(page, msg)
        if not card.count():
            return []
        canvas = card.locator("canvas.videoCanvas").first
        if not canvas.count():
            return []
        info = card.evaluate("""card=>{
            const c=card.querySelector('canvas.videoCanvas');
            const t=(card.querySelector('.videoMessage .time')?.textContent||'').trim();
            const m=t.match(/^(\\d{1,2}):(\\d{2})$/);
            return {width:c?.width||0,height:c?.height||0,durationMs:m?Math.max(500,(Number(m[1])*60+Number(m[2]))*1000):1000};
        }""")
        fps = 15
        wanted_duration = float(msg.get("media_duration_seconds") or 0)
        duration_ms = round(wanted_duration * 1000) if wanted_duration > 0 else int(info.get("durationMs") or 1000)
        duration_ms = max(1000, min(10000, duration_ms))
        frame_count = max(15, min(150, round((duration_ms / 1000) * fps)))
        frames = []
        for _ in range(frame_count):
            frames.append(canvas.screenshot(type="png", animations="allow", timeout=4000))
            page.wait_for_timeout(round(1000 / fps))

        distinct_visuals = []
        for frame in frames:
            visual = _image_visual_hash(frame)
            if visual and not any(_same_visual_image(visual, previous) for previous in distinct_visuals):
                distinct_visuals.append(visual)
        if len(distinct_visuals) < 2:
            debug_logger.warning(
                "MAX_CANVAS_VIDEO_CAPTURE_REJECTED chat_id=%s source_id=%s reason=static_frame frames=%s distinct=%s duration_ms=%s",
                chat_id, msg.get("source_id") or msg.get("idx"), len(frames), len(distinct_visuals), duration_ms
            )
            return []

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            try:
                import imageio_ffmpeg
                ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                ffmpeg = None
        if not ffmpeg:
            raise RuntimeError("ffmpeg отсутствует; кадры MAX есть, но MP4 собрать нечем")
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "image2pipe",
            "-vcodec", "png", "-framerate", str(fps), "-i", "pipe:0", "-an",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", "pipe:1",
        ]
        converted = subprocess.run(
            command, input=b"".join(frames), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60, check=False,
        )
        data = converted.stdout
        if converted.returncode == 0 and len(data) > 500 and b"ftyp" in data[:64]:
            media = {
                "url": "canvas-recording:max-video-note",
                "bytes": data,
                "type": "video_note",
                "filename": "video_note.mp4",
                "content_type": "video/mp4",
                "hash": get_media_hash(data=data),
            }
            debug_logger.info(
                "MAX_CANVAS_VIDEO_CAPTURED chat_id=%s source_id=%s bytes=%s duration_ms=%s frames=%s fps=%s size=%sx%s content_type=%s",
                chat_id, msg.get("source_id") or msg.get("idx"), len(data), duration_ms,
                len(frames), fps, info.get("width"), info.get("height"), media["content_type"]
            )
            return [media]
        detail = converted.stderr.decode("utf-8", "replace")[-300:]
        debug_logger.warning(
            "MAX_CANVAS_VIDEO_CAPTURE_EMPTY chat_id=%s source_id=%s detail=%s",
            chat_id, msg.get("source_id") or msg.get("idx"), log_preview(detail or f"bytes={len(data)}", 160)
        )
    except Exception as exc:
        debug_logger.warning(
            "MAX_CANVAS_VIDEO_CAPTURE_EMPTY chat_id=%s source_id=%s detail=%s",
            chat_id, msg.get("source_id") or msg.get("idx"), log_preview(exc, 160)
        )
    return []


def _capture_animated_sticker(page, msg: dict, chat_id: int = 0) -> list[dict]:
    """Record a rendered sticker when MAX exposes only its first PNG frame."""
    try:
        card = _strict_message_card_locator(page, msg)
        if card.count() == 0:
            return []
        selector = 'video,canvas,img,svg,[style*="background-image" i],[class*="sticker" i],[data-sticker],[data-testid*="sticker" i],[aria-label*="sticker" i],[aria-label*="стикер" i]'
        nodes = card.locator(selector)
        candidates = nodes.evaluate_all("""nodes => nodes.map((el,index) => {
            const r=el.getBoundingClientRect();
            const meta=[];
            for(let n=el,d=0;n&&d<5;n=n.parentElement,d++) {
                meta.push(n.className||'',n.getAttribute?.('data-testid')||'',n.getAttribute?.('data-sticker')||'',
                    n.getAttribute?.('aria-label')||'',n.getAttribute?.('data-type')||'');
            }
            const text=meta.join(' ').toLowerCase();
            const tag=String(el.tagName||'').toLowerCase();
            let duration=tag==='video' && Number.isFinite(el.duration) ? el.duration : 0;
            let animated=['video','canvas'].includes(tag) || /animated|lottie/.test(text);
            try {
                const style=getComputedStyle(el);
                const cssDurations=String(style.animationDuration||'').split(',').map(value=>{
                    const n=parseFloat(value)||0;
                    return value.includes('ms') ? n/1000 : n;
                });
                const cssDuration=Math.max(0,...cssDurations);
                if(!duration) duration=cssDuration;
                if(cssDuration>0 && style.animationName!=='none') animated=true;
            } catch(e) {}
            const src=String(el.currentSrc||el.src||el.getAttribute?.('src')||'');
            if(/\\.(?:gif|webp)(?:[?#]|$)/i.test(src)) animated=true;
            return {index,tag,meta:text,width:r.width||0,height:r.height||0,duration,animated};
        })""")
        valid = [
            item for item in (candidates or [])
            if re.search(r"sticker|стикер", str(item.get("meta") or ""), re.I)
            and float(item.get("width") or 0) >= 20
            and float(item.get("height") or 0) >= 20
        ]
        if not valid:
            return []
        valid.sort(key=lambda item: (
            0 if item.get("animated") else 1,
            0 if item.get("tag") in {"video", "canvas", "img", "svg"} else 1,
            float(item.get("width") or 0) * float(item.get("height") or 0),
        ))
        chosen = valid[0]
        sticker = nodes.nth(int(chosen["index"]))
        try:
            sticker.scroll_into_view_if_needed(timeout=1500)
        except Exception:
            pass
        try:
            sticker.evaluate("""el => {
                if(String(el.tagName||'').toLowerCase()==='video') {
                    el.muted=true;
                    const p=el.play(); if(p && p.catch) p.catch(()=>{});
                }
            }""")
        except Exception:
            pass

        fps = 12
        duration = float(chosen.get("duration") or 0) or float(msg.get("media_duration_seconds") or 0) or 3.0
        duration = min(10.0, max(1.0, duration))
        frame_count = max(12, min(120, round(duration * fps)))
        frames = []
        distinct = []
        for index in range(frame_count):
            frame = sticker.screenshot(type="png", animations="allow", timeout=5000)
            if not frame:
                return []
            frames.append(frame)
            visual = _image_visual_hash(frame)
            if visual and not any(_same_visual_image(visual, previous) for previous in distinct):
                distinct.append(visual)
            elif visual is None and frame != (frames[-2] if len(frames) > 1 else None):
                distinct.append((index,))
            if index == min(frame_count - 1, fps - 1) and len(distinct) < 2:
                return []
            if index + 1 < frame_count:
                page.wait_for_timeout(round(1000 / fps))
        if len(distinct) < 2:
            return []

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            try:
                import imageio_ffmpeg
                ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                ffmpeg = None
        if not ffmpeg:
            debug_logger.warning("MAX_STICKER_VIDEO_CAPTURE_FAILED chat_id=%s reason=ffmpeg_missing", chat_id)
            return []
        try:
            from PIL import Image
            with Image.open(io.BytesIO(frames[0])) as image:
                side = max(image.size)
        except Exception:
            side = max(int(chosen.get("width") or 0), int(chosen.get("height") or 0), 2)
        side = max(2, side + side % 2)
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "image2pipe",
            "-vcodec", "png", "-framerate", str(fps), "-i", "pipe:0", "-an",
            "-vf", f"scale={side}:{side}:force_original_aspect_ratio=decrease,pad={side}:{side}:(ow-iw)/2:(oh-ih)/2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1",
        ]
        converted = subprocess.run(
            command, input=b"".join(frames), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=60, check=False,
        )
        data = converted.stdout
        if converted.returncode != 0 or len(data) <= 500 or b"ftyp" not in data[:64]:
            debug_logger.warning(
                "MAX_STICKER_VIDEO_CAPTURE_FAILED chat_id=%s source_id=%s detail=%s",
                chat_id, msg.get("source_id") or msg.get("idx"),
                log_preview(converted.stderr.decode("utf-8", "replace")[-240:], 160),
            )
            return []
        media = {
            "url": "sticker-recording:max-sticker",
            "bytes": data,
            "type": "video",
            "filename": "animated_sticker.mp4",
            "content_type": "video/mp4",
            "hash": get_media_hash(data=data),
        }
        debug_logger.info(
            "MAX_STICKER_VIDEO_CAPTURED chat_id=%s source_id=%s bytes=%s duration_seconds=%.2f frames=%s size=%sx%s",
            chat_id, msg.get("source_id") or msg.get("idx"), len(data), duration,
            len(frames), side, side,
        )
        return [media]
    except Exception as exc:
        debug_logger.warning(
            "MAX_STICKER_VIDEO_CAPTURE_FAILED chat_id=%s source_id=%s error=%s",
            chat_id, msg.get("source_id") or msg.get("idx"), log_preview(exc, 180),
        )
        return []


def _record_animated_image_as_video(media: dict, chat_id: int = 0) -> dict:
    """Convert animated sticker image payloads into an MP4 Telegram can play as video."""
    if media.get("type") not in {"image", "sticker"}:
        return media
    data = media.get("bytes")
    if not isinstance(data, (bytes, bytearray)):
        return media
    kind = _animated_image_kind(bytes(data))
    if not kind:
        return media

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            ffmpeg = None
    if not ffmpeg:
        if kind == "gif":
            media["type"] = "animation"
            media["filename"] = "animated_sticker.gif"
        else:
            print(f"[{chat_id}] ⚠️ Для преобразования анимированного WebP в MP4 нужен ffmpeg")
        return media

    if kind in {"webp", "apng"}:
        output = _encode_animated_image_frames(bytes(data), ffmpeg, chat_id=chat_id)
        if output:
            media["bytes"] = output
            media["type"] = "video"
            media["filename"] = "animated_sticker.mp4"
            media["content_type"] = "video/mp4"
            media["hash"] = get_media_hash(data=output)
        return media

    command = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if kind == "gif":
        command.extend(["-ignore_loop", "1"])
    command.extend([
        "-f", kind, "-i", "pipe:0", "-map", "0:v:0", "-an",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ])
    try:
        converted = subprocess.run(
            command,
            input=bytes(data),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=45,
            check=False,
        )
        output = converted.stdout
        if converted.returncode == 0 and output and b"ftyp" in output[:64]:
            media["bytes"] = output
            media["type"] = "video"
            media["filename"] = "animated_sticker.mp4"
            media["content_type"] = "video/mp4"
            media["hash"] = get_media_hash(data=output)
        else:
            detail = converted.stderr.decode("utf-8", "replace")[-500:]
            print(f"[{chat_id}] ⚠️ Не удалось записать анимированный стикер в MP4: {detail}")
            if kind == "gif":
                media["type"] = "animation"
                media["filename"] = "animated_sticker.gif"
    except Exception as exc:
        print(f"[{chat_id}] ⚠️ Ошибка преобразования анимированного стикера: {exc}")
        if kind == "gif":
            media["type"] = "animation"
            media["filename"] = "animated_sticker.gif"
    return media


def _prepare_video_note_for_telegram(media: dict, chat_id: int = 0) -> dict:
    """Telegram video notes need MP4; transcode MAX WebM circles when needed."""
    if media.get("type") != "video_note":
        return media
    data = media.get("bytes")
    if not isinstance(data, (bytes, bytearray)):
        return media
    if len(data) > 12 and data[4:8] == b"ftyp":
        media["filename"] = "video_note.mp4"
        media["content_type"] = "video/mp4"
        return media
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            ffmpeg = None
    if not ffmpeg:
        media["type"] = "video"
        media["filename"] = _media_filename("video_note.bin", media.get("url", ""), media.get("content_type", ""), bytes(data), "video")
        print(f"[{chat_id}] ⚠️ Для кружка MAX без MP4 нужен ffmpeg; отправляю исходное видео")
        return media
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
        "-map", "0:v:0", "-an", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ]
    try:
        converted = subprocess.run(
            command, input=bytes(data), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60, check=False,
        )
        output = converted.stdout
        if converted.returncode == 0 and output and b"ftyp" in output[:64]:
            media["bytes"] = output
            media["filename"] = "video_note.mp4"
            media["content_type"] = "video/mp4"
            media["hash"] = get_media_hash(data=output)
        else:
            media["type"] = "video"
            media["filename"] = _media_filename("video_note.bin", media.get("url", ""), media.get("content_type", ""), bytes(data), "video")
            detail = converted.stderr.decode("utf-8", "replace")[-500:]
            print(f"[{chat_id}] ⚠️ Не удалось подготовить MP4-кружок: {detail}")
    except Exception as exc:
        media["type"] = "video"
        media["filename"] = _media_filename("video_note.bin", media.get("url", ""), media.get("content_type", ""), bytes(data), "video")
        print(f"[{chat_id}] ⚠️ Ошибка подготовки MP4-кружка: {exc}")
    return media

def _strict_message_card_locator(page, msg: dict):
    """Return only the exact DOM card tagged while parsing this message.

    Media fallbacks must never guess a neighboring .item: MAX virtualizes the
    feed and content-based matching can resolve the wrong message.
    """
    dom_id = str(msg.get("dom_id") or "").strip()
    if dom_id:
        loc = page.locator(f'[data-max-parser-message-id="{dom_id}"]').first
        try:
            if loc.count() > 0:
                return loc
        except Exception:
            pass
    for key in ("data_message_id", "mid", "data_id"):
        value = str(msg.get(key) or "").strip()
        if not value:
            continue
        safe = value.replace("\\", "\\\\").replace('"', '\\"')
        loc = page.locator(
            f'.item[data-message-id="{safe}"],.item[data-mid="{safe}"],.item[data-id="{safe}"]'
        ).first
        try:
            if loc.count() > 0:
                return loc
        except Exception:
            pass
    source_id = str(msg.get("source_id") or "").strip()
    if source_id and bool(msg.get("source_id_is_index")):
        safe = source_id.replace("\\", "\\\\").replace('"', '\\"')
        loc = page.locator(f'.item[data-index="{safe}"]').first
        try:
            if loc.count() > 0:
                return loc
        except Exception:
            pass
    return page.locator('[data-max-parser-message-id="__max_parser_not_found__"]').first


def _message_card_locator(page, msg: dict):
    """Find a MAX .item for a parsed message, with stable fallbacks.

    MAX virtualizes the message list, so the custom parser attribute may disappear
    when the list is recycled.  Keep several local identifiers and fall back to
    data-index / original DOM index instead of giving up on the media.
    """
    candidates = []
    dom_id = str(msg.get("dom_id") or "").strip()
    if dom_id:
        candidates.append(page.locator(f'[data-max-parser-message-id="{dom_id}"]').first)

    # data-index is much more stable in MAX than our temporary parser attribute.
    source_id = str(msg.get("source_id") or "").strip()
    if source_id and bool(msg.get("source_id_is_index")):
        safe = source_id.replace("\\", "\\\\").replace('"', '\\"')
        candidates.append(page.locator(f'.item[data-index="{safe}"]').first)

    for key in ("data_message_id", "message_id", "mid", "data_id"):
        value = str(msg.get(key) or "").strip()
        if value:
            safe = value.replace("\\", "\\\\").replace('"', '\\"')
            candidates.append(page.locator(
                f'.item[data-message-id="{safe}"],.item[data-mid="{safe}"],.item[data-id="{safe}"]'
            ).first)

    for loc in candidates:
        try:
            if loc.count() > 0:
                return loc
        except Exception:
            continue

    # В виртуализированном списке data-index может меняться. Ищем карточку
    # по содержимому конкретного сообщения, чтобы media fallback не попал
    # в соседний .item.
    try:
        text_hint = " ".join(str(msg.get("text") or "").split()).strip().lower()
        name_hint = " ".join(str(msg.get("name") or "").split()).strip().lower()
        time_hint = " ".join(str(msg.get("time") or "").split()).strip().lower()
        items = page.locator(".item")
        matched = items.evaluate_all(
            r"""(els, q) => {
                const clean = s => String(s||'').replace(/\s+/g,' ').trim().toLowerCase();
                for (let i=0;i<els.length;i++) {
                    const row = els[i];
                    const all = clean(row.innerText);
                    const time = clean((row.querySelector('.meta .text,.meta time,time')||{}).textContent);
                    const name = clean((row.querySelector('.bubbleContent .header .name .text,.bubbleContent .header .name,.header .name')||{}).textContent);
                    const textOk = !q.text || all.includes(q.text);
                    const timeOk = !q.time || all.includes(q.time) || time===q.time;
                    const nameOk = !q.name || !name || name.includes(q.name) || all.includes(q.name);
                    if (textOk && timeOk && nameOk) return i;
                }
                return -1;
            }""",
            {"text": text_hint, "name": name_hint, "time": time_hint},
        )
        if isinstance(matched, int) and matched >= 0:
            return items.nth(matched)
    except Exception:
        pass

    # Никогда не возвращаем произвольную .item: это могло отправить медиа
    # соседнего сообщения.
    return page.locator('[data-max-parser-message-id="__max_parser_not_found__"]').first


def _download_document_to_memory(page, context, url: str, filename: str = "", chat_id: int = 0, media_type: str = "document") -> dict | None:
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
        if media_type in {"voice", "audio", "video", "video_note", "animation"}:
            final_name = _media_filename(final_name, url, content_type, file_bytes, media_type)
        return {
            "url": url,
            "bytes": file_bytes,
            "type": media_type,
            "filename": final_name,
            "content_type": content_type,
        }
    except Exception as e:
        print(f"[{chat_id}] ❌ Не удалось получить документ в память: {e}")
        return None

def _download_document_from_message_card(page, context, msg: dict, chat_id: int = 0, media_type: str = "document") -> dict | None:
    """Find the visible MAX file card and obtain its attachment without saving it to disk."""
    dom_id = (msg.get("dom_id") or "").strip()
    if not dom_id:
        return None
    filename_hint = ""
    text = msg.get("text", "") or ""
    m = re.search(
        r'([^<>:"/\\|?*]{1,220}\.(?:pdf|doc|docx|xls|xlsx|ppt|pptx|zip|rar|txt|rtf|odt|ods|odp))' ,
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
                            media_type=media_type,
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
        card = _message_card_locator(page, msg)
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
                media_type=media_type,
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
                    media_type=media_type,
                )
        except Exception as e:
            print(f"[{chat_id}] ℹ️ Карточка файла не дала download event: {e}")
    except Exception as e:
        print(f"[{chat_id}] ❌ Ошибка поиска карточки документа: {e}")
    return None

def _refresh_card_image_urls(page, msg: dict, chat_id: int = 0) -> list[str]:
    """Re-read image URLs from the live MAX card immediately before download."""
    out=[]; seen=set()
    try:
        card=_strict_message_card_locator(page,msg)
        if card.count()==0: return []
        try:
            card.scroll_into_view_if_needed(timeout=1500)
            page.wait_for_timeout(140)
            imgs=card.locator("img")
            for i in range(min(imgs.count(), 12)):
                try:
                    imgs.nth(i).scroll_into_view_if_needed(timeout=700)
                    page.wait_for_timeout(90)
                except Exception:
                    continue
            card.evaluate("""card=>{
                for(const img of card.querySelectorAll('img')){
                    try{
                        img.loading='eager';
                        const attrs=['data-src','data-original','data-lazy-src','data-image','data-photo','data-image-url','data-photo-url','data-media-url','data-original-src','data-attachment-url','data-content-url'];
                        const cur=img.currentSrc||img.getAttribute('src')||'';
                        if(!cur || /^data:image/i.test(cur)){
                            for(const a of attrs){ const v=img.getAttribute(a)||''; if(/^(https?:|blob:)/i.test(v)){ img.src=v; break; } }
                        }
                    }catch(e){}
                }
            }""")
            page.wait_for_timeout(350)
        except Exception: pass
        values=card.locator("img, picture source, a[href]").evaluate_all(r"""els=>els.map(el=>{
            const tag=(el.tagName||'').toLowerCase();
            const meta=[el.className||'',el.getAttribute?.('data-testid')||'',el.getAttribute?.('aria-label')||'',el.getAttribute?.('alt')||'',el.getAttribute?.('title')||'',el.getAttribute?.('data-type')||'',el.parentElement?.className||''].join(' ').toLowerCase();
            const attrs=['src','current-src','data-src','data-original','data-lazy-src','data-image','data-photo','data-image-url','data-photo-url','data-media-url','data-original-src','data-attachment-url','data-content-url','href'];
            const urls=[];
            for(const a of attrs){ let v=(a==='src'&&el.currentSrc)?el.currentSrc:(el.getAttribute?.(a)||''); if(v&&/^(https?:|blob:)/i.test(v)) urls.push(v); }
            try{ const bg=getComputedStyle(el).backgroundImage||''; const m=bg.match(/url\([\"']?(.*?)[\"']?\)/i); if(m&&/^(https?:|blob:)/i.test(m[1])) urls.push(m[1]); }catch(e){}
            const href=(el.getAttribute?.('href')||'');
            const anchorLooksLikeImage=tag!=='a' || /(?:\.(?:jpe?g|png|gif|webp|bmp)(?:[?#]|$))|^blob:|photo|image|media|attachment|picture/i.test(href+' '+meta);
            return {tag,meta,urls:anchorLooksLikeImage?urls:urls.filter(u=>u!==href)};
        })""")
        for item in values:
            meta=str(item.get('meta') or '')
            if re.search(r'avatar|userpic|profile|favicon|spacer|blank',meta,re.I): continue
            if re.search(r'emoji|animated\s*emoji|эмодзи|emoticon|smiley',meta,re.I) and not re.search(r'sticker|стикер',meta,re.I): continue
            for url in item.get('urls') or []:
                url=str(url).strip()
                if url and url not in seen:
                    seen.add(url); out.append(url)
    except Exception as exc:
        print(f"[{chat_id}] ℹ️ Не удалось повторно получить URL фото из карточки: {exc}")
    return out


def _warmup_captioned_photo_card(page, msg: dict, chat_id: int = 0) -> None:
    """Force only the current captioned-photo card to materialize its image."""
    try:
        card = _strict_message_card_locator(page, msg)
        if card.count() == 0:
            return
        card.scroll_into_view_if_needed(timeout=1500)
        page.wait_for_timeout(300)
        card.evaluate("""card => {
            const attrs = ['data-src','data-original','data-lazy-src','data-image','data-photo',
                'data-image-url','data-photo-url','data-media-url','data-original-src',
                'data-attachment-url','data-content-url'];
            for (const img of card.querySelectorAll('img')) {
                try {
                    img.loading = 'eager';
                    img.decoding = 'sync';
                    if (!img.currentSrc || /^data:image/i.test(img.currentSrc)) {
                        for (const a of attrs) {
                            const v = img.getAttribute(a) || '';
                            if (/^(https?:|blob:)/i.test(v)) {
                                img.src = v;
                                break;
                            }
                        }
                    }
                } catch(e) {}
            }
            for (const el of card.querySelectorAll('img, [style*="background-image" i], [data-image-url], [data-photo-url], [data-media-url]')) {
                try { el.scrollIntoView({block:'center', inline:'nearest'}); } catch(e) {}
            }
        }""")
        page.wait_for_timeout(700)
    except Exception as exc:
        print(f"[{chat_id}] ℹ️ Не удалось прогреть фото с caption: {exc}")


def _refresh_emoji_texts(page, msg: dict, chat_id: int = 0) -> list[str]:
    """Refresh Unicode emoji from the exact card after MAX lazy-renders it."""
    try:
        card = _strict_message_card_locator(page, msg)
        if card.count() == 0:
            return []
        try:
            card.scroll_into_view_if_needed(timeout=1200)
            page.wait_for_timeout(350)
        except Exception:
            pass
        values = card.evaluate("""card => {
            const out=[];
            const add=(s,nodeSeen)=>{
                s=String(s||'').trim(); if(!s) return;
                let ms=[]; try{ms=s.match(/[\\p{Extended_Pictographic}](?:\\uFE0F|\\u200D[\\p{Extended_Pictographic}])*/gu)||[];}catch(e){}
                for(const x of ms){if(!nodeSeen.has(x)){nodeSeen.add(x);out.push(x);}}
            };
            const cp=(s,nodeSeen)=>{
                const raw=String(s||'');
                const explicit=raw.match(/(?:U\\+|0x)[0-9a-f]{4,6}/gi)||[];
                const parts=[...explicit.map(p=>p.replace(/^(?:U\\+|0x)/i,'')),...raw.split(/[^0-9a-f]+/i).filter(p=>/^[0-9a-f]{4,6}$/i.test(p)&&/\\d/.test(p))];
                const chars=[]; for(const token of parts){try{if(!/\\d/.test(token))continue;const n=parseInt(token,16);if(n>=0x2000&&n<=0x1FAFF)chars.push(String.fromCodePoint(n));}catch(e){}}
                if(chars.length)add(chars.join(''),nodeSeen);
            };
            const nodes=[card,...Array.from(card.querySelectorAll('*'))];
            for(const el of nodes){
                const nodeSeen=new Set();
                const ancestorMeta=[]; for(let n=el;n&&ancestorMeta.length<20;n=n.parentElement) ancestorMeta.push(n.className||'',n.getAttribute?.('data-type')||'',n.getAttribute?.('data-testid')||'',n.getAttribute?.('aria-label')||'');
                const meta=[...ancestorMeta,el.getAttribute?.('title')||'',el.getAttribute?.('role')||'',el.getAttribute?.('data-name')||'',el.getAttribute?.('data-icon')||'',el.getAttribute?.('data-emoji')||'',el.getAttribute?.('data-emoji-char')||'',el.getAttribute?.('data-emoji-text')||'',el.getAttribute?.('data-animated-emoji')||'',el.getAttribute?.('data-custom-emoji')||'',el.getAttribute?.('data-emoji-id')||'',el.getAttribute?.('data-emoji-name')||''].join(' ').toLowerCase();
                if(/sticker|стикер/.test(meta)) continue;
                if(!/(emoji|эмодзи|animated.?emoji|custom.?emoji|emoticon|smiley)/i.test(meta) && !/^(img|canvas|video)$/i.test(el.tagName||'') && !el.hasAttribute?.('data-custom-emoji') && !el.hasAttribute?.('data-emoji-id') && !el.hasAttribute?.('data-emoji-name')) continue;
                const attrs=['aria-label','alt','title','data-emoji','data-emoji-char','data-emoji-text','data-text','data-character','data-name','data-custom-emoji','data-emoji-name','data-emoji-alias'];
                for(const a of attrs) add(el.getAttribute?.(a),nodeSeen);
                for(const a of ['data-codepoint','data-codepoints','data-unicode','data-emoji-code','data-emoji-codepoint','data-code','data-custom-emoji']) cp(el.getAttribute?.(a),nodeSeen);
                add(el.textContent,nodeSeen);
                try{cp(String(el.outerHTML||''),nodeSeen);}catch(e){}
            }
            return out;
        }""")
        return [str(x) for x in (values or []) if str(x).strip()]
    except Exception as exc:
        print(f"[{chat_id}] ℹ️ Не удалось обновить emoji из карточки: {exc}")
        return []


def _screenshot_card_images(page, msg: dict, chat_id: int = 0) -> list[dict]:
    """Fallback for rendered photo nodes when MAX hides the original URL.
    Sticker images remain allowed; emoji visual nodes are excluded.
    """
    dom_id = str(msg.get("dom_id") or "").strip()
    if not dom_id:
        return []
    out=[]
    try:
        card=_strict_message_card_locator(page, msg)
        if card.count()==0: return []
        try:
            card.scroll_into_view_if_needed(timeout=1500)
            page.wait_for_timeout(250)
        except Exception:
            pass
        imgs=card.locator("img, picture img")
        for i in range(min(imgs.count(),20)):
            img=imgs.nth(i)
            try:
                info=img.evaluate("""el => {
                    const r=el.getBoundingClientRect();
                    const parent=el.parentElement;
                    const attrs={};
                    for(const a of ['alt','aria-label','title','data-type','data-testid','data-emoji','data-emoji-char','data-sticker','data-codepoint','data-codepoints','data-unicode','data-emoji-id','data-custom-emoji','data-animated-emoji','data-smile','data-character']) attrs[a]=el.getAttribute(a)||'';
                    const meta=[el.className||'',attrs['data-testid'],attrs['aria-label'],attrs['alt'],attrs['title'],attrs['data-type'],parent?.className||'',parent?.getAttribute?.('data-testid')||''].join(' ').toLowerCase();
                    return {w:r.width||0,h:r.height||0,nw:el.naturalWidth||0,nh:el.naturalHeight||0,meta,attrs,visible:!!(r.width&&r.height)};
                }""")
            except Exception: continue
            meta=str(info.get('meta') or '')
            attrs=info.get('attrs') or {}
            sticker_marker=bool(re.search(r'sticker|стикер',meta,re.I)) or bool(attrs.get('data-sticker'))
            hint=' '.join(str(v) for v in attrs.values())
            try:
                pictographic=bool(re.search(r'[\u2600-\u27BF\U0001F000-\U0001FAFF]',hint))
            except re.error:
                pictographic=False
            rw=max(float(info.get('w') or 0),float(info.get('nw') or 0)); rh=max(float(info.get('h') or 0),float(info.get('nh') or 0));
            if rw<20 or rh<20: continue
            if re.search(r'avatar|userpic|profile|favicon|spacer|blank',meta,re.I): continue
            if not sticker_marker and (re.search(r'emoji|animated.?emoji|эмодзи|emoticon|smiley',meta,re.I) or pictographic): continue
            if not sticker_marker and max(float(info.get('w') or 0),float(info.get('h') or 0)) <= 48 and not (msg.get('text') or '').strip().replace(' ',''):
                continue
            if not sticker_marker and re.search(r'file|document|attachment|download|pdf|docx?|xlsx?|pptx?|zip|rar',meta,re.I): continue
            try: data=img.screenshot(type='png',animations='disabled',timeout=5000)
            except Exception as exc:
                print(f"[{chat_id}] ℹ️ Screenshot фото #{i} не получен: {exc}"); continue
            if data and len(data)>200:
                out.append({'url':f'screenshot:{dom_id}:{i}','bytes':data,'type':'image','hash':get_media_hash(data=data),'content_type':'image/png','visual_fallback':True})
        try:
            candidates=card.locator("[style*='background-image' i], [data-image-url], [data-photo-url], [data-media-url]")
            for i in range(min(candidates.count(),20)):
                el=candidates.nth(i)
                try:
                    info=el.evaluate("""el=>{
                        const r=el.getBoundingClientRect();
                        const meta=[el.className||'',el.getAttribute('data-testid')||'',el.getAttribute('aria-label')||'',el.getAttribute('alt')||'',el.getAttribute('title')||'',el.getAttribute('data-type')||''].join(' ').toLowerCase();
                        return {w:r.width,h:r.height,meta};
                    }""")
                    meta=str(info.get('meta') or '')
                    if float(info.get('w') or 0)<40 or float(info.get('h') or 0)<40: continue
                    if re.search(r'avatar|userpic|profile|favicon|spacer|blank',meta,re.I): continue
                    if re.search(r'emoji|animated.?emoji|эмодзи',meta,re.I) and not re.search(r'sticker|стикер',meta,re.I): continue
                    data=el.screenshot(type='png',animations='disabled',timeout=5000)
                    if data and len(data)>500:
                        h=get_media_hash(data=data)
                        if not any(x.get('hash')==h for x in out):
                            out.append({'url':f'screenshot-bg:{dom_id}:{i}','bytes':data,'type':'image','hash':h,'content_type':'image/png','visual_fallback':True})
                except Exception:
                    continue
        except Exception:
            pass
        # Третий fallback: lazy-photo может быть дочерним img без URL, но картинка уже
        # реально отрисована. Берём screenshot ближайшего визуального контейнера с img.
        try:
            for i in range(min(imgs.count(), 20)):
                img=imgs.nth(i)
                try:
                    info=img.evaluate("""el=>{
                        const out=[]; let n=el;
                        for(let d=0; d<4 && n; d++, n=n.parentElement){
                            const r=n.getBoundingClientRect();
                            const meta=[n.className||'',n.getAttribute?.('data-testid')||'',n.getAttribute?.('aria-label')||'',n.getAttribute?.('data-type')||''].join(' ').toLowerCase();
                            out.push({w:r.width||0,h:r.height||0,meta});
                        }
                        return out;
                    }""")
                    for depth,inf in enumerate(info or []):
                        w=float(inf.get('w') or 0); h=float(inf.get('h') or 0); meta=str(inf.get('meta') or '')
                        if w<100 or h<80 or re.search(r'avatar|userpic|profile|favicon|emoji|animated.?emoji|эмодзи',meta,re.I):
                            continue
                        parent=img
                        for _ in range(depth):
                            parent=parent.locator("xpath=..")
                        try:
                            data=parent.screenshot(type='png',animations='disabled',timeout=5000)
                        except Exception:
                            continue
                        if data and len(data)>500:
                            hsh=get_media_hash(data=data)
                            if not any(x.get('hash')==hsh for x in out):
                                out.append({'url':f'screenshot-parent:{dom_id}:{i}:{depth}','bytes':data,'type':'image','hash':hsh,'content_type':'image/png','visual_fallback':True})
                            break
                except Exception:
                    continue
        except Exception:
            pass
    except Exception as exc:
        print(f"[{chat_id}] ℹ️ Fallback screenshot gallery: {exc}")
    return out

def _read_media_element_bytes(page, msg: dict, expected_type: str, chat_id: int = 0) -> list[dict]:
    """Read bytes directly from the media element inside this exact MAX card."""
    try:
        card = _strict_message_card_locator(page, msg)
        if card.count() == 0:
            return []
        selector = "video" if expected_type == "video_note" else "audio"
        values = card.locator(selector).evaluate_all("""els => els.map((el,i)=>({
            i,
            src: el.currentSrc || el.src || el.getAttribute('src') || '',
            readyState: el.readyState || 0,
            duration: Number.isFinite(el.duration) ? el.duration : 0
        }))""")
        for item in values[:4]:
            idx = int(item.get("i") or 0)
            el = card.locator(selector).nth(idx)
            try:
                el.scroll_into_view_if_needed(timeout=1000)
            except Exception:
                pass
            try:
                result = el.evaluate("""async el => {
                    const url = el.currentSrc || el.src || el.getAttribute('src') || '';
                    if (!url) return {url:'',bytes:[],contentType:''};
                    try {
                        const r = await fetch(url, {credentials:'include'});
                        if (!r.ok) return {url,bytes:[],contentType:''};
                        const b = await r.arrayBuffer();
                        return {url,bytes:Array.from(new Uint8Array(b)),contentType:r.headers.get('content-type')||''};
                    } catch(e) {
                        return {url,bytes:[],contentType:''};
                    }
                }""")
            except Exception:
                continue
            data = bytes(result.get("bytes") or [])
            if len(data) <= 300:
                continue
            ct = str(result.get("contentType") or "").lower()
            ext = ".ogg" if expected_type == "voice" and ("ogg" in ct or "opus" in ct) else (".mp4" if expected_type == "video_note" and ("mp4" in ct or "mpeg" in ct) else (".webm" if "webm" in ct else ".bin"))
            return [{
                "url": str(result.get("url") or ""),
                "bytes": data,
                "type": expected_type,
                "filename": ("voice" + ext if expected_type == "voice" else "video_note" + ext),
                "hash": get_media_hash(data=data),
                "content_type": ct,
            }]
    except Exception as exc:
        print(f"[{chat_id}] ℹ️ Не удалось прочитать {expected_type} из media-element: {exc}")
    return []


def _activate_media_card(page, msg: dict, chat_id: int = 0, wait_after_click_ms: int = 450) -> None:
    """Force MAX to materialize lazy voice/video-note sources."""
    dom_id=str(msg.get('dom_id') or '').strip()
    if not dom_id: return
    try:
        card=_strict_message_card_locator(page, msg)
        if card.count()==0: return
        selectors=[
            "button[aria-label*='видео' i]","[role='button'][aria-label*='видео' i]",
            "button[aria-label*='video' i]","[role='button'][aria-label*='video' i]",
            "button[class*='video' i]","[class*='videoCanvas' i]",
            "[class*='attachAudio' i] button","[class*='attach-audio' i] button",
            "[class*='audio' i] button","[class*='wave' i] button",
            "button[aria-label*='воспроизв' i]","[role='button'][aria-label*='воспроизв' i]",
            "button[aria-label*='слушать' i]","[role='button'][aria-label*='слушать' i]",
            "button[aria-label*='play' i]","[role='button'][aria-label*='play' i]",
            "button[title*='воспроизв' i]","[role='button'][title*='воспроизв' i]",
            "button[title*='слушать' i]","[role='button'][title*='слушать' i]",
            "button[title*='play' i]","[role='button'][title*='play' i]",
            "[data-testid*='play' i]","[class~='play' i]","[class*='playButton' i]",
            'video','audio',
            "[class*='voice-message' i]","[class*='voice' i]",
            "[class*='video-note' i]","[class*='videonote' i]","[class*='круж' i]",
            "[data-testid*='video-note' i]","[data-testid*='voice' i]"
        ]
        for sel in selectors:
            loc=card.locator(sel)
            for i in range(min(loc.count(),8)):
                el=loc.nth(i)
                try:
                    if not el.is_visible(timeout=200): continue
                    el.scroll_into_view_if_needed(timeout=1000)
                    el.click(timeout=1200)
                    page.wait_for_timeout(wait_after_click_ms)
                    return
                except Exception: continue
        # MAX may expose only the 00:01 duration and make the real play button
        # a parent element without a useful aria-label. Click the closest
        # button/role=button inside the SAME already-classified media card.
        try:
            clicked = card.evaluate("""card => {
                const nodes = Array.from(card.querySelectorAll('*'));
                for (const n of nodes) {
                    const t = String(n.textContent || '').trim();
                    if (!/^\d{1,2}:\d{2}$/.test(t)) continue;
                    let p = n;
                    for (let d=0; p && d<5; d++, p=p.parentElement) {
                        if ((p.matches && p.matches('button,[role=\"button\"]')) ||
                            p.getAttribute?.('data-testid')?.toLowerCase().includes('play')) {
                            try { p.click(); return true; } catch(e) {}
                        }
                    }
                }
                return false;
            }""")
            if clicked:
                page.wait_for_timeout(700)
                return
        except Exception:
            pass
    except Exception as exc:
        print(f"[{chat_id}] ℹ️ Не удалось активировать lazy-media: {exc}")

def _capture_media_after_click(page, msg: dict, expected_type: str, chat_id: int = 0) -> list[dict]:
    """Capture a freshly fetched audio/video response triggered by MAX play control."""
    dom_id=str(msg.get('dom_id') or '').strip()
    if not dom_id: return []
    captured=[]
    def on_response(resp):
        try:
            ct=str(resp.headers.get('content-type','')).lower()
            url=str(resp.url or '')
            body=resp.body()
            if not body or len(body) <= 300:
                return
            parsed_url=urlparse(url)
            path=(parsed_url.netloc + parsed_url.path)[:180]
            head=body[:16].hex()
            if len(captured) < 20 and (
                any(term in ct for term in ('audio','video','octet-stream','json','binary')) or
                re.search(r'voice|audio|video|media|attachment|upload|file|message', path, re.I)
            ):
                debug_logger.info(
                    "MAX_MEDIA_RESPONSE chat_id=%s source_id=%s expected=%s status=%s endpoint=%s content_type=%s bytes=%s head=%s",
                    chat_id, msg.get("source_id") or msg.get("idx"), expected_type,
                    getattr(resp, "status", "?"), path, ct or "<none>", len(body), head
                )
            obvious = ct.startswith('audio/') or ct.startswith('video/') or re.search(
                r'\.(?:ogg|opus|mp3|m4a|aac|wav|webm|mp4|mov|m4v)(?:[?#]|$)', url, re.I
            )
            if not obvious and ('application/octet-stream' in ct or 'binary' in ct or not ct):
                head=body[:64]
                signature = (head.startswith(b'OggS') or head.startswith(b'ID3') or
                             head.startswith(b'RIFF') or b'ftyp' in head[:32] or
                             head.startswith(b'\x1aE\xdf\xa3'))
                hinted = bool(re.search(r'voice|audio|video|media|attachment|upload|file|message', url, re.I))
                # During a click inside an already identified voice/circle card,
                # MAX often serves valid media as an opaque binary URL.
                obvious = signature and (hinted or expected_type in ('voice','video_note'))
            if not obvious:
                return
            kind = "audio" if (ct.startswith("audio/") or body.startswith((b"OggS", b"ID3", b"fLaC")) or (body.startswith(b"RIFF") and body[8:12] == b"WAVE")) else (
                "video" if (ct.startswith("video/") or b"ftyp" in body[:32] or body.startswith(b"\x1aE\xdf\xa3")) else ""
            )
            wanted = "audio" if expected_type == "voice" else "video" if expected_type == "video_note" else ""
            if wanted and kind != wanted:
                debug_logger.warning(
                    "MAX_MEDIA_RESPONSE_KIND_MISMATCH chat_id=%s source_id=%s expected=%s detected=%s content_type=%s bytes=%s head=%s",
                    chat_id, msg.get("source_id") or msg.get("idx"), expected_type,
                    kind or "unknown", ct or "<none>", len(body), head
                )
                return
            captured.append((url,ct,body))
        except Exception:
            pass
    try:
        page.on('response',on_response)
        _activate_media_card(
            page, msg, chat_id=chat_id,
            wait_after_click_ms=40 if expected_type == "video_note" else 450,
        )
        if expected_type == "video_note":
            # Do not screen-record MAX's playback canvas: the result is not the
            # original video note and changes on every parse, defeating dedupe.
            page.wait_for_timeout(200)
            if not captured:
                debug_logger.warning(
                    "MAX_CANVAS_VIDEO_CAPTURE_SKIPPED chat_id=%s source_id=%s reason=original_video_unavailable",
                    chat_id, msg.get("source_id") or msg.get("idx"),
                )
        else:
            page.wait_for_timeout(1600)
    finally:
        try: page.remove_listener('response',on_response)
        except Exception: pass
    out=[]
    seen_hashes=set()
    for url,ct,body in captured[-12:]:
        typ=expected_type if expected_type in ('voice','video_note') else ('voice' if ct.startswith('audio/') else 'video_note' if ct.startswith('video/') else expected_type)
        ext='.ogg' if typ=='voice' and ('ogg' in ct or 'opus' in ct) else ('.mp4' if typ=='video_note' and ('mp4' in ct or 'mpeg' in ct) else ('.webm' if 'webm' in ct else '.bin'))
        h=get_media_hash(data=body)
        if h in seen_hashes: continue
        seen_hashes.add(h)
        out.append({'url':url,'bytes':body,'type':typ,'filename':('voice'+ext if typ=='voice' else 'video_note'+ext),'hash':h,'content_type':ct})
    if expected_type == "voice" and len(out) > 1:
        wanted_duration = float(msg.get("media_duration_seconds") or 0)
        if wanted_duration:
            measured = [(_probe_media_duration(item["bytes"]), item) for item in out]
            known = [(abs(duration-wanted_duration), item, duration) for duration,item in measured if duration is not None]
            if known:
                difference, selected, actual_duration = min(known, key=lambda pair: pair[0])
                if difference <= max(1.25, wanted_duration*0.75):
                    debug_logger.warning(
                        "MAX_MEDIA_CAPTURE_DISAMBIGUATED chat_id=%s source_id=%s candidates=%s wanted_seconds=%.2f selected_seconds=%.2f selected_bytes=%s",
                        chat_id, msg.get("source_id") or msg.get("idx"), len(out),
                        wanted_duration, actual_duration, len(selected.get("bytes") or b"")
                    )
                    out=[selected]
    if out:
        debug_logger.info(
            "MAX_MEDIA_CAPTURED chat_id=%s source_id=%s expected=%s count=%s types=%s sizes=%s content_types=%s",
            chat_id, msg.get("source_id") or msg.get("idx"), expected_type, len(out),
            [item.get("type") for item in out], [len(item.get("bytes") or b"") for item in out],
            [item.get("content_type") or "<none>" for item in out]
        )
    else:
        debug_logger.warning(
            "MAX_MEDIA_CAPTURE_EMPTY chat_id=%s source_id=%s expected=%s",
            chat_id, msg.get("source_id") or msg.get("idx"), expected_type
        )
    return out


def _extract_live_rich_media(page, msg: dict, chat_id: int = 0) -> list[dict]:
    """Read live video/audio DOM sources after lazy-loading/activation."""
    dom_id=str(msg.get('dom_id') or '').strip()
    if not dom_id: return []
    out=[]
    try:
        card=_strict_message_card_locator(page, msg)
        if card.count()==0: return []
        selector=(
            'video,audio,source,'
            "[data-video-url],[data-audio-url],[data-voice-url],[data-video-note],[data-voice-message],"
            "[data-video],[data-audio],[data-voice],[data-media],"
            "[class*='voice' i],[class*='video-note' i],[class*='videonote' i],[class*='videoMessage' i],"
            "[class*='videoCanvas' i],[class*='attachAudio' i],[class*='attach-audio' i],"
            "[class*='waveform' i],[class*='круж' i],[class*='голос' i]"
        )
        values=card.locator(selector).evaluate_all("""els=>els.map(el=>{
            const attrs=['src','current-src','data-video-url','data-audio-url','data-voice-url','data-video-note','data-voice-message','data-video','data-audio','data-voice','data-media','data-url','data-href','data-media-url','data-src','href','data-attachment-url','data-content-url'];
            let url='';
            for(const a of attrs){
                let v=(a==='src'&&typeof el.currentSrc==='string'&&el.currentSrc)?el.currentSrc:(el.getAttribute?el.getAttribute(a):'');
                if(v&&/^(https?:|blob:)/i.test(v)){url=v;break;}
            }
            if(!url&&typeof el.src==='string'&&/^(https?:|blob:)/i.test(el.src)) url=el.src;
            let meta=[el.className||'',el.getAttribute?.('data-testid')||'',el.getAttribute?.('aria-label')||'',el.getAttribute?.('data-type')||'',el.getAttribute?.('role')||''];
            let parent=el.parentElement;
            for(let d=0;parent&&d<4;d++,parent=parent.parentElement){
                meta.push(parent.className||'',parent.getAttribute?.('data-testid')||'',parent.getAttribute?.('aria-label')||'',parent.getAttribute?.('data-type')||'');
                if(!url){for(const a of attrs){const v=parent.getAttribute?parent.getAttribute(a):''; if(v&&/^(https?:|blob:)/i.test(v)){url=v;break;}}}
            }
            if(!url&&el.querySelectorAll){for(const s of el.querySelectorAll('source')){const v=s.src||s.getAttribute('src')||s.getAttribute('data-src')||'';if(/^(https?:|blob:)/i.test(v)){url=v;break;}}}
            const r=el.getBoundingClientRect?el.getBoundingClientRect():{width:0,height:0};
            return {url,meta:meta.join(' '),tag:(el.tagName||'').toLowerCase(),width:r.width||0,height:r.height||0};
        }).filter(x=>x.url)""")
        seen=set()
        for item in values:
            url=str(item.get('url') or '').strip()
            if not url or url in seen: continue
            seen.add(url)
            low=str(item.get('meta') or '').lower()
            if re.search(r'video.?note|videonote|кружок|кружочек|round.?video|roundvideo',low,re.I): typ='video_note'
            elif re.search(r'voice|voice.?message|voice_message|attach.?audio|waveform|голосов(?:ое|ая|ым|ого)|аудиосообщ',low,re.I): typ='voice'
            elif item.get('tag')=='video' or re.search(r'(^|[\s_-])video(?:[\s_-]|message|$)|movie|mp4',low,re.I): typ='video'
            else: typ='audio'
            media=_download_document_to_memory(page=page,context=page.context,url=url,filename='',chat_id=chat_id,media_type=typ)
            if media and media.get('bytes'):
                media['type']=typ; out.append(media)
    except Exception as exc:
        print(f"[{chat_id}] ℹ️ Live rich-media fallback: {exc}")
    return out


def _normalize_contact_phone(value: str, allow_bare: bool = False) -> str:
    """Accept actual phone-shaped values and reject short MAX user identifiers."""
    raw = unquote(str(value or "")).replace("%2B", "+").strip()
    digits = re.sub(r"\D", "", raw)
    if not 10 <= len(digits) <= 15:
        return ""
    if not allow_bare and not raw.startswith("+") and not re.search(r"[\s().-]", raw):
        return ""
    return ("+" if raw.startswith("+") else "") + digits


def _phone_from_labeled_text(value: str) -> str:
    text = str(value or "")
    candidate = r"(\+?\d[\d\s().-]{8,}\d)"
    labeled = re.search(
        rf"(?:phone(?:\s*number)?|mobile(?:\s*phone)?|telephone|tel|msisdn|телефон|мобильный)\s*[:=]?\s*{candidate}",
        text,
        re.I,
    )
    if labeled:
        phone = _normalize_contact_phone(labeled.group(1), allow_bare=True)
        if phone:
            return phone
    for match in re.finditer(r"(?<!\w)\+?\d[\d\s().-]{8,}\d(?!\w)", text):
        phone = _normalize_contact_phone(match.group(0))
        if phone:
            return phone
    return ""


def _phone_from_contact_payload(value) -> str:
    """Read phone fields from MAX JSON/vCard without mistaking adjacent IDs for phones."""
    if isinstance(value, (bytes, bytearray)):
        text = bytes(value).decode("utf-8", "ignore")
    else:
        text = str(value or "")
    phone_keys = {
        "phone", "phonenumber", "contactphone", "userphone", "telephone",
        "tel", "mobile", "mobilephone", "msisdn", "dataphone",
        "datacontactphone", "datauserphone", "datamobile", "dataphonenumber",
    }
    try:
        payload = json.loads(text)
    except Exception:
        payload = None

    def scan(item):
        if isinstance(item, dict):
            for key, raw in item.items():
                normalized_key = re.sub(r"[^a-z]", "", str(key).lower())
                if normalized_key in phone_keys and isinstance(raw, (str, int, float)):
                    phone = _normalize_contact_phone(str(raw), allow_bare=True)
                    if phone:
                        return phone
            for raw in item.values():
                phone = scan(raw)
                if phone:
                    return phone
        elif isinstance(item, list):
            for raw in item:
                phone = scan(raw)
                if phone:
                    return phone
        return ""

    phone = scan(payload)
    if phone:
        return phone
    field_pattern = re.compile(
        r"[\"']?(?:data[-_])?(?:phone(?:number)?|contactphone|userphone|telephone|tel|mobile(?:phone)?|msisdn)"
        r"[\"']?\s*[:=]\s*[\"']?(\+?\d[\d\s().-]{8,}\d)",
        re.I,
    )
    match = field_pattern.search(text)
    if match:
        phone = _normalize_contact_phone(match.group(1), allow_bare=True)
        if phone:
            return phone
    vcard = re.search(r"TEL(?:;[^:\r\n]*)?:\s*(?:tel:)?([+\d][\d ()+.-]{8,}\d)", text, re.I)
    return _normalize_contact_phone(vcard.group(1), allow_bare=True) if vcard else ""


def _resolve_contact_phone_from_card(page, msg: dict, chat_id: int = 0, group_url: str = "") -> str:
    """Ищет номер только в явно распознанной карточке контакта."""
    dom_id=str(msg.get("dom_id") or "").strip()
    if not dom_id: return ""
    def norm(v, allow_bare=False): return _normalize_contact_phone(v, allow_bare=allow_bare)
    def scan_scope(scope):
        try:
            info=scope.evaluate("""el=>{
                const fields=[];
                for(const n of [el,...Array.from(el.querySelectorAll('*'))]){
                    for(const a of Array.from(n.attributes||[])){
                        const key=String(a.name||'').toLowerCase();
                        if(/(?:^|[-_])(phone|telephone|tel|mobile|msisdn)(?:$|[-_])/.test(key) && !/(^|[-_])(?:id|identifier)(?:$|[-_])/.test(key)) fields.push(a.value||'');
                    }
                }
                const links=Array.from(el.querySelectorAll("a[href^='tel:' i],[href^='tel:' i]")).map(a=>a.getAttribute('href')||'');
                return {fields,links,text:el.innerText||el.textContent||''};
            }""")
            for value in (info or {}).get('fields',[]):
                phone=norm(value,allow_bare=True)
                if phone: return phone
            for href in (info or {}).get('links',[]):
                m=re.search(r'tel:([^?#]+)',str(href),re.I)
                if m:
                    phone=norm(m.group(1),allow_bare=True)
                    if phone: return phone
            phone=_phone_from_labeled_text((info or {}).get('text',''))
            if phone: return phone
        except Exception:
            pass
        return ''
    try:
        card=_strict_message_card_locator(page, msg)
        if card.count()==0: return ''
        phone=scan_scope(card)
        if phone: return phone

        # Самый надёжный доступ в web MAX к данным контакта — штатная кнопка
        # "Перейти к диалогу" внутри уже подтверждённой contact-card. Никаких
        # кликов по медиа/аудио здесь нет.
        try:
            dialog_btn = card.locator(
                "button[aria-label*='Перейти к диалогу' i],"
                "[role='button'][aria-label*='Перейти к диалогу' i],"
                "button[title*='Перейти к диалогу' i],"
                "button:has-text('Перейти к диалогу'),"
                "[role='button']:has-text('Перейти к диалогу'),"
                "button[aria-label*='open chat' i],"
                "[role='button'][aria-label*='open chat' i],"
                "button[aria-label*='go to conversation' i],"
                "[role='button'][aria-label*='go to conversation' i],"
                "button[title*='go to conversation' i],"
                "button:has-text('Go to conversation'),"
                "[role='button']:has-text('Go to conversation')"
            ).first
            if dialog_btn.count() and dialog_btn.is_visible(timeout=300):
                old_url = page.url
                captured_api = []
                def on_contact_response(resp):
                    try:
                        u=str(resp.url or '')
                        ct=str(resp.headers.get('content-type','')).lower()
                        if ('json' not in ct and 'vcard' not in ct and 'contact' not in ct
                                and 'api' not in u.lower() and 'graphql' not in u.lower()
                                and 'contact' not in u.lower() and 'vcard' not in u.lower()):
                            return
                        if resp.status >= 400:
                            return
                        body=resp.body()
                        if body and len(body) < 2_000_000:
                            low=body.lower()
                            if any(k in low for k in (b'phone', b'tel', b'mobile', b'telephone', b'msisdn', b'vcard')):
                                captured_api.append(body)
                    except Exception:
                        pass
                try: page.on('response', on_contact_response)
                except Exception: pass
                try:
                    dialog_btn.scroll_into_view_if_needed(timeout=1000)
                    dialog_btn.click(timeout=2500)
                    page.wait_for_timeout(1800)
                finally:
                    try: page.remove_listener('response', on_contact_response)
                    except Exception: pass

                phone_from_api=''
                for raw in captured_api:
                    try:
                        phone_from_api=_phone_from_contact_payload(raw)
                        if phone_from_api: break
                    except Exception:
                        continue
                if phone_from_api:
                    try:
                        if group_url and page.url != group_url:
                            page.goto(group_url, timeout=30000, wait_until='domcontentloaded')
                            page.wait_for_timeout(500)
                    except Exception:
                        pass
                    return phone_from_api

                # 0) Некоторые сборки MAX меняют содержимое SPA без смены URL.
                # Сразу сканируем body, но только после нажатия кнопки контакта.
                try:
                    body_text = page.locator("body").inner_text(timeout=1000)
                    phone = _phone_from_labeled_text(str(body_text or ""))
                    if phone:
                        if phone:
                            if group_url and page.url != group_url:
                                try:
                                    page.goto(group_url, timeout=30000, wait_until='domcontentloaded')
                                    page.wait_for_timeout(500)
                                except Exception: pass
                            return phone
                except Exception:
                    pass

                # Открываем информацию о человеке в 1:1 чате, если она представлена
                # отдельной кнопкой/аватаром.
                try:
                    name_hint = str(msg.get('contact_name') or (msg.get('contact') or {}).get('first_name') or '').strip()
                    controls = page.locator("button, [role='button'], a")
                    for ci in range(min(120, controls.count())):
                        el = controls.nth(ci)
                        try:
                            if not el.is_visible(timeout=80): continue
                            label = ((el.get_attribute('aria-label') or '')+' '+(el.get_attribute('title') or '')+' '+(el.inner_text(timeout=100) or '')).strip()
                            low = label.lower()
                            if not re.search(r'профил|profile|информац|information|details|подробнее|о пользовател|contact', low):
                                if name_hint and name_hint.lower() not in low:
                                    continue
                            if re.search(r'назад|back|закрыть|close|вызов|call|video|микроф', low): continue
                            el.click(timeout=1000)
                            page.wait_for_timeout(500)
                            phone = scan_scope(page.locator("body"))
                            if phone:
                                if group_url and page.url != group_url:
                                    try:
                                        page.goto(group_url, timeout=30000, wait_until='domcontentloaded')
                                        page.wait_for_timeout(500)
                                    except Exception: pass
                                return phone
                            try: page.keyboard.press('Escape')
                            except Exception: pass
                        except Exception:
                            continue
                except Exception:
                    pass

                # 1) Видимые окна/панели.
                scopes = page.locator(
                    "[role='dialog'], [class*='drawer' i], [class*='profile' i],"
                    "[class*='user-card' i], [class*='chat-info' i]"
                )
                for i in range(max(0, scopes.count()-12), scopes.count()):
                    sc=scopes.nth(i)
                    try:
                        if not sc.is_visible(timeout=150): continue
                        phone=scan_scope(sc)
                        if phone:
                            if group_url and page.url != group_url:
                                try:
                                    page.goto(group_url, timeout=30000, wait_until='domcontentloaded')
                                    page.wait_for_timeout(500)
                                except Exception: pass
                            return phone
                    except Exception: continue

                # 2) Если кнопка перевела в отдельный 1:1 диалог — ищем номер
                # только при реально изменившемся route либо внутри открытого окна.
                if page.url != old_url:
                    phone = scan_scope(page.locator("body"))
                    if not phone:
                        phone = _phone_from_contact_payload(page.content())
                    if phone:
                        if group_url:
                            try:
                                page.goto(group_url, timeout=30000, wait_until='domcontentloaded')
                                page.wait_for_timeout(500)
                            except Exception: pass
                        return phone

                    # Открываем профиль/информацию только в этом 1:1 диалоге.
                    controls=page.locator("button[aria-label], [role='button'][aria-label], button[title], [role='button'][title]")
                    for i in range(min(80, controls.count())):
                        el=controls.nth(i)
                        try:
                            if not el.is_visible(timeout=80): continue
                            a=((el.get_attribute('aria-label') or '')+' '+(el.get_attribute('title') or '')).lower()
                            if not re.search(r'профил|profile|информац|information|info|подробнее|details|о пользовател',a): continue
                            el.click(timeout=1200)
                            page.wait_for_timeout(400)
                            scopes=page.locator("[role='dialog'], [class*='modal' i], [class*='drawer' i], [class*='profile' i]")
                            for j in range(max(0,scopes.count()-8), scopes.count()):
                                sc=scopes.nth(j)
                                try:
                                    if sc.is_visible(timeout=100):
                                        phone=scan_scope(sc)
                                        if phone:
                                            if group_url and page.url != group_url:
                                                try:
                                                    page.goto(group_url, timeout=30000, wait_until='domcontentloaded')
                                                    page.wait_for_timeout(500)
                                                except Exception: pass
                                            return phone
                                except Exception: continue
                            try: page.keyboard.press('Escape')
                            except Exception: pass
                        except Exception: continue
        except Exception:
            pass

        # Открываем только имя/карточку уже подтверждённого контакта.
        name_hint=str(msg.get('contact_name') or (msg.get('contact') or {}).get('first_name') or '').strip()
        sels=["[data-contact-name]","[data-display-name]","[data-testid*='contact-name' i]","[class*='contact-name' i]","[class*='display-name' i]",".name"]
        clicked=False
        for sel in sels:
            loc=card.locator(sel)
            for i in range(min(loc.count(),10)):
                el=loc.nth(i)
                try:
                    if not el.is_visible(timeout=200): continue
                    txt=' '.join((el.inner_text(timeout=200) or '').split()).strip()
                    if name_hint and name_hint.lower() not in txt.lower(): continue
                    aria=((el.get_attribute('aria-label') or '')+' '+(el.get_attribute('title') or '')).lower()
                    if re.search(r'call|позвон|video|видео|mic|микрофон',aria): continue
                    el.click(timeout=1500); clicked=True; break
                except Exception: continue
            if clicked: break
        if clicked:
            page.wait_for_timeout(600)
            scopes=page.locator("[role='dialog'],[class*='modal' i],[class*='drawer' i],[class*='profile' i]")
            for i in range(max(0,scopes.count()-8),scopes.count()):
                sc=scopes.nth(i)
                try:
                    if not sc.is_visible(timeout=200): continue
                    phone=scan_scope(sc)
                    if phone:
                        try: page.keyboard.press('Escape')
                        except Exception: pass
                        return phone
                except Exception: continue
            try: page.keyboard.press('Escape')
            except Exception: pass
        # Узкий HTML fallback: ключ phone рядом с именем контакта.
        html=page.content()
        if name_hint:
            pos=html.lower().find(name_hint.lower())
            if pos>=0:
                window=html[max(0,pos-10000):min(len(html),pos+20000)]
                phone=_phone_from_contact_payload(window)
                if phone: return phone
        if group_url and page.url != group_url:
            try:
                page.goto(group_url, timeout=30000, wait_until='domcontentloaded')
                page.wait_for_timeout(500)
            except Exception: pass
        return ''
    except Exception as exc:
        print(f"[{chat_id}] ℹ️ Не удалось получить номер контакта: {exc}")
        if group_url:
            try:
                if page.url != group_url:
                    page.goto(group_url, timeout=30000, wait_until='domcontentloaded')
                    page.wait_for_timeout(500)
            except Exception: pass
        return ''


def _resolve_contact_link_via_dialog(page, msg: dict, group_url: str, chat_id: int = 0) -> str:
    """Для подтверждённого контакта нажимает ровно 'Перейти к диалогу',
    затем ищет share-link /c/ в открытом диалоге/профиле и возвращается в группу.
    """
    dom_id=str(msg.get('dom_id') or '').strip()
    if not dom_id or not group_url:
        return ''
    pat=re.compile(r"https?://max\.ru/c/[^\s\"'<>]+", re.I)
    try:
        card=_message_card_locator(page, msg)
        if card.count()==0: return ''
        btn=card.locator("button[aria-label*='Перейти к диалогу' i], [role='button'][aria-label*='Перейти к диалогу' i]").first
        if btn.count()==0 or not btn.is_visible(timeout=300): return ''
        try: btn.scroll_into_view_if_needed(timeout=1000)
        except Exception: pass
        btn.click(timeout=2500)
        page.wait_for_timeout(1200)
        # Прямой поиск /c/ на странице/в атрибутах.
        try:
            vals=page.locator("a[href], [data-url], [data-href], [data-link], [data-share-url], [data-profile-url]").evaluate_all("""els=>els.flatMap(el=>[el.getAttribute('href')||'',el.getAttribute('data-url')||'',el.getAttribute('data-href')||'',el.getAttribute('data-link')||'',el.getAttribute('data-share-url')||'',el.getAttribute('data-profile-url')||''])""")
            for v in vals:
                m=pat.search(str(v or ''))
                if m: return m.group(0)
        except Exception: pass
        try:
            m=pat.search(page.content())
            if m: return m.group(0)
        except Exception: pass
        # Попытка открыть только профиль/info в новом диалоге.
        controls=page.locator("button[aria-label], [role='button'][aria-label], button[title], [role='button'][title]")
        for i in range(min(70,controls.count())):
            el=controls.nth(i)
            try:
                if not el.is_visible(timeout=100): continue
                a=((el.get_attribute('aria-label') or '')+' '+(el.get_attribute('title') or '')).lower()
                if not re.search(r'профил|profile|информац|information|info|подробнее|details',a): continue
                el.click(timeout=1200); page.wait_for_timeout(500)
                m=pat.search(page.content())
                if m: return m.group(0)
                try: page.keyboard.press('Escape')
                except Exception: pass
            except Exception: continue
        return ''
    except Exception as e:
        print(f"[{chat_id}] ℹ️ Не удалось открыть диалог контакта: {e}")
        return ''
    finally:
        try:
            if page.url != group_url:
                page.goto(group_url, timeout=30000, wait_until='domcontentloaded')
                page.wait_for_timeout(1000)
        except Exception: pass

def parse_max_group_media(
    group_url: str,
    chat_id: int,
    session_path: str = None,
    verify_only: bool = False,
    navigation_timeout_ms: int = 60000,
    navigation_attempts: int = 4,
) -> List[Dict]:
    """Парсит чат MAX через точные карточки .item[data-index]."""
    debug_logger.info("MAX_PARSE_START chat_id=%s", chat_id)
    if not isinstance(chat_id, int):
        raise Exception(f"Некорректный chat_id: {chat_id} (должен быть int)")
    session_file = session_path or f"{SESSIONS_DIR}/{chat_id}.json"
    if not os.path.exists(session_file):
        raise Exception("NO_SESSION")
    pw = browser = context = page = None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            storage_state=session_file,
            viewport={"width": 1280, "height": 800},
            timezone_id=MAX_TIMEZONE_ID,
        )
        page = context.new_page()
        if not verify_only:
            _install_max_media_response_cache(page, chat_id)
        print(f"[{chat_id}] Переход на главную MAX...")
        _goto_max_with_retry(
            page, "https://web.max.ru", chat_id, "главная страница MAX",
            timeout=navigation_timeout_ms, max_attempts=navigation_attempts,
        )
        page.wait_for_timeout(1200 if verify_only else 3000)
        if not _is_logged_in(page):
            debug_path = f"{SESSIONS_DIR}/parse_debug_{chat_id}.png"
            page.screenshot(path=debug_path, full_page=True)
            raise Exception(f"SESSION_EXPIRED|{debug_path}")
        _goto_max_with_retry(
            page, group_url, chat_id, "переход в чат MAX",
            timeout=navigation_timeout_ms, max_attempts=navigation_attempts,
        )
        page.wait_for_timeout(1200 if verify_only else 4000)
        _accept_max_invite_if_available(page, group_url, chat_id)
        current_url = page.url
        requested_host = (urlparse(group_url).hostname or "").lower().rstrip(".")
        current_parts = urlparse(current_url)
        current_host = (current_parts.hostname or "").lower().rstrip(".")
        current_path = current_parts.path.rstrip("/").lower()
        redirected_to_max_chat = (
            (requested_host == "max.ru" or requested_host.endswith(".max.ru"))
            and (current_host == "max.ru" or current_host.endswith(".max.ru"))
            and current_path not in {"", "/", "/auth", "/login", "/signin", "/sign-in"}
        )
        if group_url not in current_url and not redirected_to_max_chat:
            debug_path = f"{SESSIONS_DIR}/wrong_chat_debug_{chat_id}.png"
            page.screenshot(path=debug_path, full_page=True)
            raise Exception(f"WRONG_CHAT_REDIRECT|{debug_path}")
        if len(page.content()) < 1000:
            debug_path = f"{SESSIONS_DIR}/parse_debug_{chat_id}.png"
            page.screenshot(path=debug_path, full_page=True)
            raise Exception(f"EMPTY_PAGE|{debug_path}")
        debug_logger.info("MAX_CHAT_OPENED chat_id=%s route=%s verify_only=%s", chat_id, urlparse(current_url).path, verify_only)
        if verify_only:
            return []
        print(f"[{chat_id}] Ожидание загрузки последних сообщений...")
        try:
            page.keyboard.press("End")
            page.wait_for_timeout(700)
            last_marker=""
            stable=0
            for _ in range(12):
                page.wait_for_timeout(250)
                try:
                    marker=page.locator(".item").last.evaluate("""el => ({
                        id: el.getAttribute('data-message-id') || el.getAttribute('data-mid') || el.getAttribute('data-id') || '',
                        index: el.getAttribute('data-index') || '', text: (el.innerText||'').slice(0,180)
                    })""")
                    marker_key=json.dumps(marker,ensure_ascii=False,sort_keys=True)
                except Exception:
                    marker_key=""
                if marker_key and marker_key==last_marker: stable+=1
                else: stable=0
                last_marker=marker_key
                if stable>=2: break
            page.evaluate("""async maxMessagesToParse => {
                const allRows=Array.from(document.querySelectorAll('.item'));
                const rowLimit=Math.max(0,Math.floor(Number(maxMessagesToParse)||0));
                const rows=rowLimit ? allRows.slice(-rowLimit) : [];
                for(const row of rows){try{row.scrollIntoView({block:'center',inline:'nearest'});}catch(e){} await new Promise(r=>setTimeout(r,45));}
                window.scrollTo(0,document.body.scrollHeight);
            }""", MAX_MESSAGES_TO_PARSE)
            page.wait_for_timeout(700)
        except Exception as e:
            print(f"[{chat_id}] ⚠️ Не удалось загрузить/прогреть последние сообщения: {e}")
        raw_messages = page.evaluate(
            r"""
            maxMessagesToParse => {
                const results = [];
                const allRows = Array.from(document.querySelectorAll('.item'));
                const rowLimit = Math.max(0, Math.floor(Number(maxMessagesToParse) || 0));
                const firstSelectedIndex = Math.max(0, allRows.length - rowLimit);
                const rows = rowLimit ? allRows.slice(firstSelectedIndex) : [];

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
                        "data-download-url", "data-file-url", "data-src", "data-link",
                        "data-media-url", "data-video-url", "data-audio-url", "data-voice-url",
                        "data-video", "data-audio", "data-voice", "data-media", "data-voice-message",
                        "data-video-note", "data-content-url", "data-attachment-url", "data-original", "data-original-src"
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
                    // MAX/Chromium может держать реальный media URL только как DOM property.
                    const mediaProperty = (typeof el.src === "string" ? el.src : "") ||
                                          (typeof el.currentSrc === "string" ? el.currentSrc : "");
                    if (/^(?:https?:|blob:)/i.test(mediaProperty)) return mediaProperty;
                    const propertyHref = typeof el.href === "string" ? el.href : "";
                    if (/^(?:https?:|blob:)/i.test(propertyHref)) return propertyHref;
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
                    // Берём только время сообщения из meta. Нельзя использовать любой
                    // [class*=time]: у кружка/видео там часто находится ДЛИТЕЛЬНОСТЬ.
                    const selectors = [
                        ".bubbleContent .meta .text",
                        ".bubbleContent .meta time",
                        ".meta .text",
                        ".meta time",
                        "time[datetime]"
                    ];
                    for (const selector of selectors) {
                        try {
                            const element = row.querySelector(selector);
                            if (!element) continue;
                            const visibleValue = clean(element.textContent);
                            const datetimeValue = safeAttr(element, "datetime");
                            const value = visibleValue || clean(datetimeValue);
                            if (!visibleValue && /^\d{4}-\d{2}-\d{2}T/.test(datetimeValue)) {
                                const absoluteTime = new Date(datetimeValue);
                                if (!Number.isNaN(absoluteTime.getTime())) {
                                    return `${String(absoluteTime.getHours()).padStart(2,"0")}:${String(absoluteTime.getMinutes()).padStart(2,"0")}`;
                                }
                            }
                            const match = value.match(/\b(\d{1,2}:\d{2})(?:\s*(AM|PM))?\b/i);
                            if (match) return match[0].trim();
                        } catch (e) {}
                    }
                    // Fallback: ищем timestamp/time в атрибутах meta, но не duration.
                    try {
                        const nodes = row.querySelectorAll("[data-time],[data-timestamp],[data-created-at],[datetime]");
                        for (const el of nodes) {
                            const v = safeAttr(el,"data-time") || safeAttr(el,"data-timestamp") || safeAttr(el,"data-created-at") || safeAttr(el,"datetime");
                            if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}T/.test(v)) {
                                const isoDate = new Date(v);
                                if (!Number.isNaN(isoDate.getTime())) return `${String(isoDate.getHours()).padStart(2,"0")}:${String(isoDate.getMinutes()).padStart(2,"0")}`;
                            }
                            const m = String(v).match(/\b(\d{1,2}:\d{2})(?:\s*(AM|PM))?\b/i);
                            if (m) return m[0];
                            const n = Number(v);
                            if (Number.isFinite(n) && n > 1000000000) {
                                const d = new Date(n < 10000000000 ? n * 1000 : n);
                                if (!Number.isNaN(d.getTime())) return `${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")}`;
                            }
                        }
                    } catch(e) {}
                    return "";
                }

                function getDate(row) {
                    const months = {
                        "января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,
                        "июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12,
                        "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
                        "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
                        "jan":1,"feb":2,"mar":3,"apr":4,"jun":6,"jul":7,"aug":8,"sep":9,"sept":9,"oct":10,"nov":11,"dec":12
                    };
                    const withDateSource = (value, source) => {
                        try { row.setAttribute("data-max-parser-date-source", source); } catch(e) {}
                        return value;
                    };

                    function dateText(value) {
                        const text = clean(value);
                        if (!text || text.length > 40) return "";
                        let m = text.match(/^\s*(\d{1,2})[.\/-](\d{1,2})[.\/-](\d{4})\s*$/);
                        if (m) return `${m[1].padStart(2,"0")}.${m[2].padStart(2,"0")}.${m[3]}`;
                        if (/^\d{4}-\d{2}-\d{2}T/.test(text)) {
                            const absoluteDate = new Date(text);
                            if (!Number.isNaN(absoluteDate.getTime())) return `${String(absoluteDate.getDate()).padStart(2,"0")}.${String(absoluteDate.getMonth()+1).padStart(2,"0")}.${absoluteDate.getFullYear()}`;
                        }
                        m = text.match(/^\s*(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?:\s+(\d{4}))?\s*$/i);
                        if (m) return `${m[1].padStart(2,"0")}.${String(months[m[2].toLowerCase()]).padStart(2,"0")}.${m[3] || new Date().getFullYear()}`;
                        m = text.match(/^\s*(\d{1,2})\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)(?:,?\s+(\d{4}))?\s*$/i);
                        if (m) return `${m[1].padStart(2,"0")}.${String(months[m[2].toLowerCase()]).padStart(2,"0")}.${m[3] || new Date().getFullYear()}`;
                        m = text.match(/^\s*(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{1,2})(?:,?\s+(\d{4}))?\s*$/i);
                        if (m) return `${String(m[2]).padStart(2,"0")}.${String(months[m[1].toLowerCase()]).padStart(2,"0")}.${m[3] || new Date().getFullYear()}`;
                        m = text.match(/^\s*(\d{4})-(\d{2})-(\d{2})(?:T.*)?\s*$/);
                        if (m) return `${m[3]}.${m[2]}.${m[1]}`;
                        if (/^(сегодня|today)$/i.test(text)) {
                            const d = new Date(); return `${String(d.getDate()).padStart(2,"0")}.${String(d.getMonth()+1).padStart(2,"0")}.${d.getFullYear()}`;
                        }
                        if (/^(вчера|yesterday)$/i.test(text)) {
                            const d = new Date(); d.setDate(d.getDate()-1);
                            return `${String(d.getDate()).padStart(2,"0")}.${String(d.getMonth()+1).padStart(2,"0")}.${d.getFullYear()}`;
                        }
                        const numeric = Number(text);
                        if (Number.isFinite(numeric) && numeric >= 1000000000 && numeric <= 2000000000000) {
                            const d = new Date(numeric < 10000000000 ? numeric * 1000 : numeric);
                            if (!Number.isNaN(d.getTime())) return `${String(d.getDate()).padStart(2,"0")}.${String(d.getMonth()+1).padStart(2,"0")}.${d.getFullYear()}`;
                        }
                        return "";
                    }

                    // Абсолютная метка времени конкретного сообщения точнее общего
                    // date-separator ленты: после подгрузки истории separator может
                    // относиться к соседнему дню или остаться от виртуализированной карточки.
                    const absoluteTimestampFallback = [];
                    const addAbsoluteTimestamp = value => {
                        const text = String(value || "").trim();
                        if (/^\d{4}-\d{2}-\d{2}T/.test(text) || /^\d{10,13}$/.test(text)) {
                            absoluteTimestampFallback.push(text);
                        }
                    };
                    ["datetime","data-timestamp","data-created-at","data-time"].forEach(a => {
                        addAbsoluteTimestamp(safeAttr(row,a));
                    });
                    try {
                        row.querySelectorAll("time[datetime],[data-timestamp],[data-created-at],[data-time]").forEach(el => {
                            ["datetime","data-timestamp","data-created-at","data-time"].forEach(a => {
                                addAbsoluteTimestamp(safeAttr(el,a));
                            });
                        });
                    } catch(e) {}
                    for (const v of absoluteTimestampFallback) {
                        const d = dateText(v);
                        if (d) return withDateSource(d,"message_timestamp");
                    }

                    // Some MAX builds place the date capsule inside the .item, while
                    // its data-date attribute can describe a stale recycled element.
                    try {
                        for (const el of row.querySelectorAll('[class*="date" i],[class*="day" i],[class*="capsule" i]')) {
                            if (el.closest && el.closest(".bubbleContent")) continue;
                            const rect=el.getBoundingClientRect();
                            const style=getComputedStyle(el);
                            if(!rect.width||!rect.height||style.display==="none"||style.visibility==="hidden") continue;
                            const d=dateText(clean(el.textContent));
                            if (d) return withDateSource(d,"message_date_capsule");
                        }
                    } catch(e) {}

                    // Prefer the closest visible timeline separator over generic
                    // row attributes, which React can leave behind when recycling
                    // the last message card.
                    try {
                        let capsuleFeed=row.parentElement;
                        for(let i=0;capsuleFeed&&i<10;i++,capsuleFeed=capsuleFeed.parentElement){
                            if(capsuleFeed.querySelectorAll(".item").length>=3) break;
                        }
                        if(capsuleFeed){
                            const rowTop=row.getBoundingClientRect().top;
                            let nearestCapsule=null,nearestCapsuleTop=-Infinity;
                            capsuleFeed.querySelectorAll("*").forEach(el=>{
                                if(row.contains(el)||el.closest?.(".bubbleContent")) return;
                                const value=clean(el.textContent);
                                const parsed=dateText(value);
                                if(!parsed||value.length>40) return;
                                let rect,style;
                                try{style=getComputedStyle(el);if(style.position==="fixed"||style.position==="sticky"||style.display==="none"||style.visibility==="hidden")return;rect=el.getBoundingClientRect();if(!rect.width||!rect.height)return;}catch(e){return;}
                                if(el.parentElement&&clean(el.parentElement.textContent)===value) return;
                                try{
                                    const pos=el.compareDocumentPosition(row);
                                    if(!(pos&Node.DOCUMENT_POSITION_FOLLOWING)) return;
                                    if(rect.bottom<=rowTop+4&&rect.top>nearestCapsuleTop){
                                        nearestCapsule={date:parsed};nearestCapsuleTop=rect.top;
                                    }
                                }catch(e){}
                            });
                            if(nearestCapsule) return withDateSource(nearestCapsule.date,"nearest_date_separator");
                        }
                    } catch(e) {}

                    // Дата внутри самой карточки имеет приоритет над separator-ом.
                    const own = [];
                    const timestampFallback = [];
                    ["data-date","data-day","data-message-date"].forEach(a => {
                        const v = safeAttr(row,a); if (v) own.push(v);
                    });
                    ["datetime","data-created-at","data-timestamp","data-time"].forEach(a => {
                        const v = safeAttr(row,a); if (v) timestampFallback.push(v);
                    });
                    try {
                        row.querySelectorAll("time[datetime],[data-date],[data-day],[data-message-date]").forEach(el => {
                            ["data-date","data-day","data-message-date"].forEach(a => {
                                const v=safeAttr(el,a); if(v) own.push(v);
                            });
                            ["datetime","data-created-at","data-timestamp","data-time"].forEach(a => {
                                const timestamp=safeAttr(el,a); if(timestamp) timestampFallback.push(timestamp);
                            });
                            const t=clean(el.textContent); if(t) own.push(t);
                        });
                    } catch(e) {}
                    for (const v of own) { const d=dateText(v); if(d) return withDateSource(d,"message_date_attribute"); }

                    // Некоторые версии MAX не используют class=data-date у разделителя.
                    // Проверяем атрибуты ближайших элементов на реальную календарную дату.
                    try {
                        const attrs = ["data-date","data-day","data-message-date","title","aria-label"];
                        for (const el of row.querySelectorAll("*")) {
                            for (const a of attrs) {
                                const v = safeAttr(el,a);
                                const d = dateText(v);
                                if (d) return withDateSource(d,"message_date_label");
                            }
                        }
                    } catch(e) {}

                    // Находим именно контейнер ленты, а не body. Это критично:
                    // sticky-копия текущей даты в шапке больше не сможет заменить дату
                    // старого сообщения.
                    let feed = row.parentElement;
                    for (let i=0; feed && i<10; i++, feed=feed.parentElement) {
                        try {
                            if (feed.querySelectorAll(".item").length >= 3) break;
                        } catch(e) {}
                    }
                    if (!feed) feed = row.parentElement || document.body;

                    const markers = [];
                    try {
                        feed.querySelectorAll("*").forEach(el => {
                            if (!el || row.contains(el)) return;
                            // Дата-разделитель MAX может быть капсулой внутри собственного
                            // .item. Отбрасываем текстовые пузырьки, но сохраняем такие метки.
                            if (el.closest && el.closest(".bubbleContent")) return;
                            const text = clean(el.textContent);
                            const parsed = dateText(text);
                            if (!parsed || text.length > 40) return;

                            // Отбрасываем sticky/fixed элементы и элементы шапки.
                            try {
                                const pos = getComputedStyle(el).position;
                                if (pos === "fixed" || pos === "sticky") return;
                            } catch(e) {}

                            // Оставляем только самый внутренний узел с датой.
                            if (el.parentElement && clean(el.parentElement.textContent) === text) return;
                            markers.push({el, date: parsed});
                        });
                    } catch(e) {}

                    // Выбираем ближайший видимый разделитель по вертикальной позиции.
                    // Это защищает от sticky-копий даты и от DOM-обёрток, где порядок узлов
                    // не совпадает с визуальным порядком сообщений.
                    let nearest = null;
                    let nearestTop = -Infinity;
                    let rowTop = 0;
                    try { rowTop = row.getBoundingClientRect().top; } catch(e) {}
                    for (const marker of markers) {
                        try {
                            const pos = marker.el.compareDocumentPosition(row);
                            if (!(pos & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
                            const r = marker.el.getBoundingClientRect();
                            if (r.bottom <= rowTop + 4 && r.top > nearestTop) {
                                nearest = marker;
                                nearestTop = r.top;
                            }
                        } catch(e) {}
                    }
                    if (nearest) return withDateSource(nearest.date,"nearest_date_separator");
                    // Fallback: если карточка вне viewport, используем DOM-порядок,
                    // но только среди не-sticky маркеров.
                    for (const marker of markers) {
                        try {
                            const pos = marker.el.compareDocumentPosition(row);
                            if (pos & Node.DOCUMENT_POSITION_FOLLOWING) nearest = marker;
                        } catch(e) {}
                    }
                    if (nearest) return withDateSource(nearest.date,"dom_order_date_separator");

                    // Иногда разделитель является дочерним элементом wrapper-а,
                    // а wrapper располагается перед row. Поднимаемся по DOM.
                    let node = row;
                    for (let depth=0; node && depth<12; depth++, node=node.parentElement) {
                        let prev = node.previousElementSibling;
                        for (let n=0; prev && n<30; n++, prev=prev.previousElementSibling) {
                            const direct = dateText(clean(prev.textContent));
                            if (direct) return withDateSource(direct,"previous_sibling_date_separator");
                            try {
                                const found = prev.querySelector(
                                    '[data-date],[data-day],[data-message-date],' +
                                    '[class*="date" i],[class*="day" i],[class*="capsule" i]'
                                );
                                if (found) {
                                    const d=dateText(clean(found.textContent));
                                    if(d) return withDateSource(d,"previous_sibling_date_capsule");
                                }
                            } catch(e) {}
                        }
                    }
                    for (const v of timestampFallback) { const d=dateText(v); if(d) return withDateSource(d,"timestamp_fallback"); }
                    return withDateSource("","unresolved");
                }

                const emojiNodeSelectors = [
                    '[class*="emoji" i]', '[class*="animated" i]', '[data-animated-emoji]',
                    '[data-custom-emoji]', '[data-emoji-id]', '[data-emoji-name]', '[data-emoji-code]',
                    '[data-emoji-codepoint]', '[data-codepoint]', '[data-codepoints]', '[data-unicode]',
                    '[data-type*="emoji" i]', '[data-testid*="emoji" i]', '[aria-label*="emoji" i]',
                    '[title*="emoji" i]', '[role="img"]', 'canvas', 'video', 'img[alt]',
                    'img[aria-label]', '[data-emoji]', '[data-emoji-char]', '[data-emoji-text]',
                    '[data-character]', '[data-name]', '[data-icon]'
                ];
                const emojiNodeSelector = emojiNodeSelectors.join(",");

                function emojiClusters(value) {
                    const text = String(value || "");
                    const matches = [];
                    try {
                        if (typeof Intl !== "undefined" && Intl.Segmenter) {
                            const segmenter = new Intl.Segmenter(undefined, {granularity:"grapheme"});
                            for (const part of segmenter.segment(text)) {
                                if (/[\p{Extended_Pictographic}\p{Regional_Indicator}]/u.test(part.segment) || /[#*0-9]\uFE0F?\u20E3/u.test(part.segment)) {
                                    matches.push(part.segment);
                                }
                            }
                            return matches;
                        }
                    } catch(e) {}
                    try {
                        return text.match(/(?:\p{Regional_Indicator}{2}|[#*0-9]\uFE0F?\u20E3|\p{Extended_Pictographic}(?:\uFE0F|\p{Emoji_Modifier}|\u200D\p{Extended_Pictographic}(?:\uFE0F|\p{Emoji_Modifier})?)*)/gu) || [];
                    } catch(e) { return []; }
                }

                function cleanMessageText(value) {
                    return String(value || "")
                        .replace(/\r\n?/g, "\n")
                        .split("\n")
                        .map(line => line.replace(/[^\S\n]+/g, " ").trim())
                        .join("\n")
                        .replace(/\n{3,}/g, "\n\n")
                        .trim();
                }

                function emojiValueFromElement(el) {
                    if (!el) return "";
                    const textAttrs = ["alt", "data-emoji", "data-emoji-char", "data-emoji-text", "data-text", "data-unicode", "aria-label", "title", "data-emoji-name", "data-custom-emoji"];
                    for (const attr of textAttrs) {
                        const found = emojiClusters(safeAttr(el, attr));
                        if (found.length) return found.join("");
                    }
                    const codeAttrs = ["data-codepoint", "data-codepoints", "data-emoji-code", "data-emoji-codepoint"];
                    for (const attr of codeAttrs) {
                        const tokens = safeAttr(el, attr).match(/(?:U\+|0x)?[0-9a-f]{4,6}/gi) || [];
                        const decoded = tokens.map(token => parseInt(token.replace(/^(?:U\+|0x)/i, ""), 16))
                            .filter(cp => cp >= 0x2000 && cp <= 0x1FAFF)
                            .map(cp => String.fromCodePoint(cp));
                        if (decoded.length) return emojiClusters(decoded.join("")).join("");
                    }
                    const mediaNodes = [el, ...Array.from(el.querySelectorAll("img,video,canvas,source"))];
                    for (const mediaNode of mediaNodes) {
                        let src = mediaNode.currentSrc || mediaNode.src || safeAttr(mediaNode, "src") || safeAttr(mediaNode, "data-src") || safeAttr(mediaNode, "data-emoji-url") || safeAttr(mediaNode, "data-animation-url") || "";
                        if (!src) {
                            try {
                                const match = String(getComputedStyle(mediaNode).backgroundImage || "").match(/url\(["']?(.*?)["']?\)/i);
                                if (match) src = match[1];
                            } catch(e) {}
                        }
                        const tokens = String(src).match(/(?:U\+|0x|emoji[=_-]|codepoint[=_-])([0-9a-f]{4,6})|(?:^|[\/_-])((?:1f|26|27)[0-9a-f]{2,4})(?=$|[\/_-])/gi) || [];
                        const decoded = [];
                        for (const token of tokens) {
                            const hexes = token.match(/[0-9a-f]{4,6}/gi) || [];
                            for (const hex of hexes) {
                                const cp = parseInt(hex, 16);
                                if (cp >= 0x2000 && cp <= 0x1FAFF) decoded.push(String.fromCodePoint(cp));
                            }
                        }
                        if (decoded.length) return emojiClusters(decoded.join("")).join("");
                    }
                    return "";
                }

                function getOwnContent(row) {
                    const pollSelector = "[data-testid*='poll' i],[class*='poll' i],[data-type='poll'],[class*='survey' i]";
                    try {
                        const bubble = row.querySelector(".bubbleContent");
                        if (!bubble) return {text:"", links:[], emojiTexts:[]};
                        const clone = bubble.cloneNode(true);
                        const reactionSelectors = [
                            '[class*="reaction" i]', '[data-testid*="reaction" i]',
                            '[data-reaction]', '[data-reaction-id]', '[data-reaction-count]',
                            '[aria-label*="reaction" i]', '[title*="reaction" i]',
                            '[aria-label*="реакц" i]', '[title*="реакц" i]'
                        ].join(",");
                        try { clone.querySelectorAll(reactionSelectors).forEach(el => el.remove()); } catch(e) {}
                        [
                            ".header", ".meta", "button.mark",
                            '[class*="reply" i]', '[class*="quoted" i]', '[class*="quote" i]', pollSelector
                        ].forEach(selector => {
                            try { clone.querySelectorAll(selector).forEach(el => el.remove()); } catch(e) {}
                        });
                        clone.querySelectorAll(".link").forEach(el => {
                            if (!el.matches("a[href]") && !el.querySelector("a[href]")) el.remove();
                        });

                        const emojiTexts = getEmojiTexts(clone);
                        const emojiQueue = emojiTexts.slice();
                        const candidates = Array.from(clone.querySelectorAll(emojiNodeSelector));
                        const consume = value => {
                            for (const emoji of emojiClusters(value)) {
                                const index = emojiQueue.indexOf(emoji);
                                if (index >= 0) emojiQueue.splice(index, 1);
                            }
                        };
                        for (const el of candidates) {
                            if (!clone.contains(el)) continue;
                            const meta = `${safeAttr(el, "class")} ${safeAttr(el, "data-testid")} ${safeAttr(el, "data-type")} ${safeAttr(el, "aria-label")} ${safeAttr(el, "alt")} ${safeAttr(el, "title")}`;
                            if (/sticker|стикер/i.test(meta)) continue;
                            let parent = el.parentElement;
                            let insideSticker = false;
                            for (; parent && parent !== clone; parent = parent.parentElement) {
                                if (/sticker|стикер/i.test(`${safeAttr(parent, "class")} ${safeAttr(parent, "data-testid")} ${safeAttr(parent, "data-type")}`)) { insideSticker = true; break; }
                            }
                            if (insideSticker) continue;
                            const hasNestedEmoji = candidates.some(child => {
                                if (child === el || !el.contains(child)) return false;
                                const childMeta = `${safeAttr(child, "class")} ${safeAttr(child, "data-testid")} ${safeAttr(child, "data-type")} ${safeAttr(child, "aria-label")} ${safeAttr(child, "alt")} ${safeAttr(child, "title")}`;
                                if (/sticker|стикер/i.test(childMeta)) return false;
                                return /emoji|эмодзи|emoticon|smiley|custom.?emoji/i.test(childMeta) ||
                                    emojiClusters(childMeta).length > 0 || child.hasAttribute("data-animated-emoji") ||
                                    ["data-custom-emoji", "data-emoji-id", "data-emoji-name", "data-codepoint", "data-codepoints", "data-emoji-code", "data-emoji-codepoint"].some(attr => child.hasAttribute(attr));
                            });
                            if (hasNestedEmoji) continue;
                            const visibleText = String(el.textContent || "");
                            if (visibleText.trim()) {
                                consume(visibleText);
                                continue;
                            }
                            const hasEmojiMetadata = /emoji|эмодзи|emoticon|smiley|custom.?emoji/i.test(meta) ||
                                emojiClusters(meta).length > 0 || el.hasAttribute("data-animated-emoji") ||
                                ["data-custom-emoji", "data-emoji-id", "data-emoji-name", "data-codepoint", "data-codepoints", "data-emoji-code", "data-emoji-codepoint"].some(attr => el.hasAttribute(attr));
                            if (!hasEmojiMetadata) continue;
                            const emoji = emojiValueFromElement(el) || emojiQueue[0] || "";
                            if (!emoji) continue;
                            el.replaceWith(document.createTextNode(emoji));
                            consume(emoji);
                        }

                        const blockTags = /^(?:DIV|P|LI|UL|OL|BLOCKQUOTE|PRE|SECTION|ARTICLE)$/;
                        const blockBreak = "\uE000";
                        const explicitBreak = "\uE001";
                        const emojiBlockStart = "\uE002";
                        const emojiBlockEnd = "\uE003";
                        const flatten = node => {
                            if (node.nodeType === 3) return node.nodeValue || "";
                            if (node.nodeType !== 1) return "";
                            if (node.tagName === "BR") return explicitBreak;
                            const inner = Array.from(node.childNodes).map(flatten).join("");
                            if (!blockTags.test(node.tagName)) return inner;
                            const visibleInner = inner.replace(/[\uE000-\uE003]/g, "").trim();
                            if (visibleInner && emojiClusters(visibleInner).join("") === visibleInner) {
                                return `${emojiBlockStart}${inner}${emojiBlockEnd}`;
                            }
                            return `${blockBreak}${inner}${blockBreak}`;
                        };
                        const flattenText = node => cleanMessageText(
                            flatten(node)
                                .replace(new RegExp(`${blockBreak}*${emojiBlockStart}([\\s\\S]*?)${emojiBlockEnd}${blockBreak}*`, "g"), "$1")
                                .replace(/[\uE000\uE001]/g, "\n")
                        );
                        const links = Array.from(clone.querySelectorAll("a[href]")).map(anchor => {
                            let url = safeAttr(anchor, "href");
                            try { url = new URL(url, location.href).href; } catch(e) {}
                            return {text:flattenText(anchor), url};
                        }).filter(link => link.text && /^(?:https?:\/\/|tg:|mailto:|tel:)/i.test(link.url));
                        return {text:flattenText(clone), links, emojiTexts};
                    } catch(e) {
                        return {text:"", links:[], emojiTexts:[]};
                    }
                }

                function getOwnText(row) {
                    return getOwnContent(row).text;
                }

                function getEmojiTexts(row) {
                    const out = [];
                    const seenElements = new WeakSet();
                    function add(value, nodeSeen) {
                        const s = clean(value);
                        if (!s) return;
                        const matches = emojiClusters(s);
                        for (const emoji of matches) {
                            if (!nodeSeen.has(emoji)) { nodeSeen.add(emoji); out.push(emoji); }
                        }
                    }
                    function addCodepoints(value, nodeSeen) {
                        const raw=String(value||'').trim(); if(!raw) return;
                        const explicit=raw.match(/(?:U\+|0x)[0-9a-f]{4,6}/gi)||[];
                        const parts=[...explicit.map(p=>p.replace(/^(?:U\+|0x)/i,'')),...raw.split(/[^0-9a-f]+/i).filter(p=>/^[0-9a-f]{4,6}$/i.test(p)&&/\d/.test(p))]; if(!parts.length) return;
                        const chars=[];
                        for(const token of parts){ try { if(!/\d/.test(token)) continue; const n=parseInt(token,16); if(n>=0x2000&&n<=0x1FAFF) chars.push(String.fromCodePoint(n)); } catch(e){} }
                        if(chars.length) add(chars.join(''), nodeSeen);
                    }

                    const selectorText = emojiNodeSelector;
                    function hasNestedEmojiNode(el) {
                        try {
                            return Array.from(el.querySelectorAll(selectorText)).some(child => {
                                const meta = `${safeAttr(child,"class")} ${safeAttr(child,"data-type")} ${safeAttr(child,"data-testid")} ${safeAttr(child,"aria-label")} ${safeAttr(child,"title")} ${safeAttr(child,"data-emoji")} ${safeAttr(child,"data-emoji-char")} ${safeAttr(child,"data-emoji-name")} ${safeAttr(child,"data-custom-emoji")}`.toLowerCase();
                                const hint = `${safeAttr(child,"alt")} ${safeAttr(child,"aria-label")} ${safeAttr(child,"title")} ${safeAttr(child,"data-emoji")} ${safeAttr(child,"data-emoji-char")} ${safeAttr(child,"data-emoji-text")}`;
                                if (/sticker|стикер/.test(meta)) return false;
                                return /emoji|эмодзи|animated|emoticon|smiley|custom.?emoji/.test(meta) ||
                                    emojiClusters(hint).length > 0;
                            });
                        } catch(e) { return false; }
                    }
                    function insideSticker(el) {
                        for (let node=el; node; node=node.parentElement) {
                            const meta=`${safeAttr(node,"class")} ${safeAttr(node,"data-type")} ${safeAttr(node,"data-testid")} ${safeAttr(node,"aria-label")} ${safeAttr(node,"title")}`;
                            if (/sticker|стикер/i.test(meta)) return true;
                        }
                        return false;
                    }
                    try {
                            row.querySelectorAll(selectorText).forEach(el => {
                                if (seenElements.has(el)) return;
                                if (insideSticker(el)) return;
                                // MAX renders one emoji as nested .emojis/.emoji/image nodes.
                                // Read the innermost matching node once; separate sibling emoji
                                // nodes remain separate so intentional repeats are preserved.
                                if (hasNestedEmojiNode(el)) return;
                                seenElements.add(el);
                                const nodeSeen = new Set();
                                const emeta = `${safeAttr(el,"class")} ${safeAttr(el,"data-type")} ${safeAttr(el,"data-testid")} ${safeAttr(el,"aria-label")} ${safeAttr(el,"title")} ${safeAttr(el,"data-custom-emoji")} ${safeAttr(el,"data-emoji-id")} ${safeAttr(el,"data-emoji-name")}`.toLowerCase();
                                if (/sticker|стикер/.test(emeta)) return;
                                add(safeAttr(el,"alt"),nodeSeen); add(safeAttr(el,"aria-label"),nodeSeen); add(safeAttr(el,"title"),nodeSeen);
                                add(safeAttr(el,"data-emoji"),nodeSeen); add(safeAttr(el,"data-emoji-char"),nodeSeen); add(safeAttr(el,"data-emoji-text"),nodeSeen); add(safeAttr(el,"data-emoji-name"),nodeSeen); add(safeAttr(el,"data-emoji-alias"),nodeSeen); add(safeAttr(el,"data-custom-emoji"),nodeSeen); add(safeAttr(el,"data-text"),nodeSeen); addCodepoints(safeAttr(el,"data-codepoint"),nodeSeen); addCodepoints(safeAttr(el,"data-codepoints"),nodeSeen); addCodepoints(safeAttr(el,"data-unicode"),nodeSeen); addCodepoints(safeAttr(el,"data-emoji-code"),nodeSeen); addCodepoints(safeAttr(el,"data-emoji-codepoint"),nodeSeen); addCodepoints(safeAttr(el,"data-code"),nodeSeen); addCodepoints(safeAttr(el,"data-custom-emoji"),nodeSeen); add(el.textContent,nodeSeen);
                                const emeta2 = `${safeAttr(el,"class")} ${safeAttr(el,"data-type")} ${safeAttr(el,"data-testid")} ${safeAttr(el,"role")} ${safeAttr(el,"data-name")} ${safeAttr(el,"data-icon")} ${safeAttr(el,"data-custom-emoji")} ${safeAttr(el,"data-emoji-id")} ${safeAttr(el,"data-emoji-name")}`.toLowerCase();
                                if (/emoji|эмодзи|animated/.test(emeta2) || el.hasAttribute("data-custom-emoji") || el.hasAttribute("data-emoji-id") || el.hasAttribute("data-emoji-name")) {
                                    add(el.textContent,nodeSeen);
                                    try {
                                        const html = String(el.outerHTML || "");
                                        add(html,nodeSeen);
                                        const mediaNodes = [el, ...Array.from(el.querySelectorAll("img,video,canvas,source"))];
                                        for (const mediaNode of mediaNodes) {
                                            let mediaSrc = (mediaNode.currentSrc || mediaNode.src || "") || safeAttr(mediaNode,"src") || safeAttr(mediaNode,"current-src") || safeAttr(mediaNode,"data-src") || safeAttr(mediaNode,"data-image-url") || safeAttr(mediaNode,"data-media-url") || safeAttr(mediaNode,"data-emoji-url") || safeAttr(mediaNode,"data-animation-url") || "";
                                            if (!mediaSrc) {
                                                try {
                                                    const bg=getComputedStyle(mediaNode).backgroundImage||"";
                                                    const bgMatch=bg.match(/url\(["']?(.*?)["']?\)/i);
                                                    if(bgMatch) mediaSrc=bgMatch[1];
                                                } catch(e) {}
                                            }
                                            if (!mediaSrc) continue;
                                            const tokens = mediaSrc.match(/(?:u[+_-]|emoji[_-]?|codepoint[_-]?)([0-9a-f]{4,6})(?:[_-]([0-9a-f]{4,6}))?|(?:^|[\/_-])((?:1f|26|27)[0-9a-f]{2,4})(?:[_-]((?:1f|26|27)[0-9a-f]{2,4}))?/gi) || [];
                                            for (const token of tokens) {
                                                const cleaned = token.replace(/^[^0-9a-f]+/i, "");
                                                addCodepoints(cleaned,nodeSeen);
                                            }
                                        }
                                        const hexes = html.match(/(?:U\\+|0x|\\\\u\\{|&#x)([0-9a-f]{4,6})\\}?;?/gi) || [];
                                        for (const token of hexes) addCodepoints(token,nodeSeen);
                                        const decs = html.match(/&#(\\d{4,7});/g) || [];
                                        for (const token of decs) {
                                            const m = token.match(/\\d+/);
                                            if (m) { try { const n=Number(m[0]); if(n>=0x2000&&n<=0x1FAFF) add(String.fromCodePoint(n),nodeSeen); } catch(e) {} }
                                        }
                                    } catch(e) {}
                                }
                            });
                    } catch(e) {}
                    return out;
                }

                function getImageCandidateCount(row) {
                    let count = 0;
                    const seen = new Set();
                    function isEmoji(el) {
                        const meta = `${safeAttr(el,"class")} ${safeAttr(el,"data-testid")} ${safeAttr(el,"aria-label")} ${safeAttr(el,"alt")} ${safeAttr(el,"title")} ${safeAttr(el,"data-type")}`;
                        if (/sticker|стикер/i.test(meta)) return false;
                        if (/(^|[ _:-])emoji([ _:-]|$)|animated.?emoji|эмодзи|emoticon|smiley|custom.?emoji/i.test(meta)) return true;
                        try { const cp=[safeAttr(el,"data-codepoint"),safeAttr(el,"data-codepoints"),safeAttr(el,"data-unicode")].join(' '); if(/^(?:U\+|0x)?[0-9a-f]{4,6}(?:[-,\s]+(?:U\+|0x)?[0-9a-f]{4,6})*$/i.test(cp.trim())) return true; } catch(e) {}
                        try { return /[\u2600-\u27BF\u{1F000}-\u{1FAFF}]/u.test(safeAttr(el,"alt") || safeAttr(el,"aria-label") || ""); } catch(e) { return false; }
                    }
                    try {
                        row.querySelectorAll("img,picture source").forEach(img => {
                            const meta = `${safeAttr(img,"class")} ${safeAttr(img,"data-testid")} ${safeAttr(img,"aria-label")} ${safeAttr(img,"alt")} ${safeAttr(img,"title")} ${safeAttr(img,"data-type")}`.toLowerCase();
                            if (/avatar|userpic|profile|favicon|spacer|blank/.test(meta) || isEmoji(img)) return;
                            const r = img.getBoundingClientRect();
                            if (r.width >= 20 && r.height >= 20) {
                                const key=`${Math.round(r.left)}:${Math.round(r.top)}:${Math.round(r.width)}:${Math.round(r.height)}`;
                                if (!seen.has(key)) {seen.add(key);count++;}
                            }
                        });
                    } catch(e) {}
                    try {
                        row.querySelectorAll("a[href],div[data-image-url],div[data-photo-url],div[data-media-url],[style*='background-image' i]").forEach(el=>{
                            const meta=`${safeAttr(el,"class")} ${safeAttr(el,"data-testid")} ${safeAttr(el,"aria-label")} ${safeAttr(el,"alt")} ${safeAttr(el,"title")} ${safeAttr(el,"data-type")}`;
                            if (/avatar|userpic|profile|favicon|spacer|blank/i.test(meta) || isEmoji(el)) return;
                            const url=safeAttr(el,"href")||safeAttr(el,"data-image-url")||safeAttr(el,"data-photo-url")||safeAttr(el,"data-media-url");
                            let bg=""; try{bg=getComputedStyle(el).backgroundImage||"";}catch(e){}
                            if(url||/^url\(/i.test(bg)) count++;
                        });
                    } catch(e) {}
                    return count;
                }

                function getEmojiImageUrls(row) {
                    const out=[]; const seen=new Set();
                    const add=u=>{ if(!u||typeof u!=='string'||u.startsWith('data:')) return; if(!seen.has(u)){seen.add(u);out.push(u);} };
                    const emojiMeta = el => {
                        const parts=[]; let n=el;
                        for(let d=0;n&&d<4;d++,n=n.parentElement){
                            parts.push(safeAttr(n,'class'),safeAttr(n,'data-testid'),safeAttr(n,'aria-label'),safeAttr(n,'alt'),
                                safeAttr(n,'title'),safeAttr(n,'data-type'),safeAttr(n,'role'),safeAttr(n,'data-emoji'),
                                safeAttr(n,'data-emoji-char'),safeAttr(n,'data-emoji-text'),safeAttr(n,'data-animated-emoji'),
                                safeAttr(n,'data-custom-emoji'),safeAttr(n,'data-emoji-id'),safeAttr(n,'data-emoji-name'));
                        }
                        return parts.join(' ').toLowerCase();
                    };
                    row.querySelectorAll('img,video,canvas,[role="img"],[class*="emoji" i],[class*="animated" i],[data-type*="emoji" i],[data-testid*="emoji" i],[data-emoji],[data-emoji-char],[data-animated-emoji],[data-custom-emoji],[data-emoji-id],[data-emoji-name],[data-emoji-code],[data-emoji-codepoint]').forEach(el=>{
                        const meta=emojiMeta(el);
                        if(/sticker|стикер/.test(meta)) return;
                        if(!/(emoji|эмодзи|animated.?emoji|custom.?emoji|emoticon|smiley)/i.test(meta)) return;
                        const attrs=['src','current-src','data-src','data-image-url','data-media-url','data-emoji-url','data-animation-url','data-original','data-original-src'];
                        for(const a of attrs){ const v=(a==='src'&&el.currentSrc)?el.currentSrc:safeAttr(el,a); if(/^(https?:|blob:)/i.test(v)) add(v); }
                        try{ const bg=getComputedStyle(el).backgroundImage||''; const m=bg.match(/url\(["']?(.*?)["']?\)/i); if(m) add(m[1]); }catch(e){}
                    });
                    return out;
                }

                function getImageUrls(row) {
                    const images = [];
                    const seen = new Set();
                    function add(src) {
                        if (!src || typeof src !== "string") return;
                        src = src.trim();
                        if (!src || src.startsWith("data:")) return;
                        try {
                            if (src.startsWith("//")) src = location.protocol + src;
                            else if (src.startsWith("/")) src = new URL(src, location.href).href;
                        } catch(e) {}
                        if (!seen.has(src)) { seen.add(src); images.push(src); }
                    }
                    function metaOf(el) {
                        return `${safeAttr(el,"class")} ${safeAttr(el,"data-testid")} ${safeAttr(el,"aria-label")} ${safeAttr(el,"alt")} ${safeAttr(el,"title")} ${safeAttr(el,"data-type")} ${safeAttr(el,"role")}`.toLowerCase();
                    }
                    function isEmojiNode(el) {
                        let node = el;
                        let stickerSeen = false;
                        for (let depth=0; node && depth<4; depth++, node=node.parentElement) {
                            const meta = metaOf(node);
                            if (/sticker|стикер/.test(meta)) stickerSeen = true;
                            if (!stickerSeen && /(^|[ _:-])emoji([ _:-]|$)|animated.?emoji|эмодзи|emoticon|smiley|custom.?emoji/.test(meta)) return true;
                            try {
                                const hint = safeAttr(node,"alt")||safeAttr(node,"aria-label")||safeAttr(node,"title")||safeAttr(node,"data-text")||safeAttr(node,"data-emoji")||"";
                                if (!stickerSeen && /[\u2600-\u27BF\u{1F000}-\u{1FAFF}]/u.test(hint)) return true;
                            } catch(e) {}
                        }
                        if (stickerSeen) return false;
                        // Маленькая картинка без caption чаще всего является визуальным emoji.
                        try {
                            const r=el.getBoundingClientRect();
                            if (r.width>0 && r.height>0 && r.width<=96 && r.height<=96) return true;
                        } catch(e) {}
                        return false;
                    }
                    try {
                        row.querySelectorAll("img").forEach(img => {
                            const meta = metaOf(img);
                            if (/avatar|userpic|profile|favicon|spacer|blank/.test(meta)) return;
                            if (isEmojiNode(img)) return;
                            // Сначала собираем реальный URL. У lazy-photo MAX размер img
                            // иногда временно равен 0x0, хотя src/data-* уже заполнены.
                            const before = images.length;
                            add((typeof img.currentSrc === "string" && img.currentSrc) || safeAttr(img,"src"));
                            if (typeof img.src === "string") add(img.src);
                            ["data-src","data-original","data-lazy-src","data-image","data-photo","data-image-url","data-photo-url","data-media-url","data-original-src","data-attachment-url","data-content-url","data-file-url"].forEach(a => add(safeAttr(img,a)));
                            const srcset = safeAttr(img,"srcset") || safeAttr(img,"data-srcset");
                            if (srcset) srcset.split(",").map(x=>x.trim().split(/\s+/)[0]).forEach(add);
                            // Если URL нет, только тогда требуем заметный размер.
                            const rect = img.getBoundingClientRect();
                            if (images.length === before && (rect.width < 12 || rect.height < 12)) return;
                        });
                    } catch(e) {}
                    try {
                        row.querySelectorAll("picture source, source").forEach(el => {
                            const meta = metaOf(el);
                            if (/avatar|userpic|profile|favicon|emoji|animated.?emoji|эмодзи/.test(meta)) return;
                            const srcset = safeAttr(el,"srcset") || safeAttr(el,"data-srcset");
                            if (srcset) srcset.split(",").map(x=>x.trim().split(/\s+/)[0]).forEach(add);
                            ["src","data-src","data-original","data-image-url","data-photo-url","data-media-url","data-attachment-url","data-content-url"].forEach(a=>add(safeAttr(el,a)));
                            if (typeof el.src === "string") add(el.src);
                        });
                    } catch(e) {}
                    try {
                        row.querySelectorAll("*").forEach(el => {
                            const meta = metaOf(el);
                            if (/avatar|userpic|profile|favicon|emoji|animated.?emoji|эмодзи/.test(meta)) return;
                            ["data-image-url","data-photo-url","data-media-url","data-image","data-photo","data-original","data-original-src","data-src","data-lazy-src","data-attachment-url","data-content-url","data-file-url"].forEach(a=>add(safeAttr(el,a)));
                            let candidates = [];
                            const style = safeAttr(el,"style");
                            const sm = style && style.match(/url\([\"']?(.*?)[\"']?\)/i);
                            if (sm) candidates.push(sm[1]);
                            try {
                                const bg = getComputedStyle(el).backgroundImage || "";
                                const bm = bg.match(/url\([\"']?(.*?)[\"']?\)/i);
                                if (bm) candidates.push(bm[1]);
                            } catch(e) {}
                            const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : {width:0,height:0};
                            // У MAX анимированный стикер может быть CSS background-image, без img/src.
                            if (rect.width >= 40 && rect.height >= 40 &&
                                /(photo|image|media|attachment|picture|sticker|стикер|фото|изображ|картин)/i.test(meta)) {
                                candidates.forEach(v => { if (/^(?:https?:|blob:)/i.test(String(v||""))) add(v); });
                            }
                        });
                    } catch(e) {}
                    try {
                        row.querySelectorAll("a[href]").forEach(a => {
                            const href = safeAttr(a,"href");
                            const meta = metaOf(a);
                            if (/avatar|userpic|profile|favicon/.test(meta) || isEmojiNode(a)) return;
                            if (/\.(?:jpe?g|png|gif|webp|bmp)(?:[?#].*)?$/i.test(href) || /^blob:/i.test(href) || /(photo|image|media|attachment|picture|фото|изображ|картин)/i.test(meta)) add(href);
                        });
                    } catch(e) {}
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


                function getRichAttachments(row) {
                    const out = [];
                    const seen = new Set();
                    function push(type, url, filename, extra = {}) {
                        if (!url) return;
                        url = String(url).trim();
                        if (!url || url.startsWith("data:")) return;
                        const key = `${type}|${url}`;
                        if (seen.has(key)) return;
                        seen.add(key);
                        out.push({type, url, filename: filename || "", ...extra});
                    }
                    function classify(meta, tag) {
                        meta = String(meta || "").toLowerCase();
                        if (/video.?note|videonote|кружок|кружочек|round.?video|roundvideo/.test(meta)) return "video_note";
                        if (/voice|voice.?message|voice_message|голосов|аудиосообщ/.test(meta)) return "voice";
                        if (/emoji|эмодзи|animated.?emoji/.test(meta) && !/sticker|стикер/.test(meta)) return "emoji";
                        if (tag === "video" || /(^|[ _-])video(?:[ _-]|message|$)|movie|mp4/.test(meta)) return "video";
                        if (tag === "audio" || /(^|[ _-])audio([ _-]|$)|ogg|opus|m4a|mp3/.test(meta)) return "audio";
                        if (/sticker/.test(meta)) return "image";
                        return "document";
                    }
                    function candidate(el) {
                        let u = getCandidateUrl(el);
                        if (!u) u = safeAttr(el,"src") || safeAttr(el,"current-src") || safeAttr(el,"data-src") ||
                            safeAttr(el,"data-video-url") || safeAttr(el,"data-audio-url") || safeAttr(el,"data-voice-url") ||
                            safeAttr(el,"data-media-url") || safeAttr(el,"data-sticker-url") || safeAttr(el,"data-url") || safeAttr(el,"data-href");
                        return u;
                    }
                    // One video element may expose the same clip through <video>,
                    // several <source> nodes and MAX's data-* attributes. Use the
                    // browser-selected source once, so quality variants are not
                    // mistaken for separate messages.
                    const mediaOwners = Array.from(row.querySelectorAll("video, audio"));
                    mediaOwners.forEach(el => {
                        const tagName = String(el.tagName || "").toLowerCase();
                        const mediaTag = tagName;
                        let url = "";
                        try {
                            if (typeof el.currentSrc === "string" && /^(https?:|blob:)/i.test(el.currentSrc)) url = el.currentSrc;
                        } catch(e) {}
                        if (!url) url = safeAttr(el, "src") || candidate(el);
                        if (!url) {
                            const source = el.querySelector("source[src],source[data-src]");
                            if (source) url = safeAttr(source, "src") || safeAttr(source, "data-src") || candidate(source);
                        }
                        let meta = "";
                        for (let node = el, depth = 0; node && depth < 4; node = node.parentElement, depth++) {
                            meta += ` ${safeAttr(node,"class")} ${safeAttr(node,"data-testid")} ${safeAttr(node,"aria-label")} ${safeAttr(node,"data-type")} ${safeAttr(node,"role")}`;
                        }
                        const duration = Number.isFinite(Number(el.duration)) && Number(el.duration) > 0
                            ? Number(el.duration) : 0;
                        const poster = mediaTag === "video"
                            ? (safeAttr(el, "poster") || (typeof el.poster === "string" ? el.poster : "")) : "";
                        if (url) push(classify(meta, mediaTag), url, safeAttr(el,"download") || safeAttr(el,"data-filename") || "", {duration, poster});
                    });
                    const elements = row.querySelectorAll(
                        "[data-video-url], [data-audio-url], [data-voice-url], [data-media-url], [data-sticker-url], " +
                        "[data-video], [data-audio], [data-voice], [data-media], [data-voice-message], [data-video-note]"
                    );
                    elements.forEach(el => {
                        // Attributes nested in an actual media element are alternate
                        // URLs for that same clip, already handled above.
                        if (el.closest && el.closest("video, audio")) return;
                        const meta = `${safeAttr(el,"class")} ${safeAttr(el,"data-testid")} ${safeAttr(el,"aria-label")} ${safeAttr(el,"data-type")} ${safeAttr(el,"role")}`;
                        const url = candidate(el);
                        if (url) push(classify(meta, String(el.tagName || "").toLowerCase()), url, safeAttr(el,"download") || safeAttr(el,"data-filename") || "");
                    });
                    // Некоторые версии MAX держат медиа в div/span как background-image.
                    row.querySelectorAll("div,span,a,canvas").forEach(el => {
                        const meta = `${safeAttr(el,"class")} ${safeAttr(el,"data-testid")} ${safeAttr(el,"aria-label")} ${safeAttr(el,"data-type")} ${safeAttr(el,"role")}`;
                        if (!/(video|voice|audio|круж|голос|sticker|media)/i.test(meta)) return;
                        if (el.closest && el.closest("video, audio")) return;
                        const wantedType = classify(meta, String(el.tagName || "").toLowerCase());
                        if ((wantedType === "video" || wantedType === "video_note") && out.some(item => item.type === "video" || item.type === "video_note")) return;
                        let url = candidate(el);
                        if (!url) {
                            try {
                                const bg = getComputedStyle(el).backgroundImage;
                                const m = bg && bg.match(/url\(["']?(.*?)["']?\)/);
                                if (m) url = m[1];
                            } catch(e) {}
                        }
                        if (url) push(classify(meta, String(el.tagName || "").toLowerCase()), url, safeAttr(el,"data-filename") || "");
                    });
                    // Sticker/emoji-картинки теперь обрабатываются как image в getImageUrls.
                    // Не добавляем их вторично как sticker, чтобы не получить дубль.
                    // Дополнительный fallback: некоторые MAX-карточки хранят видео/voice URL
                    // в произвольных data-* атрибутах контейнера.
                    row.querySelectorAll("*").forEach(el => {
                        const meta = `${safeAttr(el,"class")} ${safeAttr(el,"data-testid")} ${safeAttr(el,"aria-label")} ${safeAttr(el,"data-type")} ${safeAttr(el,"role")}`;
                        if (!/(video|voice|audio|круж|голос|media|videonote|video-note)/i.test(meta)) return;
                        if (el.closest && el.closest("video, audio")) return;
                        const wantedType = classify(meta, String(el.tagName || "").toLowerCase());
                        if ((wantedType === "video" || wantedType === "video_note") && out.some(item => item.type === "video" || item.type === "video_note")) return;
                        const url = candidate(el);
                        if (url) push(classify(meta, String(el.tagName || "").toLowerCase()), url, safeAttr(el,"data-filename") || "");
                    });
                    // MAX может показывать только длительность 00:01, а URL видео/voice
                    // лежит у одного из ближайших родителей. Ищем такой URL до 6 уровней вверх.
                    try {
                        let hasVideoAttachment = out.some(item => item.type === "video" || item.type === "video_note");
                        row.querySelectorAll("*").forEach(el => {
                            const t = clean(el.textContent);
                            if (!/^\d{1,2}:\d{2}$/.test(t)) return;
                            let node = el;
                            for (let depth = 0; node && depth < 6; depth++, node = node.parentElement) {
                                const meta = `${safeAttr(node,"class")} ${safeAttr(node,"data-testid")} ${safeAttr(node,"aria-label")} ${safeAttr(node,"data-type")} ${safeAttr(node,"role")}`;
                                const url = candidate(node);
                                if (url && /(video|voice|audio|круж|голос|message)/i.test(meta)) {
                                    const type = classify(meta, String(node.tagName || "").toLowerCase());
                                    if (!((type === "video" || type === "video_note") && hasVideoAttachment)) {
                                        push(type, url, safeAttr(node,"data-filename") || "");
                                        if (type === "video" || type === "video_note") hasVideoAttachment = true;
                                    }
                                    break;
                                }
                            }
                        });
                    } catch(e) {}
                    return out;
                }

                function getSpecialContent(row, messageRow=null) {
                    const result = {location:null, poll:null, contact:null};
                    const outerRow = messageRow || row.closest(".item") || row;

                    // Геопозиция.
                    try {
                        const nodes = row.querySelectorAll(
                            "[data-latitude][data-longitude],[data-lat][data-lng]," +
                            "[data-latitude][data-long],[class*='location' i],[class*='map' i]"
                        );
                        for (const el of nodes) {
                            const lat = safeAttr(el,"data-latitude") || safeAttr(el,"data-lat");
                            const lon = safeAttr(el,"data-longitude") || safeAttr(el,"data-long") || safeAttr(el,"data-lng");
                            if (lat && lon && isFinite(Number(lat)) && isFinite(Number(lon))) {
                                result.location = {latitude:Number(lat), longitude:Number(lon), title:"", url:""};
                                break;
                            }
                        }
                        if (!result.location) {
                            for (const a of row.querySelectorAll("a[href]")) {
                                const href = safeAttr(a,"href");
                                if (!/yandex\.(?:ru|com)\/maps|maps\.google|2gis/i.test(href)) continue;
                                let m = href.match(/[?&]ll=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)/i);
                                if (!m) m = href.match(/(?:lat|latitude)=(-?\d+(?:\.\d+)?).*?(?:lon|lng|longitude)=(-?\d+(?:\.\d+)?)/i);
                                if (m) {
                                    if (/yandex\.(?:ru|com)\/maps/i.test(href) && /[?&]ll=/i.test(href)) {
                                        result.location = {latitude:Number(m[2]), longitude:Number(m[1]), title:"", url:href};
                                    } else {
                                        result.location = {latitude:Number(m[1]), longitude:Number(m[2]), title:"", url:href};
                                    }
                                    break;
                                }
                            }
                        }
                        // MAX иногда оставляет только ссылку/атрибут без явных data-lat/data-lng.
                        if (!result.location) {
                            const attrNames = ["href","data-url","data-href","data-map-url","data-location","data-coordinates","aria-label","title"];
                            for (const el of row.querySelectorAll("*") ) {
                                for (const an of attrNames) {
                                    const v = safeAttr(el, an);
                                    if (!v) continue;
                                    let m = String(v).match(/[?&]ll=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)/i);
                                    if (m) { result.location={latitude:Number(m[2]),longitude:Number(m[1]),title:"",url:(an === "href" ? String(v) : "")}; break; }
                                    m = String(v).match(/(?:lat|latitude)[=:](-?\d+(?:\.\d+)?)[^\d-]+(?:lon|lng|longitude)[=:](-?\d+(?:\.\d+)?)/i);
                                    if (m) { result.location={latitude:Number(m[1]),longitude:Number(m[2]),title:"",url:(an === "href" ? String(v) : "")}; break; }
                                    m = String(v).match(/geo:(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)/i);
                                    if (m) { result.location={latitude:Number(m[1]),longitude:Number(m[2]),title:"",url:""}; break; }
                                }
                                if (result.location) break;
                            }
                        }
                        if (!result.location && /open yandex maps|открыть yandex maps/i.test(clean(row.innerText))) {
                            for (const el of row.querySelectorAll("*")) {
                                const values = [safeAttr(el,"href"), safeAttr(el,"data-url"), safeAttr(el,"data-href"), safeAttr(el,"data-map-url"), safeAttr(el,"data-location"), safeAttr(el,"data-coordinates")];
                                for (const v of values) {
                                    const m = String(v || "").match(/[?&]ll=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)/i);
                                    if (m) { result.location={latitude:Number(m[2]),longitude:Number(m[1]),title:"",url:String(v)}; break; }
                                }
                                if (result.location) break;
                            }
                        }
                        // Даже без координат сохраняем прямую ссылку на Yandex Maps.
                        if (!result.location && /open yandex maps|открыть yandex maps/i.test(clean(row.innerText))) {
                            for (const el of row.querySelectorAll("a[href],button,[role='button'],[data-url],[data-href],[data-map-url]")) {
                                const values = [safeAttr(el,"href"),safeAttr(el,"data-url"),safeAttr(el,"data-href"),safeAttr(el,"data-map-url")];
                                for (const v of values) {
                                    if (/https?:\/\/yandex\.(?:ru|com)\/maps/i.test(String(v || ""))) {
                                        result.location={latitude:null,longitude:null,title:"",url:String(v)};
                                        break;
                                    }
                                }
                                if (result.location) break;
                            }
                        }
                    } catch(e) {}

                    // Опрос. Название определяем отдельно от вариантов и служебных чисел MAX.
                    try {
                        const poll = row.querySelector("[data-testid*='poll' i],[class*='poll' i],[data-type='poll'],[class*='survey' i]");
                        if (poll) {
                            const cleanPollLine = s => clean(String(s || "")).replace(/^[⚪⚫▫•]+\s*/u, "").replace(/\s+/g, " ").trim();
                            const multiSelectMeta = /^(?:select one or multiple answers|select one or more answers|select multiple answers|выберите один или несколько (?:вариантов )?ответов)(?:\s*[:.]?\s*\d+)?$/i;
                            const isPollMetaLine = s => multiSelectMeta.test(cleanPollLine(s)) || /^(?:select an answer|выберите(?:\s+вариант)?\s+ответ(?:а)?|голосовать|vote|ответить|проголосовать|show results|показать результаты(?: опроса)?|poll ended\b.*|опрос заверш[её]н\b.*|total votes?\b.*|всего голосов?\b.*)$/i.test(cleanPollLine(s));
                            const serviceOnly = /^(?:select one or multiple answers|select one or more answers|select multiple answers|выберите один или несколько (?:вариантов )?ответов|select an answer|выберите(?:\s+вариант)?\s+ответ(?:а)?|голосовать|vote|ответить|проголосовать|show results|показать результаты(?: опроса)?)(?:\b|$)/i;
                            const rawPollText = String(poll.innerText || poll.textContent || "");
                            const activePoll = /select\s+an\s+answer|choose\s+an\s+answer|выберите\s+(?:вариант\s+)?ответ(?:а)?/i.test(rawPollText);
                            const flatPollVotesMatch = rawPollText.match(/(?:^|\s)(\d+)\s*(?:votes?|голос(?:а|ов)?)(?=$|[\s.,;])/i);
                            const options = [];
                            const addOption = (rawValue, attrVoteValue="", isExplicitOption=false) => {
                                let value = cleanPollLine(rawValue);
                                if (!value || serviceOnly.test(value) || isPollMetaLine(value)) return;
                                let votes = null;
                                // Результат MAX иногда склеен в одну строку:
                                // "Вариант . 2 votes, 100%. Ваш выбор".
                                // Число перед процентом — голоса; отметки выбора/победителя не часть названия.
                                value = value.replace(/\s+(?:your choice|ваш выбор|winner|победитель|you voted|вы проголосовали|selected)(?=$|[\s.,;:]).*$/i, "");
                                let result = value.match(/^(.*?)\s*[.,]\s*(\d+)\s*[,;]\s*\d+(?:[.,]\d+)?\s*%.*$/i);
                                if (result) {
                                    value = cleanPollLine(result[1]);
                                    votes = Number(result[2]);
                                }
                                // Убираем процент/полосу результата прежде, чем разбираем
                                // счетчик "2 votes", иначе хвост процента мешает распознаванию.
                                value = value.replace(/\s*[,;·|]?\s*\d+(?:[.,]\d+)?\s*%.*$/, "").trim();
                                // Сначала явный формат "Название: 2" или "Название . 2 votes".
                                let m = value.match(/^(.*?)\s*:\s*(\d+)\s*$/);
                                if (votes == null && m) {
                                    value = cleanPollLine(m[1]);
                                    votes = Number(m[2]);
                                } else if (votes == null) {
                                    m = value.match(/^(.*?)[\s.,]+(\d+)\s*(?:votes?|голос(?:а|ов)?)$/i);
                                    if (m) {
                                        value = cleanPollLine(m[1]);
                                        votes = Number(m[2]);
                                    } else {
                                        const av = String(attrVoteValue || "");
                                        const avm = av.match(/(?:^|\s)(\d+)\s*(?:votes?|голос(?:а|ов)?)?/i);
                                        if (avm) votes = Number(avm[1]);
                                    }
                                }
                                value = value.replace(/[\s.,;:]+$/, "").trim();
                                value = value.replace(/^\s*[-•▫]+\s*/, "").trim();
                                if (!value || serviceOnly.test(value)) return;
                                const numberedAnswerSequence = value.match(/((?:(?:ответ|answer)\s+\d+)(?:\s+(?:ответ|answer)\s+\d+)+)$/i);
                                if (numberedAnswerSequence) {
                                    const labels = numberedAnswerSequence[1].match(/(?:ответ|answer)\s+\d+/gi) || [];
                                    labels.forEach((label, index) => {
                                        const optionVotes = index === labels.length - 1 && votes != null ? votes : 0;
                                        const existing = options.find(x => String(x.text || "").toLowerCase() === label.toLowerCase());
                                        if (!existing) {
                                            options.push({text:label, votes:optionVotes, explicit:isExplicitOption});
                                        } else {
                                            existing.votes = Math.max(Number(existing.votes) || 0, optionVotes);
                                            existing.explicit = existing.explicit || isExplicitOption;
                                        }
                                    });
                                    return;
                                }
                                const capitalizedChoices = value.split(/\s+(?=[A-ZА-ЯЁ])/u);
                                if (capitalizedChoices.length >= 4) {
                                    capitalizedChoices.forEach((rawLabel, index) => {
                                        let label = cleanPollLine(rawLabel).replace(/^[\s.,;:]+|[\s.,;:]+$/g, "");
                                        let optionVotes = index === capitalizedChoices.length - 1 && votes != null ? votes : 0;
                                        const inlineCount = label.match(/^(.*?)\s*:\s*(\d+)$/);
                                        if (inlineCount) {
                                            label = cleanPollLine(inlineCount[1]);
                                            optionVotes = Number(inlineCount[2]);
                                        }
                                        if (!label) return;
                                        const existing = options.find(x => String(x.text || "").toLowerCase() === label.toLowerCase());
                                        if (!existing) {
                                            options.push({text:label, votes:optionVotes, explicit:isExplicitOption});
                                        } else {
                                            existing.votes = Math.max(Number(existing.votes) || 0, optionVotes);
                                            existing.explicit = existing.explicit || isExplicitOption;
                                        }
                                    });
                                    return;
                                }
                                if (/^\d+$/.test(value) && !isExplicitOption) return;
                                if (/^(option|вариант|вариант ответа|название)$/i.test(value)) return;
                                const existing = options.find(x => String(x.text || "").toLowerCase() === value.toLowerCase());
                                if (!existing) {
                                    options.push({text:value, votes, explicit:isExplicitOption});
                                } else {
                                    if (votes != null) existing.votes = votes;
                                    existing.explicit = existing.explicit || isExplicitOption;
                                }
                            };

                            const optionSelector = "[data-option],[class*='option' i],[class*='answer' i],[data-testid*='option' i],[role='radio'],[role='option'],button,[role='button']";
                            const explicitNodes = Array.from(poll.querySelectorAll(optionSelector)).filter(node => {
                                const ownText = cleanPollLine(node.innerText || node.textContent);
                                if (!ownText) return false;
                                const semanticOption = node.matches("[data-option],[class*='option' i],[class*='answer' i],[data-testid*='option' i],[role='radio'],[role='option']");
                                if (isPollMetaLine(ownText) || (!semanticOption && /^\d+(?:\s+(?:votes?|голос(?:а|ов)?))?$/i.test(ownText))) return false;
                                if (!semanticOption && !/[\p{L}\p{Extended_Pictographic}]/u.test(ownText)) return false;
                                // Prefer the innermost option node when MAX nests label spans
                                // inside a clickable answer; this avoids parsing the same row twice.
                                return !Array.from(node.querySelectorAll(optionSelector)).some(child =>
                                    cleanPollLine(child.innerText || child.textContent) === ownText
                                );
                            });
                            explicitNodes.forEach(node => addOption(node.innerText || node.textContent, safeAttr(node,"data-votes") || safeAttr(node,"data-count") || safeAttr(node,"aria-label"), true));

                            const pollStateNodes = [poll, outerRow, ...Array.from(outerRow.querySelectorAll(
                                "[data-status],[data-state],[data-poll-status],[class*='closed' i],[class*='ended' i],[class*='finished' i]"
                            ))];
                            const pollStateMetadata = pollStateNodes.map(el => [
                                safeAttr(el,"data-status"), safeAttr(el,"data-state"),
                                safeAttr(el,"data-poll-status"), safeAttr(el,"class"),
                                safeAttr(el,"data-testid"), safeAttr(el,"aria-label"), safeAttr(el,"title")
                            ].join(" ")).join(" ");
                            // "Show results" is not a closed state; require an explicit end marker.
                            const closedText = /(?:poll|survey)\s+(?:is\s+)?(?:closed|ended|finished)|(?:опрос\s+(?:закрыт|заверш[её]н)|голосование\s+(?:закрыто|завершено))/i
                                .test(`${rawPollText} ${outerRow.innerText || ""}`);
                            const closedState = /(?:^|[\s_-])(?:closed|ended|finished|completed|закрыт|закрыто|заверш[её]н|завершено)(?:$|[\s_-])/i
                                .test(pollStateMetadata);
                            const isClosed = closedText || closedState;
                            const rawLines = rawPollText.split(/\n+/).map(cleanPollLine).filter(Boolean);
                            let aggregateVotes = null;
                            for (const line of rawLines) {
                                // A flattened MAX result line contains every option. It is
                                // parsed as a whole below; never treat it as one option here.
                                if (/\d+\s*(?:votes?|голос(?:а|ов)?)\s*[,;]\s*\d+(?:[.,]\d+)?\s*%/iu.test(line)) continue;
                                if (/^\d+$/.test(line)) {
                                    const n=Number(line);
                                    if (n>=0 && n<=1000000) aggregateVotes=n;
                                }
                                if (/votes?|голос(?:а|ов)/i.test(line)) {
                                    const aggregateMatch=line.match(/^(\d+)\s*(?:votes?|голос(?:а|ов)?)$/i);
                                    if (aggregateMatch) {
                                        aggregateVotes=Number(aggregateMatch[1]);
                                    } else {
                                        const vm=line.match(/^(.*?)\s+(?:\d+\s+)?(\d+)\s*(?:votes?|голос(?:а|ов)?)$/i);
                                        if (vm) addOption(vm[1], vm[2]);
                                    }
                                } else if (/^(.*?)\s*:\s*\d+$/.test(line)) {
                                    addOption(line, line);
                                }
                            }
                            for (const a of ["data-total-votes","data-total-vote-count"]) {
                                const v=safeAttr(poll,a); const m=String(v).match(/\d+/);
                                if (m) aggregateVotes=Number(m[0]);
                            }
                            if (aggregateVotes == null) {
                                const aria=safeAttr(poll,"aria-label");
                                const m=aria.match(/(\d+)\s*(?:votes?|голос(?:а|ов)?)/i);
                                if (m) aggregateVotes=Number(m[1]);
                            }

                            // Название: data-question -> question-node -> первая осмысленная строка,
                            // причём чистое число и служебный UI никогда не становятся названием.
                            let question=cleanPollLine(poll.getAttribute("data-question") || "");
                            if (!question) {
                                const qnode=poll.querySelector("[class*='question' i],[class*='poll-question' i]");
                                if (qnode) question=cleanPollLine(qnode.textContent);
                            }
                            if (!question) {
                                const serviceMarker=rawPollText.match(/select\s+one\s+or\s+(?:multiple|more)\s+answers|select\s+multiple\s+answers|choose\s+one\s+or\s+(?:multiple|more)\s+answers|select\s+an\s+answer|choose\s+an\s+answer|выберите\s+один\s+или\s+несколько\s+(?:вариантов\s+)?ответов|выберите\s+(?:вариант\s+)?ответ(?:а)?/i);
                                if (serviceMarker) question=cleanPollLine(rawPollText.slice(0,serviceMarker.index));
                            }
                            const meaningful=rawLines.filter(line =>
                                !/^\d+$/.test(line) &&
                                !serviceOnly.test(line) && !isPollMetaLine(line) &&
                                !/^\d+\s*(?:votes?|голос(?:а|ов)?)$/i.test(line)
                            );
                            if (!question && meaningful.length) {
                                question=meaningful[0].replace(/\s*:\s*\d+\s*$/," ").trim();
                                question=question.replace(/\s+(?:\d+)\s*$/," ").trim();
                            }

                            // Когда отдельные option-node существуют, rawLines только дополняют их.
                            // Когда их нет, после названия остальные строки являются вариантами.
                            if (!options.length || (options.length===1 && question && String(options[0].text||"").toLowerCase()===question.toLowerCase())) {
                                const qIdx=meaningful.findIndex(line => question && line.toLowerCase()===question.toLowerCase());
                                meaningful.slice(qIdx>=0?qIdx+1:1).forEach(line => {
                                    if (/^\d+$/.test(line)) return;
                                    addOption(line, line);
                                });
                            }

                            // Иногда option-node ошибочно включает название. Удаляем точное совпадение.
                            if (question) {
                                for (let i=options.length-1;i>=0;i--) {
                                    if (!options[i].explicit && String(options[i].text||"").toLowerCase()===question.toLowerCase()) options.splice(i,1);
                                }
                            }
                            if (!question || /^\d+$/.test(question) || serviceOnly.test(question)) question="Опрос";

                            // MAX can expose a whole active poll's choices as one option node.
                            // Recover only the observed flat shapes, after all DOM and text sources
                            // have been combined and the poll-level vote count is available.
                            if (activePoll && options.length===1) {
                                const flatValue=cleanPollLine(options[0].text);
                                const flatTwoChoiceSequence=flatValue.match(/^([a-zа-яё][a-zа-яё0-9_-]*)\s+([a-zа-яё][a-zа-яё0-9_-]*)$/u);
                                const flatThreeZeroVoteChoices=flatValue.match(/^([a-zа-яё][a-zа-яё0-9_-]*)\s+([a-zа-яё][a-zа-яё0-9_-]*)\s+([a-zа-яё][a-zа-яё0-9_-]*)$/u);
                                const pollVoteCount=flatPollVotesMatch
                                    ? Number(flatPollVotesMatch[1])
                                    : (options[0].votes==null ? null : Number(options[0].votes));
                                let recoveredLabels=null;
                                let recoveredVotes=[];
                                if (pollVoteCount!=null && pollVoteCount>0 && flatTwoChoiceSequence) {
                                    recoveredLabels=flatTwoChoiceSequence.slice(1);
                                    recoveredVotes=[0,pollVoteCount];
                                } else if ((pollVoteCount==null || pollVoteCount===0) &&
                                    /no\s+answers\s+yet/i.test(rawPollText) && flatThreeZeroVoteChoices) {
                                    recoveredLabels=flatThreeZeroVoteChoices.slice(1);
                                    recoveredVotes=recoveredLabels.map(()=>0);
                                }
                                if (recoveredLabels) {
                                    options.splice(0,1,...recoveredLabels.map((label,index)=>({
                                        text:label, votes:recoveredVotes[index], explicit:true,
                                    })));
                                }
                            }

                            // MAX can render every answer and its vote count inside one
                            // text node. Recover each "... N votes, P%" segment from that
                            // flattened text so zero-vote options are not lost.
                            const resultPattern=/(\d+)\s*(?:votes?|голос(?:а|ов)?)\s*[,;]\s*\d+(?:[.,]\d+)?\s*%/giu;
                            const serviceMarker=rawPollText.match(/select\s+one\s+or\s+(?:multiple|more)\s+answers|select\s+multiple\s+answers|choose\s+one\s+or\s+(?:multiple|more)\s+answers|select\s+an\s+answer|choose\s+an\s+answer|выберите\s+один\s+или\s+несколько\s+(?:вариантов\s+)?ответов|выберите\s+(?:вариант\s+)?ответ(?:а)?/i);
                            let segmentStart=serviceMarker
                                ? serviceMarker.index + serviceMarker[0].length
                                : (question!=="Опрос" ? Math.max(0,rawPollText.toLowerCase().indexOf(question.toLowerCase()) + question.length) : 0);
                            let previousEnd=segmentStart;
                            let previousVotes=null;
                            let resultMatch;
                            const resultOptionLabels=[];
                            while ((resultMatch=resultPattern.exec(rawPollText)) !== null) {
                                if (resultMatch.index < segmentStart) continue;
                                let label=rawPollText.slice(previousEnd,resultMatch.index);
                                label=label.replace(/(?:your choice|ваш выбор|you voted|вы проголосовали|selected)(?=$|[\s.,;:])/ig," ");
                                label=label.replace(/(?:winner|победитель)(?=$|[\s.,:;])\s*[.,:;]?\s*\d*/ig," ");
                                label=label.replace(/(?:select\s+an\s+answer|choose\s+an\s+answer|выберите\s+(?:вариант\s+)?ответ(?:а)?|выбор\s+сразу\s+отправит\s+голос)(?=$|[\s.,;:])/ig," ");
                                if (previousVotes !== null) {
                                    const leadingCount=label.match(/^[\s.,;:]*(\d+)\s+/);
                                    if (leadingCount && Number(leadingCount[1])===previousVotes) {
                                        label=label.slice(leadingCount[0].length);
                                    }
                                }
                                label=cleanPollLine(label).replace(/^[\s.,;:]+|[\s.,;:]+$/g,"").trim();
                                if (question!=="Опрос" && label.toLowerCase().startsWith(question.toLowerCase())) {
                                    label=label.slice(question.length).trim();
                                }
                                if (label) {
                                    addOption(label,resultMatch[1]);
                                    resultOptionLabels.push(label.toLowerCase());
                                }
                                previousVotes=Number(resultMatch[1]);
                                previousEnd=resultPattern.lastIndex;
                            }

                            // A generic MAX button can wrap several result labels into one
                            // pseudo-option. When the percentage rows split those labels,
                            // discard only that composite wrapper and keep each real answer.
                            if (resultOptionLabels.length > 1) {
                                for (let i=options.length-1;i>=0;i--) {
                                    const existing=cleanPollLine(options[i].text).toLowerCase();
                                    if (!resultOptionLabels.includes(existing) && resultOptionLabels.every(label=>existing.includes(label))) {
                                        options.splice(i,1);
                                    }
                                }
                            }

                            // Ключевой случай MAX: отдельное число сверху = общее число ответов.
                            // Если остался один реальный вариант без собственного счётчика, оно относится к нему.
                            if (options.length===1 && (options[0].votes==null || Number(options[0].votes)===0) && aggregateVotes!=null) {
                                options[0].votes=aggregateVotes;
                            }
                            const normalizedOptions=options
                                // A standalone number is a vote/count control in MAX's
                                // poll DOM, not an answer option (for example "1: 0").
                                .filter(item=>!/^[+-]?\d+(?:[.,]\d+)?$/.test(String(item.text||"").trim()))
                                .map(item=>({
                                    text:String(item.text||"").trim(),
                                    votes:Number.isFinite(Number(item.votes))?Math.max(0,Number(item.votes)):0,
                                    explicit:Boolean(item.explicit)
                                }));
                            const totalVotes=normalizedOptions.reduce((sum,item)=>sum+item.votes,0);
                            if (options.length || question!=="Опрос") {
                                result.poll={question:question.slice(0,255),options:normalizedOptions,total_votes:totalVotes,raw_text:cleanPollLine(rawPollText),is_closed:isClosed};
                            }
                        }
                    } catch(e) {}

                    // Контакт.
                    try {
                        const rowText = clean(outerRow.innerText);
                        const visibleContactText = clean(getOwnText(outerRow));
                        const phonePattern = /(?<!\w)\+?\d[\d\s().-]{8,}\d(?!\w)/;
                        const validPhoneText = value => {
                            const digitCount = String(value || "").replace(/\D/g, "").length;
                            return digitCount >= 10 && digitCount <= 15;
                        };
                        const contactSelector="[data-testid*='contact-card' i],[data-testid*='contact-message' i],[data-testid*='vcard' i],[class*='contact-card' i],[class*='contact-message' i],[class*='vcard' i],[data-contact],[data-vcard],[data-phone],[data-contact-phone],[data-contact-phone-number]";
                        const telLink = row.querySelector("a[href^='tel:' i],[href^='tel:' i]");
                        const contact = row.querySelector(contactSelector) || outerRow.querySelector(contactSelector) || telLink;
                        const explicitContactText = Boolean(contact);
                        // Номер в тексте, аватар или кнопка перехода не доказывают,
                        // что сообщение является контактом: у обычных карточек MAX
                        // встречаются длинные числовые ID.
                        if (!contact && !explicitContactText) {
                            // Обычное сообщение/имя — не контакт. Ничего не добавляем.
                        } else {
                        const scope = contact || row;
                        let phone = "";
                        const normalizePhone = (raw, allowBare=false) => {
                            const value=String(raw||"").replace(/%2B/ig,"+").trim();
                            const digits=value.replace(/\D/g,"");
                            if(digits.length<10 || digits.length>15) return "";
                            if(!allowBare && !value.startsWith("+") && !/[\s().-]/.test(value)) return "";
                            return (value.startsWith("+")?"+":"")+digits;
                        };
                        const phoneAttrs = ["data-phone","data-contact-phone","data-phone-number","data-user-phone","data-mobile","phone","tel","mobile","telephone","msisdn"];
                        for (const el of [scope, ...Array.from(scope.querySelectorAll("*")).slice(0,500)]) {
                            for (const a of phoneAttrs) {
                                const v = safeAttr(el,a);
                                if (v) { phone = normalizePhone(v,true); if(phone) break; }
                            }
                            if (phone) break;
                        }
                        if (!phone) {
                        const tel=scope.querySelector("a[href^='tel:' i],[href^='tel:' i]") ||
                            (scope.matches && scope.matches("a[href^='tel:' i],[href^='tel:' i]") ? scope : null);
                            if(tel) phone=normalizePhone((safeAttr(tel,"href")||"").replace(/^tel:/i,""),true);
                        }
                        if (!phone) {
                            const labeled=clean(scope.textContent).match(/(?:phone(?:\s*number)?|mobile(?:\s*phone)?|telephone|tel|msisdn|телефон|мобильный)\s*[:=]?\s*(\+?\d[\d\s().-]{8,}\d)/i);
                            if(labeled) phone=normalizePhone(labeled[1],true);
                        }
                        if (!phone) {
                            const visible=clean(scope.textContent);
                            const formatted=(visible.match(/(?<!\w)\+?\d[\d\s().-]{8,}\d(?!\w)/g) || []).find(validPhoneText);
                            if(formatted) phone=normalizePhone(formatted);
                        }
                        if (!phone) {
                            const vcard=String(safeAttr(scope,"data-vcard")||safeAttr(scope,"data-contact")||"");
                            const tel=vcard.match(/TEL(?:;[^:\r\n]*)?:\s*(?:tel:)?([+\d][\d ()+.-]{8,}\d)/i);
                            if(tel) phone=normalizePhone(tel[1],true);
                        }
                        let contactName = "";
                        if (visibleContactText) {
                            const cleanedVisible = visibleContactText.replace(/already in your contacts|уже в ваших контактах/ig," ").trim();
                            const nameVisible = phone ? cleanedVisible
                                .replace(phonePattern, " ")
                                .replace(/\b(?:phone(?:\s*number)?|mobile(?:\s*phone)?|telephone|tel|телефон|мобильный)\b\s*[:=]?/ig, " ")
                                .replace(/go to conversation|open chat|перейти к диалогу|перейти в чат|открыть чат/ig, " ")
                                .replace(/\s+/g, " ").trim() : cleanedVisible;
                            const lines = nameVisible.split(/\n+/).map(x=>x.trim()).filter(Boolean);
                            contactName = lines.find(x => !/\d{5,}/i.test(x)) || "";
                        }
                        if (!contactName && contact) {
                            const nameNode = scope.querySelector("[data-contact-name],[data-display-name],[class*='contact-name' i],[class*='display-name' i]");
                            if (nameNode) contactName = clean(nameNode.textContent);
                        }
                        if (!contactName) {
                            const lines = rowText.split(/\n+/).map(x=>x.trim()).filter(Boolean);
                            contactName = lines.find(x => !/already in your contacts|уже в ваших контактах|\d{5,}/i.test(x)) || "";
                        }
                        contactName = contactName.replace(/already in your contacts|уже в ваших контактах/ig,"").trim();
                        if (phone || contactName) result.contact = {phone: phone.slice(0,64), first_name: (contactName || "").slice(0,64), last_name: "", is_contact: true};
                        }
                    } catch(e) {}

                    return result;
                }

                rows.forEach((row, index) => {
                    try {
                        const domIndex = firstSelectedIndex + index;
                        const fullRowText = clean(row.innerText);
                        const isSystemEvent = /(?:изменил(?:а)?(?:\s+[^.!?]{0,80})?(?:название|имя)|добавил(?:а)?[^.!?]{0,80}(?:в чат|в группу)|удалил(?:а)?[^.!?]{0,80}(?:из чата|из группы)|исключил(?:а)?[^.!?]{0,80}(?:из чата|из группы)|пригласил(?:а)?[^.!?]{0,80}(?:в чат|в группу)|вошел(?:\s+в)?\s+чат|вошла(?:\s+в)?\s+чат|покинул(?:а)?\s+чат|создал(?:а)?\s+чат|вернулся(?:\s+в)?\s+чат|вернулась(?:\s+в)?\s+чат)/i.test(fullRowText);

                        const sourceId =
                            safeAttr(row, "data-message-id") ||
                            safeAttr(row, "data-mid") ||
                            safeAttr(row, "data-id") ||
                            safeAttr(row, "data-index") ||
                            String(domIndex);

                        if (isSystemEvent) {
                            results.push({
                                idx: domIndex,
                                source_id: String(sourceId),
                                source_id_is_index: !Boolean(
                                    safeAttr(row, "data-message-id") ||
                                    safeAttr(row, "data-mid") ||
                                    safeAttr(row, "data-id")
                                ),
                                is_system_event: true,
                                name: "",
                                text: "",
                                time: "",
                                date: "",
                                images: [],
                                documents: [],
                                reply_text: ""
                            });
                            return;
                        }

                        const block = row.querySelector(".block");
                        if (!block) return;
                        const bubble = block.querySelector(".bubbleContent");
                        if (!bubble) return;

                        const name = getAuthor(row);
                        const role = getRole(row, name);
                        // Сначала получаем emoji из DOM, затем используем их как fallback
                        // для emoji-only сообщений. В v10 переменная emojiTexts объявлялась
                        // ПОСЛЕ первого обращения к ней, из-за чего каждый обычный .item
                        // падал с ReferenceError и в итоге парсер видел 0 сообщений.
                        const richContent = getOwnContent(row);
                        const emojiTexts = richContent.emojiTexts;
                        let text = richContent.text;
                        // Emoji-only сообщения часто не имеют отдельного .text в MAX.
                        // Сохраняем сами эмодзи, не превращая время/служебный текст в сообщение.
                        if (!text && emojiTexts.length) text = emojiTexts.join("");
                        const time = getTime(row);
                        const date = getDate(row);
                        const replyBlock = getReplyBlock(row);
                        const replyText = getReplyText(replyBlock);
                        // Media and special-card selectors are scoped to this message's bubble.
                        // The outer .item can also contain nested/replied cards and UI controls.
                        const images = getImageUrls(bubble);
                        const emojiImageUrls = getEmojiImageUrls(bubble);
                        const imageCandidateCount = getImageCandidateCount(bubble);
                        let photoVisualHint = Boolean(bubble.querySelector("img, picture source, canvas, [data-image-url], [data-photo-url], [data-media-url], [style*='background-image' i]"));
                        if (!photoVisualHint) {
                            try {
                                const visualNodes = bubble.querySelectorAll("div,span,a,button,figure");
                                for (const el of visualNodes) {
                                    const meta = `${safeAttr(el,"class")} ${safeAttr(el,"data-testid")} ${safeAttr(el,"aria-label")} ${safeAttr(el,"alt")} ${safeAttr(el,"title")}`;
                                    if (/avatar|userpic|profile|favicon|emoji|animated.?emoji|эмодзи|sticker|стикер/i.test(meta)) continue;
                                    const rect = el.getBoundingClientRect();
                                    const bg = getComputedStyle(el).backgroundImage || "";
                                    if (rect.width >= 100 && rect.height >= 80 && /^url\(/i.test(bg) && /(photo|image|media|picture|attachment|gallery|фото|изображ|картин)/i.test(meta)) {
                                        photoVisualHint = true;
                                        break;
                                    }
                                    if (rect.width >= 120 && rect.height >= 90 && /^url\(/i.test(bg)) { photoVisualHint = true; break; }
                                }
                            } catch(e) {}
                        }
                        const richAttachments = getRichAttachments(bubble);
                        const stickerHint = Boolean(bubble.querySelector("[class*='sticker' i],[data-testid*='sticker' i],[aria-label*='sticker' i],[aria-label*='стикер' i],[data-sticker]"));
                        const mediaMeta = (()=>{ try { return Array.from(bubble.querySelectorAll("video,audio,source,button,[role='button'],[aria-label],[data-testid]")).map(el=>`${safeAttr(el,'class')} ${safeAttr(el,'data-testid')} ${safeAttr(el,'aria-label')} ${safeAttr(el,'title')} ${safeAttr(el,'data-type')} ${el.tagName||''}`).join(' ').toLowerCase(); } catch(e) { return ''; } })();
                        const deepMediaMeta = (()=>{ try { return Array.from(bubble.querySelectorAll("*")).map(el=>`${safeAttr(el,'class')} ${safeAttr(el,'data-testid')} ${safeAttr(el,'aria-label')} ${safeAttr(el,'title')} ${safeAttr(el,'data-type')}`).join(' ').toLowerCase(); } catch(e) { return ''; } })();
                        const callLike = /звонок|вызов|call|calling|ringing|incoming call|outgoing call/.test(`${fullRowText} ${mediaMeta}`.toLowerCase());
                        // time в шапке .item — время сообщения, а не длительность
                        // вложения. Берём длительность только из самой bubbleContent.
                        const bubbleText = clean(bubble.innerText);
                        // Bubble text also contains the message timestamp (for example 11:12 PM).
                        // Only MAX's dedicated media duration nodes can mark audio/video.
                        const mediaDurationText = Array.from(bubble.querySelectorAll("[class~='duration'],[class~='time']"))
                            .map(el=>clean(el.textContent)).filter(value=>/^\d{1,2}:\d{2}$/.test(value)).join(" ");
                        const hasDuration = Boolean(mediaDurationText);
                        const durationMatch = mediaDurationText.match(/^(\d{1,2}):(\d{2})/);
                        const liveVideoDuration = Array.from(bubble.querySelectorAll("video"))
                            .map(el => Number.isFinite(el.duration) && el.duration > 0 ? el.duration : 0)
                            .find(value => value > 0) || 0;
                        const mediaDurationSeconds = (durationMatch ? Number(durationMatch[1])*60 + Number(durationMatch[2]) : 0) || liveVideoDuration;
                        let videoNoteHint = !callLike && (/video.?note|videonote|кружок|кружочек|round.?video|roundvideo/.test(mediaMeta) || richAttachments.some(a=>a.type==="video_note"));
                        let voiceHint = !videoNoteHint && !callLike && (/(?:voice|voice.?message|voice_message|attach.?audio|waveform|голосов(?:ое|ая|ым|ого)(?:\s+(?:сообщение|запись|файл))?|аудиосообщ)/.test(deepMediaMeta) || richAttachments.some(a=>a.type==="voice") || bubble.querySelector('audio')!=null || (hasDuration && /audio|attach.?audio|waveform|голосов(?:ое|ая)/.test(deepMediaMeta)));
                        const special = getSpecialContent(bubble, row);
                        // Vote controls include the Russian word "голосовать"; they must
                        // never turn a completed poll into a voice-message candidate.
                        if (special.poll) {
                            voiceHint = false;
                            videoNoteHint = false;
                        }
                        // На некоторых сборках MAX строка статуса контакта находится рядом
                        // с bubbleContent. Используем только точную системную фразу как маркер.
                        if (!special.contact && /already in your contacts|уже в ваших контактах/i.test(fullRowText)) {
                            const contactName = clean(text.replace(/already in your contacts|уже в ваших контактах/ig, " "));
                            if (contactName) special.contact = {phone:"", first_name:contactName.slice(0,64), last_name:"", is_contact:true};
                        }
                        const pollServiceOnly = !special.poll &&
                            /poll|survey|option|answer|vote|опрос|голос/i.test(`${safeAttr(row,"class")} ${safeAttr(row,"data-testid")} ${fullRowText}`) &&
                            /^(.*?)\s+\d+(?:\s+\d+)?\s+(?:votes?|голос(?:а|ов)?)$/i.test(fullRowText);
                        // Не выбрасываем такую строку здесь: Python объединит её с основным
                        // опросом и удалит после переноса количества голосов.
                        // Длительность кружка/голосового не является текстом сообщения.
                        if (videoNoteHint || voiceHint || bubble.querySelector("video") || richAttachments.some(a => a.type === "video" || a.type === "video_note" || a.type === "voice")) {
                            if (/^\s*\d{1,2}:\d{2}\s*$/.test(text)) text = "";
                        }
                        if (special.location) {
                            text = text.replace(/(?:open yandex maps|открыть yandex maps|открыть карту)/ig, "").trim();
                        }
                        if (special.contact) {
                            text = text.replace(/already in your contacts|уже в ваших контактах/ig, "").trim();
                        }
                        const documents = [];
                        const filenameHint = extractFilename(fullRowText);

                        getFileNodes(bubble).forEach((el, nodeIndex) => {
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
                                    const clickId = `max-parser-download-${domIndex}-${nodeIndex}`;
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
                        const clickControls = bubble.querySelectorAll(
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
                                downloadClickId = `max-parser-download-${domIndex}-final-${nodeIndex}`;
                                try {
                                    el.setAttribute("data-max-parser-download-id", downloadClickId);
                                } catch (e) {}
                                break;
                            }
                        }

                        const bareMediaCard = Boolean(bubble.querySelector("video,audio,canvas,[class*='voice' i],[class*='video-note' i],[class*='videonote' i],[class*='круж' i],[class*='голос' i]"));
                        const hasSomething = Boolean(text || emojiTexts.length || emojiImageUrls.length || images.length || documents.length || richAttachments.length || stickerHint || special.location || special.poll || special.contact || filenameHint || downloadClickId || bareMediaCard);
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
                            idx: domIndex,
                            source_id: String(sourceId),
                            data_message_id: safeAttr(row, "data-message-id"),
                            mid: safeAttr(row, "data-mid"),
                            data_id: safeAttr(row, "data-id"),
                            source_id_is_index: !Boolean(
                                safeAttr(row, "data-message-id") ||
                                safeAttr(row, "data-mid") ||
                                safeAttr(row, "data-id")
                            ),
                            dom_id: `max-parser-message-${domIndex}`,
                            download_click_id: downloadClickId,
                            download_click_filename: filenameHint,
                            name: finalName || "Аноним",
                            text,
                            inline_links: richContent.links,
                            time,
                            date,
                            date_source: safeAttr(row,"data-max-parser-date-source") || "unresolved",
                            images,
                            emoji_image_urls: emojiImageUrls,
                            image_candidate_count: imageCandidateCount,
                            photo_visual_hint: photoVisualHint,
                            emoji_texts: emojiTexts,
                            documents,
                            attachments: richAttachments,
                            sticker_hint: stickerHint,
                            location: special.location,
                            poll: special.poll,
                            contact: special.contact,
                            video_note_hint: Boolean(videoNoteHint),
                            voice_hint: Boolean(voiceHint),
                            media_duration_seconds: mediaDurationSeconds,
                            media_hint_debug: `${mediaDurationText} | ${mediaMeta}`.slice(0,500),
                            is_reply: Boolean(replyBlock),
                            reply_text: replyText,
                            is_system_event: isSystemEvent,
                            is_poll_service: Boolean(pollServiceOnly),
                        });

                        try {
                            row.setAttribute("data-max-parser-message-id", `max-parser-message-${domIndex}`);
                        } catch (e) {}

                    } catch (e) {
                        console.error("MAX Parser: ошибка обработки .item:", e);
                    }
                });

                return results;
            }
            """,
            MAX_MESSAGES_TO_PARSE,
        )
        if raw_messages is None:
            print(f"[{chat_id}] ⚠️ MAX не вернул список сообщений; считаю ленту пустой")
            raw_messages = []
        elif not isinstance(raw_messages, list):
            raise TypeError(
                f"MAX вернул неожиданный тип списка сообщений: {type(raw_messages).__name__}"
            )
        debug_logger.info(
            "MAX_DOM_EXTRACTED chat_id=%s raw_cards=%s max_messages=%s",
            chat_id, len(raw_messages), MAX_MESSAGES_TO_PARSE,
        )
        if len(raw_messages) > MAX_MESSAGES_TO_PARSE:
            raise RuntimeError(
                f"MAX извлёк {len(raw_messages)} карточек при установленном лимите "
                f"{MAX_MESSAGES_TO_PARSE}; отправка остановлена"
            )

        # Системные события MAX (переименование чата, вход/выход участника и т.п.)
        # могут на мгновение перестроить DOM и изменить data-index у старых сообщений.
        # Ничего из этого цикла не отправляем. На следующем цикле сообщения будут
        # проверены по стабильной сигнатуре содержимого и уже отправленные не уйдут повторно.
        cleaned = []
        seen_ids = set()
        for msg in raw_messages:
            text = _normalize_message_lines(msg.get("text") or "")
            text = _strip_browser_video_fallback(text)
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
            if re.fullmatch(r"\d{1,2}:\d{2}", text) and (msg.get("attachments") or re.search(r"voice|audio|video|круж|голос", json.dumps(msg, ensure_ascii=False), re.I)):
                text = ""
            msg["text"] = text
            msg["source_id"] = source_id
            if not is_human_message(msg):
                continue
            if source_id:
                if source_id in seen_ids:
                    existing = next((x for x in cleaned if str(x.get("source_id") or "") == source_id), None)
                    if existing and existing.get("_pre_signature") == msg.get("_pre_signature"):
                        continue
                seen_ids.add(source_id)
            cleaned.append(msg)

        # В режиме тестирования отправляем ВСЕ найденные сообщения, которые
        # реально присутствуют в загруженной ленте MAX. DOM-порядок MAX —
        # от старых к новым, поэтому ничего не переворачиваем.
        all_messages = cleaned

        # MAX иногда выводит количество голосов отдельной соседней .item, например
        # "Название 1 1 vote". Это не отдельное сообщение — переносим его в poll.
        consumed_poll_service = set()
        for i, poll_msg in enumerate(all_messages):
            poll = poll_msg.get("poll") or {}
            if not poll:
                continue
            for j in range(i + 1, min(i + 6, len(all_messages))):
                if j in consumed_poll_service:
                    continue
                candidate = " ".join((all_messages[j].get("text") or "").split()).strip()
                m = re.match(r"^(.*?)\s+(\d+)\s+(?:votes?|голос(?:а|ов))$", candidate, re.I)
                if not m:
                    if j > i + 2:
                        break
                    continue
                label = re.sub(r"\s+\d+\s*$", "", m.group(1)).strip(" -–—")
                if not label or re.fullmatch(r"\d+", label):
                    continue
                votes = int(m.group(2))
                options = poll.setdefault("options", [])
                found = None
                for opt in options:
                    if isinstance(opt, dict) and str(opt.get("text") or "").casefold() == label.casefold():
                        found = opt
                        break
                if found is not None:
                    found["votes"] = votes
                else:
                    options.append({"text": label, "votes": votes})
                consumed_poll_service.add(j)
            poll["total_votes"] = sum(max(0, int(o.get("votes") or 0)) for o in poll.get("options", []) if isinstance(o, dict))
        all_messages = [m for idx, m in enumerate(all_messages) if idx not in consumed_poll_service]
        all_messages = all_messages[-MAX_MESSAGES_TO_PARSE:] if MAX_MESSAGES_TO_PARSE else []
        print(f"[{chat_id}] После фильтрации: {len(all_messages)} сообщений (от старых к новым)")

        human_posts = []
        for msg in all_messages:
            if not (msg.get("emoji_texts") or "") and bool(msg.get("emoji_image_urls")):
                refreshed_emoji = _refresh_emoji_texts(page, msg, chat_id=chat_id)
                if refreshed_emoji:
                    msg["emoji_texts"] = refreshed_emoji
                    if not str(msg.get("text") or "").strip():
                        msg["text"] = "".join(refreshed_emoji)
            if (msg.get("contact") or {}).get("is_contact"):
                contact_obj=msg.setdefault("contact", {})
                if not str(contact_obj.get("phone") or "").strip():
                    resolved_phone = _resolve_contact_phone_from_card(page, msg, chat_id=chat_id, group_url=group_url)
                    if resolved_phone:
                        contact_obj["phone"] = resolved_phone
                if not str(contact_obj.get("phone") or "").strip() and not str(contact_obj.get("url") or "").strip():
                    resolved_link = _resolve_contact_link_via_dialog(page, msg, group_url=group_url, chat_id=chat_id)
                    if resolved_link:
                        contact_obj["url"] = resolved_link
            media_files = []
            def has_expected_rich_media():
                if msg.get("video_note_hint") or msg.get("voice_hint"):
                    return any(_media_satisfies_hint(m, msg) for m in media_files)
                return any(m.get("type") in ("voice", "video_note", "video", "audio") for m in media_files)
            image_urls = []
            seen_image_urls = set()
            rich_media_types = {"voice", "audio", "video", "video_note", "animation", "emoji"}
            rich_media_urls = {
                str(a.get("url") or "").strip()
                for a in (msg.get("attachments") or [])
                if isinstance(a, dict) and a.get("type") in rich_media_types and str(a.get("url") or "").strip()
            }
            rich_media_hint = bool(rich_media_urls) or any(
                isinstance(a, dict) and a.get("type") in rich_media_types
                for a in (msg.get("attachments") or [])
            )
            rich_video_hint = any(
                isinstance(a, dict) and a.get("type") in {"video", "video_note"}
                for a in (msg.get("attachments") or [])
            )
            image_video_fallbacks = []
            emoji_image_urls = {str(u).strip() for u in (msg.get("emoji_image_urls") or []) if str(u).strip()}
            # MAX emoji assets are inline glyphs, not standalone photo attachments.
            # Keep Unicode in the message text and omit assets whose glyph MAX hides.
            for u in (msg.get("images", []) or []):
                u=str(u or "").strip()
                if not u or u in emoji_image_urls:
                    continue
                if u not in seen_image_urls:
                    seen_image_urls.add(u); image_urls.append(u)
            # Повторно читаем живую карточку: именно captioned-photo часто получает
            # настоящий src только после того, как карточка попала в viewport.
            for u in _refresh_card_image_urls(page, msg, chat_id=chat_id):
                if u in emoji_image_urls:
                    continue
                if u not in seen_image_urls:
                    seen_image_urls.add(u); image_urls.append(u)
            rich_url_overlaps = {u for u in image_urls if u in rich_media_urls}
            image_urls = [u for u in image_urls if u not in rich_media_urls]
            try:
                raw_candidate_count = max(0, int(msg.get("image_candidate_count") or 0))
            except Exception:
                raw_candidate_count = 0
            candidate_count = max(
                len(image_urls),
                max(0, raw_candidate_count - len(rich_url_overlaps)),
            )
            photo_visual_hint = bool(msg.get("photo_visual_hint")) and not (
                rich_media_hint and not image_urls
            )

            # Captioned-photo is warmed up before reading its URL. A screenshot is a
            # fallback only: adding it alongside a successfully downloaded image creates
            # visually identical duplicates with different byte hashes.
            emoji_only = bool(msg.get("emoji_texts")) and not bool((msg.get("text") or "").strip().replace(" ", ""))
            has_captioned_photo_hint = bool((msg.get("text") or "").strip()) and bool(
                image_urls or (photo_visual_hint and not rich_media_hint)
            )
            if has_captioned_photo_hint and not rich_media_hint and not emoji_only and not (msg.get("contact") or {}).get("is_contact") and not msg.get("location") and not msg.get("poll"):
                _warmup_captioned_photo_card(page, msg, chat_id=chat_id)

            non_image_urls = set()
            for img_url in image_urls:
                media = get_media_bytes(
                    img_url,
                    media_type="image",
                    chat_id=chat_id,
                    context=context,
                    page=page,
                )
                if media and media.get("bytes"):
                    detected_type = media.get("type")
                    if detected_type in {"video", "audio"}:
                        non_image_urls.add(img_url)
                        if detected_type == "video" and rich_video_hint:
                            # MAX can expose a low-resolution preview URL beside the
                            # real attachment. Keep it only as a fallback if the
                            # attachment itself cannot be downloaded.
                            image_video_fallbacks.append(media)
                            continue
                        if detected_type == "video" and msg.get("video_note_hint"):
                            media["type"] = "video_note"
                        elif detected_type == "audio" and msg.get("voice_hint"):
                            media["type"] = "voice"
                        media["filename"] = _media_filename(
                            media.get("filename", ""),
                            img_url,
                            media.get("content_type", ""),
                            media["bytes"],
                            media["type"],
                        )
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
            candidate_count = max(0, candidate_count - len(non_image_urls))
            if non_image_urls and candidate_count == 0:
                photo_visual_hint = False
                has_captioned_photo_hint = False
            downloaded_image_count = sum(1 for m in media_files if m.get("type") == "image")
            should_visual_fallback = (
                ((candidate_count and downloaded_image_count < candidate_count) or
                 (photo_visual_hint and downloaded_image_count == 0) or
                 (has_captioned_photo_hint and downloaded_image_count == 0))
                and not emoji_only and not msg.get("voice_hint") and not msg.get("video_note_hint")
            )
            if should_visual_fallback:
                debug_logger.warning(
                    "MAX_VISUAL_FALLBACK_SKIPPED chat_id=%s source_id=%s reason=screenshot_fallback_disabled",
                    chat_id, msg.get("source_id") or msg.get("idx"),
                )

            for attachment in msg.get("attachments", []) or []:
                att_url = (attachment.get("url") or "").strip()
                if not att_url:
                    continue
                att_type = attachment.get("type") or "document"
                if att_type == "emoji":
                    continue
                if att_type == "video" and msg.get("video_note_hint"):
                    att_type = "video_note"
                elif att_type == "audio" and msg.get("voice_hint"):
                    att_type = "voice"
                media = _download_document_to_memory(
                    page=page,
                    context=context,
                    url=att_url,
                    filename=attachment.get("filename", ""),
                    chat_id=chat_id,
                    media_type=att_type,
                )
                if media and media.get("bytes"):
                    media_kind = _media_payload_kind(media)
                    expected_type = "video" if msg.get("video_note_hint") else "audio" if msg.get("voice_hint") else ""
                    if expected_type and media_kind != expected_type:
                        debug_logger.warning(
                            "MAX_MEDIA_ATTACHMENT_KIND_MISMATCH chat_id=%s source_id=%s expected=%s detected=%s content_type=%s bytes=%s",
                            chat_id, msg.get("source_id") or msg.get("idx"), expected_type,
                            media_kind or "unknown", media.get("content_type") or "<none>", len(media.get("bytes") or b"")
                        )
                        continue
                    if not expected_type and att_type in {"image", "sticker"} and media_kind == "video":
                        # MAX animated stickers can be exposed by a sticker card
                        # instead of a <video> node. Trust the downloaded container
                        # signature so WebM/MP4 reaches Telegram as a video.
                        att_type = "video"
                        media["filename"] = _media_filename(
                            media.get("filename", ""), att_url,
                            media.get("content_type", ""), media["bytes"], "video",
                        )
                    media["type"] = "video_note" if expected_type == "video" else "voice" if expected_type == "audio" else att_type
                    if att_type in {"video", "video_note"} and attachment.get("duration"):
                        try:
                            media["duration"] = float(attachment["duration"])
                        except (TypeError, ValueError):
                            pass
                    poster_url = str(attachment.get("poster") or "").strip()
                    if att_type == "video" and poster_url:
                        try:
                            poster_media = get_media_bytes(
                                poster_url,
                                media_type="image",
                                chat_id=chat_id,
                                context=context,
                                page=page,
                            )
                            if poster_media and poster_media.get("bytes") and poster_media.get("type") == "image":
                                media["thumbnail_bytes"] = poster_media["bytes"]
                        except Exception as exc:
                            debug_logger.info(
                                "MAX_VIDEO_POSTER_FETCH_FAILED chat_id=%s source_id=%s error=%s",
                                chat_id, msg.get("source_id") or msg.get("idx"), log_preview(exc, 160),
                            )
                    media_files.append(media)
            if image_video_fallbacks and not any(
                _media_payload_kind(media) == "video" for media in media_files
            ):
                fallback_video = max(
                    image_video_fallbacks,
                    key=lambda media: len(media.get("bytes") or b""),
                )
                fallback_video["type"] = "video"
                fallback_video["filename"] = _media_filename(
                    fallback_video.get("filename", ""),
                    fallback_video.get("url", ""),
                    fallback_video.get("content_type", ""),
                    fallback_video["bytes"],
                    "video",
                )
                media_files.append(fallback_video)
            # Exact media element fallback: read bytes from the <audio>/<video>
            # belonging to this message card only. No page-wide resource scan.
            if not has_expected_rich_media():
                expected_direct = "video_note" if msg.get("video_note_hint") else "voice" if msg.get("voice_hint") else ""
                if expected_direct:
                    media_files.extend(_read_media_element_bytes(page, msg, expected_direct, chat_id=chat_id))
            if not has_expected_rich_media():
                media_files.extend(_extract_live_rich_media(page, msg, chat_id=chat_id))
            if not has_expected_rich_media() and (msg.get("voice_hint") or msg.get("video_note_hint")):
                expected_type = "video_note" if msg.get("video_note_hint") else "voice"
                media_files.extend(_take_preloaded_media(page, msg, expected_type, chat_id))
            if not has_expected_rich_media() and (
                bool(msg.get("voice_hint")) or bool(msg.get("video_note_hint")) or
                (re.search(r"^\d{1,2}:\d{2}$", str(msg.get("text") or "").strip()) and
                 (msg.get("attachments") or msg.get("voice_hint") or msg.get("video_note_hint")))
            ):
                expected_type = "video_note" if bool(msg.get("video_note_hint")) else "voice"
                clicked_media = _capture_media_after_click(page,msg,expected_type=expected_type,chat_id=chat_id)
                media_files.extend(clicked_media)
                _claim_preloaded_media(page, clicked_media)
                if not has_expected_rich_media():
                    _activate_media_card(page, msg, chat_id=chat_id)
                    page.wait_for_timeout(500)
                    media_files.extend(_extract_live_rich_media(page, msg, chat_id=chat_id))
            if msg.get("download_click_id") and not media_files:
                click_type = msg.get("download_click_type") or "document"
                media = _download_document_from_message_card(
                    page=page,
                    context=context,
                    msg=msg,
                    chat_id=chat_id,
                    media_type=click_type,
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
            if msg.get("sticker_hint"):
                has_animation_payload = any(
                    media.get("type") in {"video", "animation"}
                    or _animated_image_kind(bytes(media.get("bytes") or b""))
                    for media in media_files
                )
                can_replace_still = not media_files or (
                    len(media_files) == 1 and media_files[0].get("type") in {"image", "sticker"}
                )
                if not has_animation_payload and can_replace_still:
                    recorded_sticker = _capture_animated_sticker(page, msg, chat_id=chat_id)
                    if recorded_sticker:
                        media_files = recorded_sticker
            unique_media = []
            seen_media_hashes = set()
            seen_direct_visual_hashes = []
            duplicate_media_count = 0
            visual_duplicate_count = 0
            if msg.get("sticker_hint"):
                for media in media_files:
                    if media.get("type") in {"image", "sticker"}:
                        media["_sticker_fallback"] = True
            for media in media_files:
                if (msg.get("video_note_hint") or msg.get("voice_hint")) and not _media_satisfies_hint(media, msg):
                    debug_logger.warning(
                        "MAX_MEDIA_PAYLOAD_KIND_MISMATCH chat_id=%s source_id=%s expected=%s detected=%s content_type=%s bytes=%s",
                        chat_id, msg.get("source_id") or msg.get("idx"),
                        "video" if msg.get("video_note_hint") else "audio",
                        _media_payload_kind(media) or "unknown", media.get("content_type") or "<none>",
                        len(media.get("bytes") or b"")
                    )
                    continue
                if msg.get("video_note_hint") and media.get("type") == "video":
                    media["type"] = "video_note"
                elif msg.get("voice_hint") and media.get("type") == "audio":
                    media["type"] = "voice"
                if media.get("_sticker_fallback"):
                    media = _record_animated_image_as_video(media, chat_id=chat_id)
                    if media.get("type") in {"image", "sticker"}:
                        media = _prepare_static_sticker_image(media, chat_id=chat_id)
                else:
                    media = _record_animated_image_as_video(media, chat_id=chat_id)
                media = _prepare_video_note_for_telegram(media, chat_id=chat_id)
                data = media.get("bytes")
                if not isinstance(data, (bytes, bytearray)) or not data:
                    continue
                media_hash = get_media_hash(data=data)
                visual_hash = _image_visual_hash(bytes(data)) if media.get("type") == "image" else None
                visual_cache_hash = _visual_cache_key(visual_hash) if visual_hash else ""
                media_identity = (
                    visual_cache_hash
                    if media.get("visual_fallback") and visual_cache_hash
                    else media_hash
                )
                if media_identity in seen_media_hashes:
                    duplicate_media_count += 1
                    continue
                if media.get("visual_fallback") and visual_hash and any(
                    _similar_visual_image(visual_hash, previous_hash)
                    for previous_hash in seen_direct_visual_hashes
                ):
                    visual_duplicate_count += 1
                    continue
                seen_media_hashes.add(media_identity)
                media["hash"] = visual_cache_hash if media.get("visual_fallback") and visual_cache_hash else media_hash
                if visual_cache_hash:
                    media["visual_hash"] = visual_cache_hash
                unique_media.append(media)
                if media.get("type") == "image" and visual_hash:
                    seen_direct_visual_hashes.append(visual_hash)
            human_posts.append({
                "source_id": msg.get("source_id", ""),
                "source_id_is_index": bool(msg.get("source_id_is_index")),
                "idx": msg.get("idx"),
                "data_message_id": msg.get("data_message_id", ""),
                "mid": msg.get("mid", ""),
                "data_id": msg.get("data_id", ""),
                "name": msg.get("name", "Аноним"),
                "text": msg.get("text", ""),
                "inline_links": msg.get("inline_links", []),
                "emoji_texts": msg.get("emoji_texts", []),
                "time": msg.get("time", ""),
                "date": msg.get("date", ""),
                "date_source": msg.get("date_source", "unresolved"),
                "location": msg.get("location"),
                "poll": msg.get("poll"),
                "contact": msg.get("contact"),
                "special": {
                    "location": msg.get("location"),
                    "poll": msg.get("poll"),
                    "contact": msg.get("contact"),
                },
                "media_files": unique_media,
                "photo_visual_hint": bool(msg.get("photo_visual_hint")),
                "voice_hint": bool(msg.get("voice_hint")),
                "video_note_hint": bool(msg.get("video_note_hint")),
                "media_duration_seconds": float(msg.get("media_duration_seconds") or 0),

            })
            parsed_post = human_posts[-1]
            parsed_types = [m.get("type", "unknown") for m in unique_media]
            media_info = [
                (m.get("type"), len(m.get("bytes") or b""), m.get("content_type") or "",
                 m.get("filename") or "", (m.get("bytes") or b"")[:16].hex())
                for m in unique_media
            ]
            debug_logger.info(
                "MAX_PARSED chat_id=%s source_id=%s date=%s date_source=%s time=%s duration=%ss media=%s candidates=%s "
                "emoji=%s emoji_image_candidates=%s poll=%s contact=%s voice_hint=%s video_note_hint=%s hint_debug=%s text=%s",
                chat_id,
                parsed_post.get("source_id") or parsed_post.get("idx"),
                parsed_post.get("date") or "<missing>",
                parsed_post.get("date_source") or "unresolved",
                parsed_post.get("time") or "<missing>",
                parsed_post.get("media_duration_seconds") or 0,
                media_info,
                [(a.get("type"), bool(a.get("url"))) for a in (msg.get("attachments") or []) if isinstance(a, dict)],
                log_preview("".join(str(x) for x in (parsed_post.get("emoji_texts") or [])), 80) or "<none>",
                len(msg.get("emoji_image_urls") or []),
                log_preview(json.dumps(parsed_post.get("poll"), ensure_ascii=False), 220) if parsed_post.get("poll") else "<none>",
                bool((parsed_post.get("contact") or {}).get("is_contact")),
                bool(parsed_post.get("voice_hint")),
                bool(parsed_post.get("video_note_hint")),
                log_preview(msg.get("media_hint_debug"), 200) or "<none>",
                log_preview(parsed_post.get("text")),
            )
            if not parsed_post.get("date"):
                debug_logger.warning(
                    "MAX_DATE_MISSING chat_id=%s source_id=%s source=%s time=%s text=%s",
                    chat_id, parsed_post.get("source_id") or parsed_post.get("idx"),
                    parsed_post.get("date_source") or "unresolved",
                    parsed_post.get("time") or "<missing>", log_preview(parsed_post.get("text"))
                )
            if msg.get("emoji_image_urls") and not parsed_post.get("emoji_texts"):
                debug_logger.warning(
                    "MAX_EMOJI_UNICODE_UNRESOLVED chat_id=%s source_id=%s image_candidates=%s text=%s",
                    chat_id, parsed_post.get("source_id") or parsed_post.get("idx"),
                    len(msg.get("emoji_image_urls") or []), log_preview(parsed_post.get("text")) or "<empty>"
                )
            if (parsed_post.get("voice_hint") or parsed_post.get("video_note_hint")) and not any(
                t in {"voice", "audio", "video", "video_note"} for t in parsed_types
            ):
                debug_logger.error(
                    "MAX_MEDIA_NOT_EXTRACTED chat_id=%s source_id=%s voice_hint=%s video_note_hint=%s",
                    chat_id, parsed_post.get("source_id") or parsed_post.get("idx"),
                    bool(parsed_post.get("voice_hint")), bool(parsed_post.get("video_note_hint"))
                )
            if parsed_post.get("poll") and not (parsed_post["poll"].get("options") or []):
                debug_logger.warning(
                    "MAX_POLL_WITHOUT_OPTIONS chat_id=%s source_id=%s question=%s raw=%s",
                    chat_id, parsed_post.get("source_id") or parsed_post.get("idx"),
                    log_preview(parsed_post["poll"].get("question")),
                    log_preview(parsed_post["poll"].get("raw_text"))
                )
            if (parsed_post.get("contact") or {}).get("is_contact") and not (parsed_post["contact"].get("phone") or "").strip():
                debug_logger.warning(
                    "MAX_CONTACT_PHONE_MISSING chat_id=%s source_id=%s name=%s",
                    chat_id, parsed_post.get("source_id") or parsed_post.get("idx"),
                    log_preview(parsed_post["contact"].get("first_name"))
                )
            if duplicate_media_count or visual_duplicate_count:
                debug_logger.warning(
                    "MAX_DUPLICATE_MEDIA_REMOVED chat_id=%s source_id=%s exact=%s visual_fallback=%s",
                    chat_id, parsed_post.get("source_id") or parsed_post.get("idx"),
                    duplicate_media_count, visual_duplicate_count
                )

        if len(human_posts) > MAX_MESSAGES_TO_PARSE:
            debug_logger.error(
                "MAX_OUTPUT_LIMIT_ENFORCED chat_id=%s parsed_posts=%s max_messages=%s",
                chat_id, len(human_posts), MAX_MESSAGES_TO_PARSE,
            )
            human_posts = human_posts[-MAX_MESSAGES_TO_PARSE:] if MAX_MESSAGES_TO_PARSE else []

        recent_post_hashes_by_chat[chat_id] = {
            _get_post_content_signature(post) for post in human_posts
        }

        media_counts = {}
        for post in human_posts:
            for media in post.get("media_files", []) or []:
                media_type = str(media.get("type") or "unknown")
                media_counts[media_type] = media_counts.get(media_type, 0) + 1
        debug_logger.info(
            "MAX_PARSE_COMPLETE chat_id=%s posts=%s media=%s missing_dates=%s voice_hints=%s circle_hints=%s polls=%s closed_polls=%s contacts=%s",
            chat_id, len(human_posts), media_counts,
            sum(not bool(post.get("date")) for post in human_posts),
            sum(bool(post.get("voice_hint")) for post in human_posts),
            sum(bool(post.get("video_note_hint")) for post in human_posts),
            sum(bool(post.get("poll")) for post in human_posts),
            sum(bool((post.get("poll") or {}).get("is_closed")) for post in human_posts),
            sum(bool((post.get("contact") or {}).get("is_contact")) for post in human_posts),
        )

        return human_posts

    except Exception as exc:
        debug_logger.exception("MAX_PARSE_FAILED chat_id=%s error=%s", chat_id, log_preview(exc, 300))
        print(f"[{chat_id}] ❌ Внутренняя ошибка парсера MAX; traceback:")
        traceback.print_exc()
        raise
    finally:
        try:
            if page and getattr(page, "_max_media_response_listener", None):
                page.remove_listener("response", page._max_media_response_listener)
        except Exception:
            pass
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        try:
            if pw:
                pw.stop()
        except Exception:
            pass
