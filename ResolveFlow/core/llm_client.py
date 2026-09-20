"""
统一 LLM 客户端封装。

ResolveFlow 各模块过去直接使用 anthropic.AsyncAnthropic 调用 Messages API。
这在"Anthropic 协议兼容"的第三方（如 DeepSeek）上工作正常，但像 Qwen（DashScope）
这类只提供 OpenAI Chat Completions 兼容协议的服务无法直接接入——请求体结构、
system prompt 的传递方式、响应体结构都不一样。

这里加一层薄封装：调用方统一用 create() 拿到纯文本回复，底层根据 provider
选择 Anthropic Messages API 或 OpenAI Chat Completions API，业务代码不需要
关心协议差异。

provider（通过构造参数或 LLM_PROVIDER 环境变量指定）：
  - "anthropic"（默认）—— Claude 官方，或 DeepSeek 这类 Anthropic 协议兼容 API
  - "openai"          —— Qwen(DashScope)、以及任何 OpenAI Chat Completions 兼容 API
"""
import os
import json
from typing import Any, Dict, List, Optional

try:
    from anthropic import AsyncAnthropic
except ImportError:  # anthropic 是运行服务时依赖；离线规则测试不应因缺失而无法 import
    AsyncAnthropic = None

from core.llm_utils import extract_text_content

try:
    from openai import AsyncOpenAI
except ImportError:  # openai 是可选依赖，只有 provider=openai 时才需要
    AsyncOpenAI = None


class LLMClient:
    """屏蔽 Anthropic / OpenAI 兼容协议差异的统一异步 LLM 客户端。"""

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        model: str = "claude-3-5-sonnet-20241022",
        provider: Optional[str] = None,
        max_retries: int = 0,
    ):
        self.model = model
        self.provider = (provider or os.getenv("LLM_PROVIDER") or "anthropic").strip().lower()

        if self.provider == "openai":
            if AsyncOpenAI is None:
                raise RuntimeError(
                    "LLM_PROVIDER=openai 需要安装 openai 库：pip install openai"
                )
            kwargs: Dict[str, Any] = {"api_key": api_key, "max_retries": max_retries}
            if base_url:
                kwargs["base_url"] = base_url
            self._client: Any = AsyncOpenAI(**kwargs)
        else:
            if AsyncAnthropic is None:
                raise RuntimeError(
                    "LLM_PROVIDER=anthropic 需要安装 anthropic 库：pip install anthropic"
                )
            kwargs = {"api_key": api_key, "max_retries": max_retries}
            if base_url:
                kwargs["base_url"] = base_url
            self._client = AsyncAnthropic(**kwargs)

    async def create_tool_turn(self, *, system, messages, tools, max_tokens=512, required_tool=None):
        """Canonical OpenAI-style transcript in/out; preserve native call IDs."""
        if required_tool and required_tool not in {tool["name"] for tool in tools}:
            raise ValueError("Required tool is not defined")
        if self.provider == "openai":
            response = await self._client.chat.completions.create(
                model=self.model, max_tokens=max_tokens,
                messages=[{"role": "system", "content": system}] + messages,
                tools=[{"type": "function", "function": tool} for tool in tools],
                parallel_tool_calls=False,
                **({"tool_choice": {"type": "function", "function": {"name": required_tool}}} if required_tool else {}),
            )
            msg = response.choices[0].message
            calls = [{"id": c.id, "type": "function", "function": {
                "name": c.function.name, "arguments": c.function.arguments}}
                for c in (msg.tool_calls or [])]
            usage = getattr(response, "usage", None)
            return {"role": "assistant", "content": msg.content or "", **({"tool_calls": calls} if calls else {}),
                    "finish_reason": getattr(response.choices[0], "finish_reason", None),
                    "_usage": {"input_tokens": usage.prompt_tokens, "output_tokens": usage.completion_tokens} if usage else None}
        converted = []
        for msg in messages:
            if msg["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": msg["tool_call_id"], "content": msg["content"]}
                if converted and converted[-1]["role"] == "user" and isinstance(converted[-1]["content"], list):
                    converted[-1]["content"].append(block)
                else:
                    converted.append({"role": "user", "content": [block]})
            elif msg.get("tool_calls"):
                blocks = ([{"type": "text", "text": msg["content"]}] if msg.get("content") else [])
                blocks.extend({"type": "tool_use", "id": c["id"], "name": c["function"]["name"],
                               "input": json.loads(c["function"]["arguments"])} for c in msg["tool_calls"])
                converted.append({"role": "assistant", "content": blocks})
            else:
                converted.append({"role": msg["role"], "content": msg["content"]})
        response = await self._client.messages.create(
            model=self.model, max_tokens=max_tokens, system=system, messages=converted,
            tools=[{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools],
            tool_choice={**({"type": "tool", "name": required_tool} if required_tool else {"type": "auto"}), "disable_parallel_tool_use": True},
        )
        calls = [{"id": b.id, "type": "function", "function": {"name": b.name, "arguments": json.dumps(b.input)}}
                 for b in response.content if b.type == "tool_use"]
        usage = getattr(response, "usage", None)
        return {"role": "assistant", "content": extract_text_content(response.content),
                "finish_reason": getattr(response, "stop_reason", None),
                **({"tool_calls": calls} if calls else {}),
                "_usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens} if usage else None}

    async def create(
        self,
        *,
        max_tokens: int,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        system: Optional[str] = None,
    ) -> str:
        """调用底层 LLM，统一返回纯文本回复（屏蔽两种协议的响应体差异）。"""
        if self.provider == "openai":
            full_messages: List[Dict[str, str]] = []
            if system:
                full_messages.append({"role": "system", "content": system})
            full_messages.extend(messages)

            kwargs: Dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": full_messages,
            }
            if temperature is not None:
                kwargs["temperature"] = temperature

            resp = await self._client.chat.completions.create(**kwargs)
            return resp.choices[0].message.content or ""

        # Anthropic Messages API：system 是独立的顶层参数，messages 只含 user/assistant。
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if system:
            kwargs["system"] = system

        resp = await self._client.messages.create(**kwargs)
        return extract_text_content(resp.content)
