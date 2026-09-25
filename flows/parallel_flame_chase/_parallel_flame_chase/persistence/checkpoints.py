from __future__ import annotations

from collections.abc import Mapping

from ..core.models import LaneCheckpoint, LaneReport

IDENTITY_FIELDS = ("version", "run_id", "lane", "mission_id", "generation")


def read_checkpoint(
    text: str | None,
    expected: Mapping[str, object],
) -> LaneCheckpoint | None:
    if text is None:
        return None
    try:
        checkpoint = LaneCheckpoint.model_validate_json(text)
    except ValueError:
        return None
    identity = checkpoint.identity.model_dump(mode="json")
    if any(identity.get(key) != expected.get(key) for key in IDENTITY_FIELDS):
        return None
    return checkpoint


def checkpoint_report(
    state: tuple[str | None, str | None],
    before: str | None,
    expected: Mapping[str, object],
) -> LaneReport | None:
    fingerprint, text = state
    if fingerprint == before:
        return None
    checkpoint = read_checkpoint(text, expected)
    return checkpoint.report if checkpoint is not None else None
