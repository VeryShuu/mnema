# Mnema

Автопротокол созвонов Zoom («Mnema помнит, кто что сказал»). Реализация ТЗ v1.13.

Подключается к встречам Zoom веб‑клиентом (Playwright/Chromium), пишет микс
WebRTC‑аудио, распознаёт речь (внешний STT/LLM на AI‑сервере компании),
определяет говорящих **только по голосу**, формирует протокол (md/docx/json) с
полным аудио‑выгрузком. Единственный интерфейс управления — веб‑панель (`api`).

## Архитектура (6.4) — 4 контейнера, 2 своих образа

```
api (FastAPI + Jinja2/htmx + APScheduler)                     :8000
worker (RQ: очереди asr/speaker/structure, тот же python-образ)
web-bot (Node 20 + Playwright + Chromium, запись webm/wav/mp3,
         headful под Xvfb + noVNC-экраны)                     :8080-8100
redis (:6379)
db  — не контейнер: SQLite на volume /data (Alembic; Postgres = смена DATABASE_URL)
```

Внешние (не деплоятся Mnema, подключение по URL): `gigastt` (ASR, OpenAI‑совместимый
`POST /v1/audio/transcriptions`) и `ollama` (LLM) на AI‑сервере компании —
`ASR_BASE_URL` / `LLM_BASE_URL`.

Worker потребляет три RQ‑очереди (`WORKER_QUEUES=asr,speaker,structure`,
`WORKER_CONCURRENCY` процессов + встроенный RQScheduler для depends_on).
Планировщик встреч и maintenance — APScheduler внутри `api`.

Экраны браузера web‑бота доступны через noVNC: slot 0 (порт 8080) — экран
настройки/логина Zoom, slot 1…N (8081+, `VNC_MAX_SLOTS`) — экраны живых встреч
(кнопка «Экран» на странице встречи). Нужны для ручного прохода reCAPTCHA и
контроля записи (§12). При публикации в недоверенную сеть — `VNC_PASSWORD`
и/или `VNC_PUBLISH_BIND=127.0.0.1:`.

## Быстрый старт (dev)

```bash
cp .env.example .env         # заполните [R]-ключи: ZOOM_PROFILE_DIR, SMTP_HOST, NOTIFY_EMAIL_TO, ARCHIVE_DIR
docker compose up -d --build # 4 контейнера
# первый login/perelogin Zoom-сессии (headful, окно доступно по noVNC :8080):
make login                   # = HEADFUL=1 docker compose run --rm web-bot
# панель: http://localhost:8000, noVNC-настройка: http://localhost:8080/vnc.html
```

## Быстрый старт (GPU-версия, v2)

Второй вариант деплоя: тот же стек, но `worker` собирается на CUDA‑образе
(`docker/Dockerfile.python.gpu`, torch 2.4.1+cu124) и считает диаризацию/ASR
на NVIDIA GPU. faster‑whisper работает в `float16` на GPU
(`WHISPER_COMPUTE_TYPE`, override в gpu‑compose). **Диаризация pyannote на GPU
включена в fp32 (`DIAR_FP16=0`)**: в fp16 эмбеддинги склеивают разных говорящих
в один кластер — качество диаризации важнее скорости. Переменные `DIAR_FP16=1`
и `DIAR_BATCH_SIZE=32` включают half precision и батчи сегментации/эмбеддингов.

Требования к хосту: драйвер NVIDIA (CUDA ≥12.4) + `nvidia-container-toolkit`
(`sudo apt install nvidia-container-toolkit && sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker`).

```bash
cp .env.example .env
make gpu-up        # = docker compose -f ...gpu.yml up -d --build (worker: ASR_DEVICE=cuda:0)
make gpu-logs      # в логе: "pyannote: device=cuda:0 fp16=False batch=32"
make gpu-down
```

`api`/`web-bot` остаются на лёгком CPU‑образе — GPU нужен только worker.
Если в worker'е CUDA недоступна, код молча откатывается на CPU (проверка
`torch.cuda.is_available()` в `diarize._get_pyannote`).

### GPU-версия v2: сводка изменений

