# Mnema

Автопротокол созвонов Zoom: «Mnema помнит, кто что сказал».

Mnema подключается к встречам Zoom веб‑клиентом (Playwright/Chromium), пишет
микс WebRTC‑аудио, распознаёт речь (внешний ASR/LLM), определяет говорящих
**только по голосу** (диаризация + голосовой реестр) и формирует протокол
(md/docx/json) с полной аудио‑выгрузкой. Единственный интерфейс управления —
веб‑панель (`api`).

Внешние ASR (`gigastt`, OpenAI‑совместимый `POST /v1/audio/transcriptions`) и
LLM (`ollama`) **не деплоятся** Mnema — подключение по URL
(`ASR_BASE_URL` / `LLM_BASE_URL`). Без них система деградирует корректно:
fallback faster‑whisper в worker, протокол замирает на стенограмме
(`awaiting_llm`).

## Возможности

- автоподключение к запланированным встречам Zoom по расписанию (импорт `.ics`),
- запись микса с ack‑протоколом и **salvage**‑восстановлением после обрывов —
  приоритет «звук записывается всегда»,
- VAD → диаризация (pyannote) → ASR по сегментам → эмбеддинги → матчинг с
  реестром голосов; уточнение нераспознанных говорящих оператором,
- стенограмма с коалесценцией реплик и разделением непрерывной записи на
  несколько совещаний (LLM‑детекция границ),
- LLM‑протокол: сводки по темам, решения, задачи с дедлайнами; вывод
  md/docx/json + плеер mp3 с переходом по реплике,
- загрузка архивных аудио/видео записей (чанки с возобновлением) — полный
  цикл анализа как у живых встреч,
- HTML‑уведомления о сбоях (со скриншотом), готовности протокола и т. п.,
- опциональное SMB‑зеркало результатов (`dd.mm.yy/<Топик>.mp3 + протокол`),
- распознавание говорящих исключительно по голосу — web‑микс не содержит
  имён участников.

## Архитектура

4 контейнера, 2 своих образа:

```
api      FastAPI + Jinja2/htmx + APScheduler (планировщик, maintenance)   :8000
worker   RQ: очереди asr / speaker / structure (тот же python-образ)
web-bot  Node 20 + Playwright + Chromium, запись webm/wav/mp3,
         headful под Xvfb, экраны через noVNC                             :8080-8100
redis    очередь RQ + локи                                                :6379
db       не контейнер: SQLite на volume /data (Alembic; Postgres = смена DATABASE_URL)
```

Worker потребляет три RQ‑очереди (`WORKER_QUEUES=asr,speaker,structure`,
`WORKER_CONCURRENCY` процессов + встроенный RQScheduler для depends_on).
Планировщик встреч и maintenance — APScheduler внутри `api`.

Экраны браузера web‑бота доступны через noVNC: slot 0 (порт 8080) — экран
настройки/логина Zoom, slot 1…N (8081+, `VNC_MAX_SLOTS`) — экраны живых встреч
(кнопка «Экран» на странице встречи). Нужны для ручного прохода reCAPTCHA и
контроля записи. При публикации в недоверенную сеть — `VNC_PASSWORD`
и/или `VNC_PUBLISH_BIND=127.0.0.1:`.

Все LLM‑анализы (протокол и детекция границ) сериализованы FIFO‑локом в
redis — одновременно к LLM идёт ровно один запрос.

## Быстрый старт (dev, CPU)

```bash
cp .env.example .env         # заполните [R]-ключи: ZOOM_PROFILE_DIR, SMTP_HOST, NOTIFY_EMAIL_TO, ARCHIVE_DIR
docker compose up -d --build # 4 контейнера
# первый login/perelogin Zoom-сессии (headful, окно доступно по noVNC :8080):
make login                   # = HEADFUL=1 docker compose run --rm web-bot
# панель: http://localhost:8000, noVNC-настройка: http://localhost:8080/vnc.html
```

## Быстрый старт (GPU)

