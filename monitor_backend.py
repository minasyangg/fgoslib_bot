from flask import Flask, jsonify
import redis
import os
import json

app = Flask(__name__)

# Подключение к Redis
redis_url = os.environ.get("UPSTASH_REDIS_URL")
r = redis.Redis.from_url(redis_url, decode_responses=True)

# Получить последние 100 логов
@app.route('/logs')
def get_logs():
    logs = r.lrange('bot_logs', -100, -1)
    logs = [json.loads(log) for log in logs]
    return jsonify(logs)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
