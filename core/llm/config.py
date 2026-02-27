import os
from typing import Any, Optional, Type

import pydantic
from pydantic import BaseModel


class LLMConfig(BaseModel):
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    api_key: Optional[str] = None
    vertex_project: Optional[str] = None
    vertex_location: Optional[str] = "us-central1"
    temperature: float = 0.7
    max_tokens: int = 2048
    use_mock: bool = False

    @pydantic.field_validator("provider")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        allowed = ["openai", "google_vertex", "gemini", "mock"]
        if v not in allowed:
            raise ValueError(f"Provider must be one of {allowed}")
        return v

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            provider=os.getenv("LLM_PROVIDER", "openai"),
            model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
            api_key=os.getenv("OPENAI_API_KEY"),
            vertex_project=os.getenv("GOOGLE_VERTEX_PROJECT"),
            vertex_location=os.getenv("GOOGLE_VERTEX_LOCATION", "us-central1"),
            temperature=float(os.getenv("LLM_TEMPERATURE", "0.7")),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "2048")),
            use_mock=os.getenv("LLM_USE_MOCK", "false").lower() == "true",
        )


__all__ = ["LLMConfig"]
