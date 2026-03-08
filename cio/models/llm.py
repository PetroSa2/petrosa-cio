from datetime import datetime

from pydantic import BaseModel, Field


class RawLLMResponse(BaseModel):
    """
    Raw output from a single LLM completion call.
    No domain parsing — that happens in complete_with_schema.
    """

    prompt_id: str
    content: str = ""  # Raw string from model, defaults to empty on error
    error: str | None = None  # Set on transport failure, None on success
    model: str  # Model version used e.g. claude-haiku-4-5-20251001
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0  # Tokens served from prompt cache
    latency_ms: int  # Wall clock time for the call
    timestamp: datetime = Field(default_factory=datetime.utcnow)
