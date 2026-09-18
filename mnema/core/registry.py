"""Reestr klyuchey konfiguracii (SSOT, FT-38).

Klassy klyuchey (po obrazcu Vexa config.v1):
  - "required-explicit": ne zadan/put => servis NE startuet;
  - "defaulted":         put => dokumentirovannyy defolt;
  - "capability":        klyuch opcional'noy vozmozhnosti (llm/asr);
                         servis startuet v lyubom sluchae.

Iz etogo zhe reestra generiruetsya .env.example (FT-37); test na dreyf
sravnivaet fail s generaciey.
"""
from __future__ import annotations

from dataclasses import dataclass, field

REQUIRED = "required-explicit"
DEFAULTED = "defaulted"
CAPABILITY = "capability"


@dataclass(frozen=True)
class Key:
    name: str
    klass: str
    default: str | None = None
    secret: bool = False
    readers: str = ""
    when: str = ""
    if_empty: str = ""
    capability: str | None = None
    comment: str | None = None


@dataclass(frozen=True)
class Block:
    title: str
    keys: tuple[Key, ...]


REGISTRY: tuple[Block, ...] = (
    Block("core (api/worker/web-bot)", (
        Key("DATABASE_URL", DEFAULTED, "sqlite:////data/db.sqlite",
            readers="api/worker", when="vsegda",
            if_empty="SQLite na /data (6.4); postgres pri roste",
            comment="SQLite na starte; postgres: ...postgresql://..."),
        Key("REDIS_URL", DEFAULTED, "redis://redis:6379/0",
            readers="api/worker/web-bot", when="vsegda",
            if_empty="redis:6379 po umolchaniyu"),
        Key("MNEMA_PUBLIC_URL", DEFAULTED, "http://localhost:8000",
            readers="notifier/api", when="ssylki v pishmah (FT-35b)",
            if_empty="http://localhost:8000"),
    )),
    Block("recorder (web-bot chitaet vsyo)", (
        Key("ZOOM_PROFILE_DIR", REQUIRED,
            readers="web-bot", when="vsegda",
            if_empty="=> web-bot ne startuet"),
        Key("BOT_NAME", DEFAULTED, "Mnema",
            readers="web-bot", when="pre-join", if_empty="Mnema"),
        Key("RECORDER_URL_TEMPLATE", DEFAULTED,
            "https://app.zoom.us/wc/{meeting_id}/join?pwd={pwd}",
            readers="web-bot", when="start sessii",
            if_empty="shablon po FT-1"),
        Key("BOT_PREJOIN_SECONDS", DEFAULTED, "120",
            readers="scheduler", when="planovye vstrechi",
            if_empty="2 min do start_at"),
        Key("JOIN_DEADLINE_MIN", DEFAULTED, "10",
            readers="web-bot", when="FT-5", if_empty="10 min"),
        Key("SILENCE_END_TIMEOUT_MIN", DEFAULTED, "5",
            readers="web-bot", when="FT-4", if_empty="5 min"),
        Key("BOT_MAX_DURATION_MIN", DEFAULTED, "360",
            readers="web-bot", when="FT-4", if_empty="6 ch"),
        Key("PAGE_RELOAD_RETRIES", DEFAULTED, "2",
            readers="web-bot", when="FT-4", if_empty="2"),
        Key("CAPTCHA_WAIT_MIN", DEFAULTED, "15",
            readers="web-bot", when="reCAPTCHA na Join (§12)",
            if_empty="15 min zhdёm vruchnuyu prokhoda cherez noVNC"),
        Key("RELOGIN_DUE_DAYS", DEFAULTED, "12",
            readers="scheduler", when="FT-24", if_empty="12"),
        Key("SESSION_HEALTHCRON", DEFAULTED, "0 9 * * *",
            readers="scheduler", when="FT-24", if_empty="09:00 ezhednevno"),
        Key("DND_CRON", DEFAULTED, "",
            readers="scheduler", when="ne zapuskat' po raskhaniyu",
            if_empty="net DND"),
        Key("ANNOUNCE_WAV", DEFAULTED, "templates/announce.wav",
            readers="web-bot", when="FT-10",
            if_empty="templates/announce.wav; net faila -> bez ob'yavleniya"),
    )),
    Block("notify (obyazatel'en; sboy bez pisma = defekt, FT-24)", (
        Key("SMTP_HOST", REQUIRED,
            readers="notifier", when="vsegda",
            if_empty="=> servis ne startuet"),
        Key("SMTP_PORT", DEFAULTED, "587",
            readers="notifier", when="vsegda", if_empty="587 STARTTLS"),
        Key("SMTP_USER", DEFAULTED, "", secret=False,
            readers="notifier", when="relay s avtorizaciey",
            if_empty="bez avtorizacii"),
        Key("SMTP_PASSWORD_REF", DEFAULTED, "", secret=False,
            readers="notifier", when="relay s avtorizaciey",
            if_empty="bez parolya",
            comment="ssylka na vault: //secret-store (FT-38), ne parol'"),
        Key("SMTP_PASSWORD", DEFAULTED, "", secret=True,
            readers="notifier", when="esli SMTP_PASSWORD_REF pust i relay trebuet",
            if_empty="bez parolya"),
        Key("NOTIFY_EMAIL_TO", REQUIRED,
            readers="notifier", when="vsegda",
            if_empty="=> servis ne startuet"),
        Key("NOTIFY_EMAIL_ADMIN", DEFAULTED, "",
            readers="notifier", when="FT-35g", if_empty="bez kopii"),
        Key("NOTIFY_FROM", DEFAULTED, "Mnema <mnema@localhost>",
            readers="notifier", when="vsegda", if_empty="Mnema <mnema@localhost>"),
        Key("ALERT_COOLDOWN_MIN", DEFAULTED, "60",
            readers="notifier", when="FT-35v", if_empty="60 min"),
        Key("ALERT_DIGEST_CRON", DEFAULTED, "0 9 * * *",
            readers="scheduler", when="FT-35v", if_empty="09:00"),
        Key("NOTIFY_EVENTS", DEFAULTED,
            "job_failed=email,session_relogin_due=email,protocol_ready=email",
            readers="notifier", when="FT-36", if_empty="defolt iz FT-36"),
    )),
    Block("asr (capability:asr; worker kak klient)", (
        Key("ASR_ENGINE", DEFAULTED, "gigastt",
            readers="worker:asr", when="FT-12",
            if_empty="gigastt | faster-whisper"),
        Key("ASR_BASE_URL", CAPABILITY, "http://ai-host:9876", capability="asr",
            readers="worker:asr", when="FT-12/FT-39",
            if_empty="=> asr=not_configured, fallback faster-whisper"),
        Key("ASR_API_KEY", CAPABILITY, "", secret=True, capability="asr",
            readers="worker:asr", when="gigastt s API_TOKEN",
            if_empty="bez auth"),
        Key("ASR_MODEL", CAPABILITY, "ml_ctc_large", capability="asr",
            readers="worker:asr", when="vybor modeli gigastt",
            if_empty="ml_ctc_large"),
        Key("ASR_DEVICE", DEFAULTED, "cpu",
            readers="worker", when="fallback-whisper i embeddingy",
            if_empty="cpu | cuda:0"),
        Key("DIARIZER", DEFAULTED, "pyannote-3.1",
            readers="worker:asr", when="FT-25", if_empty="pyannote-3.1"),
        Key("SPEAKER_T_CONFIRM", DEFAULTED, "0.65",
            readers="worker:speaker", when="FT-27",
            if_empty="0.65 (kalibrovat'!)"),
        Key("SPEAKER_T_HINT", DEFAULTED, "0.45",
            readers="worker:speaker", when="FT-27", if_empty="0.45"),
        Key("REVIEW_TIMEOUT_HOURS", DEFAULTED, "48",
            readers="worker:speaker", when="FT-28", if_empty="48 ch"),
    )),
    Block("llm (capability:llm; strukturirovanie protokola)", (
        Key("LLM_BASE_URL", CAPABILITY, "http://ai-host:11434/v1", capability="llm",
            readers="worker:structure", when="FT-40",
            if_empty="=> llm=not_configured, Job v awaiting_llm"),
        Key("LLM_API_KEY", CAPABILITY, "", secret=True, capability="llm",
            readers="worker:structure", when="ne-Ollama brendy",
            if_empty="pusto dlya Ollama"),
        Key("LLM_MODEL", CAPABILITY, "llama3-saiga:32b", capability="llm",
            readers="worker:structure", when="FT-17", if_empty="llama3-saiga:32b"),
        Key("LLM_MODEL_ALLOWLIST", DEFAULTED, "",
            readers="worker:structure", when="operatorskiy geyt",
            if_empty="razresheny vse modeli"),
    )),
    Block("storage", (
        Key("ARCHIVE_DIR", REQUIRED,
            readers="archiver/api", when="vsegda",
            if_empty="=> servis ne startuet"),
        Key("RETENTION_DAYS", DEFAULTED, "180",
            readers="scheduler", when="HT-3", if_empty="180"),
    )),
)


