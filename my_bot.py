import os
import json
import io
import requests
from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import redis
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
    await update.message.reply_text(
        "Привет! Отправь задание (текст + изображения), а затем /prompt <текст> для доп. промта.\n"
        "Чтобы выбрать формат ответа, используй /format md или /format pdf."
    )

async def handle_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    task_text = update.message.text or ""
    if not task_text and not update.message.photo:
        await update.message.reply_text("Пожалуйста, отправь текст задания или изображение.")
        return

    # Сохраняем текст
    images = []
    if update.message.photo:
        for photo in update.message.photo:
            images.append(photo.file_id)

    save_session(user_id, task_text, images)
    await update.message.reply_text("Задание сохранено! Добавь /prompt если хочешь дать дополнительный промт.")

async def handle_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    prompt = " ".join(context.args)
    if not prompt:
        await update.message.reply_text("Используй /prompt <твой текст>")
        return

    update_prompt(user_id, prompt)
    await update.message.reply_text("Промт сохранён! Получаем решение...")

    session = load_session(user_id)
    if not session:
        await update.message.reply_text("Сессия истекла или отсутствует.")
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
        elif "text" in result:
            await update.message.reply_text(result["text"])
        else:
            await update.message.reply_text("Не удалось получить решение.")
    except Exception as e:
        logger.exception("Ошибка при обращении к HF API")
        await update.message.reply_text(f"Ошибка при генерации решения: {e}")

async def handle_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    fmt = " ".join(context.args).lower()
    if fmt not in ["md", "pdf"]:
        await update.message.reply_text("Используй /format md или /format pdf")
        return
    update_format(user_id, fmt)
    await update.message.reply_text(f"Формат ответа установлен: {fmt}")

# ----------------------------
# Основная функция запуска
# ----------------------------
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("prompt", handle_prompt))
    app.add_handler(CommandHandler("format", handle_format))

    # Обработка текстовых сообщений и фото
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_task))

    logger.info("Бот запущен...")
    app.run_polling()
