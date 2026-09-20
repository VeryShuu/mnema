"""Golosovaya identifikatsiya (4.6): emgeddingi, matsching s porogami, otvety operatora.

Tyazhelaya chast' (pyannote embedding) — lazy import: esli model' nie
ustanovlena/ispol'zuyetsya CPU-only stub, sistema ne upadaet (kholodnyj start,
FT-29). Embedding — vektor; sravnenie — kosinusnoe shodstvo (FT-27).
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from sqlalchemy import select

from .db import get_session
from .models import ReviewRequest, Segment, VoiceProfile

# ---- emgeddingi --------------------------------------------------------------
_backend_cache: dict = {}


def _embedding_backend():
    """pyannote-embedding / SpeechBrain; None esli niedostupen (CPU stub)."""
    if "backend" in _backend_cache:
        return _backend_cache["backend"]
    backend = None
    try:
        import torch  # noqa: F401
        from pyannote.audio import Inference, Model  # noqa: F401
        backend = "pyannote"
    except Exception:  # noqa: BLE001
        try:
            from speechbrain.pretrained import EncoderClassifier  # noqa: F401
            backend = "speechbrain"
        except Exception:  # noqa: BLE001
            backend = None
    _backend_cache["backend"] = backend
    return backend


def compute_embedding_for_file(path: Path) -> list[float] | None:
    """Zhivoy embedding (pyannote). V dev/stub — stabil'ny determin. hash-vektor

    po soderzhanimou fayla, chtoby matj po rosshnosti rabotal na testah.
    """
    backend = _embedding_backend()
    if backend == "pyannote":
        try:
            return _pyannote_embedding(path)
        except Exception:  # noqa: BLE001
            return _fallback_embedding(path)
    return _fallback_embedding(path)


def _pyannote_embedding(path: Path) -> list[float]:
    from pyannote.audio import Inference, Model
    import torch
    device = torch.device(os.environ.get("ASR_DEVICE", "cpu"))
    tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    model = Model.from_pretrained("pyannote/embedding", use_auth_token=tok)
    inference = Inference(model, window="whole", device=device)
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav") as tf:
        from .audio import webm_to_wav16k
        p = Path(path)
        if p.suffix.lower() in (".webm", ".mp3", ".ogg"):
            webm_to_wav16k(p, Path(tf.name))
            vec = inference(Path(tf.name))
        else:
            vec = inference(p)
    import numpy as np
    arr = np.asarray(vec).flatten()
    norm = float(np.linalg.norm(arr)) or 1.0
    return (arr / norm).tolist()


def _fallback_embedding(path: Path) -> list[float]:
    """Determin-stub dlya testov/offline-bez-modeli: hash->edininichny vektor 32-D."""
    import hashlib
    import math
    data = Path(path).read_bytes()
    h = hashlib.sha256(data).digest()
    vals = [b / 127.5 - 1.0 for b in h[:32]]
    norm = math.sqrt(sum(x * x for x in vals)) or 1.0
    return [x / norm for x in vals]


def quality_gate(path: Path) -> tuple[bool, str]:
    """FT-29a: SNR/dlitel'nost' — iache periezapis'. V1 minimal'no: dur >= 1.5 s."""
    from .audio import ffprobe_duration, wav_duration_s
    p = Path(path)
    dur = wav_duration_s(p) if p.suffix == ".wav" else ffprobe_duration(p)
    if dur < 1.5:
        return False, f"slisikom korotko ({dur:.1f} s < 1.5 s)"
    if dur > 120:
        return False, "slishkom dlino (> 120 s)"
    return True, "ok"


# ---- kosinusnoe shodstvo i matsching (FT-27) --------------------------------
def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


def thresholds() -> tuple[float, float]:
    t_conf = float(os.environ.get("SPEAKER_T_CONFIRM", "0.65") or 0.65)
    t_hint = float(os.environ.get("SPEAKER_T_HINT", "0.45") or 0.45)
    return t_conf, t_hint


def match_cluster(embedding: list[float]) -> tuple[str, VoiceProfile | None, float]:
    """Vozvr. ('auto'|'hint'|'unresolved', profil'|None, similarity)."""
    t_conf, t_hint = thresholds()
    best_profile = None
    best_sim = 0.0
    with get_session() as s:
        for v in s.scalars(select(VoiceProfile)).all():
            if not v.embedding:
                continue
            sim = cosine(embedding, v.embedding)
            if sim > best_sim:
                best_sim, best_profile = sim, v
    if best_profile and best_sim >= t_conf:
        return "auto", best_profile, best_sim
    if best_profile and best_sim >= t_hint:
        return "hint", best_profile, best_sim
    return "unresolved", None, best_sim


def apply_review_answer(rid: int, employee_id: int | None, employee_name: str | None,
                        create_new: bool, answered_by: str) -> str | None:
    """FT-28/FT-32: otvet primenyaetsya ko vsemu klasteryu + dokuchaet profil'."""
    with get_session() as s:
        r = s.get(ReviewRequest, rid)
        if r is None:
            return None
        job_id, cluster = r.job_id, r.cluster_label
        if create_new and employee_name:
            v = VoiceProfile(employee_name=employee_name,
                             consent_ts=dt.datetime.now(dt.timezone.utc))
            s.add(v)
            s.flush()
            name = employee_name
            r.candidate_profile_id = v.id
        elif employee_id:
            v = s.get(VoiceProfile, employee_id)
            name = v.employee_name if v else employee_name
            r.candidate_profile_id = employee_id
        else:
            name = employee_name
        r.status = "confirmed"
        r.answered_by = answered_by
        r.answered_ts = dt.datetime.now(dt.timezone.utc)
        # peremenuyat' segmenty klasterya
        for seg in s.scalars(select(Segment).where(
                Segment.job_id == job_id, Segment.speaker_id == cluster)).all():
            if name:
                seg.speaker_name = name
                seg.speaker_source = "voiceprint"
            seg.needs_review = False
        # update match_stats (FT-33)
        if r.candidate_profile_id:
            v = s.get(VoiceProfile, r.candidate_profile_id)
            if v:
                stats = dict(v.match_stats or {})
                stats["confirmed"] = stats.get("confirmed", 0) + 1
                v.match_stats = stats
        s.commit()
        return name