Мотивация: диаризация и распознавание на CPU‑варианте занимают ~5 минут на
тестовых данных. В v2 тяжёлые инференс‑шаги (диаризация pyannote, ASR
faster‑whisper, эмбеддинги спикеров) перенесены на NVIDIA GPU.

| Что | Где | Эффект |
|---|---|---|
| CUDA‑образ workera: nvidia/cuda 12.4 + Python 3.12 (venv) + torch 2.4.1+cu124, ffmpeg/mkvtoolnix | `docker/Dockerfile.python.gpu` | inference на GPU вместо CPU |
| Override‑файл: worker на GPU‑образ, `ASR_DEVICE=cuda:0`, `deploy.resources.devices` (driver nvidia), `DIAR_FP16=0`, `WHISPER_COMPUTE_TYPE=float16` | `docker-compose.gpu.yml` | изолированный GPU‑деплой, api/web-bot не меняются |
| fp16 диаризации через `torch.autocast` + авто‑откат на fp32 при ошибке (по умолчанию выкл. — качество) | `mnema/core/diarize.py` (`_run_pipeline`, `_fp16_enabled`) | тюнинг: `DIAR_FP16=1` даёт ~2x |
| Батчи сегментации/эмбеддингов pyannote (после загрузки пайплайна — `from_pretrained` их не принимает) | `mnema/core/diarize.py` (`_get_pyannote`) | выше загрузка GPU, ~2–3x на диаризации |
| Кэш `Inference` модели эмбеддингов по устройству (раньше модель грузилась заново на каждый срез кластера) | `mnema/core/speaker.py` (`_get_inference`) | speaker‑шаг: минус ~20 c × кол‑во кластеров |
| faster‑whisper: `compute_type=float16` на GPU / `int8` на CPU | `mnema/core/asr_client.py` (`_whisper_compute_type`) | ASR fallback ~2x быстрее на GPU |
| Ключи конфигурации (в реестре и `.env.example`) | `WHISPER_COMPUTE_TYPE`, `DIAR_FP16`, `DIAR_BATCH_SIZE` | тюнинг/отключение fp16 и батчей без пересборки |
| Makefile: `gpu-build` / `gpu-up` / `gpu-down` / `gpu-logs` | `Makefile` | `-f docker-compose.yml -f docker-compose.gpu.yml` |

Ожидаемый совокупный эффект на типичном датацентровом GPU (T4/A10):
ASR fallback ~2x, диаризация — при включённом fp16/батчах до ~5–10x (профилирование
на реальных данных — задача деплоя; CPU‑фолбэк‑тест test_pipeline подтверждает
эквивалентность результатов).

Ограничения/заметки:
- CUDA образ тяжелее CPU (~6–8 ГБ слоёв из‑за wheels torch cu124).
- pyannote gated модели по‑прежнему требуют `HF_TOKEN`.
- fp16 включается только когда `ASR_DEVICE=cuda*`; на CPU остаётся fp32 +
  whisper `int8` (тоже быстрее float32).
- Диаризация не «ускоряется до нуля»: clustering (AgglomerativeClustering) —
  CPU‑часть пайплайна, на GPU не переносится (незначительно при обычном
  числе спикеров).

## Конвейер (FT‑34)

```
запись микса (webm) -> ffmpeg -> WAV + mp3 выгрузки (FT-41) -> VAD -> диаризация
-> ASR по сегментам (gigastt verbose_json с восстановлением пунктуации;
   fallback faster-whisper) -> эмбеддинги кластеров -> матчинг с голосовым
реестром -> (пороги) -> уточнение оператора (ФТ-28) -> накопление профилей
-> стенограмма (коалесценция реплик, COALESCE_MAX_GAP_S) -> разделение встреч
(LLM-детекция границ, см. ниже) -> LLM-протокол
```

Состояния Job (НТ‑2): `scheduled → launching → recording → processing →
[awaiting_llm?] → [awaiting_review?] → done | failed`. Для архивных загрузок:
`scheduled → uploading → processing → …`. Пере-запуск LLM из `done`/`awaiting_llm`
(`→ processing`) — кнопка в UI и `POST /jobs/{id}/rerun-llm`. Processing
идемпотентен.

