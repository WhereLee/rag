"""结构化日志：控制台 INFO 简洁格式 + 文件 DEBUG JSON 行。"""
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import config

LOG_DIR = config.RAG_ROOT / "rag-python" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # 允许通过 extra 携带结构化字段
        for k, v in record.__dict__.items():
            if k.startswith("ctx_"):
                payload[k[4:]] = v
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO"):
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(name)s] %(message)s", "%H:%M:%S"))
    root.addHandler(console)

    fileh = logging.FileHandler(LOG_DIR / "rag.log", encoding="utf-8")
    fileh.setLevel(logging.DEBUG)
    fileh.setFormatter(JsonFormatter())
    root.addHandler(fileh)

    # 降噪
    for noisy in ("httpx", "httpcore", "jieba"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
