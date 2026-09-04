import re
import time
import requests
import os
import json
import hashlib
import shutil
import io
import subprocess
import base64
import sys
import subprocess
import base64
from typing import List, Dict
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv


load_dotenv()
MAX_GROUP_URL = os.getenv("MAX_GROUP_URL")
MAX_PHONE = os.getenv("MAX_PHONE")

SESSION_DIR = "chrome_max_session_permanent"
SEEN_MESSAGES_FILE = "seen_messages.json"
PHOTO_CACHE_FILE = "seen_images.json"

os.makedirs(SESSION_DIR, exist_ok=True)
os.makedirs("downloads", exist_ok=True)

message_cache = set()
photo_cache = set()

_playwright_instance = None
_browser_context = None
_page = None


def load_message_cache():
    global message_cache
    message_cache.clear()
    if os.path.exists(SEEN_MESSAGES_FILE):
        try:
            with open(SEEN_MESSAGES_FILE, "r", encoding="utf-8") as f:
                message_cache = set(json.load(f).get('message_hashes', []))
        except Exception:
            message_cache = set()
    else:
        pass


def save_message_cache():
    try:
        with open(SEEN_MESSAGES_FILE, "w", encoding="utf-8") as f:
            json.dump({'message_hashes': list(message_cache)}, f)
    except Exception:
        pass


def load_photo_cache():
    global photo_cache
    photo_cache.clear()
    if os.path.exists(PHOTO_CACHE_FILE):
        try:
            with open(PHOTO_CACHE_FILE, "r", encoding="utf-8") as f:
                photo_cache = set(json.load(f).get('photo_hashes', []))
        except Exception:
            photo_cache = set()
    else:
        pass


def save_photo_cache():
    try:
        with open(PHOTO_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({'photo_hashes': list(photo_cache)}, f)
    except Exception:
        pass

def normalize_for_hash(text: str) -> str:
    """Очищает текст от эмодзи 👤 и лишних пробелов для корректного сравнения"""
    if not text:
        return ""
    text = text.replace('👤', '').strip()
    return " ".join(text.split())


def get_message_hash(post: dict) -> str:
    clean_name = normalize_for_hash(post.get('name', ''))
    clean_text = normalize_for_hash(post.get('text', ''))
    
    content = f"{clean_name}|{clean_text[:150]}"
    return hashlib.md5(content.encode('utf-8')).hexdigest()


def get_media_hash(url: str) -> str:
    return hashlib.md5(url.encode('utf-8')).hexdigest()


def is_new_message(post: dict) -> bool:
    msg_hash = get_message_hash(post)
    if msg_hash in message_cache:
        return False
    message_cache.add(msg_hash)
    save_message_cache()
    return True


def is_new_media(url: str) -> bool:
    media_hash = get_media_hash(url)
    if media_hash in photo_cache:
        return False
    photo_cache.add(media_hash)
    save_photo_cache()
    return True


def clear_all_caches():
    global message_cache, photo_cache
    message_cache.clear()
    photo_cache.clear()

    for f in [SEEN_MESSAGES_FILE, PHOTO_CACHE_FILE]:
        if os.path.exists(f):
            os.remove(f)

    if os.path.exists("downloads"):
        shutil.rmtree("downloads")
    os.makedirs("downloads", exist_ok=True)


def get_media_bytes(url: str, media_type: str = 'image') -> dict:
    if not is_new_media(url):
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
        'теперь в max', 'напишите что-нибудь', 'сферум',
        'удалил', 'удалила', 'изменил', 'изменила',
        'вошел', 'вошла', 'покинул', 'покинула',
        'добавил', 'добавила', 'исключил', 'исключила',
        'пригласил', 'пригласила', 'системное',
        'создал чат', 'создала чат', 'вернулся', 'вернулась'
    ]
    if any(phrase in text for phrase in bot_phrases):
        return False
    return 10 < len(text) < 2000


def get_or_init_browser():
    global _playwright_instance, _browser_context, _page
    if _browser_context is None:
        _playwright_instance = sync_playwright().start()
        _browser_context = _playwright_instance.chromium.launch_persistent_context(
        user_data_dir=SESSION_DIR,
        headless=False,
        viewport={'width': 1280, 'height': 800},
        args=[
        '--no-sandbox',
        '--disable-blink-features=AutomationControlled',
        '--disable-application-cache',
        '--disable-offline-load-stale-cache',
        '--disk-cache-size=10485760',
        '--media-cache-size=10485760',
        '--disable-gpu-compositing',
        '--disable-extensions',
        '--disable-background-networking',
        '--disable-default-apps',
        '--disable-sync',
        '--disable-translate',
        '--no-first-run',
        ],
        slow_mo=30
        )
        _page = _browser_context.pages[0] if _browser_context.pages else _browser_context.new_page()
    return _browser_context, _page

