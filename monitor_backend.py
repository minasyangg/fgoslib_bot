from flask import Flask, jsonify, send_from_directory, request, render_template_string
import requests
import redis
import os
import json

app = Flask(__name__)

# Config: TTL for created keys (seconds)
REDIS_TTL = int(os.environ.get('REDIS_TTL', '600'))

# Resolve bot username from TELEGRAM_TOKEN for redirects
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
BOT_USERNAME = None
if TELEGRAM_TOKEN:
    try:
        resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=5)
        resp.raise_for_status()
        BOT_USERNAME = resp.json().get('result', {}).get('username')
    except Exception:
        BOT_USERNAME = None

# Подключение к Redis
redis_url = os.environ.get("UPSTASH_REDIS_URL")
r = redis.Redis.from_url(redis_url, decode_responses=True)


# Главная страница (монитор)
INDEX_HTML = """
<!doctype html>
<html>
    <head>
        <meta charset="utf-8" />
        <title>Monitor — Task simulator</title>
        <style>
            body{font-family: Arial, Helvetica, sans-serif; margin:24px}
            .task{border:1px solid #ddd;padding:12px;margin:8px 0;border-radius:6px}
            .task a{color:#0b69ff;text-decoration:none}
            .small{font-size:0.9em;color:#666}
            .controls{margin-top:12px}
        </style>
    </head>
    <body>
        <h1>Monitor — Task simulator</h1>
        <p class="small">Click a simulated task to emulate Hugo behaviour: it will create a `task:&lt;id&gt;` in Redis, enqueue a render job, and redirect you to the Telegram bot with <code>?start=&lt;id&gt;</code>.</p>

        {% for tid, meta in tasks.items() %}
            <div class="task">
                <strong>Task id: {{ tid }}</strong>
                <div>
                    {% if meta.images and meta.images|length > 0 %}
                        <img src="{{ meta.images[0] }}" alt="thumb" style="height:90px;float:right;margin-left:12px;border:1px solid #eee;padding:4px;background:#fff;border-radius:4px"/>
                    {% endif %}
                    <div>{{ meta.text }}</div>
                </div>
                <div style="clear:both" class="controls">
                    <a href="#" data-task="{{ tid }}" data-text="{{ meta.text|e }}" class="open">Open in bot (simulate)</a>
                </div>
            </div>
        {% endfor %}

        <hr/>
        <div style="margin:10px 0">
            <button id="clear-all" style="background:#ef4444;color:#fff;border:none;padding:8px 12px;border-radius:6px;cursor:pointer">Clear Redis (tasks, queue, logs)</button>
            <button id="clear-logs" style="margin-left:8px;padding:8px 12px;border-radius:6px;cursor:pointer">Clear Bot Logs</button>
        </div>
        <h3>Custom task</h3>
        <form id="custom">
            <label>Task id (eg. task-60-27-4): <input name="task_id" value="task-60-99-1"/></label>
            <br/>
            <label>Text: <input name="text" value="Симулированное задание" style="width:60%"/></label>
            <br/>
            <button>Simulate click</button>
        </form>

        <script>
            async function simulate(task_id, text){
                const url = '/simulate_click';
                const resp = await fetch(url, {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({task_id, text})
                });
                if(!resp.ok){
                    alert('Failed to create task: '+resp.statusText);
                    return;
                }
                const j = await resp.json();
                if(j.redirect){
                    window.location = j.redirect;
                } else if(j.tg_start_url){
                    window.location = j.tg_start_url;
                } else {
                    alert('OK — task created.');
                }
            }

            document.querySelectorAll('.open').forEach(el=>{
                el.addEventListener('click', async (ev)=>{
                    ev.preventDefault();
                    const tid = el.dataset.task;
                    const text = el.dataset.text || ('Симулированное задание '+tid);
                    await simulate(tid, text);
                });
            });

            document.getElementById('clear-logs').addEventListener('click', async ()=>{
                if(!confirm('Clear bot logs?')) return;
                const r = await fetch('/clear_logs', {method:'POST'});
                if(r.ok) alert('Bot logs cleared'); else alert('Failed');
            });

            document.getElementById('clear-all').addEventListener('click', async ()=>{
                if(!confirm('This will delete temporary task keys, render queue and bot logs in Redis. Continue?')) return;
                const r = await fetch('/clear_all', {method:'POST'});
                if(!r.ok){ alert('Failed to clear: '+r.statusText); return; }
                const j = await r.json();
                alert('Cleared '+(j.deleted||0)+' keys');
            });

            document.getElementById('custom').addEventListener('submit', async (ev)=>{
                ev.preventDefault();
                const form = ev.currentTarget;
                const fd = new FormData(form);
                const id = fd.get('task_id');
                const text = fd.get('text');
                await simulate(id, text);
            });
        </script>
    </body>
</html>
"""


