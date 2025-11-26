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
try:
    from gradio_client import Client
except Exception:
    Client = None

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

# Gradio/Space settings
HF_SPACE = os.environ.get('HF_SPACE', 'mingg93/fgoslib-qwen3')
HF_API_NAME = os.environ.get('HF_API_NAME', '/solve_problem')
USE_GRADIO_CLIENT = os.environ.get('HF_USE_GRADIO_CLIENT', 'true').lower() in ('1', 'true', 'yes')

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
    urls_to_try = []
    if HF_API_URL:
        urls_to_try.append(HF_API_URL)
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
    client = Client(HF_SPACE)
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
                    import base64, tempfile, os, re
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
        res = client.predict(
            text_input=task_text,
            image_input=image_input,
            api_name=HF_API_NAME
        )
        # res expected [markdown_str, filepath_or_obj, time_str]
        markdown = None
        file_bytes = None
        gen_time = None
        try:
            markdown = res[0]
            file_part = res[1]
            gen_time = res[2] if len(res) > 2 else None
            # file_part can be a dict with 'url' or a local path
            if isinstance(file_part, dict):
                url = file_part.get('url') or file_part.get('path')
                if url and isinstance(url, str) and url.startswith('http'):
                    file_bytes = download_url(url)
                elif url and isinstance(url, str) and os.path.exists(url):
                    with open(url, 'rb') as f:
                        file_bytes = f.read()
            elif isinstance(file_part, str):
                if file_part.startswith('http'):
                    file_bytes = download_url(file_part)
                elif os.path.exists(file_part):
                    with open(file_part, 'rb') as f:
                        file_bytes = f.read()
        except Exception:
            logger.exception('Failed to parse gradio client result')
        return {'markdown': markdown, 'file_bytes': file_bytes, 'time': gen_time}
    except Exception:
        logger.exception('Gradio client call failed')
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
            # prefer gradio_client when configured
            resp = None
            if USE_GRADIO_CLIENT:
                try:
                    resp = call_hf_via_gradio_client(payload['task_text'], payload.get('images', []), payload.get('user_prompt', ''))
                except Exception:
                    logger.exception('gradio_client call failed, falling back to HTTP')
                    resp = None
            if resp is None:
                # fallback to previous HTTP approach
                resp = call_hf_api(payload)
            # response handling: look for pdf_url or pdf_base64
            if not resp:
                raise RuntimeError('empty response from HF')

            # If gradio_client was used it returns dict with 'file_bytes' possibly
            pdf_bytes = None
            if isinstance(resp, dict) and 'file_bytes' in resp and resp.get('file_bytes'):
                pdf_bytes = resp.get('file_bytes')
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
