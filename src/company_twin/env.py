from __future__ import annotations

import os
from pathlib import Path


DEFAULT_MODEL = "openrouter:qwen/qwen3.6-flash"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_local_env(root: Path) -> None:
    load_env_file(root / ".env")
    load_env_file(root / ".env.local")


def normalize_openrouter_model(model: str | None) -> str:
    model = (model or os.getenv("DEEPAGENT_MODEL") or os.getenv("OPENROUTER_MODEL") or DEFAULT_MODEL).strip()
    if not model:
        return DEFAULT_MODEL
    if model.startswith("openrouter:"):
        return model
    if model.startswith("openrouter/"):
        return "openrouter:" + model.split("/", 1)[1]
    if "/" in model:
        return "openrouter:" + model
    return model


def openrouter_slug(model: str | None = None) -> str:
    normalized = normalize_openrouter_model(model)
    if normalized.startswith("openrouter:"):
        return normalized.split(":", 1)[1]
    return normalized
