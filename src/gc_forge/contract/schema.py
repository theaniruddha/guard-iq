from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Side(str, Enum):
    input = "input"
    output = "output"


class Bucket(str, Enum):
    block = "block"
    steer = "steer"
    graceful_reject = "graceful_reject"
    pass_through = "pass_through"


class Severity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class ReviewStatus(str, Enum):
    draft = "draft"
    approved = "approved"
    edited = "edited"
    rejected = "rejected"


class Rule(BaseModel):
    id: str
    side: Side
    bucket: Bucket
    intent: str
    message: Optional[str] = None
    route_to: Optional[str] = None
    severity: Severity
    rationale: str
    source: str
    pii_delegate: Optional[str] = None
    needs_review: bool = True
    review_status: ReviewStatus = ReviewStatus.draft


class Policy(BaseModel):
    business_type: str
    intent: Optional[str] = None
    version: int = 1
    knowledge_version: str
    input_rules: list[Rule] = Field(default_factory=list)
    output_rules: list[Rule] = Field(default_factory=list)