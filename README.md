# Mnema

Автопротокол созвонов Zoom («Mnema помнит, кто что сказал»). Реализация ТЗ v1.13.

Подключается к встречам Zoom веб‑клиентом (Playwright/headless Chromium), пишет
микс WebRTC‑аудио, распознаёт речь (внешний STT/LLM на AI‑сервере компании),
определяет говорящих **только по голосу**, формирует протокол (md/docx/json) с
полным аудио‑выгрузком. Единственный интерфейс управления — веб‑панель (`api`).

## Архитектура (6.4) — 5 сервисов, 2 своих образа

```
api (FastAPI + Jinja2/htmx + APScheduler)  :8000
worker (RQ: asr/speaker/structure/archive, тот же python-образ)
web-bot (Node 20 + Playwright + Chromium, запись webm/wav/mp3)
redis (:6379)
db  (SQLite на /data, Alembic; Postgres = смена DATABASE_URL)
```

Внешние (не деплоятся Mnema, подключение по URL): `gigastt` (ASR) и `ollama`
(LLM) на AI‑сервере компании — `ASR_BASE_URL` / `LLM_BASE_URL`.

## Быстрый старт (dev)

```bash
cp .env.example .env         # заполните [R]-ключи: ZOOM_PROFILE_DIR, SMTP_HOST, NOTIFY_EMAIL_TO, ARCHIVE_DIR
docker compose up -d --build # 5 сервисов
# первый login/perelogin Zoom-сессии (headful):
HEADFUL=1 docker compose run --rm web-bot
# панель: http://localhost:8000
```

## Конвейер (FT‑34)

```
запись микса (webm) -> ffmpeg -> WAV + mp3 выгрузки (FT-41) -> VAD -> диаризация
-> ASR по сегментам -> эмбеддинги кластеров -> матчинг с голосовым реестром
-> (пороги) -> уточнение оператора (ФТ-28) -> накопление профилей -> LLM-протокол
```

Состояния Job (НТ‑2): `scheduled → launching → recording → processing →
[awaiting_llm?] → [awaiting_review?] → done | failed`. Для архивных загрузок:
`scheduled → uploading → processing → …`. Processing идемпотентен.

## Загрузка архивных записей совещаний

На вкладке «Встречи» — карточка «Загрузить архивную запись совещания»:
тема, дата прошедшей встречи, видео/аудиофайл (mp4/mkv/mov/avi/webm/m4a/mp3/wav/…).
Создаётся Job `source=upload` (без Zoom), запускается **полный цикл анализа**:
извлечение аудиодорожки (ffmpeg), VAD, диаризация, ASR, распознавание людей по
реестру голосов, стенограмма и LLM‑протокол — как у живых записей.

- Загрузка **чанками с возобновлением** (оборванный файл догружается с последней
  принятой позиции): `POST /api/uploads`, `POST /api/uploads/{id}/chunk?offset=`,
  `GET /api/uploads/{id}/offset`, `POST .../finalize`, `GET /api/jobs/{id}/uploads/active`.
- После finalize оригинал лежит в `/data/jobs/<id>/raw.<ext>`; из него worker
  делает `meeting.wav/mp3` — аудиодорожка доступна теми же эндпоинтами, что у
  записанных встреч: `GET /api/jobs/{id}/audio?format=mp3|wav` (Range, плеер на
  странице встречи), оригинал — `GET /api/jobs/{id}/source`.
- Лимит `UPLOAD_MAX_GB` (8), размер чанка `UPLOAD_CHUNK_MB` (16);
  `KEEP_UPLOADED_SOURCE=0` — удалять исходник после извлечения дорожек.
  Незавершённые загрузки старше 24 ч чистит maintenance.

## Управление

- Веб: дашборд (capabilities llm/asr, health Zoom‑сессии), встречи/расписание
  (+ импорт `.ics`), детали Job (event‑log, скачивание аудио), протоколы
  (плеер mp3 с переходом по реплике), очередь уточнений, реестр голосов
  (регистрация с микрофона браузера), редактирование сегментов.
- REST: `GET/POST/DELETE /api/jobs`, `POST /api/uploads*` (архив‑загрузки),
  `GET /api/protocols/{id}` (`?format=md|docx|json`),
  `GET /api/protocols/{id}/audio?format=mp3|wav|webm` (Range), `PATCH .../segments`,
  `GET/POST /api/reviews`, `GET/POST/DELETE /api/voice-profiles`, `GET /health`.

## Тесты

```bash
python -m venv venv && ./venv/bin/pip install -r requirements.txt && ./venv/bin/pip install pytest
./venv/bin/python -m pytest    # unit + api-integration + offline pipeline smoke (без сети/Zoom/GPU)
```

Проверка конфига (ФТ‑37…40): `.env.example` генерируется из реестра ключей
(`python -m mnema.core.registry > .env.example`); CI‑тест ловит дрейф; на старте
сервисов — preflight `[R]`‑ключей.

## Первичная настройка AI‑сервисов ( capabilities, ФТ‑39)

- `LLM_BASE_URL` пуст → `llm: not_configured`: запись + стенограмма
  (`steno.md`) делаются, Job → `awaiting_llm` + письмо; донастройка LLM + повтор
  processing собирают полный протокол.
- `ASR_BASE_URL` пуст → `asr: not_configured`: fallback faster‑whisper в worker.
- задан, но не отвечает → `misconfigured` видно на `/health` и в алерте.

## Приватность / безопасность (границы v1)

Биометрия голоса — только при согласии (ФТ‑31); аудио/протоколы не покидают
инфраструктуру (кроме транспорта Zoom) (ФТ‑16). Авторизация/роли/TLS веб‑панели
— **вне объёма v1** (доступ по сетевому периметру, решение пользователя, §13).
Пароли встреч не пишутся в логи и письма; web‑микс не содержит имён участников —
имена только из голосового реестра.

## Развёртывание на prod‑сервере (10.1)

Тот же репозиторий, код не меняется — только `.env`:

```bash
git pull && docker compose up -d --build   # alembic‑миграции на старте api
```

Персистентное (не в git): `db.sqlite`, `/data/archive`, `/data/zoom-profile`.
Регламент перелогина Zoom‑сессии — `RELOGIN_DUE_DAYS` (напоминание + ежедневный
health‑check `SESSION_HEALTHCRON`). `mkvmerge`, `ffmpeg` — в образе.

