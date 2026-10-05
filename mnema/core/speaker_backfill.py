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
from pathlib import Path

from sqlalchemy import select

from .db import get_session
from .models import Job, ReviewRequest, Segment, SessionEvent, VoiceProfile


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def mean_embedding(vecs: list[list[float]]) -> list[float] | None:
    """Srednee po vektoram odinovoy razmernosti s L2-normalizaciey.

    Vektory drugoi razmernosti (legendnie 32-D hash-stuby, poyavlennye
    starym api-bez-pyannote) propuskayutsya: `v[i]` po pervomu vektoru
    uekal by ih v srednee i portil kesh embeddinga."""
    if not vecs:
        return None
    dims = [len(v) for v in vecs]
    dim = max(set(dims), key=dims.count)
    use = [v for v in vecs if len(v) == dim]
    return _l2_normalize([sum(v[i] for v in use) / len(use) for i in range(dim)])


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
    """Embedding klastera po zapisi vstrechi (zhivoy pyannote, ne stub).

    Esli v jobe sohraneny turns diagramizatsii, v embedding berutsya tol'ko
    chisty podinterval klastera (bez nakhalozheniya chuzhoy rechi)."""
    from ..worker.tasks import _cluster_embedding, _job_paths
    wav = _job_paths(job_id)["wav"]
    if not wav.exists():
        return None
    with get_session() as s:
        job = s.get(Job, job_id)
        turns = job.diar_turns if job is not None else None
        segs = list(s.scalars(select(Segment).where(
            Segment.job_id == job_id,
            Segment.speaker_id == cluster,
            Segment.excluded.is_(False))).all())
    if not segs:
        return None
    return _cluster_embedding(wav, segs, turns=turns)


def backfill_profile_embeddings(refresh: bool = False, max_jobs: int = 10,
                                dry_run: bool = False) -> dict[int, str]:
    from .speaker import _embedding_backend
    if _embedding_backend() != "pyannote":
        raise RuntimeError(
            "embedding backend niedostupen (nuzhen pyannote + HF_TOKEN) — "
            "zpuskayte backfill v konteynere workera, a ne api")
    from .speaker import compute_embedding_for_file
    results: dict[int, str] = {}
    with get_session() as s:
        profiles = [(v.id, v.employee_name, v.embedding)
                    for v in s.scalars(select(VoiceProfile)).all()]
    for pid, name, existing in profiles:
        if existing and not refresh:
            results[pid] = f"{name}: skip (embedding yest')"
            continue
        vecs: list[list[float]] = []
        keys: list[str] = []
        samples = _profile_sample_paths(pid)
        for sp in samples:
            try:
                emb = compute_embedding_for_file(Path(sp))
                if emb:
                    vecs.append(_l2_normalize(emb))
                    keys.append(f"path:{sp}")
            except Exception:  # noqa: BLE001
                continue
        for job_id, cluster in profile_backfill_sources(pid, max_jobs):
            try:
                emb = cluster_embedding_for(job_id, cluster)
                if emb:
                    vecs.append(_l2_normalize(emb))
                    keys.append(f"job:{job_id}:{cluster}")
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
            _store_refs(vp, vecs, sources_keys=keys)
            vp.embedding = mean
            stats = dict(vp.match_stats or {})
            stats["backfill_clusters"] = len(vecs)
            vp.match_stats = stats
            s2.commit()
        results[pid] = f"{name}: embedding iz {len(vecs)} klasterov"
    return results


def _profile_sample_paths(profile_id: int) -> list[str]:
    with get_session() as s:
        v = s.get(VoiceProfile, profile_id)
        return [p for p in ((v.sample_paths if v else None) or []) if Path(p).is_file()]


def _store_refs(vp: VoiceProfile, vecs: list[list[float]],
                sources_keys: list[str]) -> None:
    """Perepisyvaet maset etalonov profila (ref_embeddings) + match_stats.ref_sources.

    Kazhdy vektor — otdel'nyy istochnik; matsching (speaker.profile_similarity)
    idyot po max cosine po vsem refam, embedding — srednee kesh. Klyuchi
    ('job:<id>:<klaster>' / 'path:<fayl>') sopostavleny refam po poziciyam."""
    take = list(zip(sources_keys[-_REF_SET_MAX:], vecs[-_REF_SET_MAX:]))
    vp.ref_embeddings = [v for _k, v in take]
    stats = dict(vp.match_stats or {})
    stats["ref_sources"] = list(sources_keys[-_REF_SET_MAX:])
    vp.match_stats = stats
    sync_ref_cache(vp)


