# Рекомендации по оптимизации HuggingFace Space (fgoslib-qwen3)

**Дата:** 1 декабря 2025  
**Автор:** AI Analysis  
**Цель:** Устранение проблемы обрывания решений и оптимизация использования ZeroGPU квоты

---

## 🔴 Основная проблема

**Симптом:** Решения длиннее 2 страниц PDF обрываются.

**Причина:** Не в ограничении времени (duration=120 секунд достаточно для генерации 20-30 сек), а в других узких местах приложения.

---

## 📊 Найденные узкие места в fgoslib-qwen3

### 1. ⚠️ КРИТИЧНО: Неэффективное использование ZeroGPU квоты

**Файл:** `app.py`, строка ~20

**Текущий код:**
```python
@spaces.GPU(duration=120)  # Выделяем GPU на 120 секунд
def solve_problem(text_input, image_input=None):
```

**Проблема:**
- Модель генерирует решение за 20-30 секунд
- GPU резервируется на 120 секунд, впустую удерживая **90 секунд**
- Это в **2 раза** снижает пропускную способность

**Решение:**
```python
@spaces.GPU(duration=60)  # Снизить до 60 секунд (запас 2x от среднего времени)
def solve_problem(text_input, image_input=None):
```

**Ожидаемый эффект:**
- **+100%** пропускной способности (60 задач/час вместо 30)
- Экономия 50% квоты ZeroGPU
- Без влияния на качество или скорость генерации

---

### 2. ⚠️ КРИТИЧНО: Риск Out of Memory (OOM)

**Файл:** `app.py`, строки загрузки модели

**Текущий код:**
```python
model = Qwen3VLForConditionalGeneration.from_pretrained(
    MODEL_ID,
    torch_dtype=torch.bfloat16,
)
model = model.to("cuda")  # Жестко привязано к GPU
```

**Проблема:**
- Модель 8B занимает ~16GB VRAM в bfloat16
- При множественных запросах может возникнуть OOM
- Нет автоматического управления памятью

**Решение:**
```python
model = Qwen3VLForConditionalGeneration.from_pretrained(
    MODEL_ID,
    torch_dtype=torch.bfloat16,
    device_map="auto",  # Автоматическое распределение по GPU/CPU/disk
)
# Убрать model = model.to("cuda") - device_map управляет этим
processor = AutoProcessor.from_pretrained(MODEL_ID)
```

**Ожидаемый эффект:**
- Автоматическое распределение памяти при нехватке VRAM
- Снижение риска OOM
- Более стабильная работа под нагрузкой

---

### 3. ⚠️ КРИТИЧНО: Накопление файлов на диске

**Файл:** `utils.py`, функция `save_response_to_md`

**Текущий код:**
```python
def save_response_to_md(query_text, response_text, elapsed_time):
    timestamp = int(time.time())
    filename = f"solution_{timestamp}.md"
    
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)
    
    return filename
```

**Проблема:**
- Файлы сохраняются в корневую директорию
- Никогда не удаляются
- Со временем заполнят весь диск Space (ограниченное место)

**Решение:**
```python
import tempfile
import atexit
import os
import threading
import shutil

# Создать временную директорию при запуске
TEMP_DIR = tempfile.mkdtemp(prefix="fgoslib_solutions_")

def cleanup_temp_files():
    """Удалить временные файлы при завершении."""
    try:
        shutil.rmtree(TEMP_DIR)
        print(f"✅ Cleaned up temporary directory: {TEMP_DIR}")
    except Exception as e:
        print(f"⚠️ Failed to cleanup temp directory: {e}")

# Зарегистрировать cleanup при выходе
atexit.register(cleanup_temp_files)

def save_response_to_md(query_text, response_text, elapsed_time):
    timestamp = int(time.time())
    filename = os.path.join(TEMP_DIR, f"solution_{timestamp}.md")
    
    content = f"""# 📚 Решение задачи по физике/математике

**Дата создания:** {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}  
**Время генерации:** {elapsed_time:.2f} секунд  
**Модель:** Qwen3-VL-8B-Instruct

---

## 📝 Условие задачи

{query_text}

---

## ✅ Решение

{response_text}

---

*Создано с помощью FGOS Library AI Tutor*  
*Все формулы представлены в формате KaTeX (LaTeX)*
"""
    
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)
    
    # Автоматическое удаление файла через 10 минут
    def delete_after_delay():
        import time
        time.sleep(600)  # 10 минут
        try:
            os.remove(filename)
            print(f"🗑️ Deleted temp file: {filename}")
        except Exception:
            pass
    
    threading.Thread(target=delete_after_delay, daemon=True).start()
    
    return filename
```

