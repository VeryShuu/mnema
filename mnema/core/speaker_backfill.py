"""Backfill embeddingtonov profilot iz raneye podtverzhdennykh klasterov.

Dva pohoda k uznawaniyu golosa dopolnyayut drug druga:
1. zapis' s mikrofona (POST /api/voice-profiles/{vid}/sample);
2. etot backfill: berem joby, gdye operator uzhe otvetil na review-request
   (confirmed s candidate_profile_id), schitaem embedding klastera po zapisi
   vstrechi i usrednyaem mezhdu jobami.

Zapusk na servere (v konteynere workera, gde est' pyannote + HF_TOKEN + wav):
    python -m mnema.core.speaker_backfill            # profiliam bez embedding
    python -m mnema.core.speaker_backfill --refresh  # i s embeddingom
    python -m mnema.core.speaker_backfill --job 55   # zatem rematch klasterov joba
"""
from __future__ import annotations

import argparse
import math

from sqlalchemy import select

from .db import get_session
from .models import ReviewRequest, Segment, SessionEvent, VoiceProfile


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def mean_embedding(vecs: list[list[float]]) -> list[float] | None:
    """Srednee po vektoram s L2-normalizaciey (porogi kosinusa okoli 0.45/0.65
    ostayutsya osmyslennymi)."""
    if not vecs:
        return None
    n = len(vecs[0])
    return _l2_normalize([sum(v[i] for v in vecs) / len(vecs) for i in range(n)])


def profile_backfill_sources(profile_id: int, max_jobs: int = 10) -> list[tuple[int, str]]:
    """(job_id, cluster_label) podtverzhdennykh klasterov profilya.

    Otvety avtomata (answered_by=voiceprint) isklyuchayutsya — inache backfill
    matsiral by sam sebya po uskoritelnoy obratnoy svyazi.
    """
    with get_session() as s:
        rows = s.scalars(
            select(ReviewRequest)
            .where(ReviewRequest.candidate_profile_id == profile_id,
                   ReviewRequest.status == "confirmed",
                   ReviewRequest.answered_by != "voiceprint")
            .order_by(ReviewRequest.id.desc())
        ).all()
    seen: set[tuple[int, str]] = set()
    out: list[tuple[int, str]] = []
    for r in rows:
        key = (r.job_id, r.cluster_label)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= max_jobs:
            break
    return out


def cluster_embedding_for(job_id: int, cluster: str) -> list[float] | None:
    """Embedding klastera po zapisi vstrechi (zhivoy pyannote, ne stub)."""
    from ..worker.tasks import _cluster_embedding, _job_paths
    wav = _job_paths(job_id)["wav"]
    if not wav.exists():
        return None
    with get_session() as s:
        segs = list(s.scalars(select(Segment).where(
            Segment.job_id == job_id,
            Segment.speaker_id == cluster,
            Segment.excluded.is_(False))).all())
    if not segs:
        return None
    return _cluster_embedding(wav, segs)


def backfill_profile_embeddings(refresh: bool = False, max_jobs: int = 10,
                                dry_run: bool = False) -> dict[int, str]:
    from .speaker import _embedding_backend
    if _embedding_backend() != "pyannote":
        raise RuntimeError(
            "embedding backend niedostupen (nuzhen pyannote + HF_TOKEN) — "
            "zpuskayte backfill v konteynere workera, a ne api")
    results: dict[int, str] = {}
    with get_session() as s:
        profiles = [(v.id, v.employee_name, v.embedding)
                    for v in s.scalars(select(VoiceProfile)).all()]
    for pid, name, existing in profiles:
        if existing and not refresh:
            results[pid] = f"{name}: skip (embedding yest')"
            continue
        vecs: list[list[float]] = []
        for job_id, cluster in profile_backfill_sources(pid, max_jobs):
            try:
                emb = cluster_embedding_for(job_id, cluster)
                if emb:
                    vecs.append(_l2_normalize(emb))
            except Exception:  # noqa: BLE001 — odin plotnyi job ne lonit wsye
                continue
        if not vecs:
            results[pid] = f"{name}: net podtverzhdennykh klasterov s zhivym wav"
            continue
        if dry_run:
            results[pid] = f"{name}: naydeno {len(vecs)} klasterov (dry-run)"
            continue
        mean = mean_embedding(vecs)
        with get_session() as s2:
            vp = s2.get(VoiceProfile, pid)
            if vp is None:
                continue
            vp.embedding = mean
            stats = dict(vp.match_stats or {})
            stats["backfill_clusters"] = len(vecs)
            vp.match_stats = stats
            s2.commit()
        results[pid] = f"{name}: embedding iz {len(vecs)} klasterov"
    return results


def rematch_job(job_id: int) -> None:
    """Pere-matsching klasterov joba po novym embeddingam (_run_speaker sama
    zakryvaet pending review pri verdict=auto). Esli utochneniy bolshe net i
    job stoyal v awaiting_review — vozobnovlyaem LLM-analiz."""
    from ..worker.tasks import _run_speaker
    _run_speaker(job_id)
    with get_session() as s:
        s.add(SessionEvent(job_id=job_id, event="voiceprint_rematch",
                           detail="posle backfill embeddingtonov"))
        s.commit()
    from .pipeline import resume_after_reviews
    return resume_after_reviews(job_id)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backfill voice profile embeddings")
    ap.add_argument("--refresh", action="store_true",
                    help="perekalkulyat' i profili s embeddingom")
    ap.add_argument("--max-jobs", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--job", type=int, default=None,
                    help="posle backfill sdelat' jobu povtornyy voiceprint-matsching")
    args = ap.parse_args(argv)
    res = backfill_profile_embeddings(refresh=args.refresh, max_jobs=args.max_jobs,
                                      dry_run=args.dry_run)
    for pid, text in res.items():
        print(f"[{pid}] {text}")
    if args.job:
        rematch_job(args.job)
        print(f"job {args.job}: voiceprint pere-machin, auto-sovpadeniya zakryty")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
