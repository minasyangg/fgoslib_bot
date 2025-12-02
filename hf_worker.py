#!/usr/bin/env python3
"""HF worker: consumes `hf_queue` and forwards tasks to a HuggingFace Space (Gradio API).
Behavior:
 - BRPOP from Redis `hf_queue` for JSON tasks
 - validate images (<= MAX_IMAGES) and user_prompt length
 - basic moderation of user_prompt using a small blacklist
 - POST payload to `HF_API_URL` with Bearer `HF_TOKEN`
 - handle response containing `pdf_url` or `pdf_base64` (or download & forward)
 - store result in Redis under `task_pdf_result:<task_id>` with TTL
 - send PDF to Telegram chat via Bot API when ready

Configuration via environment variables:
 - UPSTASH_REDIS_URL (required)
 - HF_TOKEN (required)
 - HF_API_URL (required)  e.g. https://hf.space/embed/<owner>/<repo>/api/predict or other endpoint
 - TELEGRAM_TOKEN (required for sending file)
 - MAX_IMAGES (default 5)
 - MAX_PROMPT_LEN (default 1000)
 - REDIS_TTL (default 900)
 - WORKER_CONCURRENCY (default 3)
 - HF_TIMEOUT (default 180)
"""

import os
import json
import time
import logging
import base64
import requests
import threading
import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor

import redis
try:
    # aiohttp used only for health endpoint
    from aiohttp import web
except Exception:
    web = None
try:
    from gradio_client import Client, handle_file
except Exception:
    Client = None
    handle_file = None

try:
    # AppError class exists in gradio_client package
    from gradio_client.client import AppError as GradioAppError
except Exception:
    GradioAppError = None


class HFQuotaError(Exception):
    """Raised when the HF Space reports a quota / resource limit error."""
    def __init__(self, msg: str, retry_after_seconds: int | None = None):
        super().__init__(msg)
        self.retry_after_seconds = retry_after_seconds

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('hf_worker')

# worker start time for uptime metric
START_TIME = time.time()

UPSTASH_REDIS_URL = os.environ.get('UPSTASH_REDIS_URL')
if not UPSTASH_REDIS_URL:
    raise RuntimeError('UPSTASH_REDIS_URL is required')

HF_TOKEN = os.environ.get('HF_TOKEN')
HF_API_URL = os.environ.get('HF_API_URL')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')

if not HF_TOKEN:
    logger.warning('HF_TOKEN not set; gradio_client calls will fail')
else:
    # Log masked token prefix to verify it's loaded correctly
    token_preview = HF_TOKEN[:8] + '...' if len(HF_TOKEN) > 8 else '***'
    logger.info('HF_TOKEN loaded: %s (len=%d)', token_preview, len(HF_TOKEN))

if not HF_API_URL:
    logger.info('HF_API_URL not set; will use gradio_client only (recommended)')

if not TELEGRAM_TOKEN:
    logger.warning('TELEGRAM_TOKEN not set; cannot send Telegram messages')

r = redis.Redis.from_url(UPSTASH_REDIS_URL, decode_responses=False)
# Отдельный клиент для строковых операций (совместимость с my_bot.py где decode_responses=True)
r_str = redis.Redis.from_url(UPSTASH_REDIS_URL, decode_responses=True)

MAX_IMAGES = int(os.environ.get('MAX_IMAGES', '5'))
MAX_PROMPT_LEN = int(os.environ.get('MAX_PROMPT_LEN', '1000'))
REDIS_TTL = int(os.environ.get('REDIS_TTL', '900'))
WORKER_CONCURRENCY = int(os.environ.get('WORKER_CONCURRENCY', '3'))
HF_TIMEOUT = int(os.environ.get('HF_TIMEOUT', '180'))
RETRIES = int(os.environ.get('HF_RETRIES', '2'))
HF_MAX_RETRIES = int(os.environ.get('HF_MAX_RETRIES', '5'))

# Rate limiting settings
RATE_LIMIT_HF_PER_USER_HOUR = int(os.environ.get('RATE_LIMIT_HF_PER_USER_HOUR', '5'))
RATE_LIMIT_HF_PER_USER_DAY = int(os.environ.get('RATE_LIMIT_HF_PER_USER_DAY', '20'))

# Gradio/Space settings
HF_SPACE = os.environ.get('HF_SPACE', 'mingg93/fgoslib-qwen3')
HF_API_NAME = os.environ.get('HF_API_NAME', '/solve_problem')
# Enforce using gradio_client with a provided HF_TOKEN only. No HTTP fallback allowed.
USE_GRADIO_CLIENT = True
HF_ALLOW_HTTP_FALLBACK = False

import re

# Improved moderation with regex patterns (word boundaries to avoid false positives)
PATTERN_BLACKLIST = [
    r'\bb[o0]mb\b',
    r'\bterr[o0]r\b',
    r'\bd[i!1]e\b',
    r'\bk[i!1]ll\b',
    r'\bmurder\b',
    r'\bass[a@]ult\b',
    r'\bdrugs?\b',
    r'\bp[o0]rn\b',
    r'\bh[a@]te\b',
    r'\br[a@]c[i!1]st\b',
    r'\b[i!1]llegal\b',
    r'\bwe[a@]pon\b',
]

