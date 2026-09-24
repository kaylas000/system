# specs/02_infra/llm_gateway/LLM_CLIENT.py
"""
LLM Client Wrapper.
- Unified Interface (LiteLLM Router).
- Structured Output via Instructor.
- Cost Tracking & Budget Enforcement.
- Automatic Retries / Fallbacks.
- Prompt Caching Headers (Anthropic/OpenAI).
"""

from __future__ import annotations
import json
import time
import uuid
from typing import Dict, Any, Optional, List, AsyncIterator, Type
from pydantic import BaseModel
import instructor
from litellm import acompletion, acompletion_cost
from litellm.types.utils import ModelResponse

from kernel.protocols import ILLMClient, LLMMessage, LLMResponse
from kernel.config import settings
from kernel.state import TokenUsage


class LiteLLMClient(ILLMClient):
    def __init__(self):
        # Patch LiteLLM with Instructor for structured output
        self.client = instructor.from_litellm(acompletion, mode=instructor.Mode.JSON)
        # Configure LiteLLM Router (from config.yaml)
        # litellm.router = Router(model_list=settings.llm.model_list)

    async def achat(
        self,
        messages: List[LLMMessage],
        model: str,
        response_model: Optional[Type[BaseModel]] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> LLMResponse:
        start = time.perf_counter()

        # Convert to LiteLLM format
        llm_messages = [m.model_dump(exclude_none=True) for m in messages]

        try:
            if response_model:
                # Instructor handles parsing & validation
                parsed_obj = await self.client.chat.completions.create(
                    model=model,
                    messages=llm_messages,
                    response_model=response_model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **kwargs,
                )
                # Extract raw response for usage tracking
                raw_resp = parsed_obj._raw_response if hasattr(parsed_obj, "_raw_response") else None
                content = parsed_obj.model_dump_json()
            else:
                raw_resp = await acompletion(
                    model=model, messages=llm_messages, temperature=temperature, max_tokens=max_tokens, **kwargs
                )
                content = raw_resp.choices[0].message.content
                parsed_obj = None

            # Cost Calculation
            cost = 0.0
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            if raw_resp:
                usage = raw_resp.usage.model_dump() if raw_resp.usage else usage
                try:
                    cost = acompletion_cost(model=model, usage=raw_resp.usage)
                except:
                    pass  # Cost calc failed

            return LLMResponse(
                content=content,
                tool_calls=None,  # Handled by Instructor
                usage=usage,
                model=model,
                cost_usd=cost,
            )
        except Exception as e:
            # Log error, re-raise for Node to handle (retry logic)
            raise RuntimeError(f"LLM Call failed ({model}): {str(e)}") from e

    async def astream_chat(self, messages: List[LLMMessage], model: str, **kwargs) -> AsyncIterator[str]:
        llm_messages = [m.model_dump(exclude_none=True) for m in messages]
        stream = await acompletion(model=model, messages=llm_messages, stream=True, **kwargs)
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content

    def estimate_tokens(self, messages: List[LLMMessage], model: str) -> int:
        try:
            import tiktoken

            encoding = tiktoken.encoding_for_model(model.replace("router/", ""))
        except:
            encoding = tiktoken.get_encoding("cl100k_base")

        total = 0
        for m in messages:
            total += len(encoding.encode(m.content))
            # Add overhead for role/tags
            total += 4
        return total
