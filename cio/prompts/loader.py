"""Resolve system prompt text from YAML registry for the active capability profile."""

from typing import Any


def select_system_prompt(data: dict[str, Any], capability_profile: str) -> str:
    if capability_profile == "minimal":
        return (
            data.get("system_prompt_minimal") or data.get("system_prompt") or ""
        ).strip()
    return (data.get("system_prompt") or "").strip()
