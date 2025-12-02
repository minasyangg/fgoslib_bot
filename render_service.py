#!/usr/bin/env python3
"""
Render worker service.

Listens on Redis `render_queue` (BRPOP) for task ids.
For each task it:
 - loads `task:<id>` from Redis
 - builds HTML (from markdown) and injects KaTeX for math rendering
 - renders to PNG if content fits single page, otherwise renders PDF
 - uploads result to S3 (if configured) or stores base64 in Redis under `task_png:<id>`/`task_pdf:<id>`
 - notifies Telegram user if `task_assignee:<id>` exists by sending the file via Bot API

Environment variables:
 - UPSTASH_REDIS_URL (required)
 - TELEGRAM_TOKEN (optional, for sending files)
 - S3_BUCKET, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_ENDPOINT (optional)
 - REDIS_TTL (seconds for stored keys, default 600)

Run in Docker image that has Playwright browsers installed (Dockerfile.render provided).
"""

import os
import time
import base64
import json
import logging
import asyncio
from typing import Optional

import redis
import requests
import boto3
from botocore.exceptions import BotoCoreError, ClientError
import markdown as md
from aiohttp import web

from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('render_service')

UPSTASH_REDIS_URL = os.environ.get('UPSTASH_REDIS_URL')
if not UPSTASH_REDIS_URL:
    raise RuntimeError('UPSTASH_REDIS_URL is required')

r = redis.Redis.from_url(UPSTASH_REDIS_URL, decode_responses=False)
# Отдельный клиент для строковых операций (совместимость с my_bot.py где decode_responses=True)
r_str = redis.Redis.from_url(UPSTASH_REDIS_URL, decode_responses=True)
REDIS_TTL = int(os.environ.get('REDIS_TTL', '600'))

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')

# File size limit (10MB default)
MAX_FILE_SIZE_MB = int(os.environ.get('MAX_FILE_SIZE_MB', '10'))
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024

# Rate limiting settings
RATE_LIMIT_RENDER_PER_USER_HOUR = int(os.environ.get('RATE_LIMIT_RENDER_PER_USER_HOUR', '10'))
RATE_LIMIT_RENDER_PER_USER_DAY = int(os.environ.get('RATE_LIMIT_RENDER_PER_USER_DAY', '30'))


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


# Optional S3 config
S3_BUCKET = os.environ.get('S3_BUCKET')
S3_ENDPOINT = os.environ.get('S3_ENDPOINT')  # e.g. https://s3.amazonaws.com or DO Spaces endpoint
AWS_KEY = os.environ.get('AWS_ACCESS_KEY_ID')
AWS_SECRET = os.environ.get('AWS_SECRET_ACCESS_KEY')

def s3_client():
    if not S3_BUCKET:
        return None
    kwargs = {}
    if S3_ENDPOINT:
        kwargs['endpoint_url'] = S3_ENDPOINT
    return boto3.client('s3', aws_access_key_id=AWS_KEY, aws_secret_access_key=AWS_SECRET, **kwargs)

def upload_to_s3(bytes_data: bytes, key: str, content_type: str) -> Optional[str]:
    client = s3_client()
    if client is None:
        return None
    try:
        client.put_object(Bucket=S3_BUCKET, Key=key, Body=bytes_data, ContentType=content_type)
        # build URL
        if S3_ENDPOINT:
            return f"{S3_ENDPOINT.rstrip('/')}/{S3_BUCKET}/{key}"
        else:
            return f"https://{S3_BUCKET}.s3.amazonaws.com/{key}"
    except (BotoCoreError, ClientError) as e:
        logger.exception('S3 upload failed')
        return None