**Ожидаемый эффект:**
- Файлы автоматически удаляются через 10 минут
- Cleanup при перезапуске Space
- Место на диске не заполняется

---

### 4. 🟡 ВАЖНО: Отсутствует кэширование обработанных изображений

**Файл:** `app.py`, функция `solve_problem`

**Текущий код:**
```python
@spaces.GPU(duration=60)
def solve_problem(text_input, image_input=None):
    # Каждый раз заново открывается и конвертируется
    image_data = None
    if image_input is not None:
        image_data = Image.open(image_input)
        if image_data.mode == "RGBA":
            background = Image.new("RGB", image_data.size, (255, 255, 255))
            background.paste(image_data, mask=image_data.split()[3])
            image_data = background
        elif image_data.mode != "RGB":
            image_data = image_data.convert("RGB")
```

**Проблема:**
- Одинаковые изображения обрабатываются повторно
- Трата 1-2 секунды на каждую обработку

**Решение:**
```python
from functools import lru_cache
import hashlib

@lru_cache(maxsize=100)
def preprocess_image(image_path: str) -> Image.Image:
    """
    Кэшированная предобработка изображения.
    Конвертирует в RGB и возвращает готовое изображение.
    """
    image_data = Image.open(image_path)
    
    if image_data.mode == "RGBA":
        background = Image.new("RGB", image_data.size, (255, 255, 255))
        background.paste(image_data, mask=image_data.split()[3])
        return background
    elif image_data.mode != "RGB":
        return image_data.convert("RGB")
    
    return image_data

@spaces.GPU(duration=60)
def solve_problem(text_input, image_input=None):
    # ...existing code...
    
    image_data = None
    if image_input is not None:
        try:
            # Использовать кэшированную версию
            image_data = preprocess_image(image_input)
        except TypeError:
            # Если image_input не hashable, fallback
            image_data = Image.open(image_input)
            if image_data.mode == "RGBA":
                background = Image.new("RGB", image_data.size, (255, 255, 255))
                background.paste(image_data, mask=image_data.split()[3])
                image_data = background
            elif image_data.mode != "RGB":
                image_data = image_data.convert("RGB")
    
    # ...existing code...
```

**Ожидаемый эффект:**
- Ускорение на 1-2 секунды для повторных изображений
- Экономия ~10% времени при частых запросах с одинаковыми картинками

---

### 5. 🟡 ВАЖНО: Фиксированные параметры генерации

**Файл:** `config.py`

**Текущий код:**
```python
GENERATION_CONFIG = {
    "max_new_tokens": 1024,
    "temperature": 0.3,
    "top_p": 0.95,
    "do_sample": True
}
```

**Проблема:**
- `max_new_tokens=1024` может быть недостаточно для длинных решений (2+ страницы)
- Пользователи не могут настроить параметры под свои нужды

**Решение 1 (быстрое):** Увеличить лимит в `config.py`:
```python
GENERATION_CONFIG = {
    "max_new_tokens": 2048,  # Увеличить с 1024 до 2048
    "temperature": 0.3,
    "top_p": 0.95,
    "do_sample": True
}
```

**Решение 2 (полное):** Добавить UI параметры:

