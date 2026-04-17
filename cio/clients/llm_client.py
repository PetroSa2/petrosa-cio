import json
import logging
import os
from abc import ABC, abstractmethod

try:
    from datetime import UTC
except ImportError:
    UTC = UTC
from typing import Any

from pydantic import BaseModel, ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from cio.models import SAFE_DEFAULTS

logger = logging.getLogger(__name__)


# Persona prompts whose JSON may omit thought_trace when LLM_CAPABILITY_PROFILE=minimal.
_PROMPTS_OPTIONAL_THOUGHT_TRACE = frozenset(
    {
        "PETROSA_PROMPT_ACTION_CLASSIFIER",
        "PETROSA_PROMPT_REGIME_CLASSIFIER",
        "PETROSA_PROMPT_STRATEGY_ASSESSOR",
    }
)


def _env_bool(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_model_prefix() -> str:
    return os.getenv("LLM_MODEL_PREFIX", "openai/")


def resolve_llm_capability_profile() -> str:
    profile = os.getenv("LLM_CAPABILITY_PROFILE", "standard").lower()
    if profile not in {"minimal", "standard"}:
        raise ValueError(
            f"Invalid LLM_CAPABILITY_PROFILE '{profile}'. Must be 'minimal' or 'standard'."
        )
    return profile


def _build_routing_model(model: str, api_base: str | None) -> str:
    if not api_base:
        return model
    prefix = _get_model_prefix()
    if not prefix:
        return model
    # Avoid openai/openai/... when env already uses provider-prefixed model ids.
    if model.startswith(prefix):
        return model
    return f"{prefix}{model}"


def _supports_json_mode(
    litellm_module: Any, routing_model: str, profile: str = "standard"
) -> bool:
    # minimal profile ALWAYS disables json_mode to avoid Llama 1B/3B issues.
    if profile == "minimal":
        return False

    if not _env_bool("LLM_SUPPORTS_JSON_MODE", default=True):
        return False
    try:
        supported_params = litellm_module.get_supported_openai_params(routing_model)
    except Exception:
        return False
    return "json_object" in (supported_params or [])


class CIO_LLM_Client(ABC):
    """Abstract base class for LLM clients."""

    def __init__(self):
        self.capability_profile = resolve_llm_capability_profile()

    @abstractmethod
    async def complete_with_schema(
        self,
        prompt_id: str,
        system_prompt: str,
        user_context: dict[str, Any],
        response_model: type[BaseModel],
    ) -> Any:
        pass


class LiteLLMClient(CIO_LLM_Client):
    """Production client using LiteLLM for routing to any model provider."""

    def __init__(self):
        super().__init__()
        try:
            import litellm

            self.litellm = litellm
            # Disable LiteLLM logging unless explicitly enabled
            self.litellm.set_verbose = _env_bool("LITELLM_VERBOSE", default=False)
        except ImportError:
            logger.error("litellm package not found. Client will fail.")
            self.litellm = None

    async def complete_with_schema(
        self,
        prompt_id: str,
        system_prompt: str,
        user_context: dict[str, Any],
        response_model: type[BaseModel],
    ) -> Any:
        """Calls LiteLLM and parses response into a Pydantic model."""
        if not self.litellm:
            return SAFE_DEFAULTS.get(prompt_id)

        routing_primary = _build_routing_model(
            os.getenv("LLM_PRIMARY_MODEL", "gpt-4o-mini"),
            os.getenv("LLM_API_BASE"),
        )

        use_json_mode = _supports_json_mode(
            self.litellm, routing_primary, self.capability_profile
        )

        try:
            raw_content = await self._call_litellm(
                routing_primary, system_prompt, user_context, use_json_mode
            )

            # S5: Post-processing for minimal profile (inject empty thought_trace if missing)
            if (
                self.capability_profile == "minimal"
                and prompt_id in _PROMPTS_OPTIONAL_THOUGHT_TRACE
            ):
                try:
                    data = json.loads(raw_content)
                    if "thought_trace" not in data:
                        data["thought_trace"] = ""
                        raw_content = json.dumps(data)
                except Exception:
                    pass

            return response_model.model_validate_json(raw_content)

        except (ValidationError, json.JSONDecodeError) as e:
            logger.error(
                f"LLM_PARSE_FAILURE_SKIP | Prompt: {prompt_id} | Model: {routing_primary} | Error: {e}"
            )
            # Emit metric here in real implementation
            return SAFE_DEFAULTS.get(prompt_id)
        except Exception as e:
            logger.error(f"LiteLLM call critical failure: {e}")
            return SAFE_DEFAULTS.get(prompt_id)

    async def _call_litellm(
        self,
        model: str,
        system_prompt: str,
        user_context: dict[str, Any],
        json_mode: bool,
    ) -> str:
        """Internal retrying caller for litellm.completion."""
        api_base = os.getenv("LLM_API_BASE")
        api_key = os.getenv("REQUESTY_API_KEY") or os.getenv("OPENAI_API_KEY")

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_context)},
        ]

        # Tenacity retry loop for transient API errors
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_random_exponential(multiplier=1, max=10),
            retry=retry_if_exception_type(Exception),
        ):
            with attempt:
                response = await self.litellm.acompletion(
                    model=model,
                    messages=messages,
                    api_base=api_base,
                    api_key=api_key,
                    response_format={"type": "json_object"} if json_mode else None,
                    temperature=0.1,
                )
                return response.choices[0].message.content


class MockLLMClient(CIO_LLM_Client):
    """Test mock that bypasses LLM and returns canned responses."""

    def __init__(self):
        super().__init__()
        self._cache = {}

    async def complete_with_schema(
        self,
        prompt_id: str,
        system_prompt: str,
        user_context: dict[str, Any],
        response_model: type[BaseModel],
    ) -> Any:
        """Returns canned response if present, otherwise safe default."""
        cache_key = f"{prompt_id}:{user_context.get('strategy_id', 'global')}"
        if cache_key in self._cache:
            raw = self._cache[cache_key]
            # S5: Post-processing for minimal profile (inject empty thought_trace if missing)
            if (
                self.capability_profile == "minimal"
                and prompt_id in _PROMPTS_OPTIONAL_THOUGHT_TRACE
            ):
                try:
                    data = json.loads(raw)
                    if "thought_trace" not in data:
                        data["thought_trace"] = ""
                        raw = json.dumps(data)
                except Exception:
                    pass
            return response_model.model_validate_json(raw)
        return SAFE_DEFAULTS.get(prompt_id)

    def set_response(self, cache_key: str, data: str):
        """S5: Explicit API for seeding the mock's in-memory cache."""
        self._cache[cache_key] = data

    def seed_cache(self, cache_key: str, data: str):
        """S5: Explicit API for seeding the mock's cache for HOT path tests."""
        self._cache[cache_key] = data