def build_html(task_obj: dict) -> str:
    """Return an HTML document string with KaTeX included for client-side math rendering."""
    text = task_obj.get('task_text', '') or ''
    try:
        body = md.markdown(text, extensions=['extra', 'tables'])
    except Exception:
        body = f"<pre>{text}</pre>"

    # Build HTML without using an f-string to avoid brace-escaping issues
    port_str = os.environ.get('PORT', os.environ.get('HTTP_PORT', '8080'))
    base_url = 'http://127.0.0.1:' + port_str
    head = ("""<!doctype html>
<html>
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>Task</title>
""")
    # reference local KaTeX resources served from /static/
    head += "\n    <link rel=\"stylesheet\" href=\"" + base_url + "/static/katex/katex.min.css\">"
    head += "\n    <script defer src=\"" + base_url + "/static/katex/katex.min.js\"></script>"
    head += "\n    <script defer src=\"" + base_url + "/static/katex/contrib/auto-render.min.js\"></script>"
    head += "\n    <style>body{font-family: DejaVu Sans, Arial, sans-serif; padding:20px;} img{max-width:100%; height:auto;} pre{white-space:pre-wrap;}</style>\n</head>\n<body>"

    # JS snippet: use normal JS braces (no doubling) since we are not in an f-string
    js = ("""
<script>
document.addEventListener('DOMContentLoaded', ()=>{
    if (window.renderMathInElement) {
        try {
            renderMathInElement(document.body, {
                delimiters: [
                    {left: '$$', right: '$$', display: true},
                    {left: '$', right: '$', display: false},
                    {left: '\\(', right: '\\)', display: false},
                    {left: '\\[', right: '\\]', display: true}
                ]
            });
        } catch(e) { console.error(e); }
    }
});
</script>
""")

    html = head + body + js + "\n</body>\n</html>"
    return html

