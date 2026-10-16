from fastapi import APIRouter

from meyar.api.human import conversations

human_router = APIRouter()
human_router.include_router(conversations.router)
