from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Protocol

from .env import normalize_openrouter_model, openrouter_slug
from .identity import display_name_for_seat
from .recorder import RunRecorder

_PROFILE_REGISTERED = False


def register_company_twin_profile() -> None:
    global _PROFILE_REGISTERED
    if _PROFILE_REGISTERED:
        return
    from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, register_harness_profile

    profile = HarnessProfile(
        excluded_tools=frozenset({"task", "write_todos", "ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute"}),
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    )
    register_harness_profile("openrouter", profile)
    register_harness_profile("openai", profile)
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


def load_role_card(root: Path, role: str) -> str:
    path = root / "data" / "design" / "role_cards" / f"{role}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def role_system_prompt(seat_id: str, role: str, *, role_card: str = "") -> str:
    card = role_card.strip() or f"（役割: {role}）"
    display_name = display_name_for_seat(seat_id)
    return f"""あなたはダミーフィナンシャルホールディングスの販売業務に従事する{display_name}です。

{card}

行動上のルール:
- 提供された世界ツール（検索・文書閲覧・チャット・ワークフロー各操作・自分用メモ）だけを通じて行動する。
- 規程やマニュアルの内容を推測で語らない。統制に関わる行為（承認・受付・記録など）の前には、必要な文書を検索・閲覧し、実際に読んだ文書を根拠として引用する。
- 統制に関わるツールは basis(JSON) を要求する。basisには、参照した文書と版、あなたの読み（construal）、決定、証跡の残し方、検討した代替案、感じている制約、確信度を正直に書く。
- 業務記録・チャット・連絡文は、社内の業務記録として読める通常の日本語で書く。システムのツール名やJSONのキー名をそのまま文章に書かない（例:「証跡」であって"evidence_json"ではない、「顧客対応記録」であって"record_customer_contact"ではない）。日時は年月日で書き、内部の管理用語（tick等）を使わない。
- 職場として自然な言葉で、必要な相手にはチャットで連絡する。判断に迷う場合は、進める・保留する・確認を取るのいずれかを自分の役割として選ぶ。
"""


class DeepAgentSeat:
    backend = "deepagents"

    def __init__(self, *, seat_id: str, role: str, tools: list[Any], model: str, root: Path, recorder: RunRecorder, recursion_limit: int):
        register_company_twin_profile()
        from deepagents import create_deep_agent

        self.seat_id = seat_id
        self.model = normalize_openrouter_model(model)
        self.recorder = recorder
        self.recursion_limit = recursion_limit
        self._agent = create_deep_agent(
            model=_chat_model(self.model),
            tools=tools,
            system_prompt=role_system_prompt(seat_id, role, role_card=load_role_card(root, role)),
            subagents=[],
            name=f"company-twin-{seat_id}",
        )

    def turn(self, prompt: str) -> str:
        self.recorder.record_attempt(
            seat_id=self.seat_id,
            tool="llm_invoke",
            args={"backend": self.backend, "model": self.model, "prompt_chars": len(prompt), "phase": "start"},
            success=True,
            result={"phase": "start"},
        )
        try:
            result = self._agent.invoke({"messages": [{"role": "user", "content": prompt}]}, config={"recursion_limit": self.recursion_limit})
        except Exception as exc:
            self.recorder.record_attempt(
                seat_id=self.seat_id,
                tool="llm_invoke",
                args={
                    "backend": self.backend,
                    "model": self.model,
                    "prompt_chars": len(prompt),
                    "phase": "error",
                    "error_type": type(exc).__name__,
                },
                success=False,
                result={"error_type": type(exc).__name__, "message": str(exc)[:500]},
            )
            raise
        text = final_text(result)
        self.recorder.record_attempt(
            seat_id=self.seat_id,
            tool="llm_response",
            args={"backend": self.backend, "model": self.model, "prompt_chars": len(prompt)},
            success=True,
            result={"response_chars": len(text)},
        )
        return text


def default_seat_factory(*, root: Path, model: str | None) -> SeatFactory:
    model_name = normalize_openrouter_model(model)

    def factory(*, seat_id: str, role: str, tools: list[Any], recorder: RunRecorder, recursion_limit: int) -> SeatAgent:
        return DeepAgentSeat(seat_id=seat_id, role=role, tools=tools, model=model_name, root=root, recorder=recorder, recursion_limit=recursion_limit)

    return factory


CUSTOMER_SYSTEM_PROMPT = """あなたは金融サービスの申込・相談を行う一般のお客様本人です。
与えられた「あなたの状況」と「あなた自身しか知らない内心」に沿って、担当者に伝える発話を一つ、日本語で自然に生成してください。
内心の記述をそのまま読み上げたり、設定や指示という言葉を使ったりせず、その人物として話してください。発話は2〜4文。
自分の状況を「標準的な条件」「通常の案件」「通常どおり」「一般的な手続き」のような分類ラベルで形容してはいけません。普通の客は自分の事情をそのように呼びません。要望や事情は具体的な言葉でそのまま伝えてください。
発話は必ず自然な日本語のみで生成し、他の言語の単語や漢字表現を混ぜないでください。"""