async def render_task(task_id: str):
    raw = r.get(f"task:{task_id}")
    if not raw:
        logger.warning('Task %s not found in Redis', task_id)
        return
    try:
        task_obj = json.loads(raw.decode('utf-8'))
    except Exception:
        logger.exception('Failed to parse task JSON')
        return

    # Проверка владельца задачи (security check)
    try:
        stored_assignee = r.get(f"task_assignee:{task_id}")
        if stored_assignee:
            assignee_chat_id = stored_assignee.decode('utf-8') if isinstance(stored_assignee, bytes) else str(stored_assignee)
            
            # Rate limiting check для пользователя
            try:
                user_id = int(assignee_chat_id)
                if not check_rate_limit(user_id, "render_hour", RATE_LIMIT_RENDER_PER_USER_HOUR, 3600):
                    logger.warning('Rate limit exceeded (hour) for render user %s', user_id)
                    r.set(f"task_error:{task_id}", 
                          f"⚠️ Превышен лимит рендеринга ({RATE_LIMIT_RENDER_PER_USER_HOUR}/час). Попробуйте позже.",
                          ex=REDIS_TTL)
                    return
                
                if not check_rate_limit(user_id, "render_day", RATE_LIMIT_RENDER_PER_USER_DAY, 86400):
                    logger.warning('Rate limit exceeded (day) for render user %s', user_id)
                    r.set(f"task_error:{task_id}",
                          f"⚠️ Превышен дневной лимит рендеринга ({RATE_LIMIT_RENDER_PER_USER_DAY}/день).",
                          ex=REDIS_TTL)
                    return
            except (ValueError, TypeError):
                pass  # assignee_chat_id не является числом - пропускаем rate limit
    except Exception:
        logger.exception('Failed to verify task assignee or rate limit for task %s', task_id)

    html = build_html(task_obj)

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=['--no-sandbox', '--disable-setuid-sandbox'])
        # create a context with higher device scale for sharper screenshots
        context = await browser.new_context(viewport={'width': 1024, 'height': 1200}, device_scale_factor=2)
        page = await context.new_page()
        try:
            await page.set_content(html, wait_until='networkidle')
            # Wait for KaTeX auto-render to finish (if present)
            try:
                await page.wait_for_selector('.katex', timeout=3000)
            except Exception:
                pass
            # Give KaTeX a moment to render as a fallback
            try:
                await page.evaluate('''() => { if (window.renderMathInElement) { return true; } }''')
            except Exception:
                pass

            # Previously we used page height to decide PNG vs PDF. Switch to PDF-only
            # by default because PDF generation is faster and matches requirements.
            # Keep PNG generation code below for future use (left as reference).

            logger.info('Rendering task %s as PDF (forced)', task_id)
            pdf_bytes = await page.pdf(format='A4', print_background=True)
            
            # Проверка размера файла
            file_size = len(pdf_bytes)
            logger.info('Generated PDF size for task %s: %d bytes (%.2f MB)', task_id, file_size, file_size / 1024 / 1024)
            
            if file_size > MAX_FILE_SIZE:
                logger.warning('PDF too large for task %s: %d bytes (max %d)', task_id, file_size, MAX_FILE_SIZE)
                
                if not S3_BUCKET:
                    # Нет S3 - сохраняем ошибку и уведомляем пользователя
                    error_msg = f"⚠️ Файл слишком большой ({file_size//1024//1024}MB, максимум {MAX_FILE_SIZE_MB}MB). S3 не настроен."
                    r.set(f"task_error:{task_id}", error_msg, ex=REDIS_TTL)
                    
                    # Попытка уведомить пользователя
                    try:
                        assignee = r.get(f"task_assignee:{task_id}")
                        if assignee and TELEGRAM_TOKEN:
                            chat_id = assignee.decode('utf-8') if isinstance(assignee, bytes) else str(assignee)
                            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                            requests.post(url, data={'chat_id': chat_id, 'text': error_msg}, timeout=10)
                    except Exception:
                        logger.exception('Failed to notify user about file size error')
                    return
                
                # Есть S3 - сохраняем только туда (не в Redis)
                logger.info('File too large, saving to S3 only for task %s', task_id)
                key = f"renders/{task_id}.pdf"
                url = upload_to_s3(pdf_bytes, key, 'application/pdf')
                if url:
                    r.set(f"task_pdf_url:{task_id}", url, ex=REDIS_TTL)
                    await notify_user_with_file(task_id, pdf_bytes, is_pdf=True)
                else:
                    error_msg = f"⚠️ Не удалось загрузить большой файл в S3"
                    r.set(f"task_error:{task_id}", error_msg, ex=REDIS_TTL)
                return
            
            # Файл в пределах лимита - сохраняем как обычно
            if S3_BUCKET:
                key = f"renders/{task_id}.pdf"
                url = upload_to_s3(pdf_bytes, key, 'application/pdf')
                if url:
                    r.set(f"task_pdf_url:{task_id}", url, ex=REDIS_TTL)
                else:
                    r.set(f"task_pdf:{task_id}", base64.b64encode(pdf_bytes).decode('ascii'), ex=REDIS_TTL)
            else:
                r.set(f"task_pdf:{task_id}", base64.b64encode(pdf_bytes).decode('ascii'), ex=REDIS_TTL)
            await notify_user_with_file(task_id, pdf_bytes, is_pdf=True)

        except Exception:
            logger.exception('Render failed for %s', task_id)
        finally:
            try:
                await page.close()
            except Exception:
                pass
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass

