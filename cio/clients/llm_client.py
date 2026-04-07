import json
import logging
import os
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from cio.models import SAFE_DEFAULTS, RawLLMResponse

logger = logging.getLogger(__name__)


class CIO_LLM_Client(ABC):
    """
    Abstract base class for LLM client implementations.
    Provides the core interface for both real and mock clients.
    """

    @abstractmethod
    async def complete(
        self, prompt_id: str, system_prompt: str, user_context: dict[str, Any]
    ) -> RawLLMResponse:
        """
        Base completion call. Must handle transport errors by returning
        a RawLLMResponse with the 'error' field populated instead of raising.
        """
        pass

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """
        Generates a vector embedding for the given text.
        """
        pass

    async def complete_with_schema(
        self,
        prompt_id: str,
        system_prompt: str,
        user_context: dict[str, Any],
        response_model: type[BaseModel],
    ) -> BaseModel:
        """
        Wraps complete() with automated Pydantic validation and safe defaults.
        This is the primary method used by all persona calls.
        """
        # 1. Base call
        raw = await self.complete(prompt_id, system_prompt, user_context)

        # 2. Check for transport errors
        if raw.error:
            logger.error(
                "LLM transport error",
                extra={"prompt_id": prompt_id, "error": raw.error},
            )
            return SAFE_DEFAULTS[prompt_id]

        # 3. Pydantic validation + JSON parsing
        try:
            content = raw.content.strip()
            # Handle potential markdown wrapping
            if content.startswith("```"):
                # Find first and last newline to extract content between markers
                lines = content.splitlines()
                if len(lines) >= 2:
                    # Remove first line (```json or ```) and last line (```)
                    # Join middle lines
                    content = "\n".join(lines[1:-1])

            return response_model.model_validate_json(content)
        except (ValidationError, json.JSONDecodeError) as e:
            logger.warning(
                "LLM response validation failed — returning safe default",
                extra={
                    "prompt_id": prompt_id,
                    "error": str(e),
                    "content_preview": raw.content[:200],
                },
            )
            return SAFE_DEFAULTS[prompt_id]

    @abstractmethod
    async def get_cached(self, cache_key: str) -> str | None:
        """Retrieve a raw result from the cache if available."""
        pass

    @abstractmethod
    async def put_cached(
        self, cache_key: str, data: str, ttl_seconds: int | None = None
    ):
        """Store a raw result in the cache."""
        pass


# Default Model Pins (Fix 5)
DEFAULT_PRIMARY_MODEL = "anthropic/claude-3-haiku-20240307"
DEFAULT_FALLBACK_MODEL = "openai/gpt-4o-mini"