TG_API_BASE = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/' if TELEGRAM_TOKEN else None


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


def schedule_delayed_task(task_obj: dict, delay_seconds: int):
    """Schedule a task for later execution using a Redis sorted set.
    score = unix timestamp when task becomes ready.
    """
    try:
        ready_at = int(time.time() + max(0, int(delay_seconds)))
        r.zadd('hf_delayed', {json.dumps(task_obj): ready_at})
        logger.info('Scheduled task %s for retry at %s (in %s s)', task_obj.get('task_id'), ready_at, delay_seconds)
    except Exception:
        logger.exception('Failed to schedule delayed task')


def move_due_delayed_tasks():
    """Background loop: move due tasks from `hf_delayed` to `hf_queue`.
    Runs in a daemon thread.
    """
    try:
        while True:
            try:
                now = int(time.time())
                # get tasks with score <= now
                items = r.zrangebyscore('hf_delayed', 0, now)
                if items:
                    for v in items:
                        try:
                            # remove from delayed set and push back to queue
                            removed = r.zrem('hf_delayed', v)
                            if removed:
                                r.lpush('hf_queue', v)
                                logger.info('Moved delayed task back to hf_queue')
                        except Exception:
                            logger.exception('Failed to move delayed task')
                time.sleep(5)
            except Exception:
                logger.exception('Delayed-task mover error')
                time.sleep(5)
    except Exception:
        logger.exception('Delayed-task mover fatal error')


def moderate_prompt(prompt: str) -> tuple:
    """
    Улучшенная модерация промпта с использованием регулярных выражений.
    Возвращает (разрешено: bool, причина: str).
    """
    if not prompt:
        return True, ""
    
    low = prompt.lower()
    for pattern in PATTERN_BLACKLIST:
        if re.search(pattern, low):
            return False, f"Заблокировано по паттерну модерации"
    
    return True, ""


def send_telegram_document(chat_id: int, file_bytes: bytes, filename: str = 'solution.pdf', task_id: str = None) -> bool:
    """Отправить документ в Telegram."""
    if not TG_API_BASE:
        logger.warning('Telegram token not set; cannot send file')
        return False
    try:
        files = {'document': (filename, file_bytes, 'application/pdf')}
        data = {'chat_id': str(chat_id)}
        # Inline кнопки не добавляем — файл уже у пользователя
        
        resp = requests.post(TG_API_BASE + 'sendDocument', data=data, files=files, timeout=30)
        if resp.status_code // 100 == 2:
            logger.info('Sent PDF to chat %s', chat_id)
            # Отправить сообщение "Задача решена" со стрелкой вверх
            try:
                requests.post(TG_API_BASE + 'sendMessage', data={
                    'chat_id': str(chat_id),
                    'text': '✅ Задача решена! 👆👁',
                    'parse_mode': 'HTML'
                }, timeout=10)
            except Exception:
                logger.exception('Failed to send completion message')
            return True
        else:
            logger.warning('Telegram send failed: %s %s', resp.status_code, resp.text)
            return False
    except Exception:
        logger.exception('Failed to send telegram document')
        return False


def call_hf_api(payload: dict) -> dict:
    """POST payload to HF API URL and return response JSON."""
    headers = {'Authorization': f'Bearer {HF_TOKEN}'} if HF_TOKEN else {}
    urls_to_try = []
    if HF_API_URL:
        # only try HF_API_URL directly if it looks like an API endpoint
        if '/api/' in HF_API_URL:
            urls_to_try.append(HF_API_URL)
        else:
            logger.warning('Configured HF_API_URL does not look like an API endpoint; skipping direct POST to avoid 405: %s', HF_API_URL)
    # try embed-style API path
    try:
        owner, repo = HF_SPACE.split('/')
        embed_url = f"https://hf.space/embed/{owner}/{repo}/api/predict{HF_API_NAME}"
        urls_to_try.append(embed_url)
        direct_url = f"https://{owner}-{repo}.hf.space/api/predict{HF_API_NAME}"
        urls_to_try.append(direct_url)
    except Exception:
        # malformed HF_SPACE; skip
        pass

    last_exc = None
    for url in urls_to_try:
        try:
            logger.info('Calling HF HTTP API at %s', url)
            resp = requests.post(url, headers=headers, json=payload, timeout=HF_TIMEOUT)
            resp.raise_for_status()
            try:
                return resp.json()
            except Exception:
                logger.exception('HF response not JSON from %s', url)
                return {'error': 'invalid_response', 'text': resp.text}
        except requests.exceptions.HTTPError as he:
            # If 405, try next candidate URL; otherwise record and continue
            logger.warning('HF HTTP error from %s: %s', url, he)
            last_exc = he
            continue
        except Exception as e:
            logger.exception('HF API call failed to %s', url)
            last_exc = e
            continue

    # all attempts failed
    logger.error('All HF HTTP API attempts failed')
    if last_exc:
        raise last_exc
    raise RuntimeError('HF API call failed (no endpoint configured)')


