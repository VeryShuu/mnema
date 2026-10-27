"""Testy exklyuzivnogo zamyka LLM (odin analiz za raz, FIFO-ochered') i
parametra razmyshleniy (think=false po umolchaniyu, fallback pri otvete).

Zamyok testiruetsya na zhivom redis-server (Lua eval): epizodicheskiy protsess
na svobodnom portu, skip yesli redis-server nedostupen. Bez redis (zavedomo
pustoy port) llm_turn — no-op, cto tozhe proveryaetsya.
"""
import os
import shutil
import subprocess
import threading
import time

import pytest

os.environ.setdefault("ARCHIVE_DIR", "/tmp/test-archive-gate")
os.environ.setdefault("DATA_DIR", "/tmp/test-data-gate")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/test-mnema-gate.sqlite")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")  # zavedomo niet

REDIS_BIN = shutil.which("redis-server")
GATE_PORT = 6398


@pytest.fixture()
def gate_env(monkeypatch):
    """Zhivoy redis + korotkiy poll dlya bystrykh testov."""
    if REDIS_BIN is None:
        pytest.skip("redis-server nedostupen")
    proc = subprocess.Popen(
        [REDIS_BIN, "--port", str(GATE_PORT), "--save", "",
         "--appendonly", "no"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        from redis import Redis
        r = Redis(port=GATE_PORT)
        for _ in range(100):
            try:
                r.ping()
                break
            except Exception:
                time.sleep(0.05)
        else:
            pytest.skip("redis-server ne podnyalsya")
        r.flushdb()
        monkeypatch.setenv("REDIS_URL", f"redis://127.0.0.1:{GATE_PORT}/0")
        monkeypatch.setenv("LLM_LOCK_POLL_S", "0.05")
        monkeypatch.setenv("LLM_LOCK_WAIT_TIMEOUT_S", "10")
        yield r
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def _active(r):
    return r.execute_command("EXISTS", "mnema:llm:current") == 1


# ------------------------------------------------------------------ zamyok
def test_turn_without_redis_is_noop(monkeypatch):
    # zavdomo svobodnyy port (6399 v etoy srede zhiv: sluchaynyy redis-server)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6501/0")
    from mnema.core.llm_gate import llm_turn
    with llm_turn():  # ne podnimaet isklyucheniy, ne blokiruet
        pass


def test_turn_exclusive_and_released(gate_env):
    from mnema.core.llm_gate import llm_turn
    r = gate_env
    entered = threading.Event()
    release = threading.Event()

    def holder():
        with llm_turn():
            entered.set()
            release.wait(timeout=5)

    t = threading.Thread(target=holder)
    t.start()
    assert entered.wait(timeout=5)
    assert _active(r), "token zanyat poka vladelec vnutri bloka"
    release.set()
    t.join(timeout=5)
    assert not _active(r), "token otpushchen pri vyhode"


def test_turn_mutual_exclusion_many_threads(gate_env):
    """Odnovremenno vnutri bloka — rovno odin (max concurrence == 1)."""
    from mnema.core.llm_gate import llm_turn
    r = gate_env
    done = []
    errs = []
    state = {"active": 0, "max": 0}
    lk = threading.Lock()

    def worker():
        try:
            with llm_turn():
                with lk:
                    state["active"] += 1
                    state["max"] = max(state["max"], state["active"])
                time.sleep(0.08)
                with lk:
                    state["active"] -= 1
            done.append(1)
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=worker) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    assert not errs, errs
    assert len(done) == 5
    assert state["max"] == 1
    assert r.llen("mnema:llm:queue") == 0


def test_turn_fifo_order(gate_env):
    """Kto ranshe zanyal ochered' — tot ranshe poluchit token."""
    from mnema.core.llm_gate import llm_turn
    entered = threading.Event()
    release = threading.Event()
    order = []
    lk = threading.Lock()

    def holder():
        with llm_turn():
            entered.set()
            release.wait(timeout=10)

    def waiter(idx):
        with llm_turn():
            with lk:
                order.append(idx)

    th = threading.Thread(target=holder)
    th.start()
    assert entered.wait(timeout=5)
    ws = []
    for i in range(3):
        t = threading.Thread(target=waiter, args=(i,))
        t.start()
        ws.append(t)
        time.sleep(0.3)  # garanta poryadka vstupleniya v ochered'
    release.set()
    for t in ws + [th]:
        t.join(timeout=20)
    assert order == [0, 1, 2]


def test_turn_wait_timeout(gate_env, monkeypatch):
    from redis import Redis
    from mnema.core.llm_gate import CURRENT_KEY, LLMGateTimeout, llm_turn
    gate_env.set(CURRENT_KEY, "chuzhoy-bilet", ex=60)
    monkeypatch.setenv("LLM_LOCK_WAIT_TIMEOUT_S", "0.2")
    with pytest.raises(LLMGateTimeout):
        with llm_turn():
            pass


def test_turn_releases_ticket_of_dead_waiter(gate_env):
    """Bilet bez zhivogo heartbeat vymetaetsya iz ocheredi (net golopaniya)."""
    from mnema.core.llm_gate import QUEUE_KEY, llm_turn
    r = gate_env
    r.rpush(QUEUE_KEY, "mertvyy-bilet")  # bez hb klyucha
    with llm_turn():  # dolzhen proyti, a ne zhdat neproyodnogo
        pass
    assert r.lrange(QUEUE_KEY, 0, -1) == []


def test_turn_release_on_exception(gate_env):
    from mnema.core.llm_gate import QUEUE_KEY, llm_turn
    r = gate_env
    with pytest.raises(RuntimeError):
        with llm_turn():
            raise RuntimeError("job upal")
    assert not _active(r)
    assert r.llen(QUEUE_KEY) == 0


# ------------------------------------------------------------------ think
@pytest.fixture(autouse=True)
def _think_flag_clean(monkeypatch):
    import mnema.core.llm_client as lc
    monkeypatch.setattr(lc, "_THINK_REJECTED", False)


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {"choices": [{"message": {"content": "{}"}}]}
        self.text = text

    def json(self):
        return self._payload


def test_think_false_by_default(monkeypatch):
    sent = []

    def fake_post(payload):
        sent.append(dict(payload))
        return _Resp()

    monkeypatch.setattr("mnema.core.llm_client._post_chat", fake_post)
    monkeypatch.delenv("LLM_THINKING", raising=False)
    from mnema.core.llm_client import _chat
    _chat([{"role": "user", "content": "x"}], retries=0)
    assert sent[0]["think"] is False
    assert sent[0]["thinking"]["type"] == "disabled"


def test_think_on_when_env_enabled(monkeypatch):
    sent = []

    def fake_post(payload):
        sent.append(dict(payload))
        return _Resp()

    monkeypatch.setattr("mnema.core.llm_client._post_chat", fake_post)
    monkeypatch.setenv("LLM_THINKING", "on")
    from mnema.core.llm_client import _chat
    _chat([{"role": "user", "content": "x"}], retries=0)
    assert sent[0]["think"] is True
    assert sent[0]["thinking"]["type"] == "enabled"


def test_think_rejected_fallback_once(monkeypatch):
    """400 'Unknown field think' -> povtor bez poly think, dalshe bez nego."""
    sent = []

    def fake_post(payload):
        sent.append(dict(payload))
        if "think" in payload:
            return _Resp(400, text="unknown field 'think' in request")
        return _Resp(payload={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("mnema.core.llm_client._post_chat", fake_post)
    monkeypatch.delenv("LLM_THINKING", raising=False)
    import mnema.core.llm_client as lc
    assert lc._chat([{"role": "user", "content": "x"}], retries=1) == "ok"
    assert "think" not in sent[-1]
    # vtoroy zapros uzhe bez think (flag zapomnen)
    assert lc._chat([{"role": "user", "content": "y"}], retries=0) == "ok"
    assert "think" not in sent[-1]


def test_max_tokens_in_payload_and_zero_off(monkeypatch):
    sent = []

    def fake_post(payload):
        sent.append(dict(payload))
        return _Resp()

    monkeypatch.setattr("mnema.core.llm_client._post_chat", fake_post)
    from mnema.core.llm_client import _chat
    _chat([{"role": "user", "content": "x"}], retries=0)
    assert sent[0]["max_tokens"] == 6000
    assert sent[0]["num_predict"] == 6000
    monkeypatch.setenv("LLM_MAX_TOKENS", "0")  # 0 = poly ne peredautsya
    _chat([{"role": "user", "content": "y"}], retries=0)
    assert "max_tokens" not in sent[-1] and "num_predict" not in sent[-1]


def test_http_error_still_raises(monkeypatch):
    def fake_post(payload):
        return _Resp(500, text="boom")

    monkeypatch.setattr("mnema.core.llm_client._post_chat", fake_post)
    from mnema.core.llm_client import LLMError, _chat
    with pytest.raises(LLMError, match="500"):
        _chat([{"role": "user", "content": "x"}], retries=0)


def test_post_chat_wraps_gate(monkeypatch):
    """LLMGateTimeout -> LLMError (konveyer: awaiting_llm, a ne padeniye)."""
    from contextlib import contextmanager

    from mnema.core import llm_gate
    import mnema.core.llm_client as lc

    @contextmanager
    def failing_turn():
        raise llm_gate.LLMGateTimeout("zhdal dolgo")
        yield

    monkeypatch.setattr(llm_gate, "llm_turn", failing_turn)
    monkeypatch.setenv("LLM_BASE_URL", "http://example.invalid/v1")
    with pytest.raises(lc.LLMError, match="zhdal dolgo"):
        lc._post_chat({"model": "m", "messages": []})
