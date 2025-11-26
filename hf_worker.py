#!/usr/bin/env python3
"""HF worker: consumes `hf_queue` and forwards tasks to a HuggingFace Space (Gradio API).

Behavior:
 - BRPOP from Redis `hf_queue` for JSON tasks
 - validate images (<= MAX_IMAGES) and user_prompt length
 - basic moderation of user_prompt using a small blacklist
 - POST payload to `HF_API_URL` with Bearer `HF_API_TOKEN`
 - handle response containing `pdf_url` or `pdf_base64` (or download & forward)
 - store result in Redis under `task_pdf_result:<task_id>` with TTL
 - send PDF to Telegram chat via Bot API when ready

Configuration via environment variables:
 - UPSTASH_REDIS_URL (required)
 - HF_API_TOKEN (required)
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
from concurrent.futures import ThreadPoolExecutor

import redis
try:
    # aiohttp used only for health endpoint
    from aiohttp import web
except Exception:
    web = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('hf_worker')

# worker start time for uptime metric
START_TIME = time.time()

UPSTASH_REDIS_URL = os.environ.get('UPSTASH_REDIS_URL')
if not UPSTASH_REDIS_URL:
    raise RuntimeError('UPSTASH_REDIS_URL is required')

HF_API_TOKEN = os.environ.get('HF_API_TOKEN')
HF_API_URL = os.environ.get('HF_API_URL')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')

if not HF_API_TOKEN or not HF_API_URL:
    logger.warning('HF_API_TOKEN or HF_API_URL not set; HF calls will fail until provided')

if not TELEGRAM_TOKEN:
    logger.warning('TELEGRAM_TOKEN not set; cannot send Telegram messages')

r = redis.Redis.from_url(UPSTASH_REDIS_URL, decode_responses=False)

MAX_IMAGES = int(os.environ.get('MAX_IMAGES', '5'))
MAX_PROMPT_LEN = int(os.environ.get('MAX_PROMPT_LEN', '1000'))
REDIS_TTL = int(os.environ.get('REDIS_TTL', '900'))
WORKER_CONCURRENCY = int(os.environ.get('WORKER_CONCURRENCY', '3'))
HF_TIMEOUT = int(os.environ.get('HF_TIMEOUT', '180'))
RETRIES = int(os.environ.get('HF_RETRIES', '2'))

# Very small blacklist for extra prompts (simple approach)
BLACKLIST = [
    'bomb', 'terror', 'drugs', 'sex', 'assault', 'kill', 'murder',
    'porn', 'hate', 'racist', 'illegal'
]

TG_API_BASE = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/' if TELEGRAM_TOKEN else None


def moderate_prompt(prompt: str) -> bool:
    """Return True if prompt is allowed, False if blocked."""
    if not prompt:
        return True
    low = prompt.lower()
    for w in BLACKLIST:
        if w in low:
            return False
    return True


def send_telegram_document(chat_id: int, file_bytes: bytes, filename: str = 'solution.pdf') -> bool:
    if not TG_API_BASE:
        logger.warning('Telegram token not set; cannot send file')
        return False
    try:
        files = {'document': (filename, file_bytes, 'application/pdf')}
        data = {'chat_id': str(chat_id)}
        resp = requests.post(TG_API_BASE + 'sendDocument', data=data, files=files, timeout=30)
        if resp.status_code // 100 == 2:
            logger.info('Sent PDF to chat %s', chat_id)
            return True
        else:
            logger.warning('Telegram send failed: %s %s', resp.status_code, resp.text)
            return False
    except Exception:
        logger.exception('Failed to send telegram document')
        return False


def call_hf_api(payload: dict) -> dict:
    """POST payload to HF API URL and return response JSON."""
    headers = {'Authorization': f'Bearer {HF_API_TOKEN}'} if HF_API_TOKEN else {}
    try:
        resp = requests.post(HF_API_URL, headers=headers, json=payload, timeout=HF_TIMEOUT)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            logger.exception('HF response not JSON')
            return {'error': 'invalid_response', 'text': resp.text}
    except Exception as e:
        logger.exception('HF API call failed')
        raise


def download_url(url: str) -> bytes:
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        return r.content
    except Exception:
        logger.exception('Failed to download %s', url)
        raise


def save_result_to_redis(task_id: str, result: dict):
    key = f'task_pdf_result:{task_id}'
    try:
        r.set(key, json.dumps(result), ex=REDIS_TTL)
    except Exception:
        logger.exception('Failed to save result to redis')


def process_task(item: dict):
    task_id = item.get('task_id')
    chat_id = item.get('chat_id')
    task_text = item.get('task_text', '')
    images = item.get('images', []) or []
    user_prompt = item.get('user_prompt', '') or ''

    logger.info('Processing task %s for chat %s', task_id, chat_id)

    # Validate
    if len(images) > MAX_IMAGES:
        images = images[:MAX_IMAGES]
    if len(user_prompt) > MAX_PROMPT_LEN:
        user_prompt = user_prompt[:MAX_PROMPT_LEN]

    allowed = moderate_prompt(user_prompt)
    if not allowed:
        # notify user and drop prompt
        try:
            if TG_API_BASE and chat_id:
                msg = {'chat_id': str(chat_id), 'text': 'Дополнительный промпт отклонён политикой; задача отправлена без него.'}
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

    # call HF with retries
    attempt = 0
    last_err = None
    while attempt <= RETRIES:
        try:
            resp = call_hf_api(payload)
            # response handling: look for pdf_url or pdf_base64
            if not resp:
                raise RuntimeError('empty response from HF')

            # common keys
            pdf_bytes = None
            pdf_url = None
            if isinstance(resp, dict) and 'pdf_url' in resp:
                pdf_url = resp['pdf_url']
                pdf_bytes = download_url(pdf_url)
            elif isinstance(resp, dict) and 'pdf_base64' in resp:
                pdf_bytes = base64.b64decode(resp['pdf_base64'])
            else:
                # try to detect data in response (some spaces return data or files)
                if isinstance(resp, dict) and 'data' in resp and isinstance(resp['data'], list):
                    # attempt to find base64 blob
                    for el in resp['data']:
                        if isinstance(el, dict) and el.get('type') == 'pdf' and el.get('data'):
                            try:
                                pdf_bytes = base64.b64decode(el.get('data'))
                                break
                            except Exception:
                                pass
                # fallback: if response contains a URL-like string
                if not pdf_bytes:
                    # no recognizable pdf -> save response and error
                    save_result_to_redis(task_id, {'status': 'error', 'error': 'no_pdf_in_hf_response', 'response': resp})
                    if TG_API_BASE and chat_id:
                        try:
                            requests.post(TG_API_BASE + 'sendMessage', data={'chat_id': str(chat_id), 'text': 'HF вернул неожиданный формат ответа.'}, timeout=10)
                        except Exception:
                            logger.exception('Failed to report HF format error to user')
                    return

            # send PDF to Telegram if we have bytes
            if pdf_bytes:
                ok = False
                if TG_API_BASE and chat_id:
                    ok = send_telegram_document(chat_id, pdf_bytes, filename=f'solution_{task_id}.pdf')
                # save to redis (store as base64 to avoid external storage)
                save_result_to_redis(task_id, {'status': 'ok', 'pdf_base64': base64.b64encode(pdf_bytes).decode('ascii')})
                return
            else:
                # unreachable normally
                save_result_to_redis(task_id, {'status': 'error', 'error': 'no_pdf_bytes'})
                return

        except Exception as e:
            logger.exception('Attempt %s: error processing task %s', attempt, task_id)
            last_err = str(e)
            attempt += 1
            time.sleep(1 + attempt * 2)

    # after retries
    save_result_to_redis(task_id, {'status': 'error', 'error': last_err})
    if TG_API_BASE and chat_id:
        try:
            requests.post(TG_API_BASE + 'sendMessage', data={'chat_id': str(chat_id), 'text': 'Ошибка при генерации решения, попробуйте позже.'}, timeout=10)
        except Exception:
            logger.exception('Failed to notify user about final error')


def main():
    logger.info('Starting hf_worker, concurrency=%s', WORKER_CONCURRENCY)

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
