"""
MiMo LLM 客户端（OpenAI 兼容协议）。

设计要点：
- 思考档位路由：thinking=True 走深度推理（生成/诊断），False 走轻任务（分类/评分/改写）
- 超时 + 指数退避重试；流式 SSE；结构化 JSON 输出辅助
- token 统计随响应返回，供可观测性层落库
"""
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Iterator

import httpx

import config

logger = logging.getLogger("rag.llm")


class LLMError(RuntimeError):
    """LLM 调用最终失败（已重试）。"""


@dataclass
class LLMResult:
    content: str
    reasoning: str = ""
    token_in: int = 0
    token_out: int = 0
    model: str = ""
    finish_reason: str = ""
    elapsed_ms: int = 0


class MiMoClient:
    def __init__(self, api_key: str = "", base_url: str = "", model: str = ""):
        self.api_key = api_key or config.MIMO_API_KEY
        self.base_url = (base_url or config.MIMO_BASE_URL).rstrip("/")
        self.model = model or config.MIMO_MODEL
        if not self.api_key:
            raise LLMError("MIMO_API_KEY 未配置")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _payload(self, messages: list[dict], thinking: bool, temperature: float,
                 max_tokens: int, response_json: bool, extra: dict) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "enable_thinking": thinking,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if response_json:
            payload["response_format"] = {"type": "json_object"}
        if extra:
            payload.update(extra)
        return payload

    def chat(self, messages: list[dict], thinking: bool = False,
             temperature: float = None, max_tokens: int = 4096,
             response_json: bool = False, **extra) -> LLMResult:
        """同步调用，带重试（指数退避）。

        注意：mimo-v2.5 的 reasoning tokens 计入 max_tokens 预算，
        即使 enable_thinking=false 仍有少量内部思考。预算不足时正文被挤空
        （finish=length 且 content 为空）→ 自动加倍预算重试一次。
        """
        payload = self._payload(messages, thinking, temperature, max_tokens, response_json, extra)
        last_err: Exception | None = None
        budget_cap = 8192   # 预算递增上限：reasoning 挤占正文时逐级加大
        # 日志：记录 prompt 摘要（仅前 200 字符，避免泄露敏感内容）
        prompt_summary = ""
        if messages:
            last_msg = messages[-1].get("content", "")
            prompt_summary = last_msg[:200] + ("..." if len(last_msg) > 200 else "")
        for attempt in range(config.LLM_MAX_RETRIES + 1):
            start = time.perf_counter()
            try:
                with httpx.Client(timeout=config.LLM_TIMEOUT) as client:
                    resp = client.post(f"{self.base_url}/chat/completions",
                                       headers=self._headers(),
                                       content=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                if resp.status_code >= 400:
                    # 4xx（除限流）重试无意义，直接抛
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                data = resp.json()
                choice = data["choices"][0]
                msg = choice["message"]
                usage = data.get("usage") or {}
                content = msg.get("content") or ""
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                # 预算耗尽但正文为空 → reasoning 挤占，逐级加大预算重试（不计入失败重试次数）
                if not content and choice.get("finish_reason") == "length":
                    nxt = min(payload["max_tokens"] * 2, budget_cap)
                    if nxt > payload["max_tokens"]:
                        payload["max_tokens"] = nxt
                        logger.warning("empty content with finish=length; retry with max_tokens=%d", nxt)
                        continue
                result = LLMResult(
                    content=content,
                    reasoning=msg.get("reasoning_content") or "",
                    token_in=usage.get("prompt_tokens", 0),
                    token_out=usage.get("completion_tokens", 0),
                    model=data.get("model", self.model),
                    finish_reason=choice.get("finish_reason", ""),
                    elapsed_ms=elapsed_ms,
                )
                # 结构化日志：记录每次 LLM 调用
                logger.info(
                    "LLM call: model=%s thinking=%s tokens_in=%d tokens_out=%d "
                    "elapsed=%dms finish=%s prompt_preview=%.200s",
                    result.model, thinking, result.token_in, result.token_out,
                    result.elapsed_ms, result.finish_reason, prompt_summary,
                    extra={"ctx_model": result.model, "ctx_tokens_in": result.token_in,
                           "ctx_tokens_out": result.token_out, "ctx_elapsed_ms": result.elapsed_ms,
                           "ctx_finish": result.finish_reason, "ctx_thinking": thinking})
                return result
            except (httpx.HTTPError, LLMError, KeyError, json.JSONDecodeError) as e:
                last_err = e
                if isinstance(e, LLMError) and "HTTP 4" in str(e):
                    break
                wait = 2 ** attempt
                logger.warning("LLM call failed (attempt %d/%d): %s; retry in %ds",
                               attempt + 1, config.LLM_MAX_RETRIES + 1, e, wait)
                time.sleep(wait)
        raise LLMError(f"LLM 调用失败（已重试）: {last_err}")

    def chat_with_tools(self, messages: list[dict], tools: list[dict] | None = None,
                        thinking: bool = False, max_tokens: int = 4096) -> dict:
        """Function Calling 调用：返回 {content, tool_calls, token_in, token_out, elapsed_ms}。

        tool_calls 格式：[{"name": "...", "arguments": {...}, "id": "..."}]
        如果没有 tool_calls，tool_calls 为空列表。
        """
        payload = self._payload(messages, thinking, None, max_tokens, False, {})
        if tools:
            payload["tools"] = tools
        start = time.perf_counter()
        with httpx.Client(timeout=config.LLM_TIMEOUT) as client:
            resp = client.post(f"{self.base_url}/chat/completions",
                               headers=self._headers(),
                               content=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if resp.status_code >= 400:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        choice = data["choices"][0]
        msg = choice["message"]
        usage = data.get("usage") or {}
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        tool_calls = []
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                try:
                    args = json.loads(tc["function"]["arguments"])
                except (json.JSONDecodeError, KeyError):
                    args = {}
                tool_calls.append({
                    "name": tc["function"]["name"],
                    "arguments": args,
                    "id": tc.get("id", ""),
                })

        result = {
            "content": (msg.get("content") or "").strip(),
            "tool_calls": tool_calls,
            "token_in": usage.get("prompt_tokens", 0),
            "token_out": usage.get("completion_tokens", 0),
            "elapsed_ms": elapsed_ms,
            "finish_reason": choice.get("finish_reason", ""),
        }
        logger.info("LLM tools call: model=%s tool_calls=%d tokens_in=%d tokens_out=%d elapsed=%dms",
                     data.get("model", self.model), len(tool_calls),
                     result["token_in"], result["token_out"], elapsed_ms)
        return result

    def chat_json(self, messages: list[dict], thinking: bool = False,
                  max_tokens: int = 2048, **extra) -> dict:
        """要求 JSON 输出并解析；解析失败时尝试从文本中提取 JSON 块。
    
        不用平台的 response_format=json_object（实测与混合推理叠加时
        偶发空响应）；改为提示词约束 + 宽容解析，更稳。
        """
        result = self.chat(messages, thinking=thinking, max_tokens=max_tokens, **extra)
        text = (result.content or "").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 兑底：从 markdown 代码块或首个 { 到末尾 } 提取
            start, end = text.find("{"), text.rfind("}")
            if 0 <= start < end:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    pass
            raise LLMError(f"LLM JSON 输出解析失败: {text[:200]}")

    def stream(self, messages: list[dict], thinking: bool = True,
               temperature: float = None, max_tokens: int = 4096) -> Iterator[str]:
        """流式输出正文 chunk（reasoning 内容不透出给用户）。"""
        payload = self._payload(messages, thinking, temperature, max_tokens, False, {})
        payload["stream"] = True
        with httpx.Client(timeout=config.LLM_TIMEOUT) as client:
            with client.stream("POST", f"{self.base_url}/chat/completions",
                               headers=self._headers(),
                               content=json.dumps(payload, ensure_ascii=False).encode("utf-8")) as resp:
                if resp.status_code >= 400:
                    raise LLMError(f"HTTP {resp.status_code}: {resp.read().decode('utf-8', 'ignore')[:300]}")
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield piece


_client: MiMoClient | None = None


def get_client() -> MiMoClient:
    global _client
    if _client is None:
        _client = MiMoClient()
    return _client
