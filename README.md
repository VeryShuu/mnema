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

## Быстрый старт (GPU-версия, v2)

Второй вариант деплоя: тот же стек, но `worker` собирается на CUDA‑образе
(`docker/Dockerfile.python.gpu`, torch 2.4.1+cu124) и считает диаризацию/ASR
на NVIDIA GPU. Диаризация пайплайна pyannote идёт в fp16 с батчами
(`DIAR_FP16=1`, `DIAR_BATCH_SIZE=32`), faster‑whisper — `float16` на GPU
(override: `WHISPER_COMPUTE_TYPE`). Это убирает повторную загрузку модели
эмбеддингов на каждый сегмент (кэш `_get_inference`) и даёт ~5–15x к CPU‑варианту.

Требования к хосту: драйвер NVIDIA (CUDA ≥12.4) + `nvidia-container-toolkit`
(`sudo apt install nvidia-container-toolkit && sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker`).

```bash
cp .env.example .env
echo "ASR_DEVICE=cuda:0" >> .env          # gpu-compose и так ставит, но явнее
make gpu-up                               # = docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.gpu.yml logs worker | head
# в логе: "pyannote: device=cuda:0 fp16=True batch=32"
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
| Override‑файл: worker на GPU‑образ, `ASR_DEVICE=cuda:0`, `deploy.resources.devices` (driver nvidia) | `docker-compose.gpu.yml` | изолированный GPU‑деплой, api/web-bot не меняются |
| fp16 диаризации через `torch.autocast` + авто‑откат на fp32 при ошибке | `mnema/core/diarize.py` (`_run_pipeline`, `_fp16_enabled`) | tensor cores: ~2x скорость без потери качества |
| Батчи сегментации/эмбеддингов pyannote (после загрузки пайплайна — `from_pretrained` их не принимает) | `mnema/core/diarize.py` (`_get_pyannote`) | выше загрузка GPU, ~2–3x на диаризации |
| Кэш `Inference` модели эмбеддингов по устройству (раньше модель грузилась заново на каждый срез кластера) | `mnema/core/speaker.py` (`_get_inference`) | speaker‑шаг: минус ~20 c × кол‑во кластеров |
| faster‑whisper: `compute_type=float16` на GPU / `int8` на CPU | `mnema/core/asr_client.py` (`_whisper_compute_type`) | ASR fallback ~2x быстрее на GPU |
| Ключи конфигурации (в реестре и `.env.example`) | `WHISPER_COMPUTE_TYPE`, `DIAR_FP16`, `DIAR_BATCH_SIZE` | тюнинг/отключение fp16 и батчей без пересборки |
| Makefile: `gpu-build` / `gpu-up` / `gpu-down` / `gpu-logs` | `Makefile` | `-f docker-compose.yml -f docker-compose.gpu.yml` |

Ожидаемый совокупный эффект на типичном датацентровом GPU (T4/A10):
диаризация ~5–10x быстрее, ASR fallback ~2x; итог по конвейеру — вместо
~5 минут десятки секунд (профилирование на реальных данных — задача деплоя;
CPU‑фолбэк‑тест test_pipeline подтверждает эквивалентность результатов).

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

