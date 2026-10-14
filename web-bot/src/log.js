// Mnema web-bot: единый ходовой лог.
// Соглашение: сообщения на английском, формат `{ts} {LEVEL} [{component}] {message}`.
// Уровень — LOG_LEVEL (default info). Отладочные скрипты (dbg_*.js) используют
// console напрямую — это вывод утилиты, не ходовой лог.
const LEVELS = { debug: 10, info: 20, warn: 30, error: 40 };
const threshold = LEVELS[(process.env.LOG_LEVEL || "info").toLowerCase()] ?? 20;

function emit(level, levelName, component, args) {
  if (level < threshold) return;
  const ts = new Date().toISOString();
  const out = level >= LEVELS.warn ? console.error : console.log;
  out(`${ts} ${levelName} [${component}]`, ...args);
}

export function createLogger(component) {
  return {
    debug: (...a) => emit(LEVELS.debug, "DEBUG", component, a),
    info: (...a) => emit(LEVELS.info, "INFO", component, a),
    warn: (...a) => emit(LEVELS.warn, "WARN", component, a),
    error: (...a) => emit(LEVELS.error, "ERROR", component, a),
  };
}
