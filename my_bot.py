# ----------------------------
# Логирование событий для мониторинга
# ----------------------------
from datetime import datetime
def log_event(username, command, response):
    log = {
        "username": username,
        "command": command,
        "response": response,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    r.rpush("bot_logs", json.dumps(log))
    r.ltrim("bot_logs", -100, -1)
import os
import json
import io
import time
import requests
import markdown as md
from jinja2 import Template
import pyppeteer
import urllib.parse
import tempfile
import base64

# Global browser instance + lock for reuse
BROWSER = None
BROWSER_LOCK = None
from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import asyncio
import redis
from flask import Flask
import threading
import logging

from dotenv import load_dotenv
load_dotenv()

# ----------------------------
# Настройки
# ----------------------------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
UPSTASH_REDIS_URL = os.environ["UPSTASH_REDIS_URL"]
HF_API_URL = "https://hf.space/embed/mingg93/fgoslib-qwen3/api/predict/"
HF_TOKEN = os.environ["HF_TOKEN"]
MONITOR_BASE_URL = os.environ.get("MONITOR_BASE_URL", "")
REDIS_TTL = 900  # 14 минут

# ----------------------------
# Логи
# ----------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----------------------------
# Подключение к Redis
# ----------------------------
r = redis.Redis.from_url(UPSTASH_REDIS_URL, decode_responses=True)

# ----------------------------
# Функции работы с сессией
# ----------------------------
def save_session(user_id, task_text, images=None, user_prompt=None, output_format="md", username=None, task_ids=None):
    key = f"session:{user_id}"
    data = {
        "user_id": user_id,
        "username": username or "",
        "task_text": task_text,
        "images": images or [],
        "user_prompt": user_prompt or "",
        "output_format": output_format,
        "task_ids": task_ids or []
    }
    r.set(key, json.dumps(data), ex=REDIS_TTL)
    logger.info(f"Saved session for user {user_id}: {data}")

def load_session(user_id):
    key = f"session:{user_id}"
    raw = r.get(key)
    if raw:
        return json.loads(raw)
    return None

def update_prompt(user_id, prompt):
    session = load_session(user_id)
    if session:
        session["user_prompt"] = prompt
        r.set(f"session:{user_id}", json.dumps(session), ex=REDIS_TTL)
        logger.info(f"Updated prompt for user {user_id}: {prompt}")

def update_format(user_id, output_format):
    session = load_session(user_id)
    if session:
        session["output_format"] = output_format
        r.set(f"session:{user_id}", json.dumps(session), ex=REDIS_TTL)
        logger.info(f"Updated output format for user {user_id}: {output_format}")

# ----------------------------
# Вызов HF API
# ----------------------------
def call_hf_api(task_text, images=None, user_prompt="", output_format="md"):
    payload = {
        "task_text": task_text,
        "user_prompt": user_prompt,
        "images": images or [],
        "output_format": output_format
    }
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    logger.info(f"Calling HF API with payload: {payload}")
    resp = requests.post(HF_API_URL, json=payload, headers=headers)
    resp.raise_for_status()
    return resp.json()


async def render_html_to_pdf_bytes(html: str, paper_format: str = "A4") -> bytes:
    """Render HTML to PDF bytes using headless Chromium (pyppeteer).
    This waits for network activity to finish so MathJax can render.
    """
    global BROWSER, BROWSER_LOCK
    if BROWSER_LOCK is None:
        # lazy-init lock in running loop
        BROWSER_LOCK = asyncio.Lock()

    # reuse browser where possible to avoid launch overhead
    async with BROWSER_LOCK:
        if BROWSER is None:
            logger.info("Launching shared Chromium instance for PDF rendering")
            BROWSER = await pyppeteer.launch(options={"args": ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]})

    browser = BROWSER
    try:
        page = await browser.newPage()
        try:
            # try setContent with a generous timeout
            await page.setContent(html, waitUntil="networkidle0", timeout=60000)
        except Exception as e:
            logger.warning(f"page.setContent failed: {e}; trying data: URL fallback")
            try:
                data_url = "data:text/html;charset=utf-8," + urllib.parse.quote(html)
                await page.goto(data_url, waitUntil="networkidle0", timeout=60000)
            except Exception as e2:
                logger.exception("Both setContent and data-URL goto failed")
                raise

        # If MathJax is present, request typesetting before printing
        try:
            await page.evaluate("() => { if (window.MathJax && MathJax.typesetPromise) { return MathJax.typesetPromise(); } }")
        except Exception:
            # ignore: evaluation may fail if MathJax not loaded yet
            logger.debug("MathJax typeset call failed or not present; continuing to PDF generation")

        pdf_bytes = await page.pdf({"format": paper_format, "printBackground": True})
        return pdf_bytes
    finally:
        try:
            # close only the page; keep the browser running for reuse
            await page.close()
        except Exception:
            pass

