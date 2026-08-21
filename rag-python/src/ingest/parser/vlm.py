"""MiMo VLM 客户端（最小实现，重写不引用旧代码）。

- OpenAI 兼容 chat/completions，图片走 data URL
- 结果按图片内容 hash 落盘缓存（data/parsed/vlm_cache，成本控制）
- 响应容错解析：纯 JSON / ```json 围栏 / 截取首尾大括号；非法 JSON 重试后降级
- 可注入 client（测试用 fake），超时/重试可配
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from pathlib import Path

import httpx

logger = logging.getLogger("rag.vlm2")

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _img_to_data_url(png_bytes: bytes) -> str:
    b64 = base64.b64encode(png_bytes).decode()
    return f"data:image/png;base64,{b64}"


def _build_messages(prompt: str, png_bytes: bytes) -> list:
    """OpenAI 兼容多模态消息（文本 + 图片 data URL）。"""
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": _img_to_data_url(png_bytes)}},
        ],
    }]


def to_png_bytes(img_bytes: bytes, max_dim: int = 1568) -> bytes:
    """任意常见图片格式 → PNG bytes（统一 VLM 输入）。

    Pillow 解码（真实像素尺寸，不受 DPI 元数据影响）；边长超限等比缩放。
    """
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(img_bytes))
    img.load()
    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def extract_json(text: str) -> dict | None:
    """容错解析模型输出：纯 JSON / 围栏包裹 / 截取首尾大括号。"""
    if not text:
        return None
    text = text.strip()
    m = _JSON_FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    # 截取首个 { 到最后一个 }
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            obj = json.loads(text[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


class VLMClient:
    """图片 → JSON 结构化内容。失败抛 VLMError（调用方按降级链处理）。"""

    def __init__(self, api_key: str = "", base_url: str = "https://api.xiaomimimo.com/v1",
                 model: str = "mimo-v2.5", timeout: float = 120, max_retries: int = 2,
                 cache_dir: Path | None = None) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.cache_dir = cache_dir  # None = 不缓存（测试场景关闭）

    # ---- 对外：图片 → JSON ----
    def chat_image(self, prompt: str, png_bytes: bytes, cache_prefix: str) -> dict:
        """调用并返回 JSON 对象（结果已校验为 dict）。缓存命中直接返回。"""
        key = f"{cache_prefix}_{hashlib.sha256(png_bytes).hexdigest()[:32]}"
        cached = self._cache_get(key)
        if cached is not None:
            logger.info("vlm cache hit: %s", key)
            return cached

        messages = _build_messages(prompt, png_bytes)
        obj = self._chat_json(messages)
        self._cache_put(key, obj)
        return obj

    # ---- 对外：图片 → 原始文本（扫描页转录，不要求 JSON） ----
    def chat_text(self, prompt: str, png_bytes: bytes, cache_prefix: str) -> str:
        """图片 → 原始文本。非法 JSON/纯文本输出均可接受（转录场景）。"""
        key = f"{cache_prefix}_{hashlib.sha256(png_bytes).hexdigest()[:32]}"
        cached = self._cache_get(key)
        if cached is not None:
            logger.info("vlm cache hit: %s", key)
            return cached.get("text") or ""

        messages = _build_messages(prompt, png_bytes)
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                content = self._chat_raw(messages).strip()
                if content:
                    self._cache_put(key, {"text": content})
                    return content
                raise ValueError("空输出")
            except Exception as e:
                last_err = e
                logger.warning("vlm text attempt %d failed: %s", attempt + 1, e)
                time.sleep(1.0 * (attempt + 1))
        raise VLMError(f"VLM 转录失败: {last_err}")

    # ---- 底层调用：chat → JSON（重试 + 容错） ----
    def _chat_json(self, messages: list) -> dict:
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                content = self._chat_raw(messages)
                obj = extract_json(content)
                if obj is None:
                    raise ValueError("模型输出非 JSON")
                return obj
            except Exception as e:  # 网络错误 / 非 JSON 均重试
                last_err = e
                logger.warning("vlm attempt %d failed: %s", attempt + 1, e)
                time.sleep(1.0 * (attempt + 1))
        raise VLMError(f"VLM 调用失败: {last_err}")

    def _chat_raw(self, messages: list) -> str:
        if not self.api_key:
            raise VLMError("MIMO_API_KEY 未配置")
        resp = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": messages, "max_tokens": 4096},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise VLMError(f"响应结构异常: {e}") from e

    # ---- 落盘缓存 ----
    def _cache_get(self, key: str) -> dict | None:
        if self.cache_dir is None:
            return None
        f = self.cache_dir / f"{key}.json"
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return None
        return None

    def _cache_put(self, key: str, obj: dict) -> None:
        if self.cache_dir is None:
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.cache_dir / f"{key}.json").write_text(
                json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            logger.warning("vlm cache write failed: %s", e)


class VLMError(Exception):
    """VLM 调用失败（网络/非 JSON/配置缺失）。"""
