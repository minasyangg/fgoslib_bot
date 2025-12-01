# Отчёт о внесённых изменениях безопасности и оптимизации

**Дата:** 1 декабря 2025  
**Ветка:** dev

## 📋 Резюме

Внесены критические исправления безопасности и оптимизации в три основных сервиса:
- `my_bot.py` - Telegram бот
- `hf_worker.py` - воркер для обработки задач через HuggingFace
- `render_service.py` - сервис рендеринга PDF

---

## 🔒 Изменения безопасности

### 1. ✅ Добавлена проверка владельца задачи (task_assignee)

**Файлы:** `hf_worker.py`, `render_service.py`

**Проблема:** Злоумышленник мог подсунуть чужой task_id в очередь и получить результат чужой задачи.

**Решение:**
- В `hf_worker.py` функция `process_task()` теперь проверяет, что `chat_id` из очереди совпадает с `task_assignee:{task_id}` из Redis
- В `render_service.py` функция `render_task()` проверяет наличие `task_assignee` и применяет rate limiting к владельцу задачи
- При несовпадении задача игнорируется с записью в лог

**Код (hf_worker.py):**
```python
# Проверка владельца задачи (security check)
stored_assignee = r.get(f"task_assignee:{task_id}")
if stored_assignee:
    expected_chat_id = stored_assignee.decode('utf-8') if isinstance(stored_assignee, bytes) else str(stored_assignee)
    if str(chat_id) != expected_chat_id:
        logger.warning('Security: chat_id mismatch for task %s', task_id)
        return  # Игнорируем подозрительную задачу
```

---

### 2. ✅ Реализован Rate Limiting

**Файлы:** `my_bot.py`, `hf_worker.py`, `render_service.py`

**Проблема:** Отсутствовала защита от злоупотреблений со стороны одного пользователя.

**Решение:** Добавлена универсальная функция `check_rate_limit()` во всех трёх сервисах с использованием Redis счётчиков.

**Лимиты по умолчанию:**

| Сервис | Параметр | Лимит/час | Лимит/день |
|--------|----------|-----------|------------|
| Bot | `RATE_LIMIT_TASKS_PER_HOUR` | 10 | 50 |
| HF Worker | `RATE_LIMIT_HF_PER_USER_HOUR` | 5 | 20 |
| Render Service | `RATE_LIMIT_RENDER_PER_USER_HOUR` | 10 | 30 |

**Функция (одинаковая во всех сервисах):**
```python
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
```

**Применение:**
- В `my_bot.py`: проверка в команде `/start` (с аргументом) и `handle_task()`
- В `hf_worker.py`: проверка в `process_task()` перед отправкой в HF
- В `render_service.py`: проверка в `render_task()` перед рендерингом

При превышении лимита пользователь получает уведомление с информацией о лимите.

---

### 3. ✅ Улучшена модерация промптов с регулярными выражениями

**Файл:** `hf_worker.py`

**Проблема:** Примитивный blacklist легко обходился (`b0mb` вместо `bomb`) и давал ложные срабатывания.

**Решение:** Замена простого списка слов на регулярные выражения с границами слов.

**Было:**
```python
BLACKLIST = ['bomb', 'terror', 'drugs', 'sex', ...]

def moderate_prompt(prompt: str) -> bool:
    for w in BLACKLIST:
        if w in low:
            return False
    return True
```

**Стало:**
```python
import re

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
```

**Преимущества:**
- Учитывает вариации символов (`0` вместо `o`, `!` вместо `i`)
- Границы слов `\b` предотвращают ложные срабатывания
- Возвращает причину блокировки для логирования

---

### 4. ✅ Ограничение размера файлов (10MB)

**Файл:** `render_service.py`

**Проблема:** Большие PDF могли заполнить Redis и вызвать OOM.

**Решение:** Проверка размера PDF после генерации.

**Логика:**
1. Если файл > 10MB и S3 не настроен → ошибка пользователю, файл не сохраняется
2. Если файл > 10MB и S3 настроен → сохранение только в S3 (не в Redis)
3. Если файл ≤ 10MB → обычное сохранение (Redis или S3)

**Переменная среды:**
```bash
MAX_FILE_SIZE_MB=10  # По умолчанию
```

**Код:**
```python
MAX_FILE_SIZE_MB = int(os.environ.get('MAX_FILE_SIZE_MB', '10'))
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024

# После генерации PDF
file_size = len(pdf_bytes)
logger.info('Generated PDF size: %d bytes (%.2f MB)', file_size, file_size / 1024 / 1024)

if file_size > MAX_FILE_SIZE:
    logger.warning('PDF too large: %d bytes (max %d)', file_size, MAX_FILE_SIZE)
    
    if not S3_BUCKET:
        error_msg = f"⚠️ Файл слишком большой ({file_size//1024//1024}MB, максимум {MAX_FILE_SIZE_MB}MB). S3 не настроен."
        r.set(f"task_error:{task_id}", error_msg, ex=REDIS_TTL)
        # Уведомление пользователю
        return
    
    # Сохранение только в S3 для больших файлов
```

