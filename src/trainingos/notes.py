"""CLI entry point: python -m trainingos.notes"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

from trainingos.domain import (
    ContextNote,
    NoteKind,
    Provenance,
    ProvenanceKind,
    RecordMetadata,
)
from trainingos.normalization import NormalizationStore
from trainingos.storage import apply_migrations, connect_database

_USER_TYPES: dict[str, NoteKind] = {
    "illness": NoteKind.ILLNESS,
    "injury": NoteKind.INJURY,
    "travel": NoteKind.TRAVEL,
    "stress": NoteKind.STRESS,
    "note": NoteKind.GENERAL,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m trainingos.notes",
        description="Manage TrainingOS context notes.",
    )
    parser.add_argument("--database", type=Path, required=True, metavar="PATH")
    sub = parser.add_subparsers(dest="command")
    sub.required = True

    add_p = sub.add_parser("add", help="Add a context note.")
    add_p.add_argument("--type", dest="note_type", required=True)
    add_p.add_argument("--body", required=True)
    add_p.add_argument("--date", dest="note_date", metavar="YYYY-MM-DD")
    add_p.add_argument("--activity", metavar="RECORD_ID")

    list_p = sub.add_parser("list", help="List context notes.")
    list_p.add_argument("--type", dest="note_type")
    list_p.add_argument("--since", metavar="YYYY-MM-DD")

    del_p = sub.add_parser("delete", help="Delete a context note by record ID.")
    del_p.add_argument("record_id")

    return parser


def _parse_date(value: str, label: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        print(f"error: {label} must be YYYY-MM-DD, got {value!r}", file=sys.stderr)
        sys.exit(1)


def _cmd_add(args: argparse.Namespace, db: Path) -> None:
    if args.note_type not in _USER_TYPES:
        valid = ", ".join(_USER_TYPES)
        print(
            f"error: unknown note type {args.note_type!r}; valid: {valid}",
            file=sys.stderr,
        )
        sys.exit(1)

    if not args.body.strip():
        print("error: body must not be blank", file=sys.stderr)
        sys.exit(1)

    if args.note_date is not None:
        parsed_date = _parse_date(args.note_date, "--date")
    else:
        parsed_date = datetime.now(UTC).date()

    occurred_at = datetime(
        parsed_date.year, parsed_date.month, parsed_date.day, 0, 0, tzinfo=UTC
    )
    kind = _USER_TYPES[args.note_type]
    now = datetime.now(UTC)
    record_id = f"note:{uuid.uuid4()}"

    with connect_database(db) as connection:
        if args.activity is not None:
            row = connection.execute(
                "SELECT 1 FROM records WHERE record_id = ?", (args.activity,)
            ).fetchone()
            if row is None:
                print(
                    f"error: activity not found: {args.activity}", file=sys.stderr
                )
                sys.exit(1)

        note = ContextNote(
            metadata=RecordMetadata(
                record_id=record_id,
                timezone="UTC",
                created_at=now,
                updated_at=now,
                provenance=Provenance(ProvenanceKind.USER_ENTERED),
            ),
            occurred_at=occurred_at,
            kind=kind,
            text=args.body.strip(),
            linked_record_ids=(args.activity,) if args.activity else (),
        )
        store = NormalizationStore(connection)
        store.upsert_context_note(note)
        connection.commit()

    print(record_id)


def _cmd_list(args: argparse.Namespace, db: Path) -> None:
    kind: NoteKind | None = None
    if args.note_type is not None:
        if args.note_type not in _USER_TYPES:
            valid = ", ".join(_USER_TYPES)
            print(
                f"error: unknown note type {args.note_type!r}; valid: {valid}",
                file=sys.stderr,
            )
            sys.exit(1)
        kind = _USER_TYPES[args.note_type]

    since: date | None = None
    if args.since is not None:
        since = _parse_date(args.since, "--since")

    with connect_database(db) as connection:
        store = NormalizationStore(connection)
        notes = store.list_context_notes(kind=kind, since=since)

    for note in notes:
        print(
            f"{note['date']}  {note['type']}  {note['record_id']}  {note['body']}"
        )
        for linked_id in note["linked_record_ids"]:
            print(f"  linked: {linked_id}")


def _cmd_delete(args: argparse.Namespace, db: Path) -> None:
    with connect_database(db) as connection:
        store = NormalizationStore(connection)
        found = store.delete_context_note(args.record_id)
        if found:
            connection.commit()

    if not found:
        print(f"error: note not found: {args.record_id}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if args.command == "add":
        _cmd_add(args, args.database)
    elif args.command == "list":
        _cmd_list(args, args.database)
    elif args.command == "delete":
        _cmd_delete(args, args.database)


if __name__ == "__main__":
    main()
