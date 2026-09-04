# Официальный легкий образ от Microsoft с уже установленным Chromium и зависимостями
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Копируем файлы
COPY requirements.txt .
COPY . .

# Устанавливаем только Python-пакеты (системные уже внутри!)
RUN pip install --no-cache-dir -r requirements.txt

# Запускаем бота
CMD ["python", "bot.py"]