def download_url(url: str) -> bytes:
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        return r.content
    except Exception:
        logger.exception('Failed to download %s', url)
        raise


def download_telegram_file_if_needed(file_id: str) -> str:
    """If given a Telegram file_id, download it and return local path. Otherwise, return None.
    Assumes TELEGRAM_TOKEN is set in env.
    """
    if not file_id or not isinstance(file_id, str):
        return None
    if file_id.startswith('http') or file_id.startswith('data:'):
        return None
    token = os.environ.get('TELEGRAM_TOKEN')
    if not token:
        logger.warning('TELEGRAM_TOKEN not set; cannot download telegram file_id')
        return None
    try:
        info = requests.get(f'https://api.telegram.org/bot{token}/getFile?file_id={file_id}', timeout=15)
        info.raise_for_status()
        j = info.json()
        file_path = j['result']['file_path']
        url = f'https://api.telegram.org/file/bot{token}/{file_path}'
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        import tempfile, os
        suffix = os.path.splitext(file_path)[1] or ''
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(resp.content)
        tmp.close()
        return tmp.name
    except Exception:
        logger.exception('Failed to download telegram file_id %s', file_id)
        return None


def call_hf_via_gradio_client(task_text: str, images: list, user_prompt: str):
    """Call the HF Space via gradio_client.Client and return a dict with results.
    Expected return keys: 'markdown', 'file_bytes' (or None), 'time'
    """
    if Client is None:
        raise RuntimeError('gradio_client is not installed')
    # If HF_TOKEN is provided in env, pass it to gradio_client.Client so
    # requests to ZeroGPU spaces are made with the authenticated token and
    # consume the account's quota/priority rather than unauthenticated quota.
    # Require HF_TOKEN so requests are made on behalf of the configured HF account.
    if not HF_TOKEN:
        logger.error('HF_TOKEN is required for gradio_client calls')
        raise RuntimeError('HF_TOKEN is required')
    
    token_preview = HF_TOKEN[:8] + '...' if len(HF_TOKEN) > 8 else '***'
    logger.info('Creating gradio Client for space=%s with token=%s', HF_SPACE, token_preview)
    
    # Prefer creating a plain Client(HF_SPACE) and let gradio_client read HF_TOKEN
    # from the environment. This matches the local snippet used during testing:
    #
    # from gradio_client import Client, handle_file
    # client = Client("mingg93/fgoslib-qwen3")
    # result = client.predict(..., api_name="/solve_problem")
    #
    # Ensure HF_TOKEN is exported so Client uses the authenticated token.
    try:
        # Ensure env vars are present before Client() attempts to resolve the space
        try:
            if HF_TOKEN:
                os.environ.setdefault('HF_TOKEN', HF_TOKEN)
                os.environ.setdefault('HUGGINGFACE_HUB_TOKEN', HF_TOKEN)
                logger.info('Exported HF env vars before Client creation (prefix=%s)', token_preview)
        except Exception:
            logger.exception('Failed to set HF env vars before client creation')

        client = None
        # Try passing token as constructor kwarg with several common names
        candidates = ['token', 'hf_token', 'api_token', 'auth', 'hf_api_token']
        tried = []
        if HF_TOKEN:
            for name in candidates:
                try:
                    kwargs = {name: HF_TOKEN}
                    client = Client(HF_SPACE, **kwargs)
                    logger.info('Gradio Client created with constructor kwarg %s', name)
                    break
                except TypeError:
                    tried.append(name)
                    continue
                except Exception:
                    logger.exception('Failed to create gradio Client with kwarg %s', name)
                    client = None
            # final fallback: set multiple env vars and try plain Client()
            if client is None:
                # ensure env var is present for older clients that read it from env
                os.environ['HF_TOKEN'] = HF_TOKEN
                os.environ['HUGGINGFACE_HUB_TOKEN'] = HF_TOKEN
                token_preview = HF_TOKEN[:8] + '...' if len(HF_TOKEN) > 8 else '***'
                logger.info('HF_TOKEN set in env for gradio_client: %s', token_preview)
                try:
                    client = Client(HF_SPACE)
                    logger.info('Gradio Client created successfully for space=%s using env var fallback', HF_SPACE)
                except Exception:
                    logger.exception('Failed to create gradio Client with env var fallback')
                    raise
        else:
            # No token provided; attempt plain Client()
            client = Client(HF_SPACE)
            logger.info('Gradio Client created successfully for space=%s (no token)', HF_SPACE)
        # Diagnostic: check whoami with the token and log a short client repr
        try:
            try:
                who = requests.get('https://huggingface.co/api/whoami-v2',
                                   headers={'Authorization': f'Bearer {HF_TOKEN}'},
                                   timeout=8)
                try:
                    j = who.json()
                    logger.info('HF whoami: id=%s isPro=%s', j.get('id'), j.get('isPro'))
                except Exception:
                    logger.info('HF whoami raw: %s', who.text[:400])
            except Exception:
                logger.exception('HF whoami check failed')
        except Exception:
            # swallow diagnostics errors
            pass
        try:
            logger.info('gradio client repr: %s', repr(client)[:300])
        except Exception:
            pass
    except Exception:
        logger.exception('Failed to create gradio Client')
        raise
    # prepare image_input: Gradio Image component expects dict with 'path' or 'url'
    image_input = None
    if images:
        img = images[0]
        if isinstance(img, dict):
            # already structured
            image_input = img
        elif isinstance(img, str):
            if img.startswith('http'):
                image_input = {'url': img}
            elif img.startswith('data:'):
                # save data url to temp file
                try:
                    import base64, tempfile, re
                    header, b64 = img.split(',', 1)
                    ext = '.png'
                    m = re.search(r'data:image/([a-zA-Z0-9]+);', header)
                    if m:
                        ext = '.' + m.group(1)
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
                    tmp.write(base64.b64decode(b64))
                    tmp.close()
                    image_input = {'path': tmp.name}
                except Exception:
                    logger.exception('Failed to write data URL to temp file')
            else:
                # assume Telegram file_id
                local = download_telegram_file_if_needed(img)
                if local:
                    image_input = {'path': local}

    # call gradio client
    try:
        # debug: log what we send to the Space (helps verify parity with local test)
        try:
            logger.debug('gradio predict args: api_name=%s, text_len=%s, image_input_preview=%s',
                         HF_API_NAME, len(task_text or ''), repr(image_input)[:400])
        except Exception:
            # don't fail if debug logging fails
            logger.debug('gradio predict args: (failed to build preview)')

        res = client.predict(
            text_input=task_text,
            image_input=image_input,
            api_name=HF_API_NAME
        )
        # res expected [markdown_str, filepath_or_obj, time_str]
        markdown = None
        file_bytes = None
        gen_time = None
        file_name = None
        try:
            markdown = res[0]
            file_part = res[1]
            gen_time = res[2] if len(res) > 2 else None
            # file_part can be a dict with 'url' or a local path
            if isinstance(file_part, dict):
                url = file_part.get('url') or file_part.get('path')
                if url and isinstance(url, str) and url.startswith('http'):
                    file_bytes = download_url(url)
                    try:
                        file_name = os.path.basename(url)
                    except Exception:
                        file_name = None
                elif url and isinstance(url, str) and os.path.exists(url):
                    with open(url, 'rb') as f:
                        file_bytes = f.read()
                    try:
                        file_name = os.path.basename(url)
                    except Exception:
                        file_name = None
            elif isinstance(file_part, str):
                if file_part.startswith('http'):
                    file_bytes = download_url(file_part)
                    try:
                        file_name = os.path.basename(file_part)
                    except Exception:
                        file_name = None
                elif os.path.exists(file_part):
                    with open(file_part, 'rb') as f:
                        file_bytes = f.read()
                    try:
                        file_name = os.path.basename(file_part)
                    except Exception:
                        file_name = None
                else:
                    # maybe base64 or inline content; no filename
                    file_name = None
        except Exception:
            logger.exception('Failed to parse gradio client result')
        return {'markdown': markdown, 'file_bytes': file_bytes, 'time': gen_time, 'file_name': file_name}
    except Exception as e:
        # If this is a Gradio AppError we may be able to detect quota issues
        try:
            if GradioAppError is not None and isinstance(e, GradioAppError):
                msg = str(e)
                logger.warning('Gradio AppError: %s', msg)
                low = msg.lower()
                if 'quota' in low or 'exceeded your gpu quota' in low or 'gpu quota' in low:
                    # try to parse retry time like 'Try again in HH:MM:SS'
                    import re
                    retry = None
                    m = re.search(r'Try again in (\d{1,2}:\d{2}:\d{2})', msg)
                    if m:
                        h, mi, s = m.group(1).split(':')
                        retry = int(h)*3600 + int(mi)*60 + int(s)
                    raise HFQuotaError(msg, retry_after_seconds=retry)
        except HFQuotaError:
            # bubble up quota error
            raise
        except Exception:
            # not a quota AppError; fall through to generic handler
            pass
        logger.exception('Gradio client call failed')
        raise