def _corrective_instruction(language_hits: list[str], glitch_hits: list[str]) -> str:
    """Compose the retry-attempt corrective instruction for whichever
    guard(s) tripped on the previous attempt (data/design/MASTER_DESIGN.md
    §17.5/§17.14). Both guards can fire on the same attempt (e.g. a garbled
    tail that also happens to mix in a stray Latin word), in which case both
    corrective notes are included in the single retry prompt.
    """
    notes: list[str] = []
    if language_hits:
        notes.append("直前の発話案に日本語として不自然な語（他言語由来の表現）が含まれていました。")
    if glitch_hits:
        notes.append("直前の発話案に、同じ内容の繰り返しや、途中で途切れた/壊れた文章が含まれていました。")
    notes.append("自然な日本語のみを使って、同じ内容を最初から一つの完結した発話としてもう一度生成してください。")
    return "注意: " + "".join(notes)


class DeepAgentCustomer:
    backend = "deepagents"

    def __init__(self, *, model: str, recorder: RunRecorder):
        register_company_twin_profile()
        from deepagents import create_deep_agent

        self.model = normalize_openrouter_model(model)
        self.recorder = recorder
        self._agent = create_deep_agent(model=_chat_model(self.model), tools=[], system_prompt=CUSTOMER_SYSTEM_PROMPT, subagents=[], name="company-twin-customer")

    def __call__(self, persona_prompt: str) -> str:
        # Language-mixing guard (data/design/MASTER_DESIGN.md §17.5, round-3
        # blind SME review): detect obviously non-Japanese tokens in the
        # generated utterance and retry once with a corrective instruction.
        # Detection lives in customer_agent (imported lazily here to avoid a
        # circular import, since customer_agent imports CustomerLLM from this
        # module). The retry is best-effort and deterministic in structure --
        # if the second attempt still trips the detector, the text is kept
        # as-is (honest: we never silently rewrite the utterance). Both calls
        # go through the ordinary llm_invoke/llm_response recording path, so
        # every attempt -- original and retry -- is auditable in
        # attempts.jsonl without any special-cased recorder semantics.
        #
        # Round-7 blind SME review follow-up (data/design/MASTER_DESIGN.md
        # §17.14): the same shape now also covers stochastic customer-LLM
        # output glitches -- a repeated contiguous fragment, or a
        # broken/truncated tail (see customer_agent.detect_customer_output_glitch).
        # Both guards are checked on every attempt (not just the first), so a
        # retry that fixes language-mixing but happens to introduce a
        # duplicated fragment (or vice versa) is still caught by the single
        # retry pass; only one retry is ever performed, matching the existing
        # honest-keep-if-still-flagged semantics.
        from .customer_agent import detect_customer_output_glitch, detect_non_japanese_tokens

        def _detect(text: str) -> tuple[list[str], list[str]]:
            return detect_non_japanese_tokens(text), detect_customer_output_glitch(text)

        text = self._invoke(persona_prompt)
        language_hits, glitch_hits = _detect(text)
        if language_hits or glitch_hits:
            corrective_prompt = f"{persona_prompt}\n\n{_corrective_instruction(language_hits, glitch_hits)}"
            text = self._invoke(corrective_prompt)
        return text

    def _invoke(self, prompt: str) -> str:
        self.recorder.record_attempt(
            seat_id="customer",
            tool="llm_invoke",
            args={"backend": self.backend, "model": self.model, "role": "customer", "prompt_chars": len(prompt), "phase": "start"},
            success=True,
            result={"phase": "start"},
        )
        result = self._agent.invoke({"messages": [{"role": "user", "content": prompt}]}, config={"recursion_limit": 8})
        text = final_text(result).strip()
        self.recorder.record_attempt(
            seat_id="customer",
            tool="llm_response",
            args={"backend": self.backend, "model": self.model, "role": "customer", "prompt_chars": len(prompt)},
            success=True,
            result={"response_chars": len(text)},
        )
        return text


def default_customer_llm(*, model: str | None, recorder: RunRecorder) -> CustomerLLM:
    return DeepAgentCustomer(model=normalize_openrouter_model(model), recorder=recorder)


def _chat_model(model: str):
    from langchain_openai import ChatOpenAI

    timeout = int(os.getenv("OPENROUTER_TIMEOUT_SECONDS", "45"))
    return ChatOpenAI(
        api_key=os.environ["OPENROUTER_API_KEY"],
        model=openrouter_slug(model),
        base_url="https://openrouter.ai/api/v1",
        timeout=timeout,
        max_retries=0,
        max_completion_tokens=int(os.getenv("OPENROUTER_MAX_COMPLETION_TOKENS", "1200")),
    )


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
