# FGOSLib Telegram Bot

## Описание

Этот проект — Telegram-бот для автоматизации работы с заданиями, интегрированный с Gradio-приложением на HuggingFace и системой мониторинга логов через Flask. Бот сохраняет сессии пользователей в Redis, поддерживает работу с изображениями, дополнительными промтами и выбором формата ответа (Markdown или PDF).

---

## Структура проекта

- `my_bot.py` — основной код Telegram-бота.
- `monitor_backend.py` — Flask-сервер для мониторинга логов.
- `monitor.html` — веб-интерфейс для просмотра логов.
- `.env` — переменные окружения (токены, URL).
- `requirements.txt` — зависимости Python.

---

## Быстрый старт

### 1. Клонирование и установка зависимостей

```sh
git clone <repo-url>
cd fgoslib_bot
python -m venv venv
source venv/bin/activate  # или venv\Scripts\activate на Windows
pip install -r requirements.txt
```

### 2. Настройка переменных окружения

Создайте файл `.env` (пример уже есть):

```
TELEGRAM_TOKEN=<ваш_токен_бота>
HF_TOKEN=<ваш_huggingface_token>
UPSTASH_REDIS_URL=<ваш_upstash_redis_url>
```

### 3. Запуск Telegram-бота

```sh
python my_bot.py
```

### 4. Запуск мониторинга логов

```sh
python monitor_backend.py
```
Мониторинг будет доступен по адресу: [http://localhost:8080](http://localhost:8080)

---

## Интеграция с Gradio-приложением на HuggingFace

Бот отправляет задания на Gradio API, размещённый на HuggingFace Spaces.  
API вызывается функцией [`call_hf_api`](my_bot.py):

- URL API задаётся переменной `HF_API_URL` (по умолчанию: `https://hf.space/embed/mingg93/fgoslib-qwen3/api/predict/`).
- Авторизация через токен HuggingFace (`HF_TOKEN`).
- Формат запроса:  
  ```json
  {
    "task_text": "текст задания",
    "user_prompt": "доп. промт",
    "images": ["file_id1", "file_id2"],
    "output_format": "md" // или "pdf"
  }
  ```
- Ответ API должен содержать ключ `text` (или `pdf` — ссылку на PDF).

---

## Интеграция с Hugo CMS

Для отображения логов или взаимодействия с ботом на сайте Hugo:

1. **Встраивание мониторинга:**
   - Разместите `monitor.html` на вашем Hugo-сайте (например, через iframe):
     ```html
     <iframe src="https://<render-monitor-service-url>" width="100%" height="600"></iframe>
     ```
   - Или скопируйте JS из `monitor.html` и подключите к вашему шаблону.

2. **Вызов бота с сайта:**
   - Добавьте ссылку на Telegram-бота:
     ```html
     <a href="https://t.me/<ваш_бот>">Написать боту</a>
     ```
   - Для интеграции с Gradio API используйте серверный proxy или прямой fetch (если CORS разрешён):
     ```js
     fetch('https://hf.space/embed/mingg93/fgoslib-qwen3/api/predict/', { ... })
     ```

---

## Переменные окружения

- `TELEGRAM_TOKEN` — токен Telegram-бота
- `HF_TOKEN` — HuggingFace API Token
- `UPSTASH_REDIS_URL` — Redis URL (Upstash)

---

## Деплой на Render

- **Web Service**: для мониторинга (`monitor_backend.py`)
- **Background Worker**: для бота (`my_bot.py`)
- Оба сервиса используют одну и ту же переменную `UPSTASH_REDIS_URL` для логов

---

## Лицензия

MIT
