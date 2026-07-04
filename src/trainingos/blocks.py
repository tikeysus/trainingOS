"""Training block lifecycle management."""

from __future__ import annotations

import argparse
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

VALID_PHASES = frozenset({"base", "build", "peak", "taper"})


@dataclass(frozen=True, slots=True)
class BlockRow:
    record_id: str
    goal: str
    start_date: date
    race_date: date | None
    ended_at: datetime | None
    current_phase: str | None


def create_block(
    connection: sqlite3.Connection,
    *,
    goal: str,
    start_date: date,
    now: datetime,
    race_date: date | None = None,
    initial_phase: str | None = None,
) -> str:
    if not goal or not goal.strip():
        raise ValueError("goal must not be blank")
    if race_date is not None and race_date < start_date:
        raise ValueError("race_date must not precede start_date")
    if initial_phase is not None and initial_phase not in VALID_PHASES:
        raise ValueError(f"invalid phase {initial_phase!r}; must be one of {sorted(VALID_PHASES)}")

    active = connection.execute(
        "SELECT record_id FROM training_blocks WHERE ended_at IS NULL LIMIT 1"
    ).fetchone()
    if active is not None:
        raise ValueError("an active training block already exists; close it before creating a new one")

    record_id = str(uuid.uuid4())
    now_iso = now.astimezone(UTC).isoformat()

    connection.execute(
        """
        INSERT INTO records (
            record_id, record_type, timezone, created_at, updated_at, provenance_kind
        ) VALUES (?, 'training_block', 'UTC', ?, ?, 'user_entered')
        """,
        (record_id, now_iso, now_iso),
    )
    connection.execute(
        """
        INSERT INTO training_blocks (record_id, goal, start_date, race_date)
        VALUES (?, ?, ?, ?)
        """,
        (record_id, goal.strip(), start_date.isoformat(), race_date.isoformat() if race_date else None),
    )
    if initial_phase is not None:
        connection.execute(
            "INSERT INTO block_phase_transitions (block_id, phase, recorded_at) VALUES (?, ?, ?)",
            (record_id, initial_phase, now_iso),
        )
    connection.commit()
    return record_id


def close_block(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    block_id: str | None = None,
) -> None:
    if block_id is None:
        row = connection.execute(
            "SELECT record_id FROM training_blocks WHERE ended_at IS NULL LIMIT 1"
        ).fetchone()
        if row is None:
            raise ValueError("no active training block to close")
        block_id = row["record_id"]
    else:
        row = connection.execute(
            "SELECT record_id, ended_at FROM training_blocks WHERE record_id = ?",
            (block_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"training block {block_id!r} not found")
        if row["ended_at"] is not None:
            raise ValueError(f"training block {block_id!r} is already closed")

    connection.execute(
        "UPDATE training_blocks SET ended_at = ? WHERE record_id = ?",
        (now.astimezone(UTC).isoformat(), block_id),
    )
    connection.commit()


def list_blocks(connection: sqlite3.Connection) -> list[BlockRow]:
    rows = connection.execute(
        """
        SELECT
            tb.record_id,
            tb.goal,
            tb.start_date,
            tb.race_date,
            tb.ended_at,
            (
                SELECT phase FROM block_phase_transitions
                WHERE block_id = tb.record_id
                ORDER BY rowid DESC LIMIT 1
            ) AS current_phase
        FROM training_blocks AS tb
        ORDER BY tb.start_date DESC
        """
    ).fetchall()
    result: list[BlockRow] = []
    for row in rows:
        ended_at = None
        if row["ended_at"] is not None:
            ended_at = datetime.fromisoformat(row["ended_at"])
            if ended_at.tzinfo is None:
                ended_at = ended_at.replace(tzinfo=UTC)
        result.append(
            BlockRow(
                record_id=row["record_id"],
                goal=row["goal"],
                start_date=date.fromisoformat(row["start_date"]),
                race_date=date.fromisoformat(row["race_date"]) if row["race_date"] else None,
                ended_at=ended_at,
                current_phase=row["current_phase"],
            )
        )
    return result


def set_phase(
    connection: sqlite3.Connection,
    phase: str,
    *,
    now: datetime,
    block_id: str | None = None,
) -> None:
    if phase not in VALID_PHASES:
        raise ValueError(f"invalid phase {phase!r}; must be one of {sorted(VALID_PHASES)}")

    if block_id is None:
        row = connection.execute(
            "SELECT record_id FROM training_blocks WHERE ended_at IS NULL LIMIT 1"
        ).fetchone()
        if row is None:
            raise ValueError("no active training block to set phase on")
        block_id = row["record_id"]
    else:
        row = connection.execute(
            "SELECT record_id, ended_at FROM training_blocks WHERE record_id = ?",
            (block_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"training block {block_id!r} not found")
        if row["ended_at"] is not None:
            raise ValueError(f"training block {block_id!r} is closed; cannot set phase")

    connection.execute(
        "INSERT INTO block_phase_transitions (block_id, phase, recorded_at) VALUES (?, ?, ?)",
        (block_id, phase, now.astimezone(UTC).isoformat()),
    )
    connection.commit()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _open_db(database_path: str) -> sqlite3.Connection:
    from trainingos.storage import apply_migrations, connect_database
    conn = connect_database(Path(database_path))
    apply_migrations(conn)
    return conn


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trainingos.blocks")
    sub = parser.add_subparsers(dest="command")

    # start
    start_p = sub.add_parser("start")
    start_p.add_argument("--goal", required=True)
    start_p.add_argument("--start-date", default=None)
    start_p.add_argument("--race-date", default=None)
    start_p.add_argument("--phase", default=None, choices=sorted(VALID_PHASES))
    start_p.add_argument("--database", required=True)

    # close
    close_p = sub.add_parser("close")
    close_p.add_argument("--block-id", default=None)
    close_p.add_argument("--database", required=True)

    # list
    list_p = sub.add_parser("list")
    list_p.add_argument("--database", required=True)

    # phase set
    phase_p = sub.add_parser("phase")
    phase_sub = phase_p.add_subparsers(dest="phase_command")
    phase_set_p = phase_sub.add_parser("set")
    phase_set_p.add_argument("phase_name")
    phase_set_p.add_argument("--block-id", default=None)
    phase_set_p.add_argument("--database", required=True)

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    try:
        conn = _open_db(args.database)
        now = datetime.now(UTC)

        if args.command == "start":
            start_date = (
                date.fromisoformat(args.start_date) if args.start_date else date.today()
            )
            race_date = date.fromisoformat(args.race_date) if args.race_date else None
            block_id = create_block(
                conn,
                goal=args.goal,
                start_date=start_date,
                race_date=race_date,
                initial_phase=args.phase,
                now=now,
            )
            print(block_id)

        elif args.command == "close":
            close_block(conn, block_id=args.block_id, now=now)

        elif args.command == "list":
            blocks = list_blocks(conn)
            for block in blocks:
                parts = [block.start_date.isoformat(), block.goal]
                if block.current_phase:
                    parts.append(f"[{block.current_phase}]")
                if block.ended_at:
                    parts.append("(closed)")
                print("  ".join(parts))

        elif args.command == "phase":
            if args.phase_command == "set":
                set_phase(conn, args.phase_name, block_id=args.block_id, now=now)
            else:
                parser.error("unknown phase subcommand")
        else:
            parser.print_help()
            return 1

    except (ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(_main())
