"""Machine REST evaluation routes.

The deterministic score / rank / evaluation-by-id routes are defined once in
``meyar.api.contracts`` (shared with the human JSON surface) and mounted by
``meyar.api.v1.router``; they call ``meyar.application.evaluations``, which
delegates to ``meyar.evaluation`` / ``meyar.scoring``. Nothing is computed here."""

from fastapi import APIRouter

router = APIRouter(tags=["evaluations"])