def save_result_to_redis(task_id: str, result: dict):
    key = f'task_pdf_result:{task_id}'
    try:
        r.set(key, json.dumps(result), ex=REDIS_TTL)
    except Exception:
        logger.exception('Failed to save result to redis')


def clear_user_active_task(chat_id):
    """Очистить флаг активной задачи пользователя после завершения."""
    if not chat_id:
        return
    try:
        # Используем r_str (decode_responses=True) для совместимости с my_bot.py
        key = f"user_active_hf:{chat_id}"
        r_str.delete(key)
        logger.info('Cleared active task flag for user %s', chat_id)
    except Exception:
        logger.exception('Failed to clear user active task flag')


# --- Периодические уведомления о статусе задачи ---
NOTIFICATION_INTERVAL = int(os.environ.get('NOTIFICATION_INTERVAL', '10'))  # секунд
GPU_DURATION_LIMIT = int(os.environ.get('GPU_DURATION_LIMIT', '90'))  # секунд


def send_status_notification(chat_id: int, message: str) -> int:
    """Отправить уведомление о статусе в Telegram. Возвращает message_id для удаления."""
    if not TG_API_BASE or not chat_id:
        return 0
    try:
        resp = requests.post(
            TG_API_BASE + 'sendMessage',
            data={'chat_id': str(chat_id), 'text': message, 'parse_mode': 'HTML'},
            timeout=10
        )
        if resp.status_code // 100 == 2:
            return resp.json().get('result', {}).get('message_id', 0)
        return 0
    except Exception:
        logger.exception('Failed to send status notification')
        return 0


