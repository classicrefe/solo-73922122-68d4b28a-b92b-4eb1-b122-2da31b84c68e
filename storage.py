"""SQLite persistence and transactional state updates."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Sequence

from accounting import (
    AccountingInputError,
    Building,
    Report,
    combine_reports,
    natural_key,
    parse_rational,
    result_to_dict,
)


MAX_BUILDINGS = 20
MAX_REPORTS = 12


class StorageError(Exception):
    status_code = 500


class ValidationError(StorageError):
    status_code = 400


class LimitExceededError(ValidationError):
    status_code = 409


class NotFoundError(StorageError):
    status_code = 404


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class BuildingInput:
    variance: Any  # Fraction, but imported lazily through parse result
    variance_text: str
    code: str | None = None
    label: str = ""


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS buildings (
    building_no INTEGER PRIMARY KEY CHECK (building_no BETWEEN 1 AND 20),
    code TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    variance_text TEXT NOT NULL,
    variance_num INTEGER NOT NULL,
    variance_den INTEGER NOT NULL CHECK (variance_den > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    mean_text TEXT NOT NULL,
    mean_num INTEGER NOT NULL,
    mean_den INTEGER NOT NULL CHECK (mean_den > 0),
    coverage_count INTEGER NOT NULL CHECK (coverage_count > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_buildings (
    report_id TEXT NOT NULL REFERENCES reports(report_id) ON DELETE CASCADE,
    building_no INTEGER NOT NULL REFERENCES buildings(building_no),
    PRIMARY KEY (report_id, building_no)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_report_buildings_building
    ON report_buildings(building_no);

CREATE TABLE IF NOT EXISTS computation (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    estimate_text TEXT,
    variance_text TEXT,
    conflict_ids_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
"""


class Database:
    """Small database manager.

    ``:memory:`` uses one shared connection for the life of the Database object;
    file databases get a short-lived connection per Flask request.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._memory_connection: sqlite3.Connection | None = None
        if self.path == ":memory:":
            self._memory_connection = self._new_connection(":memory:")
            init_schema(self._memory_connection)

    def _new_connection(self, target: str) -> sqlite3.Connection:
        if target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            target,
            timeout=30.0,
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,  # explicit BEGIN IMMEDIATE/COMMIT
            check_same_thread=target != ":memory:",
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        if target != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def connect(self) -> sqlite3.Connection:
        if self._memory_connection is not None:
            return self._memory_connection
        conn = self._new_connection(self.path)
        init_schema(conn)
        return conn

    def release(self, conn: sqlite3.Connection) -> None:
        if conn is not self._memory_connection:
            conn.close()

    def close(self) -> None:
        if self._memory_connection is not None:
            self._memory_connection.close()
            self._memory_connection = None


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    row = conn.execute("SELECT 1 FROM computation WHERE id = 1").fetchone()
    if row is None:
        empty = combine_reports([], [])
        payload = result_to_dict(empty)
        conn.execute(
            """
            INSERT OR IGNORE INTO computation
                (id, status, payload_json, estimate_text, variance_text,
                 conflict_ids_json, updated_at)
            VALUES (1, ?, ?, NULL, NULL, '[]', ?)
            """,
            (empty.status, json.dumps(payload, ensure_ascii=False), utc_now()),
        )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Commit a state change and its computation together, or roll both back."""

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def row_to_building(row: sqlite3.Row | dict[str, Any]) -> Building:
    return Building(
        building_no=int(row["building_no"]),
        variance=parse_rational(
            f'{row["variance_num"]}/{row["variance_den"]}', field_name="方差"
        ),
        code=row["code"],
        label=row["label"] or "",
    )


def list_building_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM buildings ORDER BY building_no ASC"
    ).fetchall()
    return [dict(row) for row in rows]


def list_report_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    report_rows = conn.execute(
        "SELECT * FROM reports ORDER BY report_id ASC"
    ).fetchall()
    reports: list[dict[str, Any]] = []
    for report_row in report_rows:
        report = dict(report_row)
        coverage_rows = conn.execute(
            """
            SELECT rb.building_no, b.code
            FROM report_buildings rb
            JOIN buildings b ON b.building_no = rb.building_no
            WHERE rb.report_id = ?
            ORDER BY rb.building_no ASC
            """,
            (report["report_id"],),
        ).fetchall()
        report["building_nos"] = [int(row["building_no"]) for row in coverage_rows]
        report["building_codes"] = [row["code"] for row in coverage_rows]
        reports.append(report)
    reports.sort(key=lambda report: natural_key(str(report["report_id"])))
    return reports


