"""DeepSeek（OpenAI 兼容端点）网关实现。

只依赖 `httpx`，请求体手工构造 —— 这样测试可以注入一个假的 `httpx.Client`
（`MockTransport`）而**不需要联网**。

与供应商的差异都在本层消化（计划第 619 行）：
- 推理档位：本项目用 `off/low/high/max`；`off` 对应「关闭思考」而不是
  一个 `reasoning_effort` 值，故翻译成 `thinking: {"type": "disabled"}`；
  其余三档直传 `reasoning_effort`。
- 结构化输出：用 `response_format={"type": "json_object"}`；仍拿不到合法 JSON
  时**抛 `StructuredOutputError`**，绝不退化成纯文本（那会让下游拿到一个
  "看起来有结构"的字符串）。
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.gateways.base import (
    ModelGateway,
    ModelResult,
    ModelUnavailableError,
    ModelRequestError,
    StructuredOutputError,
)

DEFAULT_TIMEOUT_SECONDS = 120.0

#: 有些模型即便开了 json_object 也会把 JSON 包在 ``` 围栏里，这里兜一层
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def parse_json_content(content: str) -> dict[str, Any] | None:
    """从模型输出里尽力取出 JSON 对象；取不到返回 None。

    依次尝试：直接解析 → 去掉 ``` 围栏 → 截取第一个 `{` 到最后一个 `}`。
    这些都是**格式容错**，不是语义降级：拿到的仍是结构化对象。
    """
    text = content.strip()
    if not text:
        return None

    for candidate in (text, _strip_fence(text), _slice_braces(text)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _strip_fence(text: str) -> str | None:
    match = _FENCE_RE.search(text)
    return match.group(1) if match else None


def _slice_braces(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]


def build_structured_prompt(prompt: str, schema: dict[str, Any]) -> str:
    """把 JSON Schema 要求并进 prompt。

    `generate` 的签名（计划第 610 行）没有单独的 system 参数，故结构化约束
    由本层拼进 prompt —— 这也正是「供应商差异在 Gateway 内部消化」的一部分。
    """
    schema_text = json.dumps(schema, ensure_ascii=False)
    return (
        f"{prompt}\n\n"
        "---\n"
        "只输出一个 JSON 对象，不要输出任何解释文字或 Markdown 代码块。\n"
        f"必须满足以下 JSON Schema：\n{schema_text}\n"
    )


class DeepSeekGateway(ModelGateway):
    """DeepSeek 官方 API（OpenAI 兼容）。"""

    provider = "deepseek"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url 不能为空")
        if not api_key:
            raise ValueError("api_key 不能为空")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout = timeout
        self._client = client

    # ---------- 内部 ----------

    def _make_client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )

    @staticmethod
    def _reasoning_fields(reasoning: str | None) -> dict[str, Any]:
        """把本项目的档位翻译成 DeepSeek 的请求字段。"""
        if reasoning in (None, "", "off"):
            # off 不是一个 reasoning_effort 取值，而是"关闭思考"
            return {"thinking": {"type": "disabled"}}
        return {"thinking": {"type": "enabled"}, "reasoning_effort": reasoning}

    # ---------- 契约 ----------

    def generate(
        self,
        prompt: str,
        schema: dict[str, Any] | None,
        max_output_tokens: int,
        *,
        model: str,
        reasoning: str | None = None,
    ) -> ModelResult:
        if not model:
            raise ValueError("model 不能为空：型号应由 Router 从配置解析后传入")
        if max_output_tokens <= 0:
            raise ValueError(f"max_output_tokens 必须为正，收到 {max_output_tokens}")

        effective_prompt = build_structured_prompt(prompt, schema) if schema else prompt
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": effective_prompt}],
            "max_tokens": max_output_tokens,
            **self._reasoning_fields(reasoning),
        }
        if schema is not None:
            payload["response_format"] = {"type": "json_object"}

        body = self._post("/chat/completions", payload)
        return self._to_result(body, schema=schema)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        client = self._client or self._make_client()
        owns_client = self._client is None
        try:
            response = client.post(path, json=payload)
        except httpx.HTTPError as exc:
            # 网络层问题：可重试，交给上层（阶段 08 的降级链）
            raise ModelUnavailableError(f"请求模型服务失败：{type(exc).__name__}: {exc}") from exc
        finally:
            if owns_client:
                client.close()

        if response.status_code >= 500:
            raise ModelUnavailableError(
                f"模型服务返回 {response.status_code}"
            )
        if response.status_code == 429:
            raise ModelUnavailableError("模型服务限流（429）")
        if response.status_code >= 400:
            # 4xx 多为输入/配置问题，重试无意义，但仍归入 Gateway 错误族
            raise ModelRequestError(
                f"模型服务拒绝请求（HTTP {response.status_code}），请检查凭据和参数"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise ModelUnavailableError(f"模型服务返回的不是 JSON：{response.text[:300]}") from exc

    @staticmethod
    def _to_result(body: dict[str, Any], *, schema: dict[str, Any] | None) -> ModelResult:
        choices = body.get("choices") or []
        if not choices:
            raise ModelUnavailableError(f"模型服务未返回 choices：{str(body)[:300]}")

        message = choices[0].get("message") or {}
        content = str(message.get("content") or "")

        usage = body.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        tokens_input = int(usage.get("prompt_tokens") or 0)
        tokens_output = int(usage.get("completion_tokens") or 0)

        parsed: dict[str, Any] | None = None
        if schema is not None:
            parsed = parse_json_content(content)
            if parsed is None:
                raise StructuredOutputError(
                    "要求结构化输出但未能解析出 JSON 对象；"
                    f"原始内容前 300 字符：{content[:300]!r}"
                )

        return ModelResult(
            content=content,
            parsed=parsed,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            # 用服务端回报的模型名：它才是真正应答的型号
            model=str(body.get("model") or ""),
            raw=body,
            reasoning_tokens=int(details.get("reasoning_tokens") or 0),
            cached_tokens=int(usage.get("prompt_cache_hit_tokens") or 0),
        )