def delete_telegram_message(chat_id: int, message_id: int):
    """Удалить сообщение из Telegram."""
    if not TG_API_BASE or not chat_id or not message_id:
        return
    try:
        requests.post(
            TG_API_BASE + 'deleteMessage',
            data={'chat_id': str(chat_id), 'message_id': str(message_id)},
            timeout=10
        )
    except Exception:
        logger.exception('Failed to delete message %s', message_id)


class TaskProgressNotifier:
    """Фоновый поток для периодических уведомлений о статусе задачи."""
    
    def __init__(self, chat_id: int, task_id: str, interval: int = NOTIFICATION_INTERVAL):
        self.chat_id = chat_id
        self.task_id = task_id
        self.interval = interval
        self.status = "⏳ Задача поставлена в очередь..."
        self.running = True
        self.start_time = time.time()
        self.thread = None
        self.message_ids = []  # Список ID сообщений для удаления
        self.timeout_notified = False
    
    def start(self):
        """Запустить поток уведомлений."""
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        # Отправить начальное уведомление
        msg_id = send_status_notification(self.chat_id, f"🚀 <b>Задача {self.task_id[:8]}...</b>\n{self.status}")
        if msg_id:
            self.message_ids.append(msg_id)
    
    def update_status(self, status: str):
        """Обновить статус (будет отправлен в следующем цикле)."""
        self.status = status
    
    def stop(self, final_message: str = None):
        """Остановить поток и удалить все временные сообщения."""
        self.running = False
        # Удалить все промежуточные сообщения
        for msg_id in self.message_ids:
            delete_telegram_message(self.chat_id, msg_id)
        self.message_ids.clear()
        # НЕ отправляем финальное сообщение — оно будет вместе с документом
    
    def _run(self):
        """Фоновый цикл отправки уведомлений."""
        while self.running:
            time.sleep(self.interval)
            if not self.running:
                break
            elapsed = int(time.time() - self.start_time)
            
            # Проверка превышения лимита времени
            if elapsed > GPU_DURATION_LIMIT and not self.timeout_notified:
                self.timeout_notified = True
                msg_id = send_status_notification(
                    self.chat_id, 
                    f"⚠️ Время решения превысило {GPU_DURATION_LIMIT} сек. Задача сложная, ожидайте..."
                )
                if msg_id:
                    self.message_ids.append(msg_id)
            
            msg = f"⏳ <b>Задача {self.task_id[:8]}...</b>\n{self.status}\n🕐 Прошло: {elapsed} сек"
            msg_id = send_status_notification(self.chat_id, msg)
            if msg_id:
                self.message_ids.append(msg_id)


