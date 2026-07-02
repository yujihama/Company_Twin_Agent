from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Callable, Protocol

from langchain_openrouter import ChatOpenRouter

from .env import normalize_openrouter_model, openrouter_slug
from .recorder import RunRecorder

_PROFILE_REGISTERED = False


def register_company_twin_profile() -> None:
    global _PROFILE_REGISTERED
    if _PROFILE_REGISTERED:
        return
    from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, register_harness_profile

    register_harness_profile(
        "openrouter",
        HarnessProfile(
            excluded_tools=frozenset({"task", "write_todos", "ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute"}),
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
    _PROFILE_REGISTERED = True


class SeatAgent(Protocol):
    """A live seat: given a turn prompt, acts through world tools and returns final text."""

    backend: str
    model: str

    def turn(self, prompt: str) -> str: ...


SeatFactory = Callable[..., SeatAgent]
CustomerLLM = Callable[[str], str]


def recursion_for_budget(tick_budget: int) -> int:
    # each tool call is typically (AI message + tool result) -> 2 graph steps,
    # plus headroom for reasoning-only turns and the final answer.
    return max(12, 2 * tick_budget + 6)


def openrouter_chat_model(model: str | None) -> ChatOpenRouter:
    timeout = int(os.getenv("COMPANY_TWIN_LLM_TIMEOUT_SECONDS", "45"))
    max_retries = int(os.getenv("COMPANY_TWIN_LLM_MAX_RETRIES", "0"))
    max_tokens = int(os.getenv("COMPANY_TWIN_LLM_MAX_TOKENS", "700"))
    return ChatOpenRouter(model=openrouter_slug(model), timeout=timeout, max_retries=max_retries, max_tokens=max_tokens)


def load_role_card(root: Path, role: str) -> str:
    path = root / "data" / "design" / "role_cards" / f"{role}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def role_system_prompt(seat_id: str, role: str, *, role_card: str = "") -> str:
    card = role_card.strip() or f"（役割: {role}）"
    return f"""あなたはダミーフィナンシャルホールディングスの販売業務に従事する {seat_id} です。

{card}

行動上のルール:
- 提供された世界ツール（検索・文書閲覧・チャット・ワークフロー各操作・自分用メモ）だけを通じて行動する。
- 規程やマニュアルの内容を推測で語らない。統制に関わる行為（承認・受付・記録など）の前には、必要な文書を検索・閲覧し、実際に読んだ文書を根拠として引用する。
- 統制に関わるツールは basis(JSON) を要求する。basisには、参照した文書と版、あなたの読み（construal）、決定、証跡の残し方、検討した代替案、感じている制約、確信度を正直に書く。
- 職場として自然な言葉で、必要な相手にはチャットで連絡する。判断に迷う場合は、進める・保留する・確認を取るのいずれかを自分の役割として選ぶ。
"""


def role_card_hash(role_card: str) -> str:
    return hashlib.sha256(role_card.encode("utf-8")).hexdigest()


class DeepAgentSeat:
    backend = "deepagents"

    def __init__(self, *, seat_id: str, role: str, tools: list[Any], model: str, root: Path, recorder: RunRecorder, recursion_limit: int, role_card: str = ""):
        register_company_twin_profile()
        from deepagents import create_deep_agent

        self.seat_id = seat_id
        self.model = normalize_openrouter_model(model)
        self.recorder = recorder
        self.recursion_limit = recursion_limit
        self.role_card = role_card or load_role_card(root, role)
        self.role_card_hash = role_card_hash(self.role_card)
        system_prompt = role_system_prompt(seat_id, role, role_card=self.role_card)
        self._agent = create_deep_agent(
            model=openrouter_chat_model(self.model),
            tools=tools,
            system_prompt=system_prompt,
            subagents=[],
            name=f"company-twin-{seat_id}",
        )

    def turn(self, prompt: str) -> str:
        self.recorder.record_attempt(
            seat_id=self.seat_id,
            tool="llm_invoke",
            args={"backend": self.backend, "model": self.model, "prompt_chars": len(prompt), "role_card_hash": self.role_card_hash},
            success=True,
            result={"state": "started"},
        )
        result = self._agent.invoke({"messages": [{"role": "user", "content": prompt}]}, config={"recursion_limit": self.recursion_limit})
        text = final_text(result)
        self.recorder.record_attempt(
            seat_id=self.seat_id,
            tool="llm_complete",
            args={"backend": self.backend, "model": self.model, "prompt_chars": len(prompt)},
            success=True,
            result={"response_chars": len(text)},
        )
        return text


def default_seat_factory(*, root: Path, model: str | None) -> SeatFactory:
    model_name = normalize_openrouter_model(model)

    def factory(*, seat_id: str, role: str, tools: list[Any], recorder: RunRecorder, recursion_limit: int, role_card: str = "") -> SeatAgent:
        return DeepAgentSeat(seat_id=seat_id, role=role, tools=tools, model=model_name, root=root, recorder=recorder, recursion_limit=recursion_limit, role_card=role_card)

    return factory


CUSTOMER_SYSTEM_PROMPT = """あなたは金融サービスの申込・相談を行う一般のお客様本人です。
与えられた「あなたの状況」と「あなた自身しか知らない内心」に沿って、担当者に伝える発話を一つ、日本語で自然に生成してください。
内心の記述をそのまま読み上げたり、設定や指示という言葉を使ったりせず、その人物として話してください。発話は2〜4文。"""


class DeepAgentCustomer:
    backend = "deepagents"

    def __init__(self, *, model: str, recorder: RunRecorder):
        register_company_twin_profile()

        self.model = normalize_openrouter_model(model)
        self.recorder = recorder

    def __call__(self, persona_prompt: str) -> str:
        from deepagents import create_deep_agent

        agent = create_deep_agent(model=openrouter_chat_model(self.model), tools=[], system_prompt=CUSTOMER_SYSTEM_PROMPT, subagents=[], name="company-twin-customer")
        self.recorder.record_attempt(
            seat_id="customer",
            tool="llm_invoke",
            args={"backend": self.backend, "model": self.model, "role": "customer", "prompt_chars": len(persona_prompt)},
            success=True,
            result={"state": "started"},
        )
        result = agent.invoke({"messages": [{"role": "user", "content": persona_prompt}]}, config={"recursion_limit": 8})
        text = final_text(result).strip()
        self.recorder.record_attempt(
            seat_id="customer",
            tool="llm_complete",
            args={"backend": self.backend, "model": self.model, "role": "customer", "prompt_chars": len(persona_prompt)},
            success=True,
            result={"response_chars": len(text)},
        )
        return text


def default_customer_llm(*, model: str | None, recorder: RunRecorder) -> CustomerLLM:
    return DeepAgentCustomer(model=normalize_openrouter_model(model), recorder=recorder)


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