## Разделение встреч (этап split)

Одна запись (непрерывный созвон) может содержать несколько совещаний подряд.
После стенограммы и уточнения голосов LLM ищет границы между совещаниями
(`SPLIT_SYSTEM_PROMPT` в `mnema/core/llm_client.py`); граница прищёлкивается
к паузе между сегментами VAD. При уверенности ≥ `SPLIT_MIN_CONFIDENCE` хвосты
переезжают в дочерние Job'ы (`source=split`, `parent_job_id`), время сегментов
пересчитывается от 0, аудио режется от полной копии `meeting-full.*`
(создаётся всегда перед усечением). Каждая часть далее обрабатывается
независимо: свой протокол, аудио, экспорт в самбу, уведомление.

Параметры (env worker'а): `SPLIT_MEETINGS=1` (kill-switch `0`),
`SPLIT_MIN_MINUTES=20` (короче — не делим), `SPLIT_MIN_CONFIDENCE=0.7`
(порог уверенности LLM), `SPLIT_WINDOW_CHARS=40000` (скользящие окна
детекции с перекрытием 10%). Решение фиксируется в `jobs.split_json`
(границы или причина пропуска) — повторный запуск не делит повторно.
Ограничения v1: потомки повторно не делятся, ручная правка границ — вне рамок.

## Загрузка архивных записей совещаний

На вкладке «Встречи» — карточка «Загрузить архивную запись совещания»:
тема, дата прошедшей встречи, видео/аудиофайл (mp4/mkv/mov/avi/webm/m4a/mp3/wav/…).
Создаётся Job `source=upload` (без Zoom), запускается **полный цикл анализа**:
извлечение аудиодорожки (ffmpeg), VAD, диаризация, ASR, распознавание людей по
реестру голосов, стенограмма и LLM‑протокол — как у живых записей.

- Загрузка **чанками с возобновлением** (оборванный файл догружается с последней
  принятой позиции): `POST /api/uploads`, `POST /api/uploads/{id}/chunk?offset=`,
  `GET /api/uploads/{id}/offset`, `POST .../finalize`, `POST .../cancel`,
  `GET /api/jobs/{id}/uploads/active`.
- После finalize оригинал лежит в `/data/jobs/<id>/raw.<ext>`; из него worker
  делает `meeting.wav/mp3` — аудиодорожка доступна теми же эндпоинтами, что у
  записанных встреч: `GET /api/jobs/{id}/audio?format=mp3|wav` (Range, плеер на
  странице встречи), оригинал — `GET /api/jobs/{id}/source`.
- Лимит `UPLOAD_MAX_GB` (8), размер чанка `UPLOAD_CHUNK_MB` (16);
  `KEEP_UPLOADED_SOURCE=0` — удалять исходник после извлечения дорожек.
  Незавершённые загрузки старше 24 ч чистит maintenance.

## Управление

- Веб: дашборд (capabilities llm/asr, health Zoom‑сессии, индикатор активной
  записи), встречи/расписание (+ импорт `.ics`, пресеты ссылок Zoom с паролями,
  создание/редактирование/переименование/удаление встреч, плановое окончание),
  детали Job (event‑log, стадии обработки, скачивание аудио, экран VNC, выход
  из встречи, rerun LLM), протоколы (плеер mp3 с переходом по реплике), очередь
  уточнений, реестр голосов (регистрация с микрофона браузера, прослушивание
  эталонов и кластеров, редактирование профиля, ручной переверификация),
  редактирование сегментов.
- REST: полное описание — `docs/api.md`; Swagger — `GET /docs`. Основное:
  `GET/POST/DELETE /api/jobs`, `GET /api/jobs/{id}/join-info`,
  `POST /api/uploads*` (архив‑загрузки), `GET /api/protocols/{id}`
  (`?format=md|docx|json`), `GET /api/protocols/{id}/audio?format=mp3|wav|webm`
  (Range), `PATCH .../segments`, `GET/POST /api/reviews`,
  `GET/POST/PATCH/DELETE /api/voice-profiles` (+ `/samples`, `/clusters`,
  `/reverify`), `GET/POST /api/vnc/*`, `GET /health`.
- Уведомления — HTML‑письма (FT‑35/36): failure + скриншот страницы inline,
  `protocol_ready`, `awaiting_llm`, relogin‑напоминания, `job_export_failed`;
  cooldown `ALERT_COOLDOWN_MIN`, дайджест `ALERT_DIGEST_CRON`.

## Выгрузка в самба-шару (SMB-зеркало)

Опциональное зеркало результатов: `EXPORT_DIR` (в контейнере `/export`) — хостовый
CIFS-маунт (`scripts/smb-mount.sh`, обязательный `iocharset=utf8`), bind-mount в
`worker`/`api`. Структура: `<EXPORT_DIR>/дд.мм.гг/<Топик>.mp3 (+ протокол .md/.docx)`.
Без `EXPORT_DIR`/`EXPORT_HOST_DIR` выгрузка — no-op. Отказ сети не роняет job:
SessionEvent `export_error` + ретраи в maintenance (≤5 за проход, после 6 неудач —
уведомление `job_export_failed`). Реализация: `mnema/core/export.py`.

## Тесты

```bash
python -m venv venv && ./venv/bin/pip install -r requirements.txt && ./venv/bin/pip install pytest
./venv/bin/python -m pytest    # unit + api-integration + offline pipeline smoke (без сети/Zoom/GPU)
                               # ~11 мин; на разработку гонять затронутые файлы, см. AGENTS.md
node --test web-bot/test/      # чистые модули web-bot: детектор тишины, ярлыки Zoom (RU/EN)
```

Проверка конфига (ФТ‑37…40): `.env.example` генерируется из реестра ключей
(`python -m mnema.core.registry > .env.example`, `make example`); CI‑тест ловит
дрейф (новый ключ — сначала в `mnema/core/registry.py`); на старте сервисов —
preflight `[R]`‑ключей.

## Первичная настройка AI‑сервисов (capabilities, ФТ‑39)

- `LLM_BASE_URL` пуст → `llm: not_configured`: запись + стенограмма
  (`steno.md`) делаются, Job → `awaiting_llm` + письмо; донастройка LLM + повтор
  processing собирают полный протокол.
- `ASR_BASE_URL` пуст → `asr: not_configured`: fallback faster‑whisper в worker.
- задан, но не отвечает → `misconfigured` видно на `/health` и в алерте.
- `LLM_MODEL_ALLOWLIST` — операторский гейт допустимых моделей.

## Приватность / безопасность (границы v1)

Биометрия голоса — только при согласии (ФТ‑31); аудио/протоколы не покидают
инфраструктуру (кроме транспорта Zoom) (ФТ‑16). Авторизация/роли/TLS веб‑панели
— **вне объёма v1** (доступ по сетевому периметру, решение пользователя, §13).
Пароли встреч не пишутся в логи и письма; web‑микс не содержит имён участников —
имена только из голосового реестра.

## Развёртывание на prod‑сервере (10.1)

Тот же репозиторий, код не меняется — только `.env`:

```bash
git pull && docker compose up -d --build   # миграции: ./venv/bin/alembic upgrade head (или make migrate)
```

GPU‑сервер: `./scripts/deploy-gpu.sh` — пуш/git-bundle в origin, на сервере
`git fetch + reset --hard`, сборка и `up -d --force-recreate api worker web-bot`
со **всеми тремя `-f`** (без `docker-compose.gpu.yml` worker соберётся из
CPU‑образа; redis не пересоздавать). Постоянные настройки — только в
`docker-compose.gpu.yml` в git; серверный `docker-compose.override.yml` untracked.
Подробности — `AGENTS.md`.

Персистентное (не в git): `db.sqlite`, `/data/archive`, `/data/zoom-profile`.
Регламент перелогина Zoom‑сессии — `RELOGIN_DUE_DAYS` (напоминание + ежедневный
health‑check `SESSION_HEALTHCRON`). `mkvmerge`, `ffmpeg` — в образе.

