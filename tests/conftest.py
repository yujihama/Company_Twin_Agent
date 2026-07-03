"""Test-only fakes.

These exist ONLY to exercise world mechanics (kernel, budgets, whitelist,
recorder, triage) without network access. They are NOT a production path:
- backend is stamped "test-fake" on every llm_invoke attempt and in meta,
  so acceptance gate A-02 rejects any bundle they produce;
- they live under tests/ and are injected via the seat_factory /
  customer_llm parameters, which default to real deepagents in production.
"""
from __future__ import annotations

import json
import re
from typing import Any

import pytest

from company_twin.recorder import RunRecorder


class FakeSeatAgent:
    backend = "test-fake"

    def __init__(self, *, seat_id: str, role: str, tools: list[Any], recorder: RunRecorder, model: str = "fake:unit"):
        self.seat_id = seat_id
        self.role = role
        self.recorder = recorder
        self.tools = {tool.__name__: tool for tool in tools}
        self.model = model

    def _basis(self, doc_id: str, decision: str) -> str:
        version = "1.0" if doc_id.endswith("@v1.0") else "1.1"
        return json.dumps(
            {
                "retrieved": [{"doc_id": doc_id, "version": version, "citation_handle": f"read:{doc_id}:v{version}"}],
                "construal": f"unit-fake read {doc_id}",
                "decision": decision,
                "evidence_plan": "keep workflow artifacts",
                "confidence": 0.5,
            },
            ensure_ascii=False,
        )

    def turn(self, prompt: str) -> str:
        self.recorder.record_attempt(
            seat_id=self.seat_id,
            tool="llm_invoke",
            args={"backend": self.backend, "model": self.model, "prompt_chars": len(prompt)},
            success=True,
            result={"response_chars": 0},
        )
        if "受信箱" not in prompt:  # S0 interpretation battery
            hits = json.loads(self.tools["search_corpus"]("高齢者 追加確認 承認", 3))
            doc_id = hits[0]["doc_id"] if hits else "DFH-SAL-021"
            self.tools["read_document"](doc_id, "確認", 800)
            response = json.dumps(
                {
                    "likely_reading": "追加確認と管理者への相談が必要と読む",
                    "required_approver_or_evidence": "管理者の確認と記録",
                    "cited_doc_ids": [doc_id],
                    "uncertainty": "第二線への報告要否",
                    "next_action": "管理者確認を登録する",
                },
                ensure_ascii=False,
            )
            self._record_response(prompt, response)
            return response
        for line in prompt.splitlines():
            line = line.strip()
            if not line.startswith("- {"):
                continue
            message = json.loads(line[2:])
            self._handle(message)
        response = "処理しました。"
        self._record_response(prompt, response)
        return response

    def _record_response(self, prompt: str, response: str) -> None:
        self.recorder.record_attempt(
            seat_id=self.seat_id,
            tool="llm_response",
            args={"backend": self.backend, "model": self.model, "prompt_chars": len(prompt)},
            success=True,
            result={"response_chars": len(response)},
        )

    def _handle(self, message: dict[str, Any]) -> None:
        kind = message.get("kind")
        if kind == "customer_utterance" and self.role == "sales":
            app_id = message["application_id"]
            hits = json.loads(self.tools["search_corpus"](f"{message['product']} 承認 証跡", 3))
            doc_id = hits[0]["doc_id"] if hits else "DFH-SAL-018"
            self.tools["read_document"](doc_id, "承認", 600)
            self.tools["note_to_self"](f"case-{app_id}", "確認済みの読みをメモ")
            self.tools["recall_notes"](5)
            self.tools["record_customer_contact"](message["customer_id"], "phone", message["utterance"][:80], self._basis(doc_id, "contact"))
            self.tools["request_approval"](app_id, "manager", "確認依頼", self._basis(doc_id, "request"))
            self.tools["send_chat"]("emp-M", "workflow", f"{app_id} の確認をお願いします")
            self.tools["send_chat"]("emp-C", "workflow", f"{app_id} の受付準備をお願いします")
        elif kind == "chat":
            body = str(message.get("body") or "")
            match = re.search(r"APP-[A-Za-z0-9-]+", body)
            app_id = match.group(0) if match else ""
            if not app_id:
                return
            if self.role in {"manager", "second_line"}:
                self.tools["read_document"]("DFH-SAL-045", "承認", 600)
                self.tools["approve_application"](app_id, f"APR-{app_id[-4:]}", "確認のうえ承認", self._basis("DFH-SAL-045", "approve"))
                self.tools["send_chat"]("emp-C", "workflow", f"{app_id} 承認済み")
            elif self.role == "application":
                self.tools["read_document"]("DFH-SAL-024", "申込", 600)
                evidence = json.dumps({"material_version": "v1.1", "recording_id": f"REC-{app_id}", "consent_log_id": f"CONS-{app_id}", "checksheet_status": "completed"}, ensure_ascii=False)
                basis = self._basis("DFH-SAL-024", "process")
                self.tools["submit_application"](app_id, app_id.replace("APP", "CUS"), "投資信託", evidence, basis)
                self.tools["verify_identity"](app_id, True, True, f"CONS-{app_id}", basis)
                self.tools["link_review"](app_id, f"REV-{app_id}", basis)
                self.tools["complete_contract"](app_id, f"CON-{app_id}", basis)
                self.tools["deliver_documents"](app_id, f"DEL-{app_id}", basis)


def fake_seat_factory(**_ignored: Any):
    def factory(*, seat_id: str, role: str, tools: list[Any], recorder: RunRecorder, recursion_limit: int, model: str = "fake:unit") -> FakeSeatAgent:
        return FakeSeatAgent(seat_id=seat_id, role=role, tools=tools, recorder=recorder, model=model)

    return factory


class FakeCustomerLLM:
    backend = "test-fake"

    def __init__(self, recorder: RunRecorder):
        self.recorder = recorder

    def __call__(self, persona_prompt: str) -> str:
        self.recorder.record_attempt(
            seat_id="customer",
            tool="llm_invoke",
            args={"backend": self.backend, "model": "fake:unit", "role": "customer", "prompt_chars": len(persona_prompt)},
            success=True,
            result={"response_chars": 30},
        )
        response = "手続きをお願いしたいのですが、進め方を確認させてください。"
        self.recorder.record_attempt(
            seat_id="customer",
            tool="llm_response",
            args={"backend": self.backend, "model": "fake:unit", "role": "customer", "prompt_chars": len(persona_prompt)},
            success=True,
            result={"response_chars": len(response)},
        )
        return response


@pytest.fixture
def seat_factory():
    return fake_seat_factory()
