import json
import os
from pathlib import Path
from typing import Any, Optional, Type

import pydantic
from pydantic import BaseModel

try:
    import litellm
    from litellm import acompletion, completion
    LITELLM_AVAILABLE = True
except ImportError:
    LITELLM_AVAILABLE = False

try:
    import instructor
    from instructor import from_litellm
    INSTRUCTOR_AVAILABLE = True
except ImportError:
    INSTRUCTOR_AVAILABLE = False

from .config import LLMConfig


class LLMResponse(BaseModel):
    content: str
    model: str
    usage: dict = {}


class CIO_LLM_Client:
    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig.from_env()
        self._setup_client()

    def _setup_client(self) -> None:
        if not LITELLM_AVAILABLE:
            raise ImportError("litellm is required. Install with: pip install litellm")

        if self.config.use_mock:
            litellm.set_verbose = False

        if self.config.provider == "openai" and self.config.api_key:
            os.environ["OPENAI_API_KEY"] = self.config.api_key

        if self.config.provider == "google_vertex":
            if not self.config.vertex_project:
                raise ValueError("GOOGLE_VERTEX_PROJECT is required for google_vertex provider")
            os.environ["VERTEX_PROJECT"] = self.config.vertex_project
            os.environ["VERTEX_LOCATION"] = self.config.vertex_location

    def _get_provider_model(self) -> str:
        if self.config.provider == "google_vertex":
            return f"vertex_ai/{self.config.model}"
        return self.config.model

    def complete(
        self,
        messages: list[dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        if self.config.use_mock:
            return self._mock_complete(messages)

        model = self._get_provider_model()
        temp = temperature or self.config.temperature
        tokens = max_tokens or self.config.max_tokens

        response = completion(
            model=model,
            messages=messages,
            temperature=temp,
            max_tokens=tokens,
        )

        return LLMResponse(
            content=response.choices[0].message.content,
            model=response.model,
            usage=response.usage.model_dump() if hasattr(response.usage, 'model_dump') else {},
        )

    async def acomplete(
        self,
        messages: list[dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        if self.config.use_mock:
            return self._mock_complete(messages)

        model = self._get_provider_model()
        temp = temperature or self.config.temperature
        tokens = max_tokens or self.config.max_tokens

        response = await acompletion(
            model=model,
            messages=messages,
            temperature=temp,
            max_tokens=tokens,
        )

        return LLMResponse(
            content=response.choices[0].message.content,
            model=response.model,
            usage=response.usage.model_dump() if hasattr(response.usage, 'model_dump') else {},
        )

    def complete_with_structure(
        self,
        messages: list[dict[str, str]],
        response_model: Type[BaseModel],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> BaseModel:
        if not INSTRUCTOR_AVAILABLE:
            raise ImportError("instructor is required for structured output. Install with: pip install instructor")

        if self.config.use_mock:
            raise ValueError("Structured output is not supported in mock mode")

        model = self._get_provider_model()
        temp = temperature or self.config.temperature
        tokens = max_tokens or self.config.max_tokens

        client = from_litellm(litellm.completion)
        return client.messages(
            model=model,
            messages=messages,
            response_model=response_model,
            temperature=temp,
            max_tokens=tokens,
        )

    def _mock_complete(self, messages: list[dict[str, str]]) -> LLMResponse:
        fixture_path = Path("tests/fixtures/llm")
        if not fixture_path.exists():
            return LLMResponse(
                content='{"error": "No mock fixtures found"}',
                model="mock",
                usage={},
            )

        prompt_hash = str(hash(str(messages)))
        fixture_file = fixture_path / f"{prompt_hash}.json"

        if fixture_file.exists():
            with open(fixture_file) as f:
                content = json.dumps(json.load(f))
        else:
            default_fixture = fixture_path / "default.json"
            if default_fixture.exists():
                with open(default_fixture) as f:
                    content = json.dumps(json.load(f))
            else:
                content = '{"response": "Mock response - no fixture found"}'

        return LLMResponse(content=content, model="mock", usage={})


__all__ = ["CIO_LLM_Client", "LLMResponse", "LLMConfig"]