KEY_INDEX: dict[str, Key] = {
    k.name: k for block in REGISTRY for k in block.keys
}


def render_env_example() -> str:
    """Generiruet .env.example (FT-37): kommentariy u kazhdogo klyuchа."""
    lines: list[str] = []
    lines.append("# Mnema — .env.example")
    lines.append("# Avtogeneraciya iz mnema/core/registry.py (FT-37/FT-38).")
    lines.append("# Klassy: [R] required-explicit, [D] defaulted, [C:*] capability.")
    lines.append("# Pustom znacheniyu = klyuch ne zadan; povedenie v kommente.")
    for block in REGISTRY:
        lines.append("")
        lines.append(f"# --- {block.title} " + "-" * max(0, 60 - len(block.title)))
        for k in block.keys:
            if k.klass == REQUIRED:
                tag = "[R]"
            elif k.klass == CAPABILITY:
                tag = f"[C:{k.capability}]"
            else:
                tag = f"[D={k.default}]" if k.default else "[D=puto]"
            lines.append(f"# {tag} chitaet: {k.readers}; nuzhen: {k.when}; esli pusto: {k.if_empty}")
            if k.comment:
                lines.append(f"#   {k.comment}")
            if k.secret:
                lines.append(f"{k.name}=")
            else:
                lines.append(f"{k.name}={k.default if k.default is not None else ''}")
    return "\n".join(lines) + "\n"


