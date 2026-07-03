"""CLI for managing context notes."""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import UTC, datetime

from trainingos.config import AppConfig
from trainingos.domain import (
    ContextNote,
    NoteKind,
    Provenance,
    ProvenanceKind,
    RecordMetadata,
)
from trainingos.normalization import NormalizationStore
from trainingos.storage import connect_database

NOTE_TYPES: list[str] = ["illness", "injury", "travel", "stress", "note"]

NOTE_TYPE_KINDS: dict[str, NoteKind] = {
    "illness": NoteKind.ILLNESS,
    "injury": NoteKind.INJURY,
    "travel": NoteKind.TRAVEL,
    "stress": NoteKind.STRESS,
    "note": NoteKind.GENERAL,
}

# NoteKind DB value → user-facing type name
NOTE_KIND_TYPES: dict[str, str] = {kind.value: name for name, kind in NOTE_TYPE_KINDS.items()}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="trainingos.notes",
        description="Manage context notes",
    )
    sub = parser.add_subparsers(dest="command")

    add_p = sub.add_parser("add", help="Add a context note")
    add_p.add_argument(
        "--type",
        required=True,
        choices=NOTE_TYPES,
        metavar="TYPE",
        help=f"Note type: {', '.join(NOTE_TYPES)}",
    )
    add_p.add_argument("--body", required=True, help="Note text")
    add_p.add_argument("--date", help="Date (YYYY-MM-DD); defaults to today")
    add_p.add_argument("--activity", help="Activity record_id to link")

    list_p = sub.add_parser("list", help="List context notes")
    list_p.add_argument(
        "--type",
        choices=NOTE_TYPES,
        metavar="TYPE",
        help=f"Filter by type: {', '.join(NOTE_TYPES)}",
    )
    list_p.add_argument("--since", help="Include notes on or after this date (YYYY-MM-DD)")

    del_p = sub.add_parser("delete", help="Delete a context note by ID")
    del_p.add_argument("note_id", help="Note record_id to delete")

    return parser.parse_args()


def _parse_iso_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        raise ValueError(f"date must be YYYY-MM-DD, got: {value!r}")


def _cmd_add(args: argparse.Namespace, config: AppConfig) -> None:
    body = args.body
    if not body.strip():
        sys.stderr.write("error: --body must not be blank\n")
        sys.exit(1)

    if args.date is not None:
        try:
            occurred_at = _parse_iso_date(args.date)
        except ValueError as error:
            sys.stderr.write(f"error: {error}\n")
            sys.exit(1)
    else:
        today = datetime.now(UTC).date()
        occurred_at = datetime(today.year, today.month, today.day, tzinfo=UTC)

    with connect_database(config.database_path) as connection:
        if args.activity is not None:
            row = connection.execute(
                "SELECT 1 FROM activities WHERE record_id = ?", (args.activity,)
            ).fetchone()
            if row is None:
                sys.stderr.write(f"error: activity not found: {args.activity!r}\n")
                sys.exit(1)

        now = datetime.now(UTC)
        record_id = str(uuid.uuid4())
        metadata = RecordMetadata(
            record_id=record_id,
            timezone="UTC",
            created_at=now,
            updated_at=now,
            provenance=Provenance(ProvenanceKind.USER_ENTERED),
        )
        linked = (args.activity,) if args.activity else ()
        note = ContextNote(
            metadata=metadata,
            occurred_at=occurred_at,
            kind=NOTE_TYPE_KINDS[args.type],
            text=body,
            linked_record_ids=linked,
        )
        NormalizationStore(connection).upsert_context_note(note)
        connection.commit()

    print(record_id)


def _cmd_list(args: argparse.Namespace, config: AppConfig) -> None:
    filters: list[str] = []
    params: list[object] = []

    if args.type is not None:
        filters.append("note.note_kind = ?")
        params.append(NOTE_TYPE_KINDS[args.type].value)

    if args.since is not None:
        try:
            since_dt = _parse_iso_date(args.since)
        except ValueError as error:
            sys.stderr.write(f"error: {error}\n")
            sys.exit(1)
        filters.append("date(note.occurred_at) >= ?")
        params.append(since_dt.date().isoformat())

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    with connect_database(config.database_path) as connection:
        rows = connection.execute(
            f"""
            SELECT note.record_id, note.occurred_at, note.note_kind, note.note_text
            FROM context_notes AS note
            JOIN records AS record ON record.record_id = note.record_id
            {where}
            ORDER BY note.occurred_at DESC
            """,
            tuple(params),
        ).fetchall()

    for row in rows:
        date_str = row["occurred_at"][:10]
        kind_str = NOTE_KIND_TYPES.get(row["note_kind"], row["note_kind"])
        print(f"{date_str} [{kind_str}] {row['record_id']}: {row['note_text']}")


def _cmd_delete(args: argparse.Namespace, config: AppConfig) -> None:
    note_id = args.note_id
    with connect_database(config.database_path) as connection:
        row = connection.execute(
            "SELECT 1 FROM context_notes WHERE record_id = ?", (note_id,)
        ).fetchone()
        if row is None:
            sys.stderr.write(f"note not found: {note_id!r}\n")
            sys.exit(1)
        connection.execute("DELETE FROM records WHERE record_id = ?", (note_id,))
        connection.commit()


def main() -> None:
    args = _parse_args()

    try:
        config = AppConfig.from_env()
    except ValueError as error:
        sys.stderr.write(f"error: {error}\n")
        sys.exit(2)

    if args.command == "add":
        _cmd_add(args, config)
    elif args.command == "list":
        _cmd_list(args, config)
    elif args.command == "delete":
        _cmd_delete(args, config)
    else:
        sys.stderr.write("error: a subcommand is required (add, list, delete)\n")
        sys.exit(2)


if __name__ == "__main__":
    main()
