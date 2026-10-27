"""Global'nyy exklyuzivnyy zamyok dlya zaprosov k LLM (odnovremenno — odin analiz).

Tocken-zamyok s FIFO-ocheryed'yu na redis: derzhatel' — klyuch s TTL-arendoy
(umershiy process otpustit ne bolee arendy), ozhidayushchiye — list biletov;
bilety merthvykh ozhidayushchikh vymetayutsya po heartbeat (TTL).
Pri otsutstvii redis (direct-rezhim testov) — no-op: single-processu
zamyok ne nuzhen.

Vladenie — na odin HTTP-zapros (ne na vsey job): okna map-reduce i split
peredayut token drug drugu, chtoby dolgaya stenogramma ne zanjimala LLM
navsegda, no s sohraneniem poryadka FIFO mezhdu zadachami.
"""
from __future__ import annotations

import contextlib
import logging
import os
import time
import uuid

log = logging.getLogger("mnema.llm_gate")

CURRENT_KEY = "mnema:llm:current"   # id bileta tekushchego vladetel'ya
QUEUE_KEY = "mnema:llm:queue"       # FIFO ochered' biletov (head — sled.)
HB_PREFIX = "mnema:llm:hb:"         # heartbeat zhivogo ozhidayushchego
HB_TTL_S = 30                       # zhivoy ozhidayet obnovlyayet kazhdy poll

# atomichnye Lua: bez gonok mezhdu get/set
_LUA_CLAIM = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
if redis.call('LINDEX', KEYS[2], 0) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2]))
redis.call('LPOP', KEYS[2])
redis.call('DEL', ARGV[3] .. ARGV[1])
return 1
"""
_LUA_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('DEL', KEYS[1])
end
redis.call('LREM', KEYS[2], 0, ARGV[1])
redis.call('DEL', ARGV[2] .. ARGV[1])
return 1
"""
_LUA_PRUNE = """
for _, id in ipairs(redis.call('LRANGE', KEYS[1], 0, -1)) do
  if id ~= ARGV[1] and redis.call('EXISTS', KEYS[2] .. id) == 0 then
    redis.call('LREM', KEYS[1], 0, id)
  end
end
return 1
"""


class LLMGateTimeout(TimeoutError):
    """Ozhidanie svoey ocheredi prevysilo LLM_LOCK_WAIT_TIMEOUT_S."""


def _wait_timeout_s() -> float:
    try:
        return float(os.environ.get("LLM_LOCK_WAIT_TIMEOUT_S", "3600") or 3600)
    except ValueError:
        return 3600.0


def _lease_s() -> int:
    """Arenda tokena: bol'she maksimal'nogo zaprosa (LLM_REQUEST_TIMEOUT_S)
    s zapasom — zhivoy derzhatel' uspeyayet zakonchit' zapol do istecheniya."""
    try:
        req = float(os.environ.get("LLM_REQUEST_TIMEOUT_S", "900") or 900)
    except ValueError:
        req = 900.0
    try:
        return max(int(req) + 60, int(os.environ.get("LLM_LOCK_LEASE_S", "720") or 720))
    except ValueError:
        return 960


def _poll_s() -> float:
    try:
        return max(0.01, float(os.environ.get("LLM_LOCK_POLL_S", "2") or 2))
    except ValueError:
        return 2.0


@contextlib.contextmanager
def llm_turn():
    """Blokiruet LLM dlya zaprosov vnutri bloka: zhdyot svoey ocheredi (FIFO),
    pri vyhode otpuskayet token. Bez redis — prosto vhod v blok (no-op)."""
    try:
        from .queue import redis_url
        from redis import Redis
        r = Redis.from_url(redis_url(), socket_connect_timeout=2)
        r.ping()
    except Exception as e:  # noqa: BLE001 — net redis: direct-rezhim, bez zamyka
        log.debug("llm gate disabled (no redis: %s)", e)
        yield
        return

    ticket = uuid.uuid4().hex
    lease = _lease_s()
    deadline = time.monotonic() + _wait_timeout_s()
    acquired = False
    unlocked = False
    r.set(HB_PREFIX + ticket, "1", ex=HB_TTL_S)  # do rpush: chtoby nas ne vyprunedili
    r.rpush(QUEUE_KEY, ticket)
    while True:
        r.set(HB_PREFIX + ticket, "1", ex=HB_TTL_S)  # heartbeat zhivogo ozhidaniya
        try:
            if r.eval(_LUA_CLAIM, 2, CURRENT_KEY, QUEUE_KEY,
                      ticket, lease, HB_PREFIX) == 1:
                acquired = True
                break
            r.eval(_LUA_PRUNE, 2, QUEUE_KEY, HB_PREFIX, ticket)
        except Exception as e:  # noqa: BLE001 — redis otkazal: pustyay bez zamyka
            log.warning("llm gate redis error (%s) — proceeding unlocked", e)
            unlocked = True
            break
        if time.monotonic() > deadline:
            break
        time.sleep(_poll_s())
    try:
        if not acquired and not unlocked:
            raise LLMGateTimeout(
                f"LLM zanyat: ozhidanie ocheredi > {_wait_timeout_s():.0f} s")
        yield
    finally:
        if not unlocked:
            try:
                if acquired:
                    r.eval(_LUA_RELEASE, 2, CURRENT_KEY, QUEUE_KEY,
                           ticket, HB_PREFIX)
                r.lrem(QUEUE_KEY, 0, ticket)
                r.delete(HB_PREFIX + ticket)
            except Exception as e:  # noqa: BLE001 — arenda istekhet sama
                log.warning("llm gate release failed: %s", e)
