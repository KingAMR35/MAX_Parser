"""Short-lived, one-time remote control page for MAX Web login.

The server intentionally uses only the Python standard library. It binds to
loopback and is expected to sit behind an HTTPS reverse proxy.
"""

import hashlib
import ipaddress
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


SESSION_TTL_SECONDS = 10 * 60
COOKIE_NAME = "max_remote_auth"

PAGE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=4,user-scalable=yes">
<meta name="referrer" content="no-referrer"><title>Вход в MAX</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#101522;color:#f4f6fb;font:16px/1.45 system-ui,-apple-system,sans-serif}
main{max-width:1080px;margin:auto;padding:clamp(9px,2vw,18px)}
h1{font-size:22px;margin:0 0 6px}
.notice{color:#d9e0ef;font-size:14px;margin:0 0 10px}
.inputCard{position:sticky;top:0;z-index:5;background:#172235;border:1px solid #4b83d4;border-radius:13px;padding:12px;margin:10px 0;box-shadow:0 6px 24px #05070b99}
.inputCard label{display:block;font-weight:750;font-size:15px;margin-bottom:7px;color:#fff}
.inputHint{font-size:12px;color:#b9c9e4;margin-top:7px}
.inputRow{display:flex;gap:8px;align-items:stretch}
.editActions{display:flex;gap:8px;margin-top:8px}
.editActions button{flex:1;background:#40526d}
#keyboard{display:block;flex:1;min-width:0;width:100%;height:58px;border:2px solid #7895bd;border-radius:10px;background:#0e1726;color:#fff;padding:12px;font:18px system-ui,sans-serif;resize:vertical}
#keyboard::placeholder{color:#aab8ce}
button{position:relative;border:0;border-radius:10px;background:#3275d9;color:white;padding:11px 14px;font-weight:700;font-size:15px;touch-action:manipulation;cursor:pointer;transition:transform .1s ease,filter .1s ease,box-shadow .1s ease}
button:active{transform:scale(.98);filter:brightness(1.18)}
button:disabled{opacity:.78;cursor:progress}
button.busy{padding-right:38px;box-shadow:0 0 0 3px #ffffff55;filter:brightness(.92)}
button.busy::after{content:"";position:absolute;right:12px;top:50%;width:15px;height:15px;margin-top:-9px;border:2px solid #ffffff66;border-top-color:#fff;border-radius:50%;animation:buttonSpin .7s linear infinite}
@keyframes buttonSpin{to{transform:rotate(360deg)}}
#sendButton{min-width:142px;min-height:58px;background:#16845a}
.guide{background:#172235;border:1px solid #394a65;border-radius:12px;padding:10px 12px;margin:10px 0;color:#dce6f7}
.guide summary{font-weight:700;cursor:pointer;border-radius:7px;transition:background .1s ease,transform .1s ease}
.guide summary:active{background:#ffffff18;transform:scale(.99)}
.guide ol{margin:8px 0 0;padding-left:22px}
.guide li{margin:6px 0}
.toolbar{display:flex;gap:8px;margin:10px 0;flex-wrap:wrap}
.toolbar button{background:#34435b}
#screenTip{color:#e2eafe;font-weight:650;margin:8px 0}
#screenFrame{position:relative;width:100%;max-height:64vh;overflow:auto;background:#05070b;border:1px solid #3c4960;border-radius:12px;transition:border-color .1s ease,box-shadow .1s ease}
#screenFrame.busy{border-color:#69a5ff;box-shadow:0 0 0 3px #69a5ff66}
#screen{display:block;width:100%;height:auto;max-height:64vh;object-fit:contain;user-select:none;-webkit-user-select:none;touch-action:pan-x pan-y;cursor:crosshair;transition:filter .1s ease}
#screen:active,#screenFrame.busy #screen{filter:brightness(.82)}
#screenFrame.zoomed #screen{width:160%;max-height:none}
#state{min-height:24px;margin-top:8px;color:#cbd6eb}
.warning{background:#282231;border:1px solid #644653;padding:12px;border-radius:10px;font-size:13px;margin-top:12px}
@media(max-width:600px){
main{padding:9px}h1{font-size:19px}.notice{font-size:13px;line-height:1.35}
.inputCard{padding:10px;margin:8px 0}.inputCard label{font-size:14px}
.inputRow{gap:6px}#keyboard{height:56px;padding:10px;font-size:17px}
#sendButton{min-width:108px;padding:9px;font-size:14px}
.editActions{gap:6px}.editActions button{padding:10px 8px;font-size:13px}
.toolbar{gap:6px}.toolbar button{padding:9px 10px;font-size:13px}
#screenFrame{max-height:50dvh}#screen{max-height:50dvh}
.warning{padding:10px;font-size:12px}
}
</style></head>
<body><main>
<h1>🔐 Вход в MAX по номеру телефона</h1>
<p class="notice">Нажмите на экране MAX «Войти по номеру телефона». Номер, полный код и пароль вводите в поле выше — поле MAX выбирается само.</p>
<section class="inputCard">
<label for="keyboard">⌨️ Сюда вводить: номер телефона, код подтверждения или пароль MAX</label>
<div class="inputRow">
<textarea id="keyboard" rows="1" maxlength="256" placeholder="Номер / код подтверждения / пароль" autocomplete="off" autocapitalize="off" spellcheck="false" aria-label="Номер телефона, код подтверждения или пароль MAX"></textarea>
<button id="sendButton" type="button">📨 Ввести в MAX</button>
</div>
<div class="editActions">
<button id="backspaceButton" type="button" title="Удалить последний символ в поле MAX">⌫ Стереть символ</button>
<button id="clearButton" type="button" title="Очистить активное поле MAX">🧹 Очистить поле MAX</button>
</div>
<div class="inputHint"><b>Одно поле на весь вход:</b> сначала номер из 10 цифр без +7 и 8, затем весь код сразу, потом пароль, если MAX его попросит. Для каждого шага нажмите «Ввести в MAX».</div>
</section>
<div class="toolbar">
<button id="refreshButton" type="button">🔄 Обновить экран</button>
<button id="reloadButton" type="button">↻ Перезагрузить MAX</button>
<button id="zoomButton" type="button">🔍 Увеличить экран</button>
</div>
<p id="screenTip">👆 Нажимайте на кнопки и ссылки MAX прямо на экране. Поле телефона, кода или пароля выбирается автоматически.</p>
<div id="screenFrame"><img id="screen" alt="Интерактивный экран входа MAX"></div>
<div id="state">Подключаем защищённую сессию…</div>
<details class="guide">
<summary>📘 Подробная инструкция по входу</summary>
<ol>
<li>На экране MAX нажмите «Войти по номеру телефона».</li>
<li>В поле сверху введите номер из 10 цифр без +7 и 8, нажмите «Ввести в MAX», затем нажмите «Продолжить» на экране MAX.</li>
<li>Если появится проверка «Я не робот», пройдите её на экране MAX. Когда придёт код, введите все цифры сразу в поле сверху и нажмите «Ввести в MAX» один раз.</li>
<li>Если MAX попросит пароль, введите его в то же поле. Если нужно сначала создать пароль на уже открытом устройстве, задайте его в MAX: «Настройки» → «Безопасность» → «Пароль для входа». Вернитесь сюда и нажмите на экране MAX «Попробовать войти снова».</li>
</ol>
</details>
<div class="warning">Одноразовая ссылка действует 10 минут. Не пересылайте её. Страница и удалённый браузер обрабатывают введённые данные; сервис не записывает текст в базу и не пересылает его в Telegram. MAX-сессия сохраняется только после подтверждённого входа.</div>
</main><script>
const screen=document.getElementById("screen"),screenFrame=document.getElementById("screenFrame"),box=document.getElementById("keyboard"),state=document.getElementById("state"),sendButton=document.getElementById("sendButton"),zoomButton=document.getElementById("zoomButton");
let stopped=false,ready=false,previousImageUrl=null;
let actionQueue=Promise.resolve(),pendingActions=0,sendingDraft=false,snapshotInFlight=false,snapshotQueued=false;
function httpError(prefix,status){const reasons={400:"MAX пока не показал поле ввода; нажмите нужную кнопку на экране MAX",401:"сессия авторизации не найдена",403:"сервер отклонил запрос страницы",404:"маршрут API не найден",410:"удалённая сессия завершена",500:"ошибка удалённого браузера",504:"удалённый браузер не ответил вовремя"};return prefix+": "+(reasons[status]||"ошибка сервера")+" (HTTP "+status+")";}
async function post(path,data={}){const r=await fetch(path,{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});if(!r.ok){const details=await r.json().catch(()=>({}));throw new Error(details.error||httpError("Не удалось управлять MAX",r.status));}return r.json().catch(()=>({}));}
async function runButton(button,operation,busyText){if(!button||button.dataset.busy==="1")return false;const wasDisabled=button.disabled;button.dataset.busy="1";button.classList.add("busy");button.setAttribute("aria-busy","true");button.disabled=true;if(busyText)state.textContent=busyText;try{return await operation();}catch(e){state.textContent=e.message||"Не удалось выполнить действие.";return false;}finally{button.classList.remove("busy");button.removeAttribute("aria-busy");delete button.dataset.busy;button.disabled=wasDisabled;}}
function action(name,values={}){if(!ready)return Promise.resolve(false);const run=async()=>{let succeeded=false;try{await post("/api/action",{action:name,...values});succeeded=true;return true;}catch(e){if(e.message.includes("HTTP 401")){const s=await fetch("/api/status",{cache:"no-store",credentials:"same-origin"}).then(x=>x.json()).catch(()=>({}));if(s.status==="complete"){state.textContent="✅ Вход подтверждён. Можно закрыть эту страницу.";stopped=true;return false;}}state.textContent=e.message;return false;}finally{pendingActions--;if(succeeded)setTimeout(()=>refreshScreen(true),25);}};pendingActions++;const queued=actionQueue.then(run,run);actionQueue=queued.catch(()=>false);return queued;}
async function sendDraft(andEnter=false){if(sendingDraft||!ready)return false;const text=box.value;if(!text&&!andEnter){state.textContent="Сначала введите текст в поле выше.";box.focus({preventScroll:true});return false;}sendingDraft=true;let ok=true;if(text){ok=await action("text",{text});if(ok&&box.value.startsWith(text))box.value=box.value.slice(text.length);}if(ok&&andEnter)ok=await action("key",{key:"Enter"});sendingDraft=false;if(ok){state.textContent=text?"✅ Весь введённый текст передан в MAX.":"↵ Enter отправлен в MAX.";box.focus({preventScroll:true});}return ok;}
async function refreshScreen(manual=false){if(stopped||!ready)return false;if(manual)await actionQueue;else if(pendingActions)return false;if(snapshotInFlight){snapshotQueued=true;return false;}snapshotInFlight=true;try{const r=await fetch("/api/snapshot",{cache:"no-store",credentials:"same-origin"});if(r.ok){const nextUrl=URL.createObjectURL(await r.blob());screen.onload=()=>{if(previousImageUrl)URL.revokeObjectURL(previousImageUrl);previousImageUrl=nextUrl;};screen.src=nextUrl;if(manual)state.textContent="Экран MAX обновлён.";return true;}const s=await fetch("/api/status",{cache:"no-store",credentials:"same-origin"}).then(x=>x.json()).catch(()=>({}));if(s.status==="complete"){state.textContent="✅ Вход подтверждён. Можно закрыть эту страницу.";stopped=true;return false;}state.textContent=httpError("Не удалось получить экран MAX",r.status);if(r.status===401||r.status===410)stopped=true;return false;}catch(e){state.textContent="Нет связи с сервером удалённого входа: "+e.message;return false;}finally{snapshotInFlight=false;if(snapshotQueued&&!stopped){snapshotQueued=false;setTimeout(()=>refreshScreen(true),35);}}}
async function reloadMax(){if(stopped||!ready)return;state.textContent="Перезагружаю страницу MAX…";const ok=await action("refresh");if(ok){const refreshed=await refreshScreen(true);if(refreshed)state.textContent="Страница MAX перезагружена. Продолжите вход на экране ниже.";}}
async function poll(){if(stopped||!ready)return;await refreshScreen();if(!stopped)setTimeout(poll,1000);}
sendButton.onclick=()=>{box.focus({preventScroll:true});runButton(sendButton,()=>sendDraft(false),"Передаю введённые данные в MAX…");};
document.getElementById("refreshButton").onclick=e=>runButton(e.currentTarget,()=>refreshScreen(true),"Обновляю изображение экрана MAX…");
document.getElementById("reloadButton").onclick=e=>runButton(e.currentTarget,reloadMax,"Перезагружаю страницу MAX…");
document.getElementById("backspaceButton").onclick=e=>runButton(e.currentTarget,async()=>{const ok=await action("backspace");if(ok)state.textContent="✅ Удалён последний символ в поле MAX.";return ok;},"Стираю последний символ в MAX…");
document.getElementById("clearButton").onclick=e=>runButton(e.currentTarget,async()=>{const ok=await action("clear");if(ok)state.textContent="✅ Поле MAX очищено.";return ok;},"Очищаю активное поле MAX…");
zoomButton.onclick=e=>runButton(e.currentTarget,async()=>{const zoomed=screenFrame.classList.toggle("zoomed");zoomButton.textContent=zoomed?"🔎 Уменьшить экран":"🔍 Увеличить экран";state.textContent=zoomed?"Экран увеличен.":"Обычный размер экрана.";await new Promise(resolve=>setTimeout(resolve,140));return true;},"Меняю размер экрана…");
screen.addEventListener("click",async e=>{if(screenFrame.classList.contains("busy"))return;const r=screen.getBoundingClientRect(),scale=Math.min(r.width/1280,r.height/800),ox=(r.width-1280*scale)/2,oy=(r.height-800*scale)/2,x=Math.round((e.clientX-r.left-ox)/scale),y=Math.round((e.clientY-r.top-oy)/scale);if(x<0||x>=1280||y<0||y>=800)return;screenFrame.classList.add("busy");state.textContent="⏳ Передаю нажатие в MAX…";try{const ok=await action("click",{x,y});if(ok)state.textContent="✅ Нажатие выполнено на экране MAX.";}finally{screenFrame.classList.remove("busy");}});
box.addEventListener("keydown",async e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();await runButton(sendButton,()=>sendDraft(true),"Передаю данные и нажимаю Enter в MAX…");return;}if(box.value)return;const allowed=["Tab","Backspace","Delete","Escape","ArrowUp","ArrowDown","ArrowLeft","ArrowRight","Home","End","PageUp","PageDown"];if(allowed.includes(e.key)){e.preventDefault();await action("key",{key:e.key});}});
screen.addEventListener("wheel",e=>{if(screenFrame.classList.contains("zoomed"))return;e.preventDefault();action("scroll",{deltaY:Math.max(-900,Math.min(900,Math.round(e.deltaY)))});},{passive:false});
async function start(){const token=location.hash.slice(1);if(!token){state.textContent="Одноразовая ссылка не найдена. Запросите новую у бота.";stopped=true;return;}try{await post("/api/claim",{token});history.replaceState(null,"","/auth");ready=true;state.textContent="Защищённая страница подключена. Можно нажимать на экран MAX или вводить текст через поле выше.";poll();}catch(e){state.textContent="Ссылка уже использована, истекла или недоступна. Запросите новую у бота.";stopped=true;}}
start();
</script></body></html>"""


class RemoteAuthService:
    def __init__(self):
        self.base_url = os.getenv("REMOTE_AUTH_BASE_URL", "").strip().rstrip("/")
        self.bind_host = os.getenv("REMOTE_AUTH_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
        try:
            self.port = int(os.getenv("REMOTE_AUTH_PORT", "8765"))
        except ValueError:
            self.port = 0
        parsed = urlparse(self.base_url)
        try:
            bind_address = ipaddress.ip_address(self.bind_host)
            loopback_bind = bind_address.version == 4 and bind_address.is_loopback
        except ValueError:
            loopback_bind = False
        loopback_bind = loopback_bind or self.bind_host.lower() == "localhost"
        try:
            valid_public_port = parsed.port is None or 1 <= parsed.port <= 65535
        except ValueError:
            valid_public_port = False
        public_port = parsed.port if valid_public_port else None
        host_label = parsed.hostname or ""
        if ":" in host_label:
            host_label = f"[{host_label}]"
        if public_port and public_port != 443:
            host_label = f"{host_label}:{public_port}"
        self.origin = f"https://{host_label}" if host_label else ""
        self.enabled = bool(
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.netloc
            and not parsed.username
            and not parsed.password
            and parsed.path in ("", "/")
            and not parsed.params
            and not parsed.query
            and not parsed.fragment
            and valid_public_port
            and 1 <= self.port <= 65535
            and loopback_bind
        )
        self._records = {}
        self._completed = {}
        self._lock = threading.RLock()
        self._server = None
        self._thread = None

    @staticmethod
    def _digest(token):
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    def start(self):
        if not self.enabled:
            return False
        if self._server:
            return True
        service = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format, *_args):
                return

            def _send(self, status, body=b"", content_type="application/json; charset=utf-8", extra=()):
                if isinstance(body, str):
                    body = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store, private")
                self.send_header("Pragma", "no-cache")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
                for name, value in extra:
                    self.send_header(name, value)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _json_body(self):
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 4096:
                        return None
                    return json.loads(self.rfile.read(size).decode("utf-8"))
                except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    return None

            def _cookie(self):
                for item in self.headers.get("Cookie", "").split(";"):
                    name, separator, value = item.strip().partition("=")
                    if separator and name == COOKIE_NAME:
                        return value
                return ""

            def _origin_ok(self):
                return self.headers.get("Origin", "") == service.origin

            def do_GET(self):
                route = urlparse(self.path).path
                if route == "/auth":
                    self._send(200, PAGE, "text/html; charset=utf-8")
                    return
                if route == "/api/snapshot":
                    record = service.authenticate(self._cookie())
                    if not record:
                        self._send(401)
                        return
                    try:
                        image = record["session"].remote_action("screenshot", timeout=8)
                        self._send(200, image, "image/jpeg")
                    except RuntimeError as error:
                        print(f"REMOTE_AUTH_SNAPSHOT_CLOSED error_type={type(error).__name__}")
                        self._send(410)
                    except TimeoutError as error:
                        print(f"REMOTE_AUTH_SNAPSHOT_TIMEOUT error_type={type(error).__name__}")
                        self._send(504)
                    except Exception as error:
                        print(f"REMOTE_AUTH_SNAPSHOT_FAILED error_type={type(error).__name__}")
                        self._send(500)
                    return
                if route == "/api/status":
                    status = service.status(self._cookie())
                    self._send(200, json.dumps({"status": status}).encode("utf-8"))
                    return
                self._send(404)

            def do_POST(self):
                route = urlparse(self.path).path
                body = self._json_body()
                if body is None or not isinstance(body, dict):
                    self._send(400)
                    return
                if not self._origin_ok():
                    self._send(403)
                    return
                if route == "/api/claim":
                    token = body.get("token")
                    record = service.claim(token)
                    if not record:
                        self._send(410)
                        return
                    cookie = f"{COOKIE_NAME}={token}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age={SESSION_TTL_SECONDS}"
                    self._send(200, b"{}", extra=(("Set-Cookie", cookie),))
                    return
                record = service.authenticate(self._cookie())
                if not record:
                    self._send(401)
                    return
                if route != "/api/action":
                    self._send(404)
                    return
                try:
                    action_name = body.get("action")
                    action_timeout = 20 if action_name == "text" else 8
                    result = record["session"].remote_action(action_name, body, timeout=action_timeout)
                    self._send(200, json.dumps({"ok": bool(result)}).encode("utf-8"))
                except ValueError as error:
                    print(f"REMOTE_AUTH_ACTION_REJECTED error_type={type(error).__name__}")
                    self._send(400, json.dumps({"error": str(error)}, ensure_ascii=False))
                except RuntimeError as error:
                    print(f"REMOTE_AUTH_ACTION_CLOSED error_type={type(error).__name__}")
                    self._send(410)
                except TimeoutError as error:
                    print(f"REMOTE_AUTH_ACTION_TIMEOUT error_type={type(error).__name__}")
                    self._send(504)
                except Exception as error:
                    print(f"REMOTE_AUTH_ACTION_FAILED error_type={type(error).__name__}")
                    self._send(500)

        self._server = ThreadingHTTPServer((self.bind_host, self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="remote-auth-http", daemon=True)
        self._thread.start()
        return True

    def create_link(self, session, target_id, user_id, output_chat_id):
        if not self.enabled or not self._server:
            raise RuntimeError("Для входа по ссылке нужен REMOTE_AUTH_BASE_URL с публичным HTTPS-доменом и запущенный HTTP-сервис.")
        token = secrets.token_urlsafe(32)
        digest = self._digest(token)
        record = {
            "session": session,
            "target_id": target_id,
            "user_id": int(user_id),
            "output_chat_id": int(output_chat_id),
            "expires_at": time.time() + SESSION_TTL_SECONDS,
            "claimed": False,
        }
        with self._lock:
            now = time.time()
            self._records = {
                key: item for key, item in self._records.items()
                if item["expires_at"] > now
            }
            self._completed = {
                key: expiry for key, expiry in self._completed.items()
                if expiry > now
            }
            self._records[digest] = record
        return f"{self.base_url}/auth#{token}", record

    def claim(self, token):
        if not isinstance(token, str) or len(token) > 128:
            return None
        digest = self._digest(token)
        with self._lock:
            record = self._records.get(digest)
            if not record or record["expires_at"] <= time.time() or record["claimed"]:
                return None
            record["claimed"] = True
            return record

    def authenticate(self, token):
        if not token:
            return None
        digest = self._digest(token)
        with self._lock:
            record = self._records.get(digest)
            if not record or not record["claimed"] or record["expires_at"] <= time.time():
                return None
            return record

    def status(self, token):
        if not token:
            return "expired"
        digest = self._digest(token)
        with self._lock:
            if self._completed.get(digest, 0) > time.time():
                return "complete"
            record = self._records.get(digest)
            if not record or record["expires_at"] <= time.time():
                return "expired"
            return "active"

    def revoke_session(self, session, completed=False):
        with self._lock:
            for digest, record in list(self._records.items()):
                if record["session"] is session:
                    self._records.pop(digest, None)
                    if completed:
                        self._completed[digest] = time.time() + 300


