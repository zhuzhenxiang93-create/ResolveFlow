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
    ):
        self.model = model
        self.provider = (provider or os.getenv("LLM_PROVIDER") or "anthropic").strip().lower()

        if self.provider == "openai":
            if AsyncOpenAI is None:
                raise RuntimeError(
                    "LLM_PROVIDER=openai 需要安装 openai 库：pip install openai"
                )
            kwargs: Dict[str, Any] = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            self._client: Any = AsyncOpenAI(**kwargs)
        else:
            if AsyncAnthropic is None:
                raise RuntimeError(
                    "LLM_PROVIDER=anthropic 需要安装 anthropic 库：pip install anthropic"
                )
            kwargs = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            self._client = AsyncAnthropic(**kwargs)

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
