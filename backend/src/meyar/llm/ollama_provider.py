import json

import httpx
from pydantic import ValidationError

from meyar.extraction.identity_prompts import IDENTITY_SYSTEM_PROMPT
from meyar.extraction.prompts import SYSTEM_PROMPT, build_user_prompt
from meyar.extraction.view import ProfessionalDocumentView
from meyar.llm.loopback import require_loopback_url
from meyar.llm.provider import ModelSchemaInvalidError, ModelTimeoutError, ModelUnavailableError
from meyar.schemas.candidate_identity import CandidateIdentityExtraction
from meyar.schemas.candidate_profile import CandidateProfileExtraction


class OllamaLLMProvider:
    """Local-only Ollama provider. Candidate document content never
    leaves this machine — the base_url is required to be a loopback
    address; anything else is rejected at construction time. See
    docs/MASTER_SPEC.md §16 and Slice 4 spec §3."""

    def __init__(self, *, base_url: str, model: str, timeout_seconds: float) -> None:
        require_loopback_url(base_url, setting_name="MEYAR_OLLAMA_BASE_URL")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_seconds = timeout_seconds

    async def health(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._base_url}/api/tags")
                resp.raise_for_status()
                tags = [m.get("name") for m in resp.json().get("models", [])]
                return {
                    "reachable": True,
                    "model": self._model,
                    "model_available": self._model in tags,
                }
        except httpx.HTTPError:
            return {"reachable": False, "model": self._model, "model_available": False}

    async def extract_candidate_profile(
        self, view: ProfessionalDocumentView
    ) -> tuple[CandidateProfileExtraction, str]:
        content = await self._chat(
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
        return extraction, self._model

    async def extract_candidate_identity(
        self, view: ProfessionalDocumentView
    ) -> tuple[CandidateIdentityExtraction, str]:
        content = await self._chat(
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
        return extraction, self._model

    async def _chat(self, *, system_prompt: str, user_prompt: str, schema: dict) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0.0},
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                resp = await client.post(f"{self._base_url}/api/chat", json=payload)
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError(
                f"Ollama request timed out after {self._timeout_seconds}s."
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailableError(f"Ollama unreachable: {exc}") from exc

        if resp.status_code != 200:
            raise ModelUnavailableError(f"Ollama returned HTTP {resp.status_code}.")

        return str(resp.json().get("message", {}).get("content", ""))
