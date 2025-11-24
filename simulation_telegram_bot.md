# Simulation / Test: Telegram Bot Redirect

Цель: дать простой способ локально или в staging протестировать поведение фронтенда и кнопки, которые после сохранения задания открывают Telegram-бота с параметром `start=<taskId>`.

Кратко: фронтенд сохраняет задачу (POST в `/.netlify/functions/task`) и получает `taskId`. Затем сайт открывает ссылку вида `https://t.me/<BOT_USERNAME>?start=<taskId>` или редиректит на неё с сервера бота.

Что здесь описано:
- минимальная статическая страница для ручного теста (`test-telegram.html`),
- пример простого серверного редиректа (Express) для Render/Heroku/другого хоста,
- необходимые переменные окружения и поведение ожидаемого flow.

Prerequisites / Переменные окружения
- `HUGO_BOT_USERNAME` — имя бота без `@` (помещается в шаблон Hugo как `HUGO_...`).
- `UPSTASH_REDIS_REST_URL` и `UPSTASH_REDIS_REST_TOKEN` — для функции `task`.
- (Опционально) `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — если хотите, чтобы сервер форвардил уведомления в Telegram.

1) Быстрый статический тест (локально)

- Создайте файл `test-telegram.html` и поместите в него ссылку вида:

  ```html
  <!doctype html>
  <meta charset="utf-8">
  <title>Test Telegram Open</title>
  <body>
    <h3>Test Telegram</h3>
    <p>Replace <code>BOT_USERNAME</code> and <code>TASK_ID</code> below.</p>
    <a id="open" href="https://t.me/BOT_USERNAME?start=TASK_ID" target="_blank">Open bot with TASK_ID</a>
  </body>
  </html>
  ```

- Откройте файл в браузере и проверьте, что при клике открывается Telegram (веб или десктоп-клиент).

2) Интеграция с фронтендом (что проверять)

- Убедитесь, что фронтенд делает ровно один POST к `/.netlify/functions/task` и получает JSON с полем `taskId`.
- После получения `taskId` фронтенд должен выполнить `window.open('https://t.me/' + BOT_USERNAME + '?start=' + taskId)`.
- Если бот не открывается — проверьте, генерируется ли корректный `taskId` и нет ли блокировок попапов в браузере.

3) Пример простого серверного redirect (Express)

```js
// server.js (Express)
const express = require('express');
const app = express();
const BOT = process.env.BOT_USERNAME; // без @

app.get('/go-to-bot', (req, res) => {
  const taskId = req.query.taskId || '';
  const turl = `https://t.me/${BOT}?start=${encodeURIComponent(taskId)}`;
  res.redirect(302, turl);
});

app.listen(process.env.PORT || 3000);
```

- Разверните этот endpoint (например на Render). Фронтенд может открывать `https://your-bot-host/go-to-bot?taskId=<taskId>` вместо прямой ссылки на `t.me`.

4) Ожидаемое поведение и проверки

- Один клик → один POST → один `taskId` → один открытый бот с тем же `taskId`.
- Чтобы избежать дублей, фронтенд должен генерировать `clientId` и прикреплять его к payload; сервер использует `clientId` для идемпотентности.

5) Отладка
- Проверьте Network → XHR: убедитесь, что POST к `/.netlify/functions/task` возвращает JSON `{ ok: true, taskId: "..." }`.
- Если возвращается другой taskId при повторных нажатиях — убедитесь, что `clientId` отправляется и что сервер поддерживает `SET NX` (Upstash) или fallback GET/SET.

Контакт: этот README даёт минимальную инструкцию для ручного тестирования открытия Telegram-бота с `taskId`.

Если нужно — могу добавить сам файл `static/test-telegram.html` в репо или закоммитить пример сервера в отдельной папке. Сейчас только README, как вы просили.