def rows_to_domain(
    building_rows: Sequence[dict[str, Any]],
    report_rows: Sequence[dict[str, Any]],
) -> tuple[list[Building], list[Report]]:
    buildings = [row_to_building(row) for row in building_rows]
    reports = [
        Report(
            report_id=str(row["report_id"]),
            building_nos=tuple(int(no) for no in row["building_nos"]),
            mean=parse_rational(
                f'{row["mean_num"]}/{row["mean_den"]}', field_name="报告均值"
            ),
        )
        for row in report_rows
    ]
    return buildings, reports


def persist_computation(conn: sqlite3.Connection) -> dict[str, Any]:
    """Recompute and write the singleton accounting status.

    This function assumes the caller owns the surrounding transaction. Thus a
    report replacement and its new ok/conflict/empty status commit atomically.
    """

    building_rows = list_building_rows(conn)
    report_rows = list_report_rows(conn)
    buildings, reports = rows_to_domain(building_rows, report_rows)
    result = combine_reports(buildings, reports)
    payload = result_to_dict(result)
    estimate_text = None if result.estimate is None else payload["estimate"]
    variance_text = None if result.variance is None else payload["variance"]
    conflict_json = json.dumps(result.conflict_ids, ensure_ascii=False)

    conn.execute(
        """
        INSERT INTO computation
            (id, status, payload_json, estimate_text, variance_text,
             conflict_ids_json, updated_at)
        VALUES (1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            status = excluded.status,
            payload_json = excluded.payload_json,
            estimate_text = excluded.estimate_text,
            variance_text = excluded.variance_text,
            conflict_ids_json = excluded.conflict_ids_json,
            updated_at = excluded.updated_at
        """,
        (
            result.status,
            json.dumps(payload, ensure_ascii=False),
            estimate_text,
            variance_text,
            conflict_json,
            utc_now(),
        ),
    )
    return payload


