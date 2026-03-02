# 🚀 Scout Web — Инструкция по сборке и запуску

## Структура проекта

```
scout-web/
├── backend/               # FastAPI (Python)
│   ├── main.py            # Точка входа + WebSocket
│   ├── routers/
│   │   ├── search.py      # POST /api/search/flights
│   │   ├── deals.py       # GET  /api/deals/hot
│   │   ├── tracker.py     # CRUD /api/tracker
│   │   └── auth.py        # Сессии
│   ├── services/
│   │   ├── chat_handler.py  # Логика FSM чата (из вашего бота)
│   │   └── flight_search.py # Скопировать из бота как есть
│   ├── utils/
│   │   ├── redis_client.py  # Адаптирован из бота
│   │   ├── cities_loader.py # Скопировать из бота как есть
│   │   ├── cities.py        # Скопировать из бота как есть
│   │   ├── link_converter.py# Скопировать из бота как есть
│   │   └── logger.py        # Скопировать из бота как есть
│   ├── requirements.txt
│   └── .env.example
│
└── frontend/              # React + Vite
    ├── src/
    │   ├── App.jsx
    │   ├── main.jsx
    │   ├── components/    # UI-компоненты
    │   ├── hooks/         # useChat, useSession
    │   └── styles/app.css
    ├── index.html
    ├── package.json
    └── vite.config.js
```

---

## 📋 Предварительные требования

| Инструмент | Версия    | Проверить        |
|-----------|-----------|------------------|
| Python    | 3.11+     | `python --version` |
| Node.js   | 20+       | `node --version`   |
| npm       | 9+        | `npm --version`    |
| Redis     | 7+ (опц.) | `redis-cli ping`   |

---

## Шаг 1 — Скопировать файлы из бота

Перед запуском нужно скопировать несколько файлов из вашего существующего Telegram-бота в `backend/`:

```bash
# Из папки с ботом в backend/services/
cp services/flight_search.py    scout-web/backend/services/
cp services/flystack_client.py  scout-web/backend/services/
cp services/transfer_search.py  scout-web/backend/services/
cp services/price_watcher.py    scout-web/backend/services/

# Из папки с ботом в backend/utils/
cp utils/cities.py        scout-web/backend/utils/
cp utils/cities_loader.py scout-web/backend/utils/
cp utils/link_converter.py scout-web/backend/utils/
cp utils/logger.py        scout-web/backend/utils/
```

> Файлы `redis_client.py` и `chat_handler.py` уже адаптированы для веба — не перезаписывайте их.

---

## Шаг 2 — Настройка Backend

### 2.1. Установить зависимости

```bash
cd scout-web/backend

# Создать виртуальное окружение
python -m venv venv

# Активировать
# macOS / Linux:
source venv/bin/activate
# Windows:
venv\Scripts\activate

# Установить пакеты
pip install -r requirements.txt
```

### 2.2. Создать файл .env

```bash
cp .env.example .env
```

Открыть `.env` и заполнить:

```env
# Из вашего Telegram-бота (те же самые токены!)
AVIASALES_TOKEN=ваш_token
AVIASALES_MARKER=ваш_marker
AVIASALES_HOST=beta.aviasales.ru

FLYSTACK_API_KEY=ваш_ключ

# Redis (рекомендуется, но можно без него)
REDIS_URL=redis://localhost:6379/0

# Где будет работать фронтенд
FRONTEND_URL=http://localhost:5173
```

### 2.3. Запустить Redis (опционально, но желательно)

```bash
# macOS через Homebrew
brew install redis && brew services start redis

# Linux (Ubuntu/Debian)
sudo apt install redis-server && sudo systemctl start redis

# Windows — использовать WSL или Docker:
docker run -d -p 6379:6379 redis:alpine
```

### 2.4. Запустить сервер

```bash
# Из папки backend/ (с активным venv)
uvicorn main:app --reload --port 8000
```

Проверить: откройте http://localhost:8000/docs — должна открыться Swagger документация API.

---

## Шаг 3 — Настройка Frontend

### 3.1. Установить зависимости

```bash
cd scout-web/frontend
npm install
```

### 3.2. Переменные окружения (опционально)

Создайте `frontend/.env.local` только если backend работает НЕ на localhost:

```env
# Нужно только если backend на другом хосте
VITE_WS_URL=ws://ваш-сервер:8000
```

При разработке на одной машине — ничего менять не нужно, Vite автоматически проксирует `/api` и `/ws` на порт 8000.

### 3.3. Запустить dev-сервер

```bash
npm run dev
```

Откройте http://localhost:5173 — приложение готово!

---

## Шаг 4 — Проверка

После запуска обоих серверов:

1. Откройте http://localhost:5173
2. В чате введите: `Москва — Сочи`
3. Введите дату: `15.04`
4. Выберите: `Только туда`
5. Выберите тип рейса и пассажиров
6. Дождитесь результатов (real-time поиск занимает 5–15 секунд)

---

## Шаг 5 — Сборка для продакшена

### Backend (на сервере)

```bash
cd backend

# Без --reload, с несколькими воркерами
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
```

Или через `gunicorn` (для production):

```bash
pip install gunicorn
gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000
```

### Frontend (статика)

```bash
cd frontend
npm run build
# Готовые файлы в папке frontend/dist/
```

Отдать папку `dist/` через Nginx или разместить на Vercel / Netlify.

---

## Пример конфига Nginx (продакшен)

```nginx
server {
    listen 80;
    server_name ваш-домен.ru;

    # Фронтенд (статика)
    location / {
        root /var/www/scout-web/dist;
        try_files $uri $uri/ /index.html;
    }

    # API
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
    }

    # WebSocket
    location /ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

---

## 🐳 Docker (альтернатива)

```yaml
# docker-compose.yml
version: '3.9'
services:
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]

  backend:
    build: ./backend
    ports: ["8000:8000"]
    env_file: ./backend/.env
    depends_on: [redis]

  frontend:
    build: ./frontend
    ports: ["5173:80"]
    depends_on: [backend]
```

Запуск:
```bash
docker-compose up --build
```

---

## ❓ Частые ошибки

| Ошибка | Решение |
|--------|---------|
| `AVIASALES_TOKEN не задан` | Заполнить .env, перезапустить uvicorn |
| `CORS error` в браузере | Проверить `FRONTEND_URL` в .env |
| WebSocket не подключается | Backend должен быть запущен до Frontend |
| `ModuleNotFoundError: cities_loader` | Скопировать файлы из шага 1 |
| Redis ошибка подключения | Запустить Redis или убрать `REDIS_URL` из .env |

---

## 📞 Ключевые различия от Telegram-бота

| Аспект | Telegram бот | Web версия |
|--------|-------------|-----------|
| Транспорт | aiogram polling | WebSocket + REST |
| Состояние FSM | aiogram FSM + Redis | dict в памяти + Redis |
| Пользователи | chat_id | session UUID в cookie |
| Уведомления | Telegram push | (в планах: Web Push) |
| Деплой | VPS + systemd | VPS / Vercel + Docker |

Все токены Aviasales и FlyStack — те же самые, что в боте. Менять ничего не нужно.