---

## 🧹 Удалён неиспользуемый код

### my_bot.py
**Удалено:**
- ❌ Функция `call_hf_api()` - устаревшая, теперь через очередь
- ❌ Функция `handle_prompt()` - команда `/prompt` не используется
- ❌ Функции `update_prompt()` и `update_format()` - не вызывались
- ❌ Переменные `HF_API_URL`, `HF_TOKEN` - не нужны в боте
- ❌ Переменные `BROWSER`, `BROWSER_LOCK` - рендеринг вынесен в отдельный сервис
- ❌ Импорты `jinja2.Template`, `urllib.parse`, `tempfile` - не использовались

### render_service.py
**Удалено:**
- ❌ Закомментированный код генерации PNG (19 строк) - решено использовать только PDF

---

## 📄 Новые файлы

### .env.example
Создан файл с примерами всех переменных окружения и рекомендуемыми значениями:

```bash
# Rate Limiting Settings
RATE_LIMIT_TASKS_PER_HOUR=10
RATE_LIMIT_TASKS_PER_DAY=50
RATE_LIMIT_HF_PER_USER_HOUR=5
RATE_LIMIT_HF_PER_USER_DAY=20
RATE_LIMIT_RENDER_PER_USER_HOUR=10
RATE_LIMIT_RENDER_PER_USER_DAY=30

# File Size Limits
MAX_FILE_SIZE_MB=10
MAX_IMAGES=5
MAX_PROMPT_LEN=1000
```

---

## 🔄 Изменённые функции

### my_bot.py
1. **start()** - добавлена проверка rate limit перед обработкой задачи
2. **handle_task()** - добавлена проверка rate limit перед сохранением локальной задачи

### hf_worker.py
1. **process_task()** - добавлены:
   - Проверка task_assignee (безопасность)
   - Rate limiting
   - Улучшенная модерация с возвратом причины
2. **moderate_prompt()** - переработана с regex, теперь возвращает tuple

### render_service.py
1. **render_task()** - добавлены:
   - Проверка task_assignee и rate limiting
   - Проверка размера файла
   - Логика сохранения больших файлов только в S3

---

## ⚙️ Новые переменные окружения

| Переменная | Сервис | Значение по умолчанию | Описание |
|------------|--------|----------------------|----------|
| `RATE_LIMIT_TASKS_PER_HOUR` | Bot | 10 | Лимит задач/час для бота |
| `RATE_LIMIT_TASKS_PER_DAY` | Bot | 50 | Лимит задач/день для бота |
| `RATE_LIMIT_HF_PER_USER_HOUR` | HF Worker | 5 | Лимит HF запросов/час |
| `RATE_LIMIT_HF_PER_USER_DAY` | HF Worker | 20 | Лимит HF запросов/день |
| `RATE_LIMIT_RENDER_PER_USER_HOUR` | Render | 10 | Лимит рендеринга/час |
| `RATE_LIMIT_RENDER_PER_USER_DAY` | Render | 30 | Лимит рендеринга/день |
| `MAX_FILE_SIZE_MB` | Render | 10 | Макс размер PDF в MB |

---

## ✅ Проверка совместимости

**Все изменения обратно совместимы:**
- ✅ Новые переменные имеют значения по умолчанию
- ✅ Rate limiting работает в режиме fail-open (при ошибке разрешает запрос)
- ✅ Проверка task_assignee не ломает существующие задачи
- ✅ Взаимодействие между сервисами не изменено

**Требуется для production:**
1. Добавить новые переменные окружения в Render.com/Huggingface
2. Настроить значения rate limits под нагрузку
3. Настроить S3 для работы с большими файлами (опционально)

---

## 📊 Метрики безопасности

**До изменений:**
- ❌ 0 проверок владельца задачи
- ❌ 0 rate limiting
- ❌ Примитивная модерация (8 слов)
- ❌ Неограниченный размер файлов

**После изменений:**
- ✅ 2 проверки task_assignee (HF + Render)
- ✅ 6 rate limit проверок (Bot x2, HF x2, Render x2)
- ✅ 12 regex паттернов модерации
- ✅ Ограничение файлов 10MB

---

## 🚀 Рекомендации по развёртыванию

1. **Тестирование:**
   ```bash
   # Проверить rate limiting
   # Отправить 11 задач подряд от одного пользователя
   # Должно заблокировать после 10-й
   ```

2. **Мониторинг:**
   - Следить за логами `Rate limit exceeded` для выявления злоупотреблений
   - Следить за `Security: chat_id mismatch` для выявления атак
   - Следить за `PDF too large` для оптимизации лимитов

3. **Настройка лимитов:**
   - Для малой нагрузки: текущие значения оптимальны
   - Для высокой нагрузки: увеличить TASKS_PER_DAY до 100-200
   - Для премиум пользователей: реализовать отдельные лимиты по user_id

---

## 📝 Заметки

- Все изменения внесены в ветку `dev`
- Функционал не сломан, добавлены только проверки безопасности
- Код готов к merge в main после тестирования
- .env.example содержит все необходимые переменные
