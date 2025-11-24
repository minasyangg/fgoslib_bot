from flask import Flask, jsonify, send_from_directory, request
import redis
import os
import json

app = Flask(__name__)

# Подключение к Redis
redis_url = os.environ.get("UPSTASH_REDIS_URL")
r = redis.Redis.from_url(redis_url, decode_responses=True)


# Главная страница (монитор)
@app.route('/')
def index():
    return send_from_directory('.', 'monitor.html')


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
    r.set(f"task:{task_id}", json.dumps(obj))
    return jsonify({'status': 'ok', 'task_id': task_id})


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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
