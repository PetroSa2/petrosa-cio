import os

VALID_LLM_FALLBACK_MODES = frozenset({"pause", "deterministic"})


def llm_fallback_mode() -> str:
    value = os.getenv("CIO_LLM_FALLBACK_MODE", "deterministic").strip().lower()
    return value if value in VALID_LLM_FALLBACK_MODES else "deterministic"