@app.route('/')
def index():
    # Try to populate monitor with real tasks from index.json (if present)
    tasks = {}
    try:
        here = os.path.dirname(__file__)
        idx_path = os.path.join(here, 'index.json')
        if os.path.exists(idx_path):
            with open(idx_path, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
            # collect up to N tasks that have at least one named task or interesting text
            N = 8
            added = 0
            import base64

            def make_svg_data_url(title, page):
                # simple SVG thumbnail with page and title (kept small)
                s = f"""<svg xmlns='http://www.w3.org/2000/svg' width='360' height='180'>
  <rect width='100%' height='100%' fill='#f7f9fc' stroke='#ddd'/> 
  <text x='12' y='28' font-size='18' fill='#1f2937'>Страница {page}</text>
  <text x='12' y='56' font-size='14' fill='#374151'>""" + (title.replace('&','&amp;').replace('<','&lt;')[:140]) + """</text>
</svg>"""
                b = s.encode('utf-8')
                return 'data:image/svg+xml;base64,' + base64.b64encode(b).decode('ascii')

            for entry in data:
                if added >= N:
                    break
                page = entry.get('page')
                tasks_list = entry.get('tasks') or []
                # if there are explicit tasks, add each as a separate simulated task
                if tasks_list:
                    for idx, tlabel in enumerate(tasks_list[:2]):
                        if added >= N:
                            break
                        tid = f"hannova-p{page}-{idx+1}"
                        text = f"{tlabel} — {entry.get('title','').strip()}"
                        thumb = make_svg_data_url(entry.get('title',''), page)
                        tasks[tid] = {'text': text, 'images': [thumb]}
                        added += 1
                else:
                    # if no explicit tasks, still add the page as sample once
                    if added < N:
                        tid = f"hannova-p{page}-pg"
                        text = f"{entry.get('title','Страница')} — {entry.get('workbook','')}"
                        thumb = make_svg_data_url(entry.get('title',''), page)
                        tasks[tid] = {'text': text, 'images': [thumb]}
                        added += 1
    except Exception:
        tasks = {
            'task-60-27-4': {'text': 'Пример задачи 60.27.4', 'images': []},
            'task-60-27-5': {'text': 'Пример задачи 60.27.5', 'images': []}
        }

    # Fallback minimal tasks if index.json missing or empty
    if not tasks:
        tasks = {
            'task-60-27-4': {'text': 'Пример задачи 60.27.4', 'images': []},
            'task-60-27-5': {'text': 'Пример задачи 60.27.5', 'images': []}
        }

    return render_template_string(INDEX_HTML, tasks=tasks)


# Получить последние 100 логов
@app.route('/logs')
def get_logs():
    logs = r.lrange('bot_logs', -100, -1)
    logs = [json.loads(log) for log in logs]
    return jsonify(logs)


# Создать тестовую задачу в Redis: POST {"task_id":"123","task_text":"...","images":[]}
@app.route('/create_task', methods=['POST'])
def create_task():
    # Поддерживаем JSON или multipart/form-data (с файлами)
    task_id = None
    task_text = ''
    images = []

    if request.content_type and request.content_type.startswith('multipart/form-data'):
        # form fields
        task_id = request.form.get('task_id') or None
        task_text = request.form.get('task_text', '')
        prompt = request.form.get('prompt', '')
        out_format = request.form.get('format', 'md')
        real_id = request.form.get('real_id', '')
        # файлы
        files = request.files.getlist('images')
        for f in files:
            try:
                data = f.read()
                import base64
                b64 = base64.b64encode(data).decode('ascii')
                mime = f.content_type or 'application/octet-stream'
                data_url = f"data:{mime};base64,{b64}"
                images.append(data_url)
            except Exception:
                continue
    else:
        try:
            data = request.get_json(force=True)
        except Exception:
            return jsonify({'error': 'invalid json'}), 400
        task_id = data.get('task_id')
        task_text = data.get('task_text', '')
        images = data.get('images', [])
        prompt = data.get('prompt', '')
        out_format = data.get('format', 'md')
        real_id = data.get('real_id', '')

    if not task_id:
        return jsonify({'error': 'task_id required'}), 400

    obj = {
        'task_text': task_text,
        'images': images,
        'prompt': prompt,
        'format': out_format,
        'real_id': real_id
    }
    r.set(f"task:{task_id}", json.dumps(obj), ex=REDIS_TTL)
    return jsonify({'status': 'ok', 'task_id': task_id})


@app.route('/simulate_click', methods=['POST'])
def simulate_click():
    data = request.get_json() or {}
    task_id = data.get('task_id')
    text = data.get('text') or f"Симулированное задание {task_id}"
    if not task_id:
        return jsonify({'error': 'task_id required'}), 400

    task_obj = {
        'task_text': text,
        'images': [],
        'prompt': '',
        'format': 'png',
        'real_id': task_id
    }
    try:
        r.set(f"task:{task_id}", json.dumps(task_obj), ex=REDIS_TTL)
        r.lpush('render_queue', task_id)
        r.set(f"task_pending:{task_id}", '1', ex=REDIS_TTL)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    if BOT_USERNAME:
        tg_url = f"https://t.me/{BOT_USERNAME}?start={task_id}"
        return jsonify({'redirect': tg_url})

    return jsonify({'tg_start_url': f"https://t.me/?start={task_id}"})


# Получить тестовую задачу (для проверки)
@app.route('/task/<task_id>')
def get_task(task_id):
    raw = r.get(f"task:{task_id}") or r.get(task_id)
    if not raw:
        return jsonify({'error': 'not found'}), 404
    try:
        return jsonify(json.loads(raw))
    except Exception:
        return jsonify({'raw': raw})


@app.route('/task_image/<task_id>/<int:idx>')
def task_image(task_id, idx):
    """Return image binary for task images stored as data URLs or external URLs."""
    raw = r.get(f"task:{task_id}") or r.get(task_id)
    if not raw:
        return jsonify({'error': 'not found'}), 404
    try:
        task_obj = json.loads(raw)
    except Exception:
        return jsonify({'error': 'invalid task data'}), 400
    images = task_obj.get('images', [])
    if idx < 0 or idx >= len(images):
        return jsonify({'error': 'index out of range'}), 404
    img = images[idx]
    # If it's a data URL, decode and return
    if isinstance(img, str) and img.startswith('data:'):
        try:
            header, b64 = img.split(',', 1)
            # header like data:image/png;base64
            mime = header.split(':', 1)[1].split(';', 1)[0]
            import base64
            data = base64.b64decode(b64)
            from flask import Response
            return Response(data, mimetype=mime)
        except Exception:
            return jsonify({'error': 'invalid data url'}), 400
    # If it's an HTTP(S) URL, redirect
    if isinstance(img, str) and (img.startswith('http://') or img.startswith('https://')):
        from flask import redirect
        return redirect(img)
    # Fallback: return text/plain
    return jsonify({'raw': img})

# Очистить все логи
@app.route('/clear_logs', methods=['POST'])
def clear_logs():
    r.delete('bot_logs')
    return jsonify({'status': 'ok'})


@app.route('/clear_all', methods=['POST'])
def clear_all():
    """Delete known temporary keys used by the monitor/bot/render pipeline.
    This intentionally targets only keys/patterns we use (not arbitrary keys).
    """
    patterns = [
        'task:*',
        'task_png:*',
        'task_pdf:*',
        'task_png_url:*',
        'task_pdf_url:*',
        'task_pending:*',
        'task_ready:*',
        'task_assignee:*',
    ]
    deleted = []
    # collect keys from patterns
    try:
        for p in patterns:
            try:
                found = r.keys(p)
            except Exception:
                found = []
            if found:
                try:
                    r.delete(*found)
                except Exception:
                    for k in found:
                        try:
                            r.delete(k)
                        except Exception:
                            pass
                deleted.extend(found)
        # remove render_queue list and bot_logs
        for single in ('render_queue', 'bot_logs'):
            try:
                if r.exists(single):
                    r.delete(single)
                    deleted.append(single)
            except Exception:
                pass
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'deleted': len(deleted), 'keys': deleted[:200]})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