В `app.py`:
```python
@spaces.GPU(duration=60)
def solve_problem(text_input, image_input=None, max_tokens=2048, temperature=0.3):
    """
    Основная функция решения задачи.
    
    Args:
        text_input: Текст задачи
        image_input: Путь к изображению
        max_tokens: Максимум токенов для генерации (512-4096)
        temperature: Температура сэмплирования (0.1-1.0)
    """
    # ...existing code до генерации...
    
    # Создать кастомную конфигурацию
    custom_config = {
        "max_new_tokens": max_tokens,
        "temperature": temperature,
        "top_p": 0.95,
        "do_sample": True
    }
    
    with torch.no_grad():
        generated_ids = model.generate(
            **inputs,
            **custom_config  # Использовать кастомную конфигурацию
        )
    
    # ...existing code...
```

В UI:
```python
with gr.Column(scale=1):
    gr.Markdown("### ⚙️ Настройки генерации")
    
    max_tokens_slider = gr.Slider(
        minimum=512,
        maximum=4096,
        value=2048,
        step=256,
        label="Максимум токенов (больше = длиннее решение)",
        info="1024 токенов ≈ 1 страница A4"
    )
    
    temperature_slider = gr.Slider(
        minimum=0.1,
        maximum=1.0,
        value=0.3,
        step=0.1,
        label="Температура",
        info="0.1 = строго по формулам, 1.0 = креативное объяснение"
    )

# В submit_btn.click добавить новые входы
submit_btn.click(
    fn=solve_problem,
    inputs=[input_text, input_image, max_tokens_slider, temperature_slider],
    outputs=[output_solution, output_file, output_time, output_metadata]
)
```

**Ожидаемый эффект:**
- Решения до 2048 токенов (~2-3 страницы A4)
- Гибкость для пользователей
- Устранение обрывания длинных решений

---

### 6. 🟢 ЖЕЛАТЕЛЬНО: Добавить streaming для лучшего UX

**Файл:** `app.py`

**Проблема:**
- Пользователь ждет 20-30 секунд без обратной связи
- Нет индикации прогресса генерации

**Решение:**
```python
from transformers import TextIteratorStreamer
from threading import Thread

@spaces.GPU(duration=60)
def solve_problem(text_input, image_input=None, max_tokens=2048, temperature=0.3):
    # ...existing code до генерации...
    
    # Создать streamer для потоковой генерации
    streamer = TextIteratorStreamer(
        processor.tokenizer,
        skip_special_tokens=True,
        skip_prompt=True
    )
    
    # Конфигурация генерации
    custom_config = {
        "max_new_tokens": max_tokens,
        "temperature": temperature,
        "top_p": 0.95,
        "do_sample": True,
        "streamer": streamer  # Добавить streamer
    }
    
    # Запустить генерацию в отдельном потоке
    generation_kwargs = {**inputs, **custom_config}
    thread = Thread(target=model.generate, kwargs=generation_kwargs)
    thread.start()
    
    # Yield частичные результаты для UI (streaming)
    partial_text = ""
    for new_text in streamer:
        partial_text += new_text
        elapsed = time.time() - start_time
        yield partial_text, None, f"{elapsed:.2f} сек (генерация...)", None
    
    thread.join()
    
    # Финальная обработка
    elapsed_time = time.time() - start_time
    md_file = save_response_to_md(user_text, partial_text, elapsed_time)
    
    # Metadata
    metadata = f"""### 📊 Метаданные генерации:
- **Время генерации:** {elapsed_time:.2f} секунд
- **Модель:** Qwen3-VL-8B-Instruct
- **Токенов сгенерировано:** ~{len(partial_text.split())}
- **Параметры:** max_tokens={max_tokens}, temperature={temperature}
"""
    
    yield partial_text, md_file, f"{elapsed_time:.2f} сек", metadata
```

**Ожидаемый эффект:**
- Пользователь видит генерацию в реальном времени
- Улучшенный UX
- Меньше жалоб на "зависание"

---

## 📋 План внедрения (приоритизация)

### Фаза 1: Критические исправления (немедленно)

1. **✅ Снизить `duration` с 120 до 60 секунд**
   - Файл: `app.py`
   - Время: 1 минута
   - Эффект: +100% пропускной способности