class LiteLLMClient(CIO_LLM_Client):
    """Concrete implementation using litellm for multi-provider access."""

    def __init__(self):
        # Circuit Breaker state
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._breaker_open_until = 0.0

    def _check_circuit_breaker(self) -> str | None:
        """Check if the circuit breaker is open."""
        now = time.time()
        if self._breaker_open_until > now:
            return "CIRCUIT_BREAKER_OPEN"

        # Reset failure count if last failure was more than 5 minutes ago
        if now - self._last_failure_time > 300:
            self._failure_count = 0

        return None

    def _record_failure(self):
        """Record a failure and potentially open the circuit breaker."""
        now = time.time()
        self._last_failure_time = now
        self._failure_count += 1

        if self._failure_count >= 5:
            self._breaker_open_until = now + 60
            logger.error(
                "LLM Circuit Breaker tripped! Opening for 60 seconds.",
                extra={"failure_count": self._failure_count},
            )

    def _record_success(self):
        """Record a success and reset the failure count."""
        self._failure_count = 0

    async def complete(
        self, prompt_id: str, system_prompt: str, user_context: dict[str, Any]
    ) -> RawLLMResponse:
        """
        Executes an LLM completion using litellm.acompletion with retries,
        circuit breaker, and fallback model. Supports Requesty proxy via api_base.
        """
        import litellm
        from litellm.exceptions import RateLimitError, ServiceUnavailableError

        # 1. Check Circuit Breaker
        breaker_error = self._check_circuit_breaker()
        if breaker_error:
            return RawLLMResponse(
                prompt_id=prompt_id,
                content="",
                error=breaker_error,
                model="circuit-breaker",
                input_tokens=0,
                output_tokens=0,
                latency_ms=0,
                timestamp=datetime.now(UTC),
            )

        primary_model = os.getenv("LLM_MODEL", DEFAULT_PRIMARY_MODEL)
        fallback_model = os.getenv("LLM_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL)
        api_base = os.getenv("LLM_API_BASE")

        # When using a proxy like Requesty, we prefix with 'openai/' to ensure
        # litellm uses the OpenAI-compatible route for all models.
        routing_primary = f"openai/{primary_model}" if api_base else primary_model
        routing_fallback = f"openai/{fallback_model}" if api_base else fallback_model

        start_time = time.perf_counter()

        # 2. Retry Loop for Primary Model
        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(
                    (RateLimitError, ServiceUnavailableError)
                ),
                wait=wait_random_exponential(multiplier=1, max=10),
                stop=stop_after_attempt(3),
                before_sleep=lambda retry_state: logger.warning(
                    f"Retrying LLM call (attempt {retry_state.attempt_number})",
                    extra={
                        "prompt_id": prompt_id,
                        "model": primary_model,
                        "exception": str(retry_state.outcome.exception()),
                    },
                ),
            ):
                with attempt:
                    response = await litellm.acompletion(
                        model=routing_primary,
                        api_base=api_base,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": json.dumps(user_context)},
                        ],
                        response_format=(
                            {"type": "json_object"}
                            if "json_object"
                            in litellm.get_supported_openai_params(primary_model)
                            else None
                        ),
                    )

            # Success on primary
            self._record_success()
            return self._process_response(
                prompt_id, response, int((time.perf_counter() - start_time) * 1000)
            )

        except Exception as primary_error:
            # 3. Fallback Attempt
            logger.error(
                f"Primary LLM failed ({primary_model}), attempting fallback ({fallback_model})",
                extra={"prompt_id": prompt_id, "error": str(primary_error)},
            )

            try:
                fallback_response = await litellm.acompletion(
                    model=routing_fallback,
                    api_base=api_base,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": json.dumps(user_context)},
                    ],
                    response_format=(
                        {"type": "json_object"}
                        if "json_object"
                        in litellm.get_supported_openai_params(fallback_model)
                        else None
                    ),
                )

                # Success on fallback
                self._record_success()
                return self._process_response(
                    prompt_id,
                    fallback_response,
                    int((time.perf_counter() - start_time) * 1000),
                )

            except Exception as fallback_error:
                # Total failure
                self._record_failure()
                latency_ms = int((time.perf_counter() - start_time) * 1000)
                logger.error(
                    f"LLM Fallback failed for {prompt_id}: {str(fallback_error)}"
                )

                return RawLLMResponse(
                    prompt_id=prompt_id,
                    content="",
                    error=f"Primary: {str(primary_error)} | Fallback: {str(fallback_error)}",
                    model=fallback_model,
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=latency_ms,
                    timestamp=datetime.now(UTC),
                )

    async def embed(self, text: str) -> list[float]:
        """Generates real embeddings via litellm.embedding."""
        import litellm

        primary_model = os.getenv("LLM_MODEL", DEFAULT_PRIMARY_MODEL)
        embedding_model = os.getenv("EMBEDDING_MODEL", "openai/text-embedding-3-small")
        api_base = os.getenv("LLM_API_BASE")

        # When using a proxy like Requesty, we prefix with 'openai/' to ensure
        # litellm uses the OpenAI-compatible route for all models.
        routing_model = f"openai/{primary_model}" if api_base else embedding_model

        try:
            response = await litellm.aembedding(
                model=routing_model, input=[text], api_base=api_base
            )
            return response.data[0]["embedding"]
        except Exception as e:
            logger.error(f"Embedding generation failed: {e}")
            # Fallback to zero vector to prevent hard crashes in async audit loops
            return [0.0] * 1536

    def _process_response(
        self, prompt_id: str, response: Any, latency_ms: int
    ) -> RawLLMResponse:
        """Helper to process a successful litellm response."""
        content = response.choices[0].message.content or ""
        usage = response.usage

        cached_tokens = 0
        if hasattr(usage, "prompt_tokens_details") and usage.prompt_tokens_details:
            cached_tokens = getattr(usage.prompt_tokens_details, "cached_tokens", 0)

        # Record Metrics
        try:
            from cio.core.metrics import LLM_LATENCY, LLM_TOKENS

            LLM_LATENCY.labels(prompt_id=prompt_id, model=response.model).observe(
                latency_ms / 1000.0
            )
            LLM_TOKENS.labels(
                prompt_id=prompt_id, model=response.model, token_type="input"
            ).inc(usage.prompt_tokens)
            LLM_TOKENS.labels(
                prompt_id=prompt_id, model=response.model, token_type="output"
            ).inc(usage.completion_tokens)
            if cached_tokens > 0:
                LLM_TOKENS.labels(
                    prompt_id=prompt_id, model=response.model, token_type="cached"
                ).inc(cached_tokens)
        except ImportError:
            pass

        return RawLLMResponse(
            prompt_id=prompt_id,
            content=content,
            model=response.model,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            cached_tokens=cached_tokens,
            latency_ms=latency_ms,
            timestamp=datetime.now(UTC),
        )

    async def get_cached(self, cache_key: str) -> str | None:
        """S5: Placeholder for Redis/Distributed cache."""
        return None

    async def put_cached(
        self, cache_key: str, data: str, ttl_seconds: int | None = None
    ):
        """S5: Placeholder for Redis/Distributed cache."""
        pass


