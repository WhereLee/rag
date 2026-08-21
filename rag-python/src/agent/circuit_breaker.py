"""
LLM 断路器（Circuit Breaker）：连续失败 N 次后自动切换到"仅检索模式"。

三态模型：
  CLOSED   → 正常工作，LLM 调用正常
  OPEN     → LLM 连续失败 ≥ 阈值，跳过 LLM，直接返回 top-3 原文
  HALF_OPEN → 冷却期过后尝试一次 LLM 调用，成功则恢复，失败则继续 OPEN

设计要点：
- 线程安全：所有状态操作加锁（单进程足够，分布式换 Redis）
- 自动恢复：冷却期（默认 60s）过后自动尝试恢复
- 不阻断业务：断路器故障时 fallback 到 CLOSED（宁可花钱也不停服务）
- 日志可观测：每次状态转换都记录告警
"""
import logging
import threading
import time
from enum import Enum

logger = logging.getLogger("rag.circuit_breaker")


class State(Enum):
    CLOSED = "closed"       # 正常
    OPEN = "open"           # 熔断
    HALF_OPEN = "half_open" # 探测中


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, recovery_timeout: int = 60):
        """
        Args:
            failure_threshold: 连续失败多少次后触发熔断
            recovery_timeout: 熔断后等待多少秒进入半开状态（尝试恢复）
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout

        self._state = State.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> State:
        """当前状态（线程安全，自动检查是否该从 OPEN 转到 HALF_OPEN）。"""
        with self._lock:
            if self._state == State.OPEN:
                if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                    self._state = State.HALF_OPEN
                    logger.warning("Circuit breaker: OPEN → HALF_OPEN (attempting recovery)")
            return self._state

    def allow_request(self) -> bool:
        """是否允许本次请求通过 LLM。

        CLOSED / HALF_OPEN → 允许
        OPEN → 拒绝（走降级路径）
        """
        return self.state in (State.CLOSED, State.HALF_OPEN)

    def record_success(self):
        """记录一次成功的 LLM 调用。"""
        with self._lock:
            if self._state != State.CLOSED:
                logger.info("Circuit breaker: %s → CLOSED (recovered)", self._state.value)
            self._state = State.CLOSED
            self._failure_count = 0

    def record_failure(self):
        """记录一次失败的 LLM 调用。"""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            if self._state == State.HALF_OPEN:
                # 半开状态失败 → 直接回到 OPEN
                self._state = State.OPEN
                logger.warning("Circuit breaker: HALF_OPEN → OPEN (probe failed, "
                               "next retry in %ds)", self.recovery_timeout)
            elif self._failure_count >= self.failure_threshold:
                self._state = State.OPEN
                logger.warning("Circuit breaker: CLOSED → OPEN (%d consecutive failures, "
                               "cooldown %ds)", self._failure_count, self.recovery_timeout)

    def get_metrics(self) -> dict:
        """诊断用：返回当前断路器状态。"""
        return {
            "state": self.state.value,
            "failure_count": self._failure_count,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout": self.recovery_timeout,
            "seconds_since_last_failure": (
                round(time.monotonic() - self._last_failure_time, 1)
                if self._last_failure_time > 0 else None
            ),
        }


# 按用途实例化：问答 / 入库 VLM 各自独立熔断，互不干扰
_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = threading.Lock()

# 用途参数：问答重试便宜（threshold 3）；入库 VLM 调用贵且对成本敏感（threshold 5，少误伤）
_BREAKER_PARAMS = {
    "qa":        {"failure_threshold": 3, "recovery_timeout": 60},
    "vlm_ingest": {"failure_threshold": 5, "recovery_timeout": 120},
}


def get_breaker(name: str = "qa") -> CircuitBreaker:
    """按用途获取断路器实例（线程安全懒加载）。默认 'qa' 兼容历史调用。"""
    global _breakers
    if name not in _breakers:
        with _breakers_lock:
            if name not in _breakers:
                _breakers[name] = CircuitBreaker(**_BREAKER_PARAMS.get(name, _BREAKER_PARAMS["qa"]))
    return _breakers[name]
