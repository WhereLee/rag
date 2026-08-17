"""
Token 预算控制：每用户每日 Token 消耗限额。

设计：
- Redis 计数器：key = `rag:token_budget:{user_id}:{date}` → 当日累计 tokens
- 阈值：默认每日 200K tokens（约 100 次标准问答）
- 80% 时告警日志，100% 时拒绝请求
- TTL 25 小时（覆盖跨日边界）
"""
import logging
import time
from datetime import datetime

import config

logger = logging.getLogger("rag.security")

# Redis key 前缀
KEY_PREFIX = "rag:token_budget"

# 默认每日限额（可通过环境变量覆盖）
DAILY_TOKEN_LIMIT = int(__import__("os").getenv("DAILY_TOKEN_LIMIT", "200000"))
# 告警阈值（80%）
WARN_RATIO = 0.8


def _redis():
    """获取 Redis 客户端（延迟导入，避免启动时依赖）。"""
    import redis as redis_lib
    return redis_lib.Redis.from_url(config.REDIS_URL, socket_timeout=3, decode_responses=True)


def _today_key(user_id: int) -> str:
    return f"{KEY_PREFIX}:{user_id}:{datetime.now().strftime('%Y%m%d')}"


def check_budget(user_id: int | None) -> tuple[bool, str]:
    """检查用户是否有剩余预算。返回 (allowed, reason)。

    user_id=None 时跳过检查（admin/评估场景）。
    """
    if user_id is None:
        return True, ""
    try:
        r = _redis()
        key = _today_key(user_id)
        used = int(r.get(key) or 0)
        if used >= DAILY_TOKEN_LIMIT:
            logger.warning("token budget exceeded: user=%d used=%d limit=%d",
                           user_id, used, DAILY_TOKEN_LIMIT)
            return False, f"今日 Token 额度已用尽（{used}/{DAILY_TOKEN_LIMIT}），请明日再试"
        if used >= DAILY_TOKEN_LIMIT * WARN_RATIO:
            logger.info("token budget warning: user=%d used=%d/%d (%.0f%%)",
                        user_id, used, DAILY_TOKEN_LIMIT,
                        used / DAILY_TOKEN_LIMIT * 100)
        return True, ""
    except Exception as e:
        # Redis 故障时降级放行（不阻断业务）
        logger.warning("token budget check failed (fallback allow): %s", e)
        return True, ""


def record_usage(user_id: int | None, tokens: int):
    """记录一次 Token 消耗。

    使用 Redis INCRBY 原子操作，TTL 25 小时。
    """
    if user_id is None or tokens <= 0:
        return
    try:
        r = _redis()
        key = _today_key(user_id)
        pipe = r.pipeline()
        pipe.incrby(key, tokens)
        pipe.expire(key, 25 * 3600)  # 25 小时后自动清理
        pipe.execute()
    except Exception as e:
        logger.warning("token budget record failed: %s", e)


def get_usage(user_id: int | None) -> dict:
    """获取用户当日使用情况（诊断/API 查询用）。"""
    if user_id is None:
        return {"limit": DAILY_TOKEN_LIMIT, "used": 0, "remaining": DAILY_TOKEN_LIMIT}
    try:
        r = _redis()
        key = _today_key(user_id)
        used = int(r.get(key) or 0)
        return {
            "limit": DAILY_TOKEN_LIMIT,
            "used": used,
            "remaining": max(0, DAILY_TOKEN_LIMIT - used),
            "ratio": round(used / DAILY_TOKEN_LIMIT, 4) if DAILY_TOKEN_LIMIT > 0 else 0,
        }
    except Exception as e:
        logger.warning("token budget get_usage failed: %s", e)
        return {"limit": DAILY_TOKEN_LIMIT, "used": 0, "remaining": DAILY_TOKEN_LIMIT}
