"""CLI for managing TrainingOS context notes."""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

from trainingos.domain import (
    ContextNote,
    Provenance,
    ProvenanceKind,
    RecordMetadata,
)
from trainingos.normalization import NormalizationStore
from trainingos.note_types import NOTE_TYPE_MAP
from trainingos.storage import connect_database


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"date must be YYYY-MM-DD, got: {value!r}")


def _cmd_add(args: argparse.Namespace, database_path: Path) -> int:
    if not args.body.strip():
        print("error: body must not be blank", file=sys.stderr)
        return 1

    try:
        note_date = _parse_date(args.date) if args.date else date.today()
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    kind = NOTE_TYPE_MAP[args.type]
    occurred_at = datetime(note_date.year, note_date.month, note_date.day, tzinfo=UTC)
    record_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    with connect_database(database_path) as connection:
        if args.activity:
            row = connection.execute(
                "SELECT record_id FROM records WHERE record_id = ?",
                (args.activity,),
            ).fetchone()
            if row is None:
                print(
                    f"error: activity {args.activity!r} not found",
                    file=sys.stderr,
                )
                return 1

        metadata = RecordMetadata(
            record_id=record_id,
            timezone="UTC",
            created_at=now,
            updated_at=now,
            provenance=Provenance(ProvenanceKind.USER_ENTERED),
        )
        note = ContextNote(
            metadata=metadata,
            occurred_at=occurred_at,
            kind=kind,
            text=args.body,
            linked_record_ids=(args.activity,) if args.activity else (),
        )
        NormalizationStore(connection).upsert_context_note(note)
        connection.commit()

    print(f"Added {kind.value} note {record_id}")
    return 0


def _cmd_list(args: argparse.Namespace, database_path: Path) -> int:
    try:
        since_str = _parse_date(args.since).isoformat() if args.since else None
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    conditions = []
    params: list[object] = []

    if args.type:
        conditions.append("cn.note_kind = ?")
        params.append(NOTE_TYPE_MAP[args.type].value)

    if since_str:
        conditions.append("date(cn.occurred_at) >= ?")
        params.append(since_str)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"""
        SELECT cn.record_id, cn.occurred_at, cn.note_kind, cn.note_text
        FROM context_notes cn
        JOIN records r USING (record_id)
        {where}
        ORDER BY cn.occurred_at DESC
    """

    with connect_database(database_path) as connection:
        rows = connection.execute(sql, params).fetchall()

    for row in rows:
        print(f"{row['occurred_at'][:10]} [{row['note_kind']}] {row['record_id']}: {row['note_text']}")

    return 0


def _cmd_delete(args: argparse.Namespace, database_path: Path) -> int:
    with connect_database(database_path) as connection:
        existing = connection.execute(
            "SELECT record_id FROM records WHERE record_id = ?",
            (args.record_id,),
        ).fetchone()
        if existing is None:
            print(f"error: note {args.record_id!r} not found", file=sys.stderr)
            return 1
        connection.execute(
            "DELETE FROM records WHERE record_id = ?",
            (args.record_id,),
        )
        connection.commit()

    print(f"Deleted note {args.record_id}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True, help="SQLite database path")

    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Add a context note")
    add_parser.add_argument(
        "--type",
        required=True,
        choices=list(NOTE_TYPE_MAP),
        help="Note type",
    )
    add_parser.add_argument("--body", required=True, help="Note text")
    add_parser.add_argument("--date", help="Date (YYYY-MM-DD); defaults to today")
    add_parser.add_argument("--activity", help="Activity record_id to link this note to")

    list_parser = subparsers.add_parser("list", help="List context notes")
    list_parser.add_argument(
        "--type",
        choices=list(NOTE_TYPE_MAP),
        help="Filter by note type",
    )
    list_parser.add_argument("--since", help="Only notes on or after this date (YYYY-MM-DD)")

    delete_parser = subparsers.add_parser("delete", help="Delete a context note by record_id")
    delete_parser.add_argument("record_id", help="Record ID of the note to delete")

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    database_path: Path = args.database

    if args.command == "add":
        return _cmd_add(args, database_path)
    if args.command == "list":
        return _cmd_list(args, database_path)
    if args.command == "delete":
        return _cmd_delete(args, database_path)

    parser.print_help()
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