class MockLLMClient(CIO_LLM_Client):
    """Behaviorally honest mock client for testing and local development."""

    def __init__(self, prompts_dir: str | None = None):
        import glob

        import yaml

        self._cache: dict[str, str] = {}
        self._prompts: dict[str, dict[str, Any]] = {}

        # 1. Load prompts from YAML files
        if prompts_dir is None:
            # Default to relative path from this file
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            prompts_dir = os.path.join(base_dir, "prompts")

        if os.path.exists(prompts_dir):
            for yaml_file in glob.glob(os.path.join(prompts_dir, "*.yaml")):
                try:
                    with open(yaml_file) as f:
                        data = yaml.safe_load(f)
                        if "prompt_id" in data:
                            self._prompts[data["prompt_id"]] = data
                except Exception as e:
                    logger.error(f"Failed to load mock prompt {yaml_file}: {e}")
        else:
            logger.warning(f"Mock prompts directory not found: {prompts_dir}")

    async def complete(
        self, prompt_id: str, system_prompt: str, user_context: dict[str, Any]
    ) -> RawLLMResponse:
        """
        Mock completion that validates context and simulates model behavior.
        Includes failure injection for testing safety nets.
        """
        start_time = time.perf_counter()

        # 0. Failure Injection (S4)
        mock_fail = user_context.get("mock_fail")
        if mock_fail:
            latency_ms = int((time.perf_counter() - start_time) * 1000)
            if mock_fail == "transport":
                return RawLLMResponse(
                    prompt_id=prompt_id,
                    content="",
                    error="SIMULATED_TRANSPORT_FAILURE",
                    model="mock-failure-injector",
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=latency_ms,
                    timestamp=datetime.now(UTC),
                )
            elif mock_fail == "malformed":
                return RawLLMResponse(
                    prompt_id=prompt_id,
                    content="INTERNAL_SERVER_ERROR: JSON_PARSE_FAILED (Not actually JSON)",
                    model="mock-failure-injector",
                    input_tokens=150,
                    output_tokens=20,
                    latency_ms=latency_ms,
                    timestamp=datetime.now(UTC),
                )
            elif mock_fail == "invalid_schema":
                return RawLLMResponse(
                    prompt_id=prompt_id,
                    content='{"unexpected_field": "hallucination", "thought_trace": "oops"}',
                    model="mock-failure-injector",
                    input_tokens=150,
                    output_tokens=30,
                    latency_ms=latency_ms,
                    timestamp=datetime.now(UTC),
                )

        # 1. Look up prompt metadata for validation
        prompt_meta = self._prompts.get(prompt_id)
        if not prompt_meta:
            logger.warning(f"No mock metadata found for prompt_id: {prompt_id}")
        else:
            # 2. Validate required context fields
            required_fields = prompt_meta.get("required_context_fields", [])
            for field in required_fields:
                if field not in user_context:
                    latency_ms = int((time.perf_counter() - start_time) * 1000)
                    error_msg = f"MISSING_CONTEXT_FIELD: {field}"
                    logger.error(f"Mock validation failed for {prompt_id}: {error_msg}")
                    return RawLLMResponse(
                        prompt_id=prompt_id,
                        content="",
                        error=error_msg,
                        model="mock-validator",
                        input_tokens=0,
                        output_tokens=0,
                        latency_ms=latency_ms,
                        timestamp=datetime.now(UTC),
                    )

        # 3. Simulate success with scenario-based logic
        content = self._simulate_classification(prompt_id, user_context)

        latency_ms = int((time.perf_counter() - start_time) * 1000)

        # 4. Record Metrics (Epic 5)
        try:
            from cio.core.metrics import LLM_LATENCY, LLM_TOKENS

            model = "mock-claude-3-haiku"
            LLM_LATENCY.labels(prompt_id=prompt_id, model=model).observe(
                latency_ms / 1000.0
            )
            LLM_TOKENS.labels(prompt_id=prompt_id, model=model, token_type="input").inc(
                150
            )
            LLM_TOKENS.labels(
                prompt_id=prompt_id, model=model, token_type="output"
            ).inc(50)
            LLM_TOKENS.labels(
                prompt_id=prompt_id, model=model, token_type="cached"
            ).inc(240)
        except ImportError:
            pass

        return RawLLMResponse(
            prompt_id=prompt_id,
            content=content,
            model="mock-claude-3-haiku",
            input_tokens=150,
            output_tokens=50,
            cached_tokens=240,
            latency_ms=max(latency_ms, 50),  # At least 50ms for realism
            timestamp=datetime.now(UTC),
        )

    async def embed(self, text: str) -> list[float]:
        """Behaviorally honest mock embedding (deterministic for same text)."""
        import hashlib

        # Create a semi-random but deterministic vector based on input text
        hash_val = int(hashlib.md5(text.encode()).hexdigest(), 16)
        return [(hash_val % (i + 1)) / float(hash_val % 100 + 1) for i in range(1536)]

    def _simulate_classification(self, prompt_id: str, context: dict[str, Any]) -> str:
        """
        Simulates LLM classification based on input context values.
        Returns a valid JSON string matching the expected domain model.
        """
        if prompt_id == "PETROSA_PROMPT_REGIME_CLASSIFIER":
            vol = context.get("volatility_percentile", 0.5)
            trend = context.get("trend_strength", 0.0)

            if vol > 0.9:
                regime, conf = "high_volatility", "high"
                trace = f"Volatility percentile {vol} is extreme; classifying as high_volatility."
            elif trend > 0.8:
                regime, conf = "trending_bull", "high"
                trace = f"Trend strength {trend} is strongly positive."
            elif trend < -0.8:
                regime, conf = "trending_bear", "high"
                trace = f"Trend strength {trend} is strongly negative."
            elif -0.2 <= trend <= 0.2:
                regime, conf = "ranging", "medium"
                trace = f"Trend strength {trend} is near zero; classifying as ranging."
            else:
                regime, conf = "choppy", "low"
                trace = "Signals are mixed or weak; defaulting to choppy/low."

            return json.dumps(
                {
                    "regime": regime,
                    "regime_confidence": conf,
                    "volatility_level": "medium",  # Mock default
                    "primary_signal": f"mock_vol_{vol}_trend_{trend}",
                    "thought_trace": trace,
                }
            )

        if prompt_id == "PETROSA_PROMPT_STRATEGY_ASSESSOR":
            losses = context.get("consecutive_losses", 0) or 0
            delta = context.get("win_rate_delta", 0.0) or 0.0

            if losses >= 3:
                health, fit, rec = "failing", "poor", "pause"
                trace = f"Strategy has {losses} consecutive losses; emergency pause recommended."
            elif delta < -0.1:
                health, fit, rec = "degraded", "neutral", "reduce"
                trace = f"Win rate delta {delta} is significantly negative; reducing exposure."
            else:
                health, fit, rec = "healthy", "good", "run"
                trace = "Strategy health signals are within normal parameters."

            return json.dumps(
                {
                    "health": health,
                    "regime_fit": fit,
                    "activation_recommendation": rec,
                    "param_change": None,
                    "thought_trace": trace,
                }
            )

        if prompt_id == "PETROSA_PROMPT_ACTION_CLASSIFIER":
            hard_blocked = context.get("hard_blocked", False)
            health = context.get("health")
            rec = context.get("activation_recommendation")
            gross_ev = context.get("gross_ev", 0.0) or 0.0

            if hard_blocked:
                action, just = "block", "Hard blocked by engine limits."
                trace = "Code Engine safety gate triggered. Bypassing all logic."
            elif health == "failing" or rec == "pause":
                action, just = (
                    "pause_strategy",
                    "Strategy is failing or pause recommended.",
                )
                trace = f"Strategy health {health} / activation {rec} requires pause."
            elif health == "healthy" and gross_ev > 0:
                action, just = "execute", "Healthy strategy with positive EV."
                trace = f"Positive EV {gross_ev} on healthy strategy. Execution recommended."
            else:
                action, just = "skip", "Mixed signals or low conviction."
                trace = "Defaulting to skip as no strong execute/pause signals met."

            return json.dumps(
                {"action": action, "justification": just, "thought_trace": trace}
            )

        return "{}"

    async def get_cached(self, cache_key: str) -> str | None:
        """Retrieves result from in-memory cache."""
        return self._cache.get(cache_key)

    async def put_cached(
        self, cache_key: str, data: str, ttl_seconds: int | None = None
    ):
        """Stores result in in-memory cache."""
        self._cache[cache_key] = data

    def seed_cache(self, cache_key: str, data: str):
        """S5: Explicit API for seeding the mock's cache for HOT path tests."""
        self._cache[cache_key] = data
