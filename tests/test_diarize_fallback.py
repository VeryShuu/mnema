"""Fallback i retri zagruzki pyannote v mnema/core/diarize.py.

Testiruet nevremenno-sboysya diariizatsiyu bez real'nogo pyannote/HF:
sboy logiruyetsya, `False` memoiziruetsya tol'ko na DIAR_RETRY_SEC,
posle okna popytka povtoryaetsya, DIARIZER=off — bez povtorov vovse.
"""
import sys
import types

import pytest

from mnema.core import diarize as dz


def _fake_torch():
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    return torch


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setattr(dz, "_pipe", None)
    monkeypatch.setattr(dz, "_failed_ts", 0.0)
    monkeypatch.delenv("DIARIZER", raising=False)
    monkeypatch.delenv("DIAR_RETRY_SEC", raising=False)
    monkeypatch.setitem(sys.modules, "pyannote", types.ModuleType("pyannote"))
    monkeypatch.setitem(sys.modules, "torch", _fake_torch())


class _Loader:
    """Zamenya Pipeline.from_pretrained schetchika popytok:
    do `fail_times` popytok padaet, dal'ше vernyaet marker."""

    def __init__(self, monkeypatch, fail_times=1):
        self.calls = 0
        self.fail_times = fail_times
        mod = types.ModuleType("pyannote.audio")

        loader = self

        class Pipeline:
            @staticmethod
            def from_pretrained(*a, **k):
                loader.calls += 1
                if loader.calls <= loader.fail_times:
                    raise RuntimeError("HF unreachable")
                return "PIPE"

        mod.Pipeline = Pipeline
        monkeypatch.setitem(sys.modules, "pyannote.audio", mod)


SEGS = [{"t_start": 0.0, "t_end": 1.0}, {"t_start": 2.0, "t_end": 3.0}]


def test_load_failure_returns_unknown_and_logs(monkeypatch, caplog):
    loader = _Loader(monkeypatch, fail_times=99)
    with caplog.at_level("WARNING", logger="mnema.diarize"):
        assert dz.diarize("x.wav", SEGS) == ["SPEAKER_UNKNOWN"] * 2
    out = caplog.text
    assert "pyannote" in out and "HF unreachable" in out
    assert dz._pipe is False
    assert loader.calls == 1


def test_failure_not_retried_within_window(monkeypatch):
    loader = _Loader(monkeypatch, fail_times=99)
    monkeypatch.setenv("DIAR_RETRY_SEC", "600")
    dz.diarize("x.wav", SEGS)
    dz.diarize("x.wav", SEGS)
    assert loader.calls == 1, "v okne retry novykh popytok byt' ne dolzhno"


def test_retry_after_window_and_recovers(monkeypatch):
    loader = _Loader(monkeypatch, fail_times=1)
    monkeypatch.setenv("DIAR_RETRY_SEC", "0")
    assert dz.diarize("x.wav", SEGS) == ["SPEAKER_UNKNOWN"] * 2
    monkeypatch.setattr(dz, "_pyannote_labels",
                        lambda pipe, wav, segs: ["SPEAKER_00", "SPEAKER_01"])
    assert dz.diarize("x.wav", SEGS) == ["SPEAKER_00", "SPEAKER_01"]
    assert loader.calls == 2
    assert dz._pipe == "PIPE", "uspeh dolzhen zakeshirovat'sya i snyat' False"


def test_success_clears_failed_state(monkeypatch):
    _Loader(monkeypatch, fail_times=1)
    monkeypatch.setenv("DIAR_RETRY_SEC", "0")
    monkeypatch.setattr(dz, "_pyannote_labels",
                        lambda pipe, wav, segs: ["SPEAKER_00", "SPEAKER_00"])
    dz.diarize("x.wav", SEGS)
    dz.diarize("x.wav", SEGS)
    assert dz._pipe == "PIPE"


def test_runtime_failure_logged_during_diarize(monkeypatch, caplog):
    _Loader(monkeypatch, fail_times=0)

    def boom(pipe, wav, segs):
        raise ValueError("cuda oom")

    monkeypatch.setattr(dz, "_pyannote_labels", boom)
    with caplog.at_level("WARNING", logger="mnema.diarize"):
        assert dz.diarize("x.wav", SEGS) == ["SPEAKER_UNKNOWN"] * 2
    out = caplog.text
    assert "cuda oom" in out and "2 segments" in out
    assert dz._pipe == "PIPE", "sboy vypolneniya ne dolzhen lomat' keshe"


def test_diarizer_off_never_loads(monkeypatch):
    loader = _Loader(monkeypatch, fail_times=0)
    monkeypatch.setenv("DIARIZER", "off")
    monkeypatch.setenv("DIAR_RETRY_SEC", "0")
    assert dz.diarize("x.wav", SEGS) == ["SPEAKER_UNKNOWN"] * 2
    assert dz.diarize("x.wav", SEGS) == ["SPEAKER_UNKNOWN"] * 2
    assert loader.calls == 0
    assert dz._pipe == "off"


def test_bad_retry_env_falls_back_to_600(monkeypatch):
    monkeypatch.setenv("DIAR_RETRY_SEC", "ne-chislo")
    assert dz._retry_sec() == 600.0