def get_computation(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT payload_json FROM computation WHERE id = 1").fetchone()
    if row is None:
        # Read-only fallback for unusual manually-created databases.
        return persist_computation(conn)
    return json.loads(row["payload_json"])


def get_state(conn: sqlite3.Connection) -> dict[str, Any]:
    computation_row = conn.execute(
        "SELECT * FROM computation WHERE id = 1"
    ).fetchone()
    computation = (
        json.loads(computation_row["payload_json"])
        if computation_row is not None
        else persist_computation(conn)
    )
    return {
        "buildings": list_building_rows(conn),
        "reports": list_report_rows(conn),
        "computation": computation,
        "limits": {
            "max_buildings": MAX_BUILDINGS,
            "max_reports": MAX_REPORTS,
        },
    }


def _validate_building_inputs(items: Sequence[BuildingInput]) -> None:
    if not items:
        raise ValidationError("请至少录入 1 栋楼的正方差。")
    if len(items) > MAX_BUILDINGS:
        raise ValidationError(f"最多只能录入 {MAX_BUILDINGS} 栋楼。")

    seen_codes: set[str] = set()
    for index, item in enumerate(items, start=1):
        try:
            variance = parse_rational(
                item.variance, positive=True, field_name=f"第 {index} 栋楼方差"
            )
        except AccountingInputError as exc:
            raise ValidationError(str(exc)) from exc
        if variance <= 0:
            raise ValidationError(f"第 {index} 栋楼方差必须为正数。")

        code = (item.code or str(index)).strip()
        if not code:
            code = str(index)
        if len(code) > 40:
            raise ValidationError(f"第 {index} 栋楼编号最长 40 个字符。")
        if code in seen_codes:
            raise ValidationError(f"楼宇编号重复：{code}")
        seen_codes.add(code)


def replace_buildings(
    conn: sqlite3.Connection, items: Sequence[BuildingInput]
) -> dict[str, Any]:
    _validate_building_inputs(items)

    with transaction(conn):
        max_referenced = conn.execute(
            "SELECT MAX(building_no) AS m FROM report_buildings"
        ).fetchone()["m"]
        if max_referenced is not None and int(max_referenced) > len(items):
            raise ValidationError(
                "已有报告引用了将要删除的楼号；请先重报或删除相关报告。"
            )

        now = utc_now()
        # Remove trailing unused rows first. Move retained codes out of the way
        # so reordering custom codes cannot hit a temporary UNIQUE collision.
        conn.execute(
            "DELETE FROM buildings WHERE building_no > ?", (len(items),)
        )
        conn.execute(
            """
            UPDATE buildings
            SET code = '__pending_code_' || building_no || '__'
            WHERE building_no <= ?
            """,
            (len(items),),
        )

        for index, item in enumerate(items, start=1):
            variance = parse_rational(
                item.variance, positive=True, field_name=f"第 {index} 栋楼方差"
            )
            code = (item.code or str(index)).strip() or str(index)
            label = (item.label or "").strip()
            conn.execute(
                """
                INSERT INTO buildings (
                    building_no, code, label, variance_text,
                    variance_num, variance_den, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(building_no) DO UPDATE SET
                    code = excluded.code,
                    label = excluded.label,
                    variance_text = excluded.variance_text,
                    variance_num = excluded.variance_num,
                    variance_den = excluded.variance_den,
                    updated_at = excluded.updated_at
                """,
                (
                    index,
                    code,
                    label,
                    item.variance_text,
                    variance.numerator,
                    variance.denominator,
                    now,
                    now,
                ),
            )

        computation = persist_computation(conn)

    state = get_state(conn)
    state["computation"] = computation
    return state


def resolve_building_tokens(
    conn: sqlite3.Connection, tokens: Sequence[Any]
) -> list[int]:
    rows = list_building_rows(conn)
    by_no = {int(row["building_no"]): int(row["building_no"]) for row in rows}
    by_code: dict[str, int] = {}
    by_folded_code: dict[str, int] = {}
    for row in rows:
        code = str(row["code"])
        no = int(row["building_no"])
        by_code[code] = no
        by_folded_code[code.casefold()] = no

    resolved: set[int] = set()
    for raw_token in tokens:
        if isinstance(raw_token, int) and not isinstance(raw_token, bool):
            token = str(raw_token)
            if raw_token in by_no:
                resolved.add(raw_token)
                continue
        else:
            token = str(raw_token).strip()
            if not token:
                continue

        if token in by_code:
            resolved.add(by_code[token])
            continue
        if token.casefold() in by_folded_code:
            resolved.add(by_folded_code[token.casefold()])
            continue

        integer_match = None
        try:
            # "01" should refer to building 1.
            integer_match = int(token)
        except (TypeError, ValueError):
            pass
        if integer_match is not None and integer_match in by_no:
            resolved.add(integer_match)
            continue

        raise ValidationError(f"覆盖楼宇中存在未录入的楼号：{token}")

    return sorted(resolved)


def upsert_report(
    conn: sqlite3.Connection,
    report_id: str,
    mean: Any,
    mean_text: str,
    building_tokens: Sequence[Any],
) -> dict[str, Any]:
    report_id = (report_id or "").strip()
    if not report_id:
        raise ValidationError("报告编号不能为空。")
    if len(report_id) > 64:
        raise ValidationError("报告编号最长 64 个字符。")
    if any(ch in report_id for ch in "\r\n\t"):
        raise ValidationError("报告编号不能包含换行或制表符。")

    try:
        parsed_mean = parse_rational(mean, field_name="报告等权均值")
    except AccountingInputError as exc:
        raise ValidationError(str(exc)) from exc

    with transaction(conn):
        building_nos = resolve_building_tokens(conn, building_tokens)
        if not building_nos:
            raise ValidationError("报告至少须覆盖一栋楼。")

        old = conn.execute(
            "SELECT created_at FROM reports WHERE report_id = ?", (report_id,)
        ).fetchone()
        is_new = old is None
        if is_new:
            count = conn.execute("SELECT COUNT(*) AS c FROM reports").fetchone()["c"]
            if int(count) >= MAX_REPORTS:
                raise LimitExceededError(
                    f"最多只能录入 {MAX_REPORTS} 份报告；"
                    "请重报已有编号或删除旧报告。"
                )

        now = utc_now()
        created_at = now if old is None else old["created_at"]
        conn.execute(
            """
            INSERT INTO reports (
                report_id, mean_text, mean_num, mean_den,
                coverage_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(report_id) DO UPDATE SET
                mean_text = excluded.mean_text,
                mean_num = excluded.mean_num,
                mean_den = excluded.mean_den,
                coverage_count = excluded.coverage_count,
                updated_at = excluded.updated_at
            """,
            (
                report_id,
                mean_text,
                parsed_mean.numerator,
                parsed_mean.denominator,
                len(building_nos),
                created_at,
                now,
            ),
        )
        conn.execute(
            "DELETE FROM report_buildings WHERE report_id = ?", (report_id,)
        )
        conn.executemany(
            "INSERT INTO report_buildings(report_id, building_no) VALUES (?, ?)",
            [(report_id, no) for no in building_nos],
        )

        computation = persist_computation(conn)

    state = get_state(conn)
    state["computation"] = computation
    state["saved_report_id"] = report_id
    state["replaced"] = not is_new
    return state


def delete_report(conn: sqlite3.Connection, report_id: str) -> dict[str, Any]:
    report_id = (report_id or "").strip()
    with transaction(conn):
        conn.execute("DELETE FROM reports WHERE report_id = ?", (report_id,))
        computation = persist_computation(conn)
    state = get_state(conn)
    state["computation"] = computation
    return state


def reset_all(conn: sqlite3.Connection) -> dict[str, Any]:
    with transaction(conn):
        conn.execute("DELETE FROM report_buildings")
        conn.execute("DELETE FROM reports")
        conn.execute("DELETE FROM buildings")
        computation = persist_computation(conn)
    state = get_state(conn)
    state["computation"] = computation
    return state