# ----------------------------
# Команды бота
# ----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    username = update.message.from_user.username or str(user_id)
    # Если приходит аргумент (taskId) — пробуем загрузить задачу из Redis
    if context.args:
        task_id = context.args[0]
        # Поддерживаем два варианта ключа: task:<id> и просто <id>
        raw = r.get(f"task:{task_id}") or r.get(task_id)
        if raw:
            try:
                task_obj = json.loads(raw)
                task_text = task_obj.get("task_text") or task_obj.get("text") or task_obj.get("content") or ""
                images = task_obj.get("images", [])
                prompt = task_obj.get("prompt", "")
                out_format = task_obj.get("format", "md")
            except Exception:
                task_text = raw
                images = []
                prompt = ""
                out_format = "md"
            # Сохраняем сессию (включая username) и добавляем task_id в session.task_ids (append без дубликатов)
            existing = load_session(user_id) or {}
            task_ids = existing.get('task_ids', []) if isinstance(existing, dict) else []
            if task_id not in task_ids:
                task_ids.append(task_id)
            save_session(user_id, task_text, images, user_prompt=prompt, output_format=out_format, username=username, task_ids=task_ids)
            # Записываем привязку task -> user
            try:
                r.set(f"task_assignee:{task_id}", user_id)
                # также обновим объект задачи, добавив assigned_user_id
                try:
                    task_obj['assigned_user_id'] = user_id
                    r.set(f"task:{task_id}", json.dumps(task_obj))
                except Exception:
                    pass
            except Exception:
                logger.exception('Не удалось записать привязку task->user в Redis')

            # Формируем Markdown файл с текстом и изображениями (встраиваем data URLs)
            md_lines = []
            md_lines.append(f"# Задача {task_id}\n")
            if prompt:
                md_lines.append(f"**Промт:** {prompt}\n")
            md_lines.append("## Текст задания:\n")
            md_lines.append(task_text + "\n")
            if images:
                md_lines.append('\n## Изображения:\n')
                for idx, img in enumerate(images):
                    # Если задан MONITOR_BASE_URL — используем короткую ссылку к /task_image/
                    if MONITOR_BASE_URL:
                        base = MONITOR_BASE_URL.rstrip('/')
                        img_url = f"{base}/task_image/{task_id}/{idx}"
                        md_lines.append(f"![]({img_url})\n")
                    else:
                        # если изображение — data URL, вставляем как картинка
                        if isinstance(img, str) and img.startswith('data:'):
                            md_lines.append(f"![]({img})\n")
                        elif isinstance(img, str) and img.startswith('http'):
                            md_lines.append(f"![]({img})\n")
                        else:
                            # неизвестный формат — вставим ссылку/текст
                            md_lines.append(f"- {img}\n")

            md_content = "\n".join(md_lines)
            # Convert markdown -> HTML and render to PDF (MathJax enabled) using headless Chromium
            title = f"Задача {task_obj.get('real_id') or task_id}"
            try:
                html_body = md.markdown(md_content, extensions=['extra', 'tables'])
            except Exception:
                # fallback: wrap raw md in pre
                html_body = f"<pre>{md_content}</pre>"

            html_template = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title}</title>
  <style>
    body{{font-family: DejaVu Sans, Arial, sans-serif; padding:20px;}}
    img{{max-width:100%;height:auto;}}
    pre{{white-space:pre-wrap;}}
  </style>
  <script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