def append_ref(vp: VoiceProfile, emb: list[float], key: str) -> bool:
    """Dobavit' odin etalon s klyuchom istochnika (False esli takoy uzhe yest').

    Sushchestvuyushchiy odinochnyy embedding migriruetsya v set' pri pervom
    dopolnenii; vp.embedding vsegda — srednee po seti (geton klya
    match_cluster po staremu)."""
    keys = [str(k) for k in ((vp.match_stats or {}).get("ref_sources") or [])]
    if key in keys:
        return False
    refs = [list(x) for x in (vp.ref_embeddings or [])]
    if not refs:
        keys = []  # ref-set pusty — klyuchi ot nesootvetstvuyushchikh legacy ne zna4imy
        if vp.embedding and len(vp.embedding) == len(emb):
            refs = [list(vp.embedding)]  # migrate: odinochnyy vektor -> pervyy ref
            keys = ["legacy"]
        # inye razmernosti — legendniy hash-stub (32-D) iz starogo api: v set'
        # ne idyot, zhivoy vektor ego zamenshat
    refs.append(_l2_normalize(emb))
    keys.append(key)
    refs, keys = refs[-_REF_SET_MAX:], keys[-_REF_SET_MAX:]
    vp.ref_embeddings = refs
    stats = dict(vp.match_stats or {})
    stats["ref_sources"] = keys
    vp.match_stats = stats
    sync_ref_cache(vp)
    return True


def sync_ref_cache(vp: VoiceProfile) -> None:
    vp.embedding = mean_embedding(vp.ref_embeddings or []) or vp.embedding


_REF_SET_MAX = 24


def rematch_job(job_id: int) -> None:
    """Pere-matsching klasterov joba po novym embeddingam (_run_speaker sama
    zakryvaet pending review pri verdict=auto). Esli utochneniy bolshe net i
    job stoyal v awaiting_review — vozobnovlyaem LLM-analiz."""
    from ..worker.tasks import _run_speaker
    _run_speaker(job_id)
    with get_session() as s:
        s.add(SessionEvent(job_id=job_id, event="voiceprint_rematch",
                           detail="после backfill embedding-ов"))
        s.commit()
    from .pipeline import resume_after_reviews
    return resume_after_reviews(job_id)


def profile_review_jobs(profile_id: int, max_jobs: int | None = None) -> list[int]:
    """Joby, gdye profil' podtverzhdён otvetom utora (pervye svidetelstva
    'eto on') — zona perepromowerki pri ruchnoy pereproverke."""
    with get_session() as s:
        rows = s.scalars(
            select(ReviewRequest.job_id)
            .where(ReviewRequest.candidate_profile_id == profile_id,
                   ReviewRequest.status == "confirmed")
            .order_by(ReviewRequest.id.desc())).all()
    out: list[int] = []
    for job_id in rows:
        if job_id not in out:
            out.append(job_id)
        if max_jobs is not None and len(out) >= max_jobs:
            break
    return out


def recompute_profile_embedding(profile_id: int) -> str:
    """Zanovo sobrat' etalon profilya: zapisi s mikrofona + podtverzhdennye
    klastery proshlykh vstrech (bez avtomaticheskikh otvetov). Vozvrajeta
    cheloveko-chitaemyy rezultat dla zhurnalnykh sobytiy."""
    from .speaker import _embedding_backend
    if _embedding_backend() != "pyannote":
        raise RuntimeError(
            "embedding backend niedostupen (nuzhen pyannote) — "
            "pereproverku delaet worker, ne api")
    from .speaker import compute_embedding_for_file
    with get_session() as s:
        v = s.get(VoiceProfile, profile_id)
        if v is None:
            return "profil' udalen"
        samples = [p for p in (v.sample_paths or []) if Path(p).is_file()]
    vecs: list[list[float]] = []
    keys: list[str] = []
    for p in samples:
        try:
            emb = compute_embedding_for_file(Path(p))
            if emb:
                vecs.append(_l2_normalize(emb))
                keys.append(f"path:{p}")
        except Exception:  # noqa: BLE001 — odin ploskoy etalon ne lonit vsyo
            continue
    n_clusters = 0
    for job_id, cluster in profile_backfill_sources(profile_id):
        try:
            emb = cluster_embedding_for(job_id, cluster)
            if emb:
                vecs.append(_l2_normalize(emb))
                keys.append(f"job:{job_id}:{cluster}")
                n_clusters += 1
        except Exception:  # noqa: BLE001
            continue
    if not vecs:
        return "net istochnikov etalona (etalonov i zhivykh klasteov)"
    mean = mean_embedding(vecs)
    with get_session() as s2:
        vp = s2.get(VoiceProfile, profile_id)
        if vp is None:
            return "profil' udalen"
        vp.embedding = mean
        _store_refs(vp, vecs, sources_keys=keys)
        stats = dict(vp.match_stats or {})
        stats["backfill_clusters"] = n_clusters
        vp.match_stats = stats
        s2.commit()
    return f"etalon: {len(samples)} zapisey + {n_clusters} klasteov"


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