def process_task(item: dict):
    task_id = item.get('task_id')
    chat_id = item.get('chat_id')
    task_text = item.get('task_text', '')
    images = item.get('images', []) or []
    user_prompt = item.get('user_prompt', '') or ''

    logger.info('Processing task %s for chat %s', task_id, chat_id)

    # Проверка владельца задачи (security check)
    try:
        stored_assignee = r.get(f"task_assignee:{task_id}")
        if stored_assignee:
            expected_chat_id = stored_assignee.decode('utf-8') if isinstance(stored_assignee, bytes) else str(stored_assignee)
            if str(chat_id) != expected_chat_id:
                logger.warning('Security: chat_id mismatch for task %s: expected %s, got %s', task_id, expected_chat_id, chat_id)
                return  # Игнорируем подозрительную задачу
    except Exception:
        logger.exception('Failed to verify task assignee for task %s', task_id)
        return

    # Rate limiting check
    if chat_id:
        try:
            user_id = int(chat_id)
            if not check_rate_limit(user_id, "hf_hour", RATE_LIMIT_HF_PER_USER_HOUR, 3600):
                logger.warning('Rate limit exceeded (hour) for user %s', user_id)
                if TG_API_BASE:
                    try:
                        requests.post(TG_API_BASE + 'sendMessage', 
                                    data={'chat_id': str(chat_id), 
                                          'text': f'⚠️ Превышен лимит запросов к HF ({RATE_LIMIT_HF_PER_USER_HOUR}/час). Попробуйте позже.'},
                                    timeout=10)
                    except Exception:
                        pass
                return
            
            if not check_rate_limit(user_id, "hf_day", RATE_LIMIT_HF_PER_USER_DAY, 86400):
                logger.warning('Rate limit exceeded (day) for user %s', user_id)
                if TG_API_BASE:
                    try:
                        requests.post(TG_API_BASE + 'sendMessage',
                                    data={'chat_id': str(chat_id),
                                          'text': f'⚠️ Превышен дневной лимит запросов к HF ({RATE_LIMIT_HF_PER_USER_DAY}/день).'},
                                    timeout=10)
                    except Exception:
                        pass
                return
        except (ValueError, TypeError):
            pass  # chat_id не является числом - пропускаем rate limit

    # Validate
    if len(images) > MAX_IMAGES:
        images = images[:MAX_IMAGES]
    if len(user_prompt) > MAX_PROMPT_LEN:
        user_prompt = user_prompt[:MAX_PROMPT_LEN]

    allowed, reason = moderate_prompt(user_prompt)
    if not allowed:
        # notify user and drop prompt
        logger.warning('Prompt moderation failed for task %s: %s', task_id, reason)
        try:
            if TG_API_BASE and chat_id:
                msg = {'chat_id': str(chat_id), 'text': f'⚠️ Дополнительный промпт отклонён модерацией. Задача отправлена без него.'}
                requests.post(TG_API_BASE + 'sendMessage', data=msg, timeout=10)
        except Exception:
            logger.exception('Failed to send moderation notice')
        user_prompt = ''

    payload = {
        'task_id': task_id,
        'task_text': task_text,
        'images': images,
        'user_prompt': user_prompt
    }

    # Запускаем периодические уведомления о статусе
    notifier = None
    if TG_API_BASE and chat_id:
        notifier = TaskProgressNotifier(chat_id, task_id)
        notifier.start()

    # call HF with retries
    attempt = 0
    last_err = None
    while attempt <= RETRIES:
        try:
            # prefer gradio_client only and DO NOT fallback to HTTP
            resp = None
            if USE_GRADIO_CLIENT:
                if notifier:
                    notifier.update_status("🧠 Процесс решения запущен...")
                try:
                    resp = call_hf_via_gradio_client(payload['task_text'], payload.get('images', []), payload.get('user_prompt', ''))
                except HFQuotaError as qe:
                    # Service quota exhausted — schedule retry with backoff
                    logger.warning('HF quota error for task %s: %s', task_id, qe)
                    # determine current retry count (stored in payload)
                    retry_count = int(item.get('retry_count', 0))
                    retry_count += 1
                    # if HF provides explicit retry_after, use it, otherwise exponential backoff
                    if qe.retry_after_seconds and qe.retry_after_seconds > 0:
                        delay = qe.retry_after_seconds
                    else:
                        delay = min(3600, 60 * (2 ** (retry_count - 1)))
                    if retry_count > HF_MAX_RETRIES:
                        logger.warning('Max retries exceeded for task %s; giving up', task_id)
                        save_result_to_redis(task_id, {'status': 'quota_exhausted', 'error': str(qe)})
                        clear_user_active_task(chat_id)
                        if TG_API_BASE and chat_id:
                            try:
                                requests.post(TG_API_BASE + 'sendMessage', data={'chat_id': str(chat_id), 'text': 'Генерация временно недоступна (квота исчерпана). Попробуйте позже.'}, timeout=10)
                            except Exception:
                                logger.exception('Failed to notify user about final quota')
                        return
                    # prepare task for requeue
                    new_task = dict(item)
                    new_task['retry_count'] = retry_count
                    # schedule into delayed set
                    schedule_delayed_task(new_task, delay)
                    save_result_to_redis(task_id, {'status': 'scheduled_retry', 'retry_count': retry_count, 'next_try_in': delay})
                    if notifier:
                        notifier.stop()
                    # Отправляем уведомление о повторе отдельно (его не удаляем)
                    if TG_API_BASE and chat_id:
                        try:
                            requests.post(TG_API_BASE + 'sendMessage', data={'chat_id': str(chat_id), 'text': f'🔄 Сервис занят. Задача повторно запланирована через {delay} сек.'}, timeout=10)
                        except Exception:
                            pass
                    return
                except Exception:
                    # Non-quota error from gradio_client. By default we DO NOT fallback to HTTP
                    # to avoid sending POSTs to non-API pages (causing 405). Only fallback
                    # when explicitly allowed via HF_ALLOW_HTTP_FALLBACK and HF_API_URL looks
                    # like an API endpoint.
                    logger.exception('gradio_client call failed')
                    save_result_to_redis(task_id, {'status': 'error', 'error': 'gradio_client_failed'})
                    clear_user_active_task(chat_id)
                    if notifier:
                        notifier.stop()
                    if not HF_ALLOW_HTTP_FALLBACK:
                        if TG_API_BASE and chat_id:
                            try:
                                requests.post(TG_API_BASE + 'sendMessage', data={'chat_id': str(chat_id), 'text': 'Ошибка интеграции с сервисом генерации (gradio client). Попробуйте позже.'}, timeout=10)
                            except Exception:
                                logger.exception('Failed to notify user about gradio_client failure')
                        return
                    # HF_ALLOW_HTTP_FALLBACK is true — ensure HF_API_URL looks like API
                    if not HF_API_URL or '/api/' not in HF_API_URL:
                        logger.warning('HF_API_URL not configured as API endpoint; skipping HTTP fallback')
                        save_result_to_redis(task_id, {'status': 'error', 'error': 'no_http_fallback_available'})
                        clear_user_active_task(chat_id)
                        if TG_API_BASE and chat_id:
                            try:
                                requests.post(TG_API_BASE + 'sendMessage', data={'chat_id': str(chat_id), 'text': 'Интеграция с HF настроена некорректно (нет HTTP fallback).'}, timeout=10)
                            except Exception:
                                logger.exception('Failed to notify user about missing HTTP fallback')
                        return
                    logger.info('gradio_client failed; HF_ALLOW_HTTP_FALLBACK enabled and HF_API_URL looks like API — falling back to HTTP')
                    resp = None
            if resp is None:
                # Do not attempt any HTTP fallback; surface the error instead
                logger.error('No response from gradio_client for task %s and HTTP fallback disabled', task_id)
                save_result_to_redis(task_id, {'status': 'error', 'error': 'no_gradio_response'})
                clear_user_active_task(chat_id)
                if TG_API_BASE and chat_id:
                    try:
                        requests.post(TG_API_BASE + 'sendMessage', data={'chat_id': str(chat_id), 'text': 'Ошибка интеграции с сервисом генерации (нет ответа от API). Попробуйте позже.'}, timeout=10)
                    except Exception:
                        logger.exception('Failed to notify user about no_gradio_response')
                return
            # response handling: look for pdf_url or pdf_base64
            if not resp:
                raise RuntimeError('empty response from HF')

            # If gradio_client was used it returns dict with 'file_bytes' possibly
            pdf_bytes = None
            file_name = None
            if isinstance(resp, dict) and 'file_bytes' in resp and resp.get('file_bytes'):
                pdf_bytes = resp.get('file_bytes')
                file_name = resp.get('file_name')
            # existing HTTP-style responses
            if not pdf_bytes:
                pdf_url = None
                if isinstance(resp, dict) and 'pdf_url' in resp:
                    pdf_url = resp['pdf_url']
                    pdf_bytes = download_url(pdf_url)
                elif isinstance(resp, dict) and 'pdf_base64' in resp:
                    pdf_bytes = base64.b64decode(resp['pdf_base64'])
                else:
                    # try to detect data in response (some spaces return data or files)
                    if isinstance(resp, dict) and 'data' in resp and isinstance(resp['data'], list):
                        # attempt to find base64 blob or file url
                        for el in resp['data']:
                            if isinstance(el, dict) and el.get('type') == 'pdf' and el.get('data'):
                                try:
                                    pdf_bytes = base64.b64decode(el.get('data'))
                                    break
                                except Exception:
                                    pass
                            if isinstance(el, dict) and el.get('url'):
                                try:
                                    pdf_bytes = download_url(el.get('url'))
                                    break
                                except Exception:
                                    pass
                    # fallback: no recognizable pdf
                    if not pdf_bytes:
                        save_result_to_redis(task_id, {'status': 'error', 'error': 'no_pdf_in_hf_response', 'response': resp})
                        clear_user_active_task(chat_id)
                        if TG_API_BASE and chat_id:
                            try:
                                requests.post(TG_API_BASE + 'sendMessage', data={'chat_id': str(chat_id), 'text': 'HF вернул неожиданный формат ответа.'}, timeout=10)
                            except Exception:
                                logger.exception('Failed to report HF format error to user')
                        return

            # send PDF to Telegram if we have bytes
            # If HF returned a file which is a markdown file -> route to render_service for PDF conversion
            if file_name and file_name.lower().endswith('.md') and pdf_bytes:
                try:
                    # create temporary render task id
                    render_task_id = f"hf_render:{task_id}"
                    # create task object where task_text is the markdown content
                    md_text = pdf_bytes.decode('utf-8', errors='replace')
                    task_obj = {
                        'task_text': md_text,
                        'images': [],
                        'prompt': '',
                        'format': 'md',
                        'real_id': task_id
                    }
                    r.set(f"task:{render_task_id}", json.dumps(task_obj), ex=REDIS_TTL)
                    # assign assignee so render_service will notify correct chat
                    r.set(f"task_assignee:{render_task_id}", str(chat_id), ex=REDIS_TTL)
                    # enqueue render
                    r.lpush('render_queue', render_task_id)
                    save_result_to_redis(task_id, {'status': 'render_queued', 'message': 'Markdown received; render queued', 'render_task_id': render_task_id})
                    if notifier:
                        notifier.stop()
                    # Уведомление о рендере (не удаляется)
                    if TG_API_BASE and chat_id:
                        try:
                            requests.post(TG_API_BASE + 'sendMessage', data={'chat_id': str(chat_id), 'text': '📄 Решение получено, идёт рендеринг PDF...'}, timeout=10)
                        except Exception:
                            pass
                    return
                except Exception:
                    logger.exception('Failed to queue render for markdown result')

            if pdf_bytes:
                if notifier:
                    notifier.stop()  # Удалить промежуточные сообщения
                ok = False
                if TG_API_BASE and chat_id:
                    ok = send_telegram_document(chat_id, pdf_bytes, filename=f'solution_{task_id}.pdf', task_id=task_id)
                # save to redis (store as base64 to avoid external storage)
                save_result_to_redis(task_id, {'status': 'ok', 'pdf_base64': base64.b64encode(pdf_bytes).decode('ascii')})
                clear_user_active_task(chat_id)
                return
            else:
                if notifier:
                    notifier.stop()  # Удалить промежуточные сообщения
                # unreachable normally
                save_result_to_redis(task_id, {'status': 'error', 'error': 'no_pdf_bytes'})
                clear_user_active_task(chat_id)
                return

        except Exception as e:
            logger.exception('Attempt %s: error processing task %s', attempt, task_id)
            last_err = str(e)
            attempt += 1
            time.sleep(1 + attempt * 2)

    # after retries
    if notifier:
        notifier.stop()
    save_result_to_redis(task_id, {'status': 'error', 'error': last_err})
    clear_user_active_task(chat_id)
    if TG_API_BASE and chat_id:
        try:
            requests.post(TG_API_BASE + 'sendMessage', data={'chat_id': str(chat_id), 'text': '❌ Ошибка при генерации решения, попробуйте позже.'}, timeout=10)
        except Exception:
            logger.exception('Failed to notify user about final error')