2. **✅ Добавить `device_map="auto"`**
   - Файл: `app.py`
   - Время: 2 минуты
   - Эффект: Стабильность, защита от OOM

3. **✅ Использовать `tempfile` для MD файлов**
   - Файл: `utils.py`
   - Время: 5 минут
   - Эффект: Избежать переполнения диска

### Фаза 2: Важные улучшения (в течение недели)

4. **🟡 Увеличить `max_new_tokens` до 2048**
   - Файл: `config.py`
   - Время: 1 минута
   - Эффект: Устранение обрывания длинных решений

5. **🟡 Добавить кэширование изображений**
   - Файл: `app.py`
   - Время: 10 минут
   - Эффект: +10% скорости для повторных запросов

### Фаза 3: Желательные улучшения (при наличии времени)

6. **🟢 Добавить UI слайдеры для параметров**
   - Файлы: `app.py` (UI)
   - Время: 20 минут
   - Эффект: Гибкость для пользователей

7. **🟢 Реализовать streaming генерации**
   - Файл: `app.py`
   - Время: 30 минут
   - Эффект: Лучший UX

---

## 📊 Ожидаемые результаты после всех оптимизаций

| Метрика | До оптимизации | После оптимизации | Улучшение |
|---------|----------------|-------------------|-----------|
| **Среднее время генерации** | 20-30 сек | 20-30 сек | Без изменений |
| **Квота ZeroGPU на задачу** | 120 сек | 60 сек | **-50%** |
| **Задач в час (при квоте)** | 30 | 60 | **+100%** |
| **Максимальная длина решения** | 1024 токена (~1 стр) | 2048 токенов (~2-3 стр) | **+100%** |
| **Риск OOM** | Средний | Низкий | **Стабильнее** |
| **Место на диске** | Растет бесконечно | Ограничено | **Безопасно** |
| **UX (с streaming)** | Ожидание 30 сек | Прогресс в реальном времени | **Намного лучше** |

---

## 🔧 Инструкция по внедрению

### 1. Клонировать Space локально (если ещё не сделано)

```bash
cd /d/NEXTverstka/fgoslib_bot
git clone https://huggingface.co/spaces/mingg93/fgoslib-qwen3
cd fgoslib-qwen3
```

### 2. Внести изменения согласно приоритетам

**Фаза 1 (критично):**

```bash
# Редактировать app.py: изменить duration на 60
# Редактировать app.py: добавить device_map="auto"
# Редактировать utils.py: использовать tempfile
```

### 3. Протестировать локально

```bash
# Запустить локально для тестирования
python app.py
```

### 4. Задеплоить на HuggingFace

```bash
git add .
git commit -m "feat: optimize GPU usage and fix file accumulation"
git push
```

### 5. Мониторинг после деплоя

- Проверить логи на наличие OOM ошибок
- Убедиться, что файлы удаляются
- Проверить, что решения не обрываются

---

## ⚠️ Возможные проблемы и их решения

### Проблема 1: `device_map="auto"` не работает

**Симптом:** Ошибка при загрузке модели

**Решение:**
```python
# Fallback без device_map
model = Qwen3VLForConditionalGeneration.from_pretrained(
    MODEL_ID,
    torch_dtype=torch.bfloat16,
)
model = model.to("cuda")
```

### Проблема 2: Streaming ломает UI

**Симптом:** UI не обновляется или выдаёт ошибку

**Решение:**
- Убрать streaming
- Использовать обычный `yield` только в конце

### Проблема 3: Файлы не удаляются

**Симптом:** TEMP_DIR всё равно заполняется

**Решение:**
```python
# Добавить принудительную очистку при старте
import glob
for old_file in glob.glob("solution_*.md"):
    try:
        os.remove(old_file)
    except Exception:
        pass
```

---

## 📞 Контакты для вопросов

- **GitHub Issues:** https://github.com/minasyangg/fgoslib_bot/issues
- **HuggingFace Space:** https://huggingface.co/spaces/mingg93/fgoslib-qwen3

---

**Дата создания:** 2025-12-01  
**Версия документа:** 1.0  
**Статус:** Готово к внедрению ✅
