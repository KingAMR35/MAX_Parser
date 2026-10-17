# Удалённый вход в MAX по номеру

Страница управления открывает браузер MAX на сервере и передаёт туда нажатия и ввод пользователя. Номер, код и пароль не проходят через Telegram, но обрабатываются браузером и сервером, где работает бот. Ввод не записывается приложением в базу или логи. Используйте только собственный сервер и HTTPS-домен.

## Настройка FirstVDS

1. Создайте DNS-запись `A` для отдельного поддомена, например `auth.example.ru`, на публичный IP FirstVDS.
2. Установите и настройте Nginx с TLS-сертификатом для этого поддомена.
3. Добавьте в `.env` рядом с ботом:

   ```env
   REMOTE_AUTH_BASE_URL=https://auth.example.ru
   REMOTE_AUTH_BIND_HOST=127.0.0.1
   REMOTE_AUTH_PORT=8765
   ```

4. Проксируйте запросы к `/auth` и `/api/` на локальный сервис. Не открывайте порт `8765` наружу напрямую:

   ```nginx
   location = /auth {
       access_log off;
       proxy_pass http://127.0.0.1:8765;
       proxy_set_header Host $host;
       proxy_set_header X-Forwarded-Proto https;
       proxy_buffering off;
       proxy_read_timeout 30s;
   }

   location ^~ /api/ {
       access_log off;
       proxy_pass http://127.0.0.1:8765;
       proxy_set_header Host $host;
       proxy_set_header X-Forwarded-Proto https;
       proxy_buffering off;
       proxy_read_timeout 30s;
       client_max_body_size 8k;
   }
   ```

5. Убедитесь, что firewall разрешает входящие соединения на HTTPS-порт `443`, а локальный порт `8765` доступен только на `127.0.0.1`.
6. Перезапустите бота. В консоли должна появиться строка о запуске сервиса удалённого входа. В Telegram нажмите «Войти по номеру телефона» и проверьте одноразовую ссылку.

Ссылка действует 10 минут и принимается один раз. После успешного входа браузер закрывается, а состояние MAX сохраняется в `sessions/`. Для отзыва ранее созданной ссылки отмените вход в Telegram или дождитесь истечения срока.