def preflight(services: tuple[str, ...] | None = None,
              env: dict[str, str] | None = None) -> list[str]:
    """Proveryaet required-explicit (FT-38). Vozvrachaet spisok oshibok.

    Imenuet KLYUCH (ne znachenie) i govorit, gde vzyat primer.
    services: esli ukazan, proveryayutsya tol'ko klyuchi, kotorye chitaet servis
    (po podstroke v readers). Bez nego — vse.
    """
    import os
    source = env if env is not None else dict(os.environ)
    errors: list[str] = []
    for block in REGISTRY:
        for k in block.keys:
            if services and not any(s in k.readers for s in services):
                continue
            if k.klass == REQUIRED:
                if not source.get(k.name, "").strip():
                    errors.append(
                        f"set {k.name} (required-explicit), see .env.example: {k.if_empty}"
                    )
    return errors


def state_for(capability: str) -> dict:
    """Proizvodnoye sostoyanie.capability (FT-39): computed iz env, ne healthprobe."""
    import os
    if capability == "llm":
        url = os.environ.get("LLM_BASE_URL", "").strip()
        model = os.environ.get("LLM_MODEL", "").strip()
        if not url:
            return {"state": "not_configured", "backend": None, "model": None}
        return {"state": "configured", "backend": "openai-compatible",
                "model": model or None}
    if capability == "asr":
        url = os.environ.get("ASR_BASE_URL", "").strip()
        engine = os.environ.get("ASR_ENGINE", "gigastt").strip() or "gigastt"
        if not url:
            return {"state": "not_configured", "backend": engine,
                    "model": os.environ.get("ASR_MODEL", "").strip() or None}
        return {"state": "configured", "backend": engine,
                "model": os.environ.get("ASR_MODEL", "").strip() or None}
    raise ValueError(capability)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        errs = preflight()
        for e in errs:
            print(e, file=sys.stderr)
        sys.exit(1 if errs else 0)
    print(render_env_example(), end="")
