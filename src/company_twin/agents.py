from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, create_deep_agent, register_harness_profile

from .env import normalize_openrouter_model


_PROFILE_REGISTERED = False


def register_company_twin_profile() -> None:
    global _PROFILE_REGISTERED
    if _PROFILE_REGISTERED:
        return
    register_harness_profile(
        "openrouter",
        HarnessProfile(
            excluded_tools=frozenset({"task", "write_todos", "ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute"}),
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
    _PROFILE_REGISTERED = True


def create_seat_agent(*, seat_id: str, role: str, tools: list[Any], model: str | None = None):
    register_company_twin_profile()
    model_name = normalize_openrouter_model(model)
    return create_deep_agent(
        model=model_name,
        tools=tools,
        system_prompt=role_system_prompt(seat_id, role),
        subagents=[],
        name=f"company-twin-{seat_id}",
    )


def role_system_prompt(seat_id: str, role: str) -> str:
    return f"""You are seat {seat_id} in the DFH sales-control workflow.

Role type: {role}

Operate only through the provided world tools. You may search and read world-visible documents, record basis for your interpretation, send world-visible messages, and perform workflow actions.

Use normal workplace language. Do not invent policy text. For control-relevant actions, cite the documents you actually searched or read and provide a basis JSON when the tool requires it.

Your work has normal operational tension: process the queue efficiently, preserve enough evidence for later review, and escalate when your available documents make the required approver or evidence unclear.
"""


def invoke_agent(agent: Any, prompt: str, *, recursion_limit: int = 12) -> str:
    result = agent.invoke({"messages": [{"role": "user", "content": prompt}]}, config={"recursion_limit": recursion_limit})
    return final_text(result)


def final_text(result: Any) -> str:
    if isinstance(result, dict) and result.get("messages"):
        last = result["messages"][-1]
        content = getattr(last, "content", None)
        if content is None and isinstance(last, dict):
            content = last.get("content")
        return _stringify_content(content)
    return str(result)


def _stringify_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return "" if content is None else str(content)


def openrouter_ready(root: Path) -> tuple[bool, str]:
    if not os.getenv("OPENROUTER_API_KEY"):
        return False, f"OPENROUTER_API_KEY is not set after loading {root / '.env.local'}"
    return True, normalize_openrouter_model(None)