Тот же стек, но `worker` собирается на CUDA‑образе
(`docker/Dockerfile.python.gpu`, torch + cu124) и считает
диаризацию/ASR/эмбеддинги на NVIDIA GPU:

```bash
cp .env.example .env
make gpu-up        # = docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
make gpu-logs      # в логе: "pyannote: device=cuda:0 ..."
make gpu-down
```

Требования к хосту: драйвер NVIDIA (CUDA ≥12.4) +
`nvidia-container-toolkit`. `api`/`web-bot` остаются на лёгком CPU‑образе.
Если CUDA недоступна, worker молча откатывается на CPU.

Ключевые переменные: `ASR_DEVICE=cuda:0`, `WHISPER_COMPUTE_TYPE=float16`
(ASR ~×2), `DIAR_FP16`, `DIAR_BATCH_SIZE` (разгон диаризации с потерей
качества кластеризации — по умолчанию fp32). pyannote‑модели gated — нужен
`HF_TOKEN`.

## Конвейер обработки

```
запись микса (webm) -> ffmpeg -> WAV + mp3 -> VAD -> диаризация
-> ASR по сегментам (внешний STT; fallback faster-whisper) -> эмбеддинги
кластеров -> матчинг с голосовым реестром -> (пороги) -> уточнение
оператора -> накопление профилей -> стенограмма (коалесценция реплик)
-> разделение встреч (LLM-детекция границ) -> LLM-протокол
```

Порядок «уточнение людей → анализ»: LLM‑протокол строится **после** того,
как все говорящие определены. При нераспознанных голосах Job останавливается
в `awaiting_review` (дедлайн `REVIEW_TIMEOUT_HOURS`), после закрытия уточнений
обработка продолжается.

Состояния Job: `scheduled → launching → recording → processing →
[awaiting_review?] → [awaiting_llm?] → done | failed`. Для архивных загрузок:
`scheduled → uploading → processing → …`. Из `failed` возможен возврат в
`processing` (перезапуск) и `uploading` (salvage‑досыл аудио). Перезапуск LLM
из `done`/`awaiting_llm` — кнопка в UI и `POST /jobs/{id}/rerun-llm`.

### Надёжность записи

- **ack‑протокол чанков**: страница хранит чанки в памяти, пока Node не
  запишет их на диск и не подтвердит; ENOSPC/IO не теряет аудио;
- disk‑guard (`MIN_FREE_DISK_MB`), мониторинг здоровья MediaRecorder/AudioContext
  с оживлением (`recorder_revived`);
- **salvage**: web‑бот дособирает сырые сегменты с диска (в т. ч. orphan'ы
  после краха) в финальные файлы и досылает через `POST .../finished`;
  watchdog `stalled_recording_watch` поднимает зависшие в `recording`;
- EBML‑склейка сегментов без потерь на битых швах;
- ручной безопасный выход из «бессмертной» встречи: `POST /api/jobs/{id}/confirm-end`
  присылает письмо со ссылками «Завершить» / «Посмотреть экран».

### Разделение встреч

Одна запись может содержать несколько совещаний подряд. После стенограммы LLM
ищет границы между ними; при уверенности ≥ `SPLIT_MIN_CONFIDENCE` хвосты
переезжают в дочерние Job'ы (`source=split`), аудио режется, каждая часть
получает свой протокол. Kill‑switch `SPLIT_MEETINGS=0`. Решение фиксируется в
`jobs.split_json` — повторный запуск не делит повторно.

## Управление

- **Веб‑панель**: дашборд, встречи/расписание (импорт `.ics`, пресеты ссылок
  Zoom с паролями), детали Job (event‑log, стадии, аудио, экран VNC, выход из
  встречи, rerun LLM), протоколы (плеер с переходом по реплике), очередь
  уточнений, реестр голосов (регистрация с микрофона, прослушивание эталонов
  и кластеров, переверификация), редактирование сегментов.
