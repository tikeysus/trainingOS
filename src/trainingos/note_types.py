"""Shared note type definitions used by CLI and API."""

from __future__ import annotations

from trainingos.domain import NoteKind

NOTE_TYPE_MAP: dict[str, NoteKind] = {
    "illness": NoteKind.ILLNESS,
    "injury": NoteKind.INJURY,
    "travel": NoteKind.TRAVEL,
    "stress": NoteKind.STRESS,
    "note": NoteKind.GENERAL,
}