def parse_max_group_media() -> List[Dict]:
    load_message_cache()
    load_photo_cache()

    browser, page = get_or_init_browser()

    try:
        page.goto("https://web.max.ru", timeout=15000)
        page.wait_for_timeout(3000)

        if not page.query_selector("div[class*='chat'], div[class*='group'], div[class*='message']"):
            print("📱 Требуется логин...")
            page.goto("https://web.max.ru")
            print("⏳ 120 секунд на логин...")
            page.wait_for_timeout(120000)

        page.goto(MAX_GROUP_URL, timeout=60000)
        page.wait_for_timeout(5000)

        for _ in range(30):
            page.keyboard.press("End")
            page.wait_for_timeout(200)
        page.wait_for_timeout(3000)

        raw_messages = page.evaluate("""
            () => {
                const results = [];

                const selectors = [
                    '[class*="message"]', '[class*="bubble"]',
                    '[class*="chat-msg"]', '[data-testid*="message"]',
                    '[class*="post"]'
                ];

                let containers = new Set();
                for (const sel of selectors) {
                    document.querySelectorAll(sel).forEach(el => containers.add(el));
                }

                // Сортируем по позиции в DOM (сверху вниз = старые → новые)
                let sorted = Array.from(containers).sort((a, b) => {
                    const pos = a.compareDocumentPosition(b);
                    if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
                    if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
                    return 0;
                });

                sorted.forEach((container, idx) => {
                const fullText = container.innerText.trim();
                if (fullText.length < 10 || fullText.length > 2000) return;

                // 1) ИМЯ АВТОРА (отдельно от текста)
                const nameEl = container.querySelector(
                    '[class*="name"], [class*="author"], [class*="sender"], [class*="user-name"]'
                );

                // Если отдельного элемента с именем нет — это "слипшийся" дубль
                // (имя+роль+текст одним куском). Пропускаем: корректная версия
                // этого же сообщения придёт из другого контейнера, где nameEl найден.
                if (!nameEl) return;

                let name = nameEl.innerText.trim().substring(0, 40);
                name = name.replace(/👤/g, '').trim();
                name = name.replace(/\s+/g, ' ');

                // 1b) РОЛЬ/СТАТУС автора (например "Учитель 🎓"), если есть рядом с именем
                let role = '';
                const authorBlock = nameEl.closest('[class*="author"], [class*="sender"], [class*="user"]') || nameEl.parentElement;
                let authorBlockText = authorBlock ? authorBlock.innerText.trim() : name;
                authorBlockText = authorBlockText.replace(/👤/g, '').trim().replace(/\s+/g, ' ');
                if (authorBlockText.startsWith(name)) {
                    role = authorBlockText.substring(name.length).trim();
                }
                // Защита: если "роль" оказалась длинной — скорее всего, туда затесался текст сообщения
                if (role.length > 40) role = '';

                // 2) ЧИСТЫЙ ТЕКСТ (без имени, роли и времени)
                let cleanText = fullText;
                cleanText = cleanText.replace(/👤/g, '').trim();

                // Удаляем связку "имя + роль" из начала текста
                const prefixToStrip = role ? `${name} ${role}` : name;
                if (cleanText.startsWith(prefixToStrip)) {
                    cleanText = cleanText.substring(prefixToStrip.length).trim();
                } else if (cleanText.startsWith(name)) {
                    cleanText = cleanText.substring(name.length).trim();
                    if (role && cleanText.startsWith(role)) {
                        cleanText = cleanText.substring(role.length).trim();
                    }
                }
                cleanText = cleanText.replace(/\s+/g, ' ').trim();

                // Обработка пересланных: "Переслано: Имя Фамилия   текст"
                const fwdMatch = cleanText.match(/^Переслано:\s*(.+?)\s{2,}/);
                if (fwdMatch) {
                    if (!name) name = fwdMatch[1].trim();
                    cleanText = cleanText.substring(fwdMatch[0].length).trim();
                }

                // Извлекаем время из конца текста
                const timeMatch = cleanText.match(/(\d{1,2}:\d{2})\s*$/);
                let msgTime = timeMatch ? timeMatch[1] : '';
                if (msgTime) {
                    cleanText = cleanText.substring(0, cleanText.length - msgTime.length).trim();
                }

                // 3) ФОТО (без аватарок)
                const images = [];
                container.querySelectorAll('img').forEach(img => {
                    const src = img.src || img.dataset.src || '';
                    if (src.length < 50) return;
                    if (src.match(/avatar|userpic|profile|icon|emoji|sticker|thumb|logo/i)) return;

                    const avatarParent = img.closest(
                        '[class*="avatar"], [class*="userpic"], [class*="profile"], [class*="sender-photo"]'
                    );
                    if (avatarParent) return;

                    const rect = img.getBoundingClientRect();
                    if (rect.width < 100 || rect.height < 100) return;

                    images.push(src);
                });

                // 4) ДОКУМЕНТЫ / ФАЙЛЫ
                const documents = [];
                container.querySelectorAll('a').forEach(a => {
                    const href = a.href || '';
                    const downloadAttr = a.getAttribute('download') || '';

                    if (href && href.startsWith('http') && !href.match(/\\.(jpg|jpeg|png|gif|webp|svg|bmp)$/i)) {
                        const hasExtension = href.match(/\\.[a-zA-Z0-9]+$/);
                        const isFileElement = downloadAttr || hasExtension || a.className.match(/file|document|attachment/i);

                        if (isFileElement) {
                            let filename = downloadAttr || href.split('/').pop().split('?')[0] || 'document';
                            if (!documents.some(d => d.url === href)) {
                                documents.push({ url: href, filename: filename });
                            }
                        }
                    }
                });

                // Итоговое имя = имя + роль (если есть)
                let fullName = role ? `${name} ${role}` : name;
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
            }
        """)


        seen_texts = set()
        unique = []
        for msg in raw_messages:
            text_key = msg['text'][:100]
            if text_key in seen_texts:
                continue
            if not is_human_message(msg['text']):
                continue
            seen_texts.add(text_key)
            unique.append(msg)

        last_10 = unique[-10:]

        human_posts = []
        for msg in last_10:
            media_files = []
            
            for img_url in msg['images']:
                res = get_media_bytes(img_url, media_type='image')
                if res:
                    media_files.append({
                        'url': img_url,
                        'bytes': res['bytes'],
                        'type': 'image'
                    })

            for doc in msg.get('documents', []):
                res = get_media_bytes(doc['url'], media_type='document')
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

            if media_files:
                types_str = ", ".join([m['type'] for m in media_files])
            else:
                pass

        return human_posts

    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()
        return []