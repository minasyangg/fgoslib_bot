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
import requests
from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
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
def save_session(user_id, task_text, images=None, user_prompt=None, output_format="md"):
    key = f"session:{user_id}"
    data = {
        "task_text": task_text,
        "images": images or [],
        "user_prompt": user_prompt or "",
        "output_format": output_format
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
            except Exception:
                task_text = raw
                images = []
            save_session(user_id, task_text, images)
            response = f"Задача {task_id} загружена в сессию. Отправь /prompt для доп. промта или /format для выбора формата."
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

    save_session(user_id, task_text, images)
    response = "Задание сохранено! Добавь /prompt если хочешь дать дополнительный промт."
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

async def handle_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    username = update.message.from_user.username or str(user_id)
    fmt = " ".join(context.args).lower()
    if fmt not in ["md", "pdf"]:
        response = "Используй /format md или /format pdf"
        await update.message.reply_text(response)
        log_event(username, f"/format {fmt}", response)
        return
    update_format(user_id, fmt)
    response = f"Формат ответа установлен: {fmt}"
    await update.message.reply_text(response)
    log_event(username, f"/format {fmt}", response)

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
    bot_app.add_handler(CommandHandler("format", handle_format))
    bot_app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_task))
    logger.info("Бот запущен...")
    bot_app.run_polling()

if __name__ == "__main__":
    flask_thread = threading.Thread(target=start_flask)
    flask_thread.start()
    run_bot()
