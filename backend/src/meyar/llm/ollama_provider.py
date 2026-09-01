import json
from typing import Any

import httpx
from pydantic import ValidationError

from meyar.agent.prompts import AGENT_SYSTEM_PROMPT, build_agent_user_prompt
from meyar.agent.schemas import AgentDecision
from meyar.extraction.identity_prompts import IDENTITY_SYSTEM_PROMPT
from meyar.extraction.prompts import SYSTEM_PROMPT, build_user_prompt
from meyar.extraction.view import ProfessionalDocumentView
from meyar.llm.concurrency import get_inference_semaphore
from meyar.llm.loopback import require_loopback_url
from meyar.llm.provider import (
    LLMResultProvenance,
    ModelSchemaInvalidError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from meyar.schemas.candidate_identity import CandidateIdentityExtraction
from meyar.schemas.candidate_profile import CandidateProfileExtraction
from meyar.search.planner_prompts import (
    SEARCH_PLANNER_SYSTEM_PROMPT,
    build_search_planner_user_prompt,
)
from meyar.search.planner_schemas import PlannerDraft


class OllamaLLMProvider:
    """Local-only Ollama provider. Candidate document content never
    leaves this machine — the base_url is required to be a loopback
    address; anything else is rejected at construction time. See
    docs/MASTER_SPEC.md §16 and Slice 4 spec §3."""

    provider_name = "ollama"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        max_concurrency: int = 1,
    ) -> None:
        require_loopback_url(base_url, setting_name="MEYAR_OLLAMA_BASE_URL")
        self._base_url = base_url.rstrip("/")
        self.model_name = model
        self.model_revision = ""
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._max_concurrency = max_concurrency

    async def health(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._base_url}/api/tags")
                resp.raise_for_status()
                tags = [m.get("name") for m in resp.json().get("models", [])]
                return {
                    "reachable": True,
                    "model": self.model_name,
                    "model_available": self.model_name in tags,
                }
        except httpx.HTTPError:
            return {"reachable": False, "model": self.model_name, "model_available": False}

    async def extract_candidate_profile(
        self, view: ProfessionalDocumentView
    ) -> tuple[CandidateProfileExtraction, str]:
        content, provenance = await self._chat(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=build_user_prompt(view),
            schema=CandidateProfileExtraction.model_json_schema(),
        )
        try:
            extraction = CandidateProfileExtraction.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ModelSchemaInvalidError(
                f"Model output failed structured-schema validation: {exc}"
            ) from exc
        return extraction, provenance.model_name

    async def extract_candidate_identity(
        self, view: ProfessionalDocumentView
    ) -> tuple[CandidateIdentityExtraction, str]:
        content, provenance = await self._chat(
            system_prompt=IDENTITY_SYSTEM_PROMPT,
            user_prompt=build_user_prompt(view),
            schema=CandidateIdentityExtraction.model_json_schema(),
        )
        try:
            extraction = CandidateIdentityExtraction.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ModelSchemaInvalidError(
                f"Model output failed structured-schema validation: {exc}"
            ) from exc
        return extraction, provenance.model_name

    async def plan_candidate_search(
        self, natural_language_request: str, *, repair: bool = False
    ) -> tuple[PlannerDraft, LLMResultProvenance]:
        content, provenance = await self._chat(
            system_prompt=SEARCH_PLANNER_SYSTEM_PROMPT,
            user_prompt=build_search_planner_user_prompt(
                natural_language_request, repair=repair
            ),
            schema=PlannerDraft.model_json_schema(),
        )
        try:
            draft = PlannerDraft.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ModelSchemaInvalidError(
                "Model output failed PlannerDraft structured-schema validation."
            ) from exc
        return draft, provenance

    async def decide_agent_action(
        self,
        *,
        recent_turns: list[tuple[str, str]],
        last_tool_result_summary: dict[str, Any] | None,
        available_candidate_refs: list[int],
        repair: bool = False,
    ) -> tuple[AgentDecision, LLMResultProvenance]:
        content, provenance = await self._chat(
            system_prompt=AGENT_SYSTEM_PROMPT,
            user_prompt=build_agent_user_prompt(
                recent_turns=recent_turns,
                last_tool_result_summary=last_tool_result_summary,
                available_candidate_refs=available_candidate_refs,
                repair=repair,
            ),
            schema=AgentDecision.model_json_schema(),
        )
        try:
            decision = AgentDecision.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ModelSchemaInvalidError(
                "Model output failed AgentDecision structured-schema validation."
            ) from exc
        return decision, provenance

    async def _chat(
        self, *, system_prompt: str, user_prompt: str, schema: dict
    ) -> tuple[str, LLMResultProvenance]:
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0.0},
        }
        semaphore = get_inference_semaphore(self._max_concurrency)
        try:
            async with semaphore:
                async with httpx.AsyncClient(
                    timeout=self._timeout_seconds, transport=self._transport
                ) as client:
                    resp = await client.post(f"{self._base_url}/api/chat", json=payload)
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError(
                f"Ollama request timed out after {self._timeout_seconds}s."
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailableError(f"Ollama unreachable: {exc}") from exc

        if resp.status_code != 200:
            raise ModelUnavailableError(f"Ollama returned HTTP {resp.status_code}.")

        response_payload = resp.json()
        actual_model = str(response_payload.get("model") or self.model_name)
        provenance = LLMResultProvenance(
            provider=self.provider_name,
            model_name=actual_model,
            model_revision=self.model_revision,
        )
        return str(response_payload.get("message", {}).get("content", "")), provenance
