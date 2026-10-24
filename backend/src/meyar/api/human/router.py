from fastapi import APIRouter

from meyar.api.contracts import AuthPolicy, build_contract_router
from meyar.api.human import conversations
from meyar.api.human.auth import require_human_mutation, require_human_scopes

human_router = APIRouter()
human_router.include_router(conversations.router)
human_router.include_router(
    build_contract_router(
        AuthPolicy(
            read=lambda scope: require_human_scopes(scope),
            write=lambda scope: require_human_mutation(scope),
            channel="UI_JSON",
        ),
        tags={
            "candidates": "human-candidates",
            "jobs": "human-jobs",
            "evaluations": "human-evaluations",
        },
    ),
    prefix="/ui/api/v1",
)