- **REST**: полное описание — `docs/api.md`; Swagger — `GET /docs`.
- **Уведомления** — HTML‑письма: failure + скриншот, `protocol_ready`,
  `awaiting_llm`, `confirm_end`, relogin‑напоминания, `job_export_failed`;
  cooldown и дайджест настраиваются.

## Конфигурация

Все ключи — в `.env` или переменных окружения; полный самодокументированный список —
`.env.example` (генерируется из реестра `mnema/core/registry.py`:
`python -m mnema.core.registry > .env.example`, `make example`). Классы
ключей: `[R]` required, `[D]` defaulted, `[C:*]` capability. CI‑тест ловит
дрейф реестра и примера; на старте сервисов — preflight `[R]`‑ключей.

Capabilities (деградация без внешних сервисов):

- `LLM_BASE_URL` пуст → запись и стенограмма делаются, Job → `awaiting_llm` + письмо;
- `ASR_BASE_URL` пуст → fallback faster‑whisper в worker;
- задан, но не отвечает → `misconfigured` видно на `/health` и в алерте.

## Загрузка архивных записей

На вкладке «Встречи» — загрузка видео/аудиофайла прошедшей встречи
(mp4/mkv/mov/avi/webm/m4a/mp3/wav/…): чанки с возобновлением
(`POST /api/uploads*`), затем полный цикл анализа как у живых записей.
Лимиты: `UPLOAD_MAX_GB` (8), `UPLOAD_CHUNK_MB` (16).

## Выгрузка в SMB-шару (опционально)

Зеркало результатов: `EXPORT_DIR` (в контейнере `/export`) — хостовый
CIFS‑маунт (`scripts/smb-mount.sh`, обязателен `iocharset=utf8`), bind‑mount в
`worker`/`api`. Структура: `<EXPORT_DIR>/дд.мм.гг/<Топик>.mp3 (+ протокол)`.
Без `EXPORT_DIR`/`EXPORT_HOST_DIR` выгрузка — no‑op. Отказ сети не роняет job:
событие `export_error` + ретраи в maintenance. Реализация: `mnema/core/export.py`.

## Тесты

```bash
python -m venv venv && ./venv/bin/pip install -r requirements.txt && ./venv/bin/pip install pytest
./venv/bin/python -m pytest    # unit + api-integration + offline pipeline smoke (без сети/Zoom/GPU, ~11 мин)
node --test web-bot/test/      # модули web-bot: ack-drain, record guard, salvage, склейка webm, детектор тишины
```

## Deploy на прод-сервер

```bash
git pull && docker compose up -d --build   # миграции: alembic upgrade head (или make migrate)
```

`scripts/deploy-gpu.sh` — эталонный деплой‑скрипт (git‑bundle на сервер +
compose с тремя `-f`: base + gpu + override). Реальные адрес сервера и каталог
задаются в untracked `scripts/deploy-gpu.local.env` (`DEST_SSH`, `DEST_DIR`)
или через переменные окружения. Подробности эксплуатации — `AGENTS.md`.

Персистентное (не в git): `db.sqlite`, `/data/archive`, `/data/zoom-profile`.
Регламент перелогина Zoom‑сессии — `RELOGIN_DUE_DAYS` (напоминание +
ежедневный health‑check). `mkvmerge`, `ffmpeg` — в образе.

## Приватность и безопасность

- Биометрия голоса — только при согласии участников; аудио/протоколы не
  покидают инфраструктуру (кроме транспорта Zoom).
- Пароли встреч не пишутся в логи и письма (`pwd_ref`, секреты вне git).
- Авторизация/роли/TLS веб‑панели **не входят** в объём проекта: доступ —
  только по сетевому периметру. Не выставляйте панель и noVNC
  (порты 8000, 8080‑8100) в недоверенную сеть без reverse-proxy/SSH-туннелей
  и `VNC_PASSWORD`.

## Документация

- `docs/api.md` — REST API
- `AGENTS.md` — заметки по разработке и эксплуатации

## Лицензия

MIT — см. [LICENSE](LICENSE).
