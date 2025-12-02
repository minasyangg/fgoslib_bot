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
import base64

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
MONITOR_BASE_URL = os.environ.get("MONITOR_BASE_URL", "")
REDIS_TTL = int(os.environ.get("REDIS_TTL", "600"))

# Rate limiting settings
RATE_LIMIT_TASKS_PER_HOUR = int(os.environ.get("RATE_LIMIT_TASKS_PER_HOUR", "10"))
RATE_LIMIT_TASKS_PER_DAY = int(os.environ.get("RATE_LIMIT_TASKS_PER_DAY", "50"))

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
# Rate limiting функция
# ----------------------------
def check_rate_limit(user_id: int, limit_key: str, max_count: int, window: int) -> bool:
    """
    Проверка rate limit для пользователя.
    Возвращает True если запрос разрешён, False если превышен лимит.
    """
    key = f"rate_limit:{limit_key}:{user_id}"
    try:
        count = r.incr(key)
        if count == 1:
            r.expire(key, window)
        return count <= max_count
    except Exception:
        logger.exception('Rate limit check failed')
        return True  # При ошибке разрешаем (fail-open)

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

# ----------------------------
# Команды бота
# ----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    username = update.message.from_user.username or str(user_id)
    
    # Проверка rate limit (только для задач, не для простого /start без аргументов)
    if context.args:
        # Проверка часового лимита
        if not check_rate_limit(user_id, "tasks_hour", RATE_LIMIT_TASKS_PER_HOUR, 3600):
            await update.message.reply_text(
                f"⚠️ Вы превысили лимит запросов ({RATE_LIMIT_TASKS_PER_HOUR} задач в час). Попробуйте позже."
            )
            log_event(username, "/start", "rate_limited_hour")
            return
        
        # Проверка дневного лимита
        if not check_rate_limit(user_id, "tasks_day", RATE_LIMIT_TASKS_PER_DAY, 86400):
            await update.message.reply_text(
                f"⚠️ Вы превысили дневной лимит запросов ({RATE_LIMIT_TASKS_PER_DAY} задач в день). Попробуйте завтра."
            )
            log_event(username, "/start", "rate_limited_day")
            return
    
    # Если приходит аргумент (taskId) — пробуем загрузить задачу из Redis
    if context.args:
        task_id = context.args[0]
        
        try:
            # СНАЧАЛА проверяем — есть ли уже готовый PDF для этого задания
            pdf_url_check = r.get(f"task_pdf_url:{task_id}")
            pdf_b64_check = r.get(f"task_pdf:{task_id}")
            
            if pdf_url_check or pdf_b64_check:
                # Задание уже сгенерировано — НЕ отправляем файл повторно, только уведомляем
                # (Telegram не поддерживает скроллинг к сообщению в личных чатах)
                await update.message.reply_text(
                    f"📄 Задание с ID {task_id} уже было сгенерировано и отправлено вам в чат ранее.\n"
                    f"Прокрутите чат вверх, чтобы найти его."
                )
                log_event(username, f"/start {task_id}", "already_generated")
                return
        except Exception:
            logger.exception('Ошибка проверки готовности PDF')
        
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
                # назначаем исполнителя с TTL
                r.set(f"task_assignee:{task_id}", user_id, ex=REDIS_TTL)
                # также обновим объект задачи, добавив assigned_user_id и обновим TTL
                try:
                    task_obj['assigned_user_id'] = user_id
                    r.set(f"task:{task_id}", json.dumps(task_obj), ex=REDIS_TTL)
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

            # Проверим готовность рендера: сначала URL в S3, затем base64 в Redis
            response = None
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Решить", callback_data=f"solve:{task_id}"), InlineKeyboardButton("Удалить", callback_data=f"del:{task_id}")]])
            try:
                # Проверяем наличие готового PDF
                pdf_url = r.get(f"task_pdf_url:{task_id}")
                pdf_b64 = None
                if pdf_url:
                    if isinstance(pdf_url, bytes):
                        pdf_url = pdf_url.decode('utf-8')
                    # скачиваем и отправляем пользователю как PDF
                    try:
                        resp = requests.get(pdf_url, timeout=30)
                        resp.raise_for_status()
                        content = resp.content
                        await update.message.reply_document(document=InputFile(io.BytesIO(content), filename=f"task_{task_obj.get('real_id') or task_id}.pdf"), reply_markup=kb)
                        response = f"Задача {task_obj.get('real_id') or task_id} готова и отправлена." 
                    except Exception:
                        logger.exception('Ошибка при скачивании/отправке файла по URL')

                else:
                    # check render-worker result keys (they may store a URL or base64 under task_pdf_result or task_result)
                    alt = r.get(f"task_pdf_result:{task_id}") or r.get(f"task_result:{task_id}")
                    if alt:
                        if isinstance(alt, bytes):
                            alt = alt.decode('utf-8')
                        # If alt looks like a URL, treat as such, otherwise treat as base64 blob
                        if isinstance(alt, str) and alt.startswith('http'):
                            pdf_url = alt
                        else:
                            pdf_b64 = alt
                    else:
                        # check base64 blobs — только PDF
                        pdf_b64 = r.get(f"task_pdf:{task_id}")
                    if pdf_b64:
                        if isinstance(pdf_b64, bytes):
                            pdf_b64 = pdf_b64.decode('ascii')
                        try:
                            data = base64.b64decode(pdf_b64)
                            # Всегда отправляем как PDF
                            await update.message.reply_document(document=InputFile(io.BytesIO(data), filename=f"task_{task_obj.get('real_id') or task_id}.pdf"), reply_markup=kb)
                            response = f"Задача {task_obj.get('real_id') or task_id} готова и отправлена." 
                        except Exception:
                            logger.exception('Ошибка декодирования base64')

            except Exception:
                logger.exception('Ошибка проверки готовности рендера')

            # Если файл не отправился — поставим задачу в очередь на рендер (если не в процессе)
            if not response:
                try:
                    pending = r.get(f"task_pending:{task_id}")
                    if not pending:
                        r.lpush('render_queue', task_id)
                        r.set(f"task_pending:{task_id}", '1', ex=REDIS_TTL)
                        response = f"Задача {task_obj.get('real_id') or task_id} принята. Начинаю рендер, файл придёт в Telegram как только будет готов."
                    else:
                        response = f"Рендер для задачи {task_obj.get('real_id') or task_id} уже в процессе — файл придёт, как только будет готов."
                except Exception:
                    logger.exception('Не удалось поставить задачу в очередь рендера')
                    response = "Извините. Сервис временно недоступен. Мы уже разбираемся с проблемой."
                
                # Отправляем текстовое сообщение только если файл НЕ был отправлен (задача в очереди)
                try:
                    await update.message.reply_text(response)
                except Exception:
                    logger.exception('Ошибка отправки response пользователю')
            # Если PDF уже был отправлен — не отправляем дополнительное текстовое сообщение

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
    
    # Проверка rate limit
    if not check_rate_limit(user_id, "tasks_hour", RATE_LIMIT_TASKS_PER_HOUR, 3600):
        await update.message.reply_text(
            f"⚠️ Вы превысили лимит запросов ({RATE_LIMIT_TASKS_PER_HOUR} задач в час). Попробуйте позже."
        )
        log_event(username, "handle_task", "rate_limited_hour")
        return
    
    if not check_rate_limit(user_id, "tasks_day", RATE_LIMIT_TASKS_PER_DAY, 86400):
        await update.message.reply_text(
            f"⚠️ Вы превысили дневной лимит запросов ({RATE_LIMIT_TASKS_PER_DAY} задач в день). Попробуйте завтра."
        )
        log_event(username, "handle_task", "rate_limited_day")
        return
    
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
        r.set(f"task:{local_task_id}", json.dumps(task_obj), ex=REDIS_TTL)
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


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user = query.from_user
    username = user.username or str(user.id)

    if data.startswith('solve:'):
        task_id = data.split(':',1)[1]
        chat_id = query.message.chat_id
        
        # Проверка: есть ли у пользователя активная задача в HF
        user_active_key = f"user_active_hf:{chat_id}"
        active_task_id = r.get(user_active_key)
        logger.info('Check active task for user %s: key=%s, value=%s', chat_id, user_active_key, active_task_id)
        if active_task_id:
            await context.bot.send_message(
                chat_id=chat_id,
                text='⏳ У вас уже есть задача в обработке. Дождитесь получения решения текущего задания, прежде чем отправлять новое.'
            )
            log_event(username, f"solve {task_id}", f"blocked_active_task:{active_task_id}")
            return
        
        # СРАЗУ устанавливаем флаг активной задачи, чтобы заблокировать параллельные запросы
        r.set(user_active_key, task_id, ex=REDIS_TTL)
        logger.info('Set active task flag for user %s: key=%s, task_id=%s', chat_id, user_active_key, task_id)
        
        raw = r.get(f"task:{task_id}") or r.get(task_id)
        if not raw:
            # Задача не найдена в Redis — снимаем флаг и уведомляем пользователя
            r.delete(user_active_key)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f'❌ Данные задачи {task_id} не найдены.\n\n'
                     f'Возможные причины:\n'
                     f'• Срок хранения данных истёк\n'
                     f'• Задача была удалена\n\n'
                     f'Пожалуйста, создайте задачу заново на сайте.'
            )
            log_event(username, f"solve {task_id}", "task_not_found")
            return
        try:
            task_obj = json.loads(raw)
        except Exception:
            # Неверные данные — снимаем флаг и уведомляем
            r.delete(user_active_key)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f'❌ Ошибка чтения данных задачи {task_id}.\n'
                     f'Пожалуйста, создайте задачу заново на сайте.'
            )
            log_event(username, f"solve {task_id}", "invalid_task_data")
            return

        # Попытка обновить текст сообщения: если это был текстовый message — редактируем текст,
        # если это был документ/фото без текста — попробуем отредактировать caption,
        # в противном случае отправим новое сообщение в чат как fallback.
        msg_text = 'Задача поставлена в очередь на обработку. Как только решение будет готово, я отправлю его в этот чат.'
        try:
            if query.message and getattr(query.message, 'text', None):
                await query.edit_message_text(msg_text)
            else:
                # try editing caption (works for media messages)
                try:
                    await query.edit_message_caption(msg_text)
                except Exception:
                    # fallback: send a separate message to the chat
                    await context.bot.send_message(chat_id=query.message.chat_id, text=msg_text)
        except Exception:
            logger.exception('Failed to edit/notify message after solve click')
            try:
                await context.bot.send_message(chat_id=query.message.chat_id, text=msg_text)
            except Exception:
                logger.exception('Failed to send fallback notification to user')
        # Собираем полезную нагрузку для HF воркера
        payload = {
            'task_id': task_id,
            'chat_id': query.message.chat_id,
            'task_text': task_obj.get('task_text',''),
            'images': task_obj.get('images', []),
            'user_prompt': task_obj.get('prompt','') or ''
        }
        try:
            # Пометим задачу как ожидающую HF, чтобы избежать дублей
            pending_key = f"task_pending_hf:{task_id}"
            pending = r.get(pending_key)
            if not pending:
                # push to hf_queue (worker uses BRPOP)
                r.lpush('hf_queue', json.dumps(payload))
                r.set(pending_key, '1', ex=REDIS_TTL)
                # Отмечаем пользователя как имеющего активную задачу
                r.set(user_active_key, task_id, ex=REDIS_TTL)
                log_event(username, f"enqueue_hf {task_id}", 'enqueued')
            else:
                log_event(username, f"enqueue_hf {task_id}", 'already_pending')
        except Exception as e:
            logger.exception('Не удалось поставить задачу в hf_queue')
            await context.bot.send_message(chat_id=query.message.chat_id, text=f'Ошибка при постановке в очередь: {e}')
            log_event(username, f"solve {task_id}", f"error: {e}")
            return

    elif data.startswith('del:'):
        task_id = data.split(':',1)[1]
        chat_id = query.message.chat_id
        # Удаляем ВСЕ связанные ключи из Redis
        try:
            keys_to_delete = [
                f"task:{task_id}",
                f"task_assignee:{task_id}",
                f"task_result:{task_id}",
                f"task_pdf:{task_id}",
                f"task_pdf_url:{task_id}",
                f"task_pdf_result:{task_id}",
                f"task_pending:{task_id}",
                f"task_pending_hf:{task_id}",
                f"hf_render:{task_id}",
                f"task:hf_render:{task_id}",
                f"task_assignee:hf_render:{task_id}",
            ]
            for key in keys_to_delete:
                r.delete(key)
            
            # Удаляем сообщение из чата (вместо редактирования)
            try:
                await query.message.delete()
            except Exception:
                # Если не удалось удалить — попробуем отредактировать
                await query.edit_message_text('Задача удалена.')
            
            # Отправляем подтверждение
            await context.bot.send_message(chat_id=chat_id, text=f'🗑 Задача {task_id} удалена.')
            log_event(username, f"delete {task_id}", 'deleted')
        except Exception as e:
            logger.exception('Ошибка удаления задачи')
            try:
                await query.edit_message_text(f'Ошибка при удалении: {e}')
            except Exception:
                pass
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
    bot_app.add_handler(CallbackQueryHandler(callback_query_handler))
    bot_app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_task))
    logger.info("Бот запущен...")
    bot_app.run_polling()

if __name__ == "__main__":
    flask_thread = threading.Thread(target=start_flask)
    flask_thread.start()
    run_bot()
