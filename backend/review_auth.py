"""Resolve deployment-provisioned bearer tokens to reviewer identities."""

from hmac import compare_digest
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, TypeAdapter

from backend.review_models import Role

ActorId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ReviewerCredential(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    role: Role
    token: Annotated[str, StringConstraints(min_length=16, max_length=256, pattern="^[!-~]+$")]


def load_reviewers(config: str) -> dict[str, ReviewerCredential]:
    try:
        reviewers = TypeAdapter(dict[ActorId, ReviewerCredential]).validate_json(config)
    except ValueError:
        raise ValueError("Invalid REVIEWER_CREDENTIALS_JSON.") from None
    if len({reviewer.token for reviewer in reviewers.values()}) != len(reviewers):
        raise ValueError("Reviewer bearer credentials must be distinct.")
    return reviewers


def authenticate(reviewers: dict[str, ReviewerCredential], authorization: str) -> tuple[str, Role] | None:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    for actor, reviewer in reviewers.items():
        if compare_digest(token.encode(), reviewer.token.encode()):
            return actor, reviewer.role
    return None
