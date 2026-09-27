"""Human-request model, CAS answer semantics, and active question finder.

Enforces that answering CASes only on question_id + question_revision,
ignoring harmless HEAD changes, lifecycle revisions, or conversation rebindings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping, Sequence

from dev_orchestrator.convergence.work_record import WorkRecord


class HumanRequestConflictError(RuntimeError):
    """Raised when CAS on question_id + question_revision fails."""


@dataclass(frozen=True)
class HumanRequestOption:
    option_id: str
    description: str
    effect: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "option_id": self.option_id,
            "description": self.description,
            "effect": self.effect,
        }


@dataclass(frozen=True)
class HumanRequest:
    question_id: str
    question_revision: int
    request_type: str
    concrete_question: str
    options: tuple[HumanRequestOption, ...]
    authorization_scope: str = "goal_scope"
    resume_checkpoint: str = "re-evaluate"
    evidence: dict[str, Any] = field(default_factory=dict)
    answer: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "question_revision": self.question_revision,
            "request_type": self.request_type,
            "concrete_question": self.concrete_question,
            "options": [opt.to_dict() for opt in self.options],
            "authorization_scope": self.authorization_scope,
            "resume_checkpoint": self.resume_checkpoint,
            "evidence": dict(self.evidence),
            "answer": dict(self.answer) if self.answer is not None else None,
        }

    @property
    def is_answered(self) -> bool:
        return self.answer is not None and bool(self.answer.get("option_id"))


def derive_question_id(
    goal_id: str,
    request_type: str,
    semantic_key: str,
) -> str:
    """Derive a stable question_id independent of HEAD, PID, or timestamps."""
    raw = f"{goal_id}:{request_type}:{semantic_key}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"q-{digest}"


def create_human_request(
    *,
    goal_id: str,
    request_type: str,
    concrete_question: str,
    options: Sequence[HumanRequestOption | Mapping[str, Any]],
    semantic_key: str = "",
    authorization_scope: str = "goal_scope",
    resume_checkpoint: str = "re-evaluate",
    evidence: Mapping[str, Any] | None = None,
    question_revision: int = 1,
) -> HumanRequest:
    """Create a new structured HumanRequest with stable question_id."""
    q_id = derive_question_id(goal_id, request_type, semantic_key or concrete_question)
    parsed_options = []
    for opt in options:
        if isinstance(opt, HumanRequestOption):
            parsed_options.append(opt)
        elif isinstance(opt, Mapping):
            parsed_options.append(
                HumanRequestOption(
                    option_id=str(opt["option_id"]),
                    description=str(opt["description"]),
                    effect=str(opt["effect"]),
                )
            )
        else:
            raise TypeError(f"Invalid option type: {type(opt).__name__}")

    return HumanRequest(
        question_id=q_id,
        question_revision=question_revision,
        request_type=request_type,
        concrete_question=concrete_question,
        options=tuple(parsed_options),
        authorization_scope=authorization_scope,
        resume_checkpoint=resume_checkpoint,
        evidence=dict(evidence or {}),
        answer=None,
    )


def answer_human_request(
    request: HumanRequest,
    *,
    question_id: str,
    question_revision: int,
    option_id: str,
    owner_id: str,
    command_id: str,
    answered_at: str,
) -> HumanRequest:
    """Answer an open HumanRequest using strict CAS on (question_id, question_revision) ONLY.

    Does not depend on project HEAD, lifecycle revision, or active role.
    """
    if request.question_id != question_id:
        raise HumanRequestConflictError(
            f"Question ID mismatch: request has {request.question_id!r}, "
            f"attempted to answer {question_id!r}"
        )
    if request.question_revision != question_revision:
        raise HumanRequestConflictError(
            f"Question revision mismatch: request has revision {request.question_revision}, "
            f"attempted to answer revision {question_revision}"
        )
    if request.is_answered:
        raise HumanRequestConflictError(
            f"Question {question_id!r} is already answered"
        )

    # Validate option_id exists
    valid_option_ids = {opt.option_id for opt in request.options}
    if option_id not in valid_option_ids:
        raise ValueError(
            f"Invalid option_id {option_id!r}. Valid options are {sorted(valid_option_ids)}"
        )

    answer_record = {
        "option_id": option_id,
        "owner_id": owner_id,
        "command_id": command_id,
        "answered_at": answered_at,
    }

    return HumanRequest(
        question_id=request.question_id,
        question_revision=request.question_revision,
        request_type=request.request_type,
        concrete_question=request.concrete_question,
        options=request.options,
        authorization_scope=request.authorization_scope,
        resume_checkpoint=request.resume_checkpoint,
        evidence=request.evidence,
        answer=answer_record,
    )


def find_active_human_request(work_record: WorkRecord) -> HumanRequest | None:
    """Find the single active open human request on the WorkRecord."""
    hr_dict = work_record.human_request
    if not hr_dict:
        return None
    raw_options = hr_dict.get("options", [])
    opts = tuple(
        HumanRequestOption(
            option_id=str(o.get("option_id", "")),
            description=str(o.get("description", "")),
            effect=str(o.get("effect", "")),
        )
        for o in raw_options
    )
    return HumanRequest(
        question_id=str(hr_dict.get("question_id", "")),
        question_revision=int(hr_dict.get("question_revision", 1)),
        request_type=str(hr_dict.get("request_type", "")),
        concrete_question=str(hr_dict.get("concrete_question", "")),
        options=opts,
        authorization_scope=str(hr_dict.get("authorization_scope", "goal_scope")),
        resume_checkpoint=str(hr_dict.get("resume_checkpoint", "re-evaluate")),
        evidence=dict(hr_dict.get("evidence", {})),
        answer=dict(hr_dict["answer"]) if hr_dict.get("answer") else None,
    )
