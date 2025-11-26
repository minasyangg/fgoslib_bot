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
REDIS_TTL = int(os.environ.get('REDIS_TTL', '600'))

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')

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

            # --- PNG generation code (kept for future use) ---
            # scroll_height = await page.evaluate('() => document.body.scrollHeight')
            # logger.info('Task %s scrollHeight=%s', task_id, scroll_height)
            # if scroll_height <= 1400:
            #     png_bytes = await page.screenshot(full_page=True, type='png')
            #     if S3_BUCKET:
            #         key = f"renders/{task_id}.png"
            #         url = upload_to_s3(png_bytes, key, 'image/png')
            #         if url:
            #             r.set(f"task_png_url:{task_id}", url, ex=REDIS_TTL)
            #         else:
            #             r.set(f"task_png:{task_id}", base64.b64encode(png_bytes).decode('ascii'), ex=REDIS_TTL)
            #     else:
            #         r.set(f"task_png:{task_id}", base64.b64encode(png_bytes).decode('ascii'), ex=REDIS_TTL)
            #     await notify_user_with_file(task_id, png_bytes, is_pdf=False)

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
    try:
        assignee = r.get(f"task_assignee:{task_id}")
        if assignee:
            chat_id = assignee.decode('utf-8') if isinstance(assignee, bytes) else str(assignee)
            if TELEGRAM_TOKEN:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/"
                if is_pdf:
                    api = url + 'sendDocument'
                    files = {'document': (f'task_{task_id}.pdf', file_bytes, 'application/pdf')}
                else:
                    api = url + 'sendPhoto'
                    files = {'photo': (f'task_{task_id}.png', file_bytes, 'image/png')}
                data = {'chat_id': chat_id}
                resp = requests.post(api, data=data, files=files, timeout=30)
                if resp.status_code // 100 != 2:
                    logger.warning('Telegram send failed: %s %s', resp.status_code, resp.text)
                else:
                    logger.info('Sent rendered file to user %s for task %s', chat_id, task_id)
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
