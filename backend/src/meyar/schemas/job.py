import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from meyar.schemas.criteria import CriteriaListIn, CriterionOut


class JobCreateRequest(CriteriaListIn):
    title: str = Field(min_length=1, max_length=255)


class CriteriaVersionCreateRequest(CriteriaListIn):
    pass


class JobCriteriaVersionOut(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    version_number: int
    criteria: list[CriterionOut]
    created_at: datetime


class JobOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    title: str
    created_at: datetime
    current_criteria_version: JobCriteriaVersionOut