def main():
    logger.info('Starting hf_worker, concurrency=%s', WORKER_CONCURRENCY)
    # Diagnostic: log sha256 of this file to help confirm deployed version
    try:
        p = os.path.abspath(__file__)
        with open(p, 'rb') as fh:
            data = fh.read()
        h = hashlib.sha256(data).hexdigest()
        logger.info('hf_worker.py sha256=%s', h[:12])
    except Exception:
        logger.exception('Failed to compute hf_worker.py sha256')

    # start health server (non-blocking) so Render sees the service as a web service
    def start_health():
        if web is None:
            logger.warning('aiohttp not available; health endpoint disabled')
            return
        port = int(os.environ.get('PORT', '8080'))

        async def health(request):
            # include quick metrics: hf_queue length and uptime
            try:
                qlen = 0
                try:
                    qlen = r.llen('hf_queue')
                except Exception:
                    logger.exception('Failed to read hf_queue length')
                uptime = int(time.time() - START_TIME)
                return web.json_response({'status': 'ok', 'queue_length': qlen, 'uptime_seconds': uptime})
            except Exception:
                return web.json_response({'status': 'ok'})

        # create a new event loop and run aiohttp AppRunner there to avoid
        # setting signal handlers from a non-main thread
        def runner():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                app = web.Application()
                app.add_routes([web.get('/', health), web.get('/health', health)])
                runner_obj = web.AppRunner(app)
                loop.run_until_complete(runner_obj.setup())
                site = web.TCPSite(runner_obj, '0.0.0.0', port)
                loop.run_until_complete(site.start())
                logger.info('Health server started on port %s (background loop)', port)
                loop.run_forever()
            except Exception:
                logger.exception('Health server stopped')

        t = threading.Thread(target=runner, daemon=True)
        t.start()

    start_health()

    # start delayed-task mover thread
    try:
        mover = threading.Thread(target=move_due_delayed_tasks, daemon=True)
        mover.start()
        logger.info('Started delayed-task mover thread')
    except Exception:
        logger.exception('Failed to start delayed-task mover')

    executor = ThreadPoolExecutor(max_workers=WORKER_CONCURRENCY)
    try:
        while True:
            try:
                item = r.brpop('hf_queue', timeout=5)
                if not item:
                    continue
                _, raw = item
                if isinstance(raw, bytes):
                    raw = raw.decode('utf-8')
                try:
                    data = json.loads(raw)
                except Exception:
                    logger.exception('Invalid JSON in queue item')
                    continue
                # submit to thread pool
                executor.submit(process_task, data)
            except Exception:
                logger.exception('Worker loop error')
                time.sleep(1)
    except KeyboardInterrupt:
        logger.info('Shutting down hf_worker')
    finally:
        executor.shutdown(wait=True)


if __name__ == '__main__':
    main()