async def notify_user_with_file(task_id: str, file_bytes: bytes, is_pdf: bool):
    # Try to send the generated file to the assignee via Telegram Bot API
    # Проверяем — это решение (hf_render:*) или задание
    is_solution = task_id.startswith('hf_render:')
    
    try:
        assignee = r.get(f"task_assignee:{task_id}")
        if assignee:
            chat_id = assignee.decode('utf-8') if isinstance(assignee, bytes) else str(assignee)
            
            # Если это решение — очистить флаг активной задачи пользователя
            if is_solution:
                try:
                    # Используем r_str (decode_responses=True) для совместимости с my_bot.py
                    r_str.delete(f"user_active_hf:{chat_id}")
                    logger.info('Cleared active task flag for user %s (via render_service)', chat_id)
                except Exception:
                    logger.exception('Failed to clear user active task flag')
            
            if TELEGRAM_TOKEN:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/"
                if is_pdf:
                    api = url + 'sendDocument'
                    files = {'document': (f'task_{task_id}.pdf', file_bytes, 'application/pdf')}
                else:
                    api = url + 'sendPhoto'
                    files = {'photo': (f'task_{task_id}.png', file_bytes, 'image/png')}
                data = {'chat_id': chat_id}
                
                # Inline кнопки только для заданий, не для решений
                if not is_solution:
                    try:
                        import json as _json
                        reply_markup = {
                            'inline_keyboard': [[
                                {'text': 'Решить', 'callback_data': f'solve:{task_id}'},
                                {'text': 'Удалить', 'callback_data': f'del:{task_id}'}
                            ]]
                        }
                        data['reply_markup'] = _json.dumps(reply_markup, ensure_ascii=False)
                    except Exception:
                        logger.exception('Failed to build reply_markup')
                
                resp = requests.post(api, data=data, files=files, timeout=30)
                if resp.status_code // 100 != 2:
                    logger.warning('Telegram send failed: %s %s', resp.status_code, resp.text)
                else:
                    logger.info('Sent rendered file to user %s for task %s', chat_id, task_id)
                    # Если это решение — отправить сообщение "Задача решена"
                    if is_solution:
                        try:
                            requests.post(url + 'sendMessage', data={
                                'chat_id': chat_id,
                                'text': '✅ Задача решена! 👆👁',
                                'parse_mode': 'HTML'
                            }, timeout=10)
                        except Exception:
                            logger.exception('Failed to send completion message')
            else:
                logger.info('TELEGRAM_TOKEN not set; skipping send to user %s for task %s', assignee, task_id)
        else:
            logger.info('No assignee for task %s; skipping direct send', task_id)
    except Exception:
        logger.exception('Failed to notify user for task %s', task_id)

async def worker_loop():
    logger.info('Render worker started, waiting for tasks...')
    while True:
        try:
            # BRPOP returns tuple (queue, value) or None on timeout
            item = r.brpop('render_queue', timeout=5)
            if not item:
                await asyncio.sleep(0.1)
                continue
            _, task_id = item
            if isinstance(task_id, bytes):
                task_id = task_id.decode('utf-8')
            logger.info('Got task %s from queue', task_id)
            # mark pending -> handled
            r.set(f"task_pending:{task_id}", '1', ex=REDIS_TTL)
            await render_task(task_id)
            # mark ready
            r.set(f"task_ready:{task_id}", '1', ex=REDIS_TTL)
            r.delete(f"task_pending:{task_id}")
        except Exception:
            logger.exception('Worker loop error')
            await asyncio.sleep(1)

async def start_services():
    # Ensure Playwright browsers are installed when running container
    logger.info('Starting render service')

    # start background worker
    worker_task = asyncio.create_task(worker_loop())

    # small HTTP server for Render health checks and basic status
    async def health(request):
        return web.Response(text='OK')

    app = web.Application()
    # serve local static files (e.g. KaTeX resources) from ./static
    static_path = os.path.abspath('./static')
    try:
        if not os.path.exists(static_path):
            os.makedirs(static_path, exist_ok=True)
            logger.info('Created missing static directory at %s', static_path)
    except Exception:
        logger.exception('Failed to ensure static directory exists: %s', static_path)

    if not os.path.isdir(static_path):
        logger.warning('Static path is not a directory, skipping static route: %s', static_path)
    else:
        app.router.add_static('/static/', path=static_path, show_index=False)
    app.add_routes([web.get('/', health), web.get('/health', health)])

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', os.environ.get('HTTP_PORT', '8080')))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info('HTTP server listening on port %s', port)

    try:
        # keep main alive while worker runs
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        logger.info('Shutdown requested')
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except Exception:
            pass
        await runner.cleanup()

def main():
    asyncio.run(start_services())

if __name__ == '__main__':
    main()