</head>
<body>
{html_body}
</body>
</html>"""

            # Check Redis cache for pre-generated PDF
            cache_key = f"task_pdf:{task_id}"
            try:
                cached = r.get(cache_key)
            except Exception:
                cached = None

            try:
                if cached:
                    pdf_bytes = base64.b64decode(cached)
                    await update.message.reply_document(document=InputFile(io.BytesIO(pdf_bytes), filename=f"task_{task_obj.get('real_id') or task_id}.pdf"))
                    response = f"Задача {task_obj.get('real_id') or task_id} загружена (из кеша) и отправлена вам в виде PDF."
                else:
                    pdf_bytes = await render_html_to_pdf_bytes(html_template)
                    # store in redis base64-encoded to serve next time
                    try:
                        b64 = base64.b64encode(pdf_bytes).decode('ascii')
                        r.set(cache_key, b64, ex=REDIS_TTL * 6)
                    except Exception:
                        logger.exception('Не удалось сохранить PDF в кэш Redis')
                    await update.message.reply_document(document=InputFile(io.BytesIO(pdf_bytes), filename=f"task_{task_obj.get('real_id') or task_id}.pdf"))
                    response = f"Задача {task_obj.get('real_id') or task_id} загружена и отправлена вам в виде PDF."
            except Exception as e:
                logger.exception('Ошибка отправки PDF файла')
                # fallback: send .md file if PDF generation fails
                try:
                    await update.message.reply_document(document=InputFile(io.BytesIO(md_content.encode('utf-8')), filename=f"task_{task_id}.md"))
                    response = f"Задача {task_obj.get('real_id') or task_id} загружена, отправлена как .md (PDF failed): {e}"
                except Exception:
                    response = f"Задача {task_obj.get('real_id') or task_id} загружена, но не удалось отправить файл: {e}"
            except Exception as e:
                logger.exception('Ошибка отправки PDF файла')
                # fallback: send .md file if PDF generation fails
                try:
                    await update.message.reply_document(document=InputFile(io.BytesIO(md_content.encode('utf-8')), filename=f"task_{task_id}.md"))
                    response = f"Задача {task_obj.get('real_id') or task_id} загружена, отправлена как .md (PDF failed): {e}"
                except Exception:
                    response = f"Задача {task_obj.get('real_id') or task_id} загружена, но не удалось отправить файл: {e}"

            # Кнопки: Решить / Удалить
            try:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("Решить", callback_data=f"solve:{task_id}"), InlineKeyboardButton("Удалить", callback_data=f"del:{task_id}")]])
                await update.message.reply_text(response, reply_markup=kb)
            except Exception:
                await update.message.reply_text(response)

            log_event(username, f"/start {task_id}", response)
            return
        else:
            response = f"Задача {task_id} не найдена. Проверьте taskId на стороне сайта."
            await update.message.reply_text(response)
            log_event(username, f"/start {task_id}", response)
            return

    await update.message.reply_text(
        "Привет! Отправь задание (текст + изображения), а затем /prompt <текст> для доп. промта.\n"
        "Чтобы выбрать формат ответа, используй /format md или /format pdf."
    )

async def handle_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    username = update.message.from_user.username or str(user_id)
    task_text = update.message.text or ""
    if not task_text and not update.message.photo:
        await update.message.reply_text("Пожалуйста, отправь текст задания или изображение.")
        log_event(username, "(empty)", "Пожалуйста, отправь текст задания или изображение.")
        return

    # Сохраняем текст
    images = []
    if update.message.photo:
        for photo in update.message.photo:
            images.append(photo.file_id)

    # Создаём локальную задачу и сохраняем её под ключом task:<local_id>
    local_task_id = f"local-{user_id}-{int(time.time())}"
    task_obj = {
        'task_text': task_text,
        'images': images,
        'prompt': '',
        'format': 'md',
        'real_id': ''
    }
    try:
        r.set(f"task:{local_task_id}", json.dumps(task_obj))
    except Exception:
        logger.exception('Ошибка записи локальной задачи в Redis')

    # Сохраняем сессию и добавляем локальный task_id в session.task_ids (append без дубликатов)
    existing = load_session(user_id) or {}
    task_ids = existing.get('task_ids', []) if isinstance(existing, dict) else []
    if local_task_id not in task_ids:
        task_ids.append(local_task_id)
    save_session(user_id, task_text, images, username=username, task_ids=task_ids)
    response = f"Локальная задача сохранена под id {local_task_id}. Добавь /prompt если хочешь дать дополнительный промт."
    await update.message.reply_text(response)
    log_event(username, task_text or "[photo]", response)

async def handle_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    username = update.message.from_user.username or str(user_id)
    prompt = " ".join(context.args)
    if not prompt:
        response = "Используй /prompt <твой текст>"
        await update.message.reply_text(response)
        log_event(username, f"/prompt", response)
        return

    update_prompt(user_id, prompt)
    response = "Промт сохранён! Получаем решение..."
    await update.message.reply_text(response)
    log_event(username, f"/prompt {prompt}", response)

    session = load_session(user_id)
    if not session:
        response = "Сессия истекла или отсутствует."
        await update.message.reply_text(response)
        log_event(username, f"/prompt {prompt}", response)
        return

    try:
        result = call_hf_api(
            task_text=session["task_text"],
            images=session["images"],
            user_prompt=session.get("user_prompt", ""),
            output_format=session.get("output_format", "md")
        )
        # Обработка PDF
        if session.get("output_format") == "pdf" and "pdf" in result:
            pdf_url = result["pdf"]  # если HF возвращает ссылку
            pdf_bytes = requests.get(pdf_url).content
            await update.message.reply_document(document=InputFile(io.BytesIO(pdf_bytes), filename="solution.pdf"))
            log_event(username, f"/prompt {prompt}", "[PDF sent]")
        elif "text" in result:
            await update.message.reply_text(result["text"])
            log_event(username, f"/prompt {prompt}", result["text"])
        else:
            response = "Не удалось получить решение."
            await update.message.reply_text(response)
            log_event(username, f"/prompt {prompt}", response)
    except Exception as e:
        logger.exception("Ошибка при обращении к HF API")
        response = f"Ошибка при генерации решения: {e}"
        await update.message.reply_text(response)
        log_event(username, f"/prompt {prompt}", response)



async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user = query.from_user
    username = user.username or str(user.id)

    if data.startswith('solve:'):
        task_id = data.split(':',1)[1]
        raw = r.get(f"task:{task_id}") or r.get(task_id)
        if not raw:
            await query.edit_message_text('Задача не найдена.')
            return
        try:
            task_obj = json.loads(raw)
        except Exception:
            await query.edit_message_text('Неверные данные задачи.')
            return

        await query.edit_message_text('Запускаю генерацию (HF)...')
        # Вызов HF в отдельном потоке
        try:
            result = await asyncio.to_thread(call_hf_api, task_obj.get('task_text',''), task_obj.get('images',[]), task_obj.get('prompt',''), task_obj.get('format','md'))
        except Exception as e:
            logger.exception('Ошибка при обращении к HF API')
            await context.bot.send_message(chat_id=query.message.chat_id, text=f'Ошибка при генерации: {e}')
            log_event(username, f"solve {task_id}", f"error: {e}")
            return

        # Сохраним результат
        try:
            r.set(f"task_result:{task_id}", json.dumps(result))
        except Exception:
            pass

        # Отправляем результат пользователю
        try:
            if isinstance(result, dict) and 'text' in result:
                await context.bot.send_message(chat_id=query.message.chat_id, text=result['text'])
                log_event(username, f"solve {task_id}", result['text'])
            elif isinstance(result, dict) and 'pdf' in result:
                pdf_url = result['pdf']
                pdf_bytes = requests.get(pdf_url).content
                await context.bot.send_document(chat_id=query.message.chat_id, document=InputFile(io.BytesIO(pdf_bytes), filename='solution.pdf'))
                log_event(username, f"solve {task_id}", '[PDF sent]')
            else:
                await context.bot.send_message(chat_id=query.message.chat_id, text=str(result))
                log_event(username, f"solve {task_id}", str(result))
        except Exception as e:
            logger.exception('Ошибка отправки результата')
            await context.bot.send_message(chat_id=query.message.chat_id, text=f'Ошибка отправки результата: {e}')

    elif data.startswith('del:'):
        task_id = data.split(':',1)[1]
        # Удаляем ключи
        try:
            r.delete(f"task:{task_id}")
            r.delete(f"task_assignee:{task_id}")
            r.delete(f"task_result:{task_id}")
            # также можно удалить session, если нужно
            await query.edit_message_text('Задача удалена.')
            await context.bot.send_message(chat_id=query.message.chat_id, text=f'Задача {task_id} удалена.')
            log_event(username, f"delete {task_id}", 'deleted')
        except Exception as e:
            logger.exception('Ошибка удаления задачи')
            await query.edit_message_text(f'Ошибка при удалении: {e}')
            log_event(username, f"delete {task_id}", f'error: {e}')
    else:
        await query.edit_message_text('Неизвестное действие.')
# ----------------------------
# Основная функция запуска
# ----------------------------

# --- Flask HTTP server for Render health check ---
app = Flask(__name__)

@app.route("/")
def health():
    return "Bot is running!"

def start_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

def run_bot():
    bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("prompt", handle_prompt))
    bot_app.add_handler(CallbackQueryHandler(callback_query_handler))
    bot_app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_task))
    logger.info("Бот запущен...")
    bot_app.run_polling()

if __name__ == "__main__":
    flask_thread = threading.Thread(target=start_flask)
    flask_thread.start()
    run_bot()
