"""Dynamic reflection helpers for generating MCP tool definitions."""

from __future__ import annotations

import importlib
import inspect
from typing import Any

from pydantic import BaseModel


def _is_pydantic_model(candidate: Any) -> bool:
    return (
        inspect.isclass(candidate)
        and issubclass(candidate, BaseModel)
        and candidate is not BaseModel
    )


def discover_schema_models(module_path: str) -> dict[str, type[BaseModel]]:
    module = importlib.import_module(module_path)
    models: dict[str, type[BaseModel]] = {}

    for name, obj in inspect.getmembers(module, _is_pydantic_model):
        if obj.__module__ == module.__name__:
            models[name] = obj

    return models


def generate_tools(module_path: str) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []

    for name, model in discover_schema_models(module_path).items():
        schema = model.model_json_schema()
        description = inspect.getdoc(model) or f"Schema tool for {name}"

        tools.append(
            {
                "name": f"get_{name}",
                "description": f"Read current {name} config. {description}",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
                "model": name,
                "mode": "read",
            }
        )

        tools.append(
            {
                "name": f"set_{name}",
                "description": f"Update {name} config. {description}",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "payload": schema,
                        "thought_trace": {
                            "type": "string",
                            "minLength": 100,
                            "description": "Required reasoning trace for audit.",
                        },
                    },
                    "required": ["payload", "thought_trace"],
                },
                "model": name,
                "mode": "write",
            }
        )

    return tools
