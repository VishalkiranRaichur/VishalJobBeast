"""Durable application states and transition validation."""
from __future__ import annotations

from enum import Enum


class ApplicationState(str, Enum):
    DISCOVERED = "DISCOVERED"
    HYDRATED = "HYDRATED"
    RANKED = "RANKED"
    RESUME_READY = "RESUME_READY"
    READY_TO_APPLY = "READY_TO_APPLY"
    APPLYING = "APPLYING"
    NEEDS_INPUT = "NEEDS_INPUT"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    INELIGIBLE_HARD_REQUIREMENT = "INELIGIBLE_HARD_REQUIREMENT"
    SUBMITTED = "SUBMITTED"
    FAILED = "FAILED"
    CLOSED = "CLOSED"
    READY_FOR_CLAUDE = "READY_FOR_CLAUDE"
    CLAUDE_RUNNING = "CLAUDE_RUNNING"
    CLAUDE_NEEDS_INPUT = "CLAUDE_NEEDS_INPUT"
    CLAUDE_SUBMITTED = "CLAUDE_SUBMITTED"
    CLAUDE_FAILED = "CLAUDE_FAILED"
    READY_FOR_HUMAN_CAPTCHA = "READY_FOR_HUMAN_CAPTCHA"
    READY_FOR_HUMAN_MFA = "READY_FOR_HUMAN_MFA"
    # The user reports submitting it themselves, but no employer confirmation
    # has been seen yet. Blocks duplicates exactly like SUBMITTED.
    SUBMITTED_UNVERIFIED = "SUBMITTED_UNVERIFIED"


TRANSITIONS: dict[str, set[str]] = {
    "DISCOVERED": {"HYDRATED", "RESUME_READY", "FAILED"},
    "HYDRATED": {"RANKED", "RESUME_READY", "FAILED"},
    "RANKED": {"RESUME_READY", "FAILED"},
    "RESUME_READY": {"READY_TO_APPLY", "NEEDS_INPUT", "NEEDS_HUMAN", "FAILED"},
    "READY_TO_APPLY": {"APPLYING", "RESUME_READY", "FAILED", "READY_FOR_CLAUDE"},
    "APPLYING": {"NEEDS_INPUT", "NEEDS_HUMAN", "SUBMITTED", "FAILED", "READY_TO_APPLY"},
    "NEEDS_INPUT": {"APPLYING", "RESUME_READY", "FAILED", "CLOSED", "INELIGIBLE_HARD_REQUIREMENT"},
    "NEEDS_HUMAN": {"APPLYING", "RESUME_READY", "FAILED", "CLOSED"},
    "SUBMITTED": {"CLOSED"},
    "FAILED": {"APPLYING", "CLOSED", "READY_FOR_CLAUDE"},
    "CLOSED": set(),
    "INELIGIBLE_HARD_REQUIREMENT": {"CLOSED"},
    # Claude operates out-of-band in a live browser session; Job Beast has no
    # live callback for "started running", so import-result must be able to
    # jump straight from READY_FOR_CLAUDE to a terminal CLAUDE_* state in one
    # shot, not just via an intermediate CLAUDE_RUNNING.
    "READY_FOR_CLAUDE": {"SUBMITTED_UNVERIFIED", "CLAUDE_RUNNING", "CLAUDE_NEEDS_INPUT", "CLAUDE_SUBMITTED", "CLAUDE_FAILED", "FAILED", "READY_FOR_HUMAN_CAPTCHA", "READY_FOR_HUMAN_MFA"},
    "CLAUDE_RUNNING": {"CLAUDE_NEEDS_INPUT", "CLAUDE_SUBMITTED", "CLAUDE_FAILED", "READY_FOR_HUMAN_CAPTCHA", "READY_FOR_HUMAN_MFA"},
    "CLAUDE_NEEDS_INPUT": {"SUBMITTED_UNVERIFIED", "CLAUDE_RUNNING", "CLAUDE_SUBMITTED", "CLAUDE_FAILED", "CLOSED", "FAILED", "INELIGIBLE_HARD_REQUIREMENT", "READY_FOR_CLAUDE", "READY_FOR_HUMAN_CAPTCHA", "READY_FOR_HUMAN_MFA"},
    # Parked for the user to complete a CAPTCHA / MFA step; the application
    # keeps its packet and tab so it can be re-verified and submitted at once.
    "READY_FOR_HUMAN_CAPTCHA": {"SUBMITTED_UNVERIFIED", "CLAUDE_RUNNING", "CLAUDE_SUBMITTED", "CLAUDE_FAILED", "CLAUDE_NEEDS_INPUT", "READY_FOR_HUMAN_MFA", "READY_FOR_CLAUDE", "CLOSED", "FAILED"},
    "READY_FOR_HUMAN_MFA": {"SUBMITTED_UNVERIFIED", "CLAUDE_RUNNING", "CLAUDE_SUBMITTED", "CLAUDE_FAILED", "CLAUDE_NEEDS_INPUT", "READY_FOR_HUMAN_CAPTCHA", "READY_FOR_CLAUDE", "CLOSED", "FAILED"},
    "CLAUDE_SUBMITTED": {"SUBMITTED", "CLAUDE_FAILED"},
    # Not truly terminal: Claude (or the operator) may retry out-of-band and a
    # later import-result can still land on any outcome, including success.
    "SUBMITTED_UNVERIFIED": {"CLAUDE_SUBMITTED", "SUBMITTED", "CLOSED"},
    "CLAUDE_FAILED": {"SUBMITTED_UNVERIFIED", "READY_FOR_CLAUDE", "CLAUDE_NEEDS_INPUT", "CLAUDE_SUBMITTED", "CLOSED", "FAILED", "READY_FOR_HUMAN_CAPTCHA", "READY_FOR_HUMAN_MFA"},
}


LEGACY_STATUS_MAP = {
    "saved": ApplicationState.DISCOVERED.value,
    "ready_to_apply": ApplicationState.READY_TO_APPLY.value,
    "applying": ApplicationState.APPLYING.value,
    "needs_input": ApplicationState.NEEDS_INPUT.value,
    "needs_human": ApplicationState.NEEDS_HUMAN.value,
    "applied": ApplicationState.SUBMITTED.value,
    "submitted": ApplicationState.SUBMITTED.value,
    "interview": ApplicationState.CLOSED.value,
    "offer": ApplicationState.CLOSED.value,
    "rejected": ApplicationState.CLOSED.value,
    "closed": ApplicationState.CLOSED.value,
}


def normalize_state(value: str | None) -> str:
    raw = str(value or "DISCOVERED").upper()
    return LEGACY_STATUS_MAP.get(str(value or "").lower(), raw)


def can_transition(current: str, target: str, *, force: bool = False) -> bool:
    current = normalize_state(current)
    target = normalize_state(target)
    if current == target:
        return True
    if force and current == ApplicationState.SUBMITTED.value:
        return target == ApplicationState.APPLYING.value
    if target == ApplicationState.INELIGIBLE_HARD_REQUIREMENT.value:
        return current != ApplicationState.SUBMITTED.value
    return target in TRANSITIONS.get(current, set())
