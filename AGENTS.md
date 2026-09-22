# Project Notes: Mnema

## Особенности тестов (pytest)

- Python: использовать venv проекта: `./venv/bin/python -m pytest` (см. правило python_venv.md).
- **Полный прогон `tests/` медленный — ~11 минут (112 passed, 7 skipped).**
  По умолчанию Bash-команды ограничены 120 с, поэтому pytest без явного `timeout`
  будет убит посреди прогона. Всегда передавайте большой `timeout`
  (≥300000 мс) или ограничивайтесь конкретными файлами тестов.
- Во время разработки гоняйте только затронутые файлы, напр.:
  `timeout 300 ./venv/bin/python -m pytest tests/test_api_integration.py -q`
  (отдельные файлы быстрые: test_api_integration.py ~5 с).
- На `tests/` целиком вывод лучше писать в файл:
  `./venv/bin/python -m pytest tests/ -q > /tmp/pytest_out.txt 2>&1; tail -f не нужен — итог в конце файла`.
  Вывод с `| tail` при таймауте теряется полностью.
- 7 тестов скипаются штатно (нет внешних зависимостей) — это не ошибка.
- Redis в тестах недоступен намеренно (`REDIS_URL` указывает на заведомо пустой
  порт `redis://127.0.0.1:6399/0`): enqueue/stop падают в no-op. Проверку
  постановки в очередь делайте через `monkeypatch.setattr("mnema.core.jobs_launch.request_stop", ...)`
  или аналог, а не через реальный redis.
- Тесты БД используют in-memory SQLite (`set_engine_for_test` + StaticPool);
  env-переменные (`DATA_DIR`, `ARCHIVE_DIR`, `DATABASE_URL`, ...) задаются через
  `os.environ.setdefault` в начале тестовых файлов — новые env добавляйте туда же.
- Переходы статусов в тестах эмулируют web-bot через внутренние
  `POST /api/jobs/{id}/status` (см. `ALLOWED_TRANSITIONS` в `mnema/core/pipeline.py`);
  недопустимый переход → 409, это проверяемое поведение.
- web-bot (Node.js, `web-bot/`) автотестов не имеет; минимальная проверка —
  `node --check src/<file>.js` для каждого изменённого файла.
