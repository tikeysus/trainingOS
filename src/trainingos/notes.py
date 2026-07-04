"""CLI for managing context notes (illness, injury, travel, stress, general)."""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import UTC, date, datetime, time
from pathlib import Path

from trainingos.domain import (
    ContextNote,
    NoteKind,
    Provenance,
    ProvenanceKind,
    RecordMetadata,
)
from trainingos.normalization import NormalizationStore
from trainingos.storage import connect_database

_VALID_KINDS = ("illness", "injury", "travel", "stress", "note")
_KIND_MAP = {k: NoteKind(k) for k in _VALID_KINDS}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="trainingos.notes",
        description="Manage context notes.",
    )
    parser.add_argument("--database", type=Path, required=True, metavar="PATH")

    subparsers = parser.add_subparsers(dest="subcommand")

    add_parser = subparsers.add_parser("add", help="Add a context note.")
    add_parser.add_argument("--type", dest="kind", required=True, choices=_VALID_KINDS, metavar="TYPE",
                            help=f"Note type: {', '.join(_VALID_KINDS)}")
    add_parser.add_argument("--body", required=True, help="Note text.")
    add_parser.add_argument("--date", dest="date_str", default=None, metavar="YYYY-MM-DD",
                            help="Date of the note (default: today).")
    add_parser.add_argument("--activity", dest="activity_id", default=None, metavar="RECORD_ID",
                            help="Link note to an activity record.")

    list_parser = subparsers.add_parser("list", help="List context notes.")
    list_parser.add_argument("--type", dest="kind", default=None, choices=_VALID_KINDS, metavar="TYPE",
                             help="Filter by note type.")
    list_parser.add_argument("--since", dest="since_str", default=None, metavar="YYYY-MM-DD",
                             help="Only show notes on or after this date.")

    delete_parser = subparsers.add_parser("delete", help="Delete a context note.")
    delete_parser.add_argument("record_id", help="Record ID of the note to delete.")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 1

    if args.subcommand is None:
        parser.print_usage(sys.stderr)
        return 2

    if args.subcommand == "add":
        return _add(args)
    if args.subcommand == "list":
        return _list(args)
    if args.subcommand == "delete":
        return _delete(args)

    return 0


def _add(args: argparse.Namespace) -> int:
    if args.date_str is not None:
        try:
            note_date = date.fromisoformat(args.date_str)
        except ValueError:
            print("--date must be in YYYY-MM-DD format", file=sys.stderr)
            return 2
    else:
        note_date = date.today()

    occurred_at = datetime.combine(note_date, time.min, tzinfo=UTC)
    now = datetime.now(UTC)
    record_id = str(uuid.uuid4())

    with connect_database(args.database) as connection:
        if args.activity_id is not None:
            row = connection.execute(
                "SELECT record_id FROM records WHERE record_id = ?",
                (args.activity_id,),
            ).fetchone()
            if row is None:
                print(f"activity not found: {args.activity_id}", file=sys.stderr)
                return 1

        note = ContextNote(
            metadata=RecordMetadata(
                record_id=record_id,
                timezone="UTC",
                provenance=Provenance(ProvenanceKind.USER_ENTERED),
                created_at=now,
                updated_at=now,
            ),
            occurred_at=occurred_at,
            kind=_KIND_MAP[args.kind],
            text=args.body,
            linked_record_ids=(args.activity_id,) if args.activity_id else (),
        )
        NormalizationStore(connection).upsert_context_note(note)
        connection.commit()

    return 0


def _list(args: argparse.Namespace) -> int:
    conditions = []
    params: list[str] = []

    if args.kind is not None:
        conditions.append("note_kind = ?")
        params.append(args.kind)

    if args.since_str is not None:
        conditions.append("occurred_at >= ?")
        params.append(args.since_str)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"SELECT record_id, note_kind, note_text, occurred_at FROM context_notes {where} ORDER BY occurred_at DESC"

    with connect_database(args.database) as connection:
        rows = connection.execute(sql, params).fetchall()

    for row in rows:
        print(f"{row['occurred_at'][:10]}  [{row['note_kind']}]  {row['note_text']}  {row['record_id']}")

    return 0


def _delete(args: argparse.Namespace) -> int:
    with connect_database(args.database) as connection:
        row = connection.execute(
            "SELECT record_id FROM context_notes WHERE record_id = ?",
            (args.record_id,),
        ).fetchone()
        if row is None:
            print(f"not found: {args.record_id}", file=sys.stderr)
            return 1
        connection.execute("DELETE FROM records WHERE record_id = ?", (args.record_id,))
        connection.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
