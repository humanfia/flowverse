"""Transactional PR, evaluation, knowledge, and telemetry storage."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
from types import TracebackType
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

SCHEMA_VERSION = 2
PR_STATES = {"draft", "ready", "reviewing", "merged", "rejected"}
FACT_STATES = {"proposed", "verified", "conflicted", "stale", "revoked"}
EXPERIMENT_OUTCOMES = {
    "active",
    "improved",
    "neutral",
    "regressed",
    "invalid",
    "exhausted",
}


def re_split_words(value: str) -> list[str]:
    """Normalize free-form intent text for deterministic lexical retrieval."""
    return re.findall(r"[\w.-]+", value.casefold())


class _ClosingConnection(sqlite3.Connection):
    """Commit or roll back a context-managed transaction, then close its handle."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def timestamp() -> str:
    """Return a stable UTC timestamp without depending on flow internals."""
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def canonical_json(value: object) -> str:
    """Serialize values for stable IDs and durable JSON columns."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_id(prefix: str, value: object) -> str:
    """Return one readable content-addressed identifier."""
    digest = hashlib.sha256(canonical_json(value).encode()).hexdigest()
    return f"{prefix}{digest[:24]}"


def append_event(path: Path, value: object) -> None:
    """Append one compact event using a single O_APPEND write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (canonical_json(value) + "\n").encode()
    if len(encoded) > 256 * 1024:
        raise ValueError("coordination event exceeds 256 KiB")
    if path.is_symlink():
        raise RuntimeError(f"refusing to append through a linked event file: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class CoordinationStore:
    """SQLite is the live truth; JSONL is the immutable audit projection."""

    def __init__(self, database: Path, events: Path) -> None:
        self.database = database
        self.events = events

    def connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        """Open one short transaction or query connection."""
        self.database.parent.mkdir(parents=True, exist_ok=True)
        if self.database.is_symlink():
            raise RuntimeError("coordination database cannot be a symbolic link")
        if readonly:
            connection = sqlite3.connect(
                f"file:{self.database}?mode=ro",
                uri=True,
                timeout=30,
                factory=_ClosingConnection,
            )
        else:
            connection = sqlite3.connect(
                self.database, timeout=30, factory=_ClosingConnection
            )
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(
        self,
        *,
        run_id: str,
        git_pr_enabled: bool,
        global_knowledge_enabled: bool,
        experiment_memory_enabled: bool = False,
        lanes: Sequence[str] = ("lane-1", "lane-2", "lane-3"),
        allowed_paths: Sequence[str],
        trusted_evaluator_command: Sequence[str] = (),
    ) -> None:
        """Create the complete run-local schema once and validate resume identity."""
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pull_requests (
                    id TEXT PRIMARY KEY,
                    lane TEXT NOT NULL,
                    branch TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    hypothesis TEXT NOT NULL,
                    status TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    base_sha TEXT NOT NULL,
                    provisional_receipt_id TEXT,
                    ready_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    rejection_reason TEXT,
                    merge_sha TEXT
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    id TEXT PRIMARY KEY,
                    lane TEXT NOT NULL,
                    role TEXT NOT NULL,
                    pr_id TEXT,
                    kind TEXT NOT NULL,
                    commit_sha TEXT NOT NULL,
                    tree_sha TEXT NOT NULL,
                    command_json TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    exit_code INTEGER NOT NULL,
                    stdout_path TEXT NOT NULL,
                    stderr_path TEXT NOT NULL,
                    stdout_sha256 TEXT NOT NULL,
                    stderr_sha256 TEXT NOT NULL,
                    environment_sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS official_ledger (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    pr_id TEXT NOT NULL UNIQUE,
                    prior_main_sha TEXT NOT NULL,
                    merge_sha TEXT NOT NULL UNIQUE,
                    receipt_ids_json TEXT NOT NULL,
                    comparison_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS report_review_queue (
                    report_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS facts (
                    id TEXT PRIMARY KEY,
                    statement TEXT NOT NULL,
                    importance TEXT NOT NULL,
                    proof TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    dependencies_json TEXT NOT NULL,
                    contradicts_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fact_edges (
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    PRIMARY KEY (source_id, target_id, kind)
                );
                CREATE TABLE IF NOT EXISTS experiences (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    method TEXT NOT NULL,
                    why_it_worked TEXT NOT NULL,
                    limitations_json TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    pr_id TEXT,
                    commit_sha TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telemetry (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    lane TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS experiments (
                    id TEXT PRIMARY KEY,
                    lane TEXT NOT NULL,
                    family TEXT NOT NULL,
                    target TEXT NOT NULL,
                    base_ref TEXT NOT NULL,
                    hypothesis TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    best_score REAL,
                    coverage_count INTEGER NOT NULL,
                    evidence_json TEXT NOT NULL,
                    limitations_json TEXT NOT NULL,
                    reopen_if_json TEXT NOT NULL,
                    next_frontier TEXT NOT NULL,
                    report_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS experiments_scope
                    ON experiments(family, target, base_ref, outcome);
                """
            )
            expected = {
                "schema_version": SCHEMA_VERSION,
                "run_id": run_id,
                "git_pr_enabled": git_pr_enabled,
                "global_knowledge_enabled": global_knowledge_enabled,
                "experiment_memory_enabled": experiment_memory_enabled,
                "lanes": list(lanes),
                "allowed_paths": list(allowed_paths),
                "trusted_evaluator_command": list(trusted_evaluator_command),
            }
            held = {
                row["key"]: json.loads(row["value_json"])
                for row in connection.execute("SELECT key, value_json FROM meta")
            }
            if held:
                if any(held.get(key) != value for key, value in expected.items()):
                    raise ValueError(
                        "coordination database is incompatible with this run"
                    )
            else:
                connection.executemany(
                    "INSERT INTO meta(key, value_json) VALUES (?, ?)",
                    [(key, canonical_json(value)) for key, value in expected.items()],
                )
        if self.events.is_symlink():
            raise RuntimeError("coordination event stream cannot be a symbolic link")
        if not self.events.exists():
            self.events.touch()

    def meta(self, key: str) -> object:
        """Read one frozen run setting."""
        with self.connect(readonly=True) as connection:
            row = connection.execute(
                "SELECT value_json FROM meta WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            raise KeyError(key)
        return json.loads(row["value_json"])

    def _event(
        self,
        connection: sqlite3.Connection,
        kind: str,
        *,
        lane: str | None,
        payload: dict[str, object],
    ) -> dict[str, object]:
        event = {"at": timestamp(), "kind": kind, "lane": lane, **payload}
        connection.execute(
            "INSERT INTO telemetry(at, kind, lane, payload_json) VALUES (?, ?, ?, ?)",
            (event["at"], kind, lane, canonical_json(payload)),
        )
        return event

    def record_telemetry(
        self, kind: str, payload: dict[str, object], *, lane: str | None = None
    ) -> None:
        """Record one event in both durable projections."""
        with self.connect() as connection:
            event = self._event(connection, kind, lane=lane, payload=payload)
        append_event(self.events, event)

    def create_pr(
        self,
        *,
        lane: str,
        branch: str,
        title: str,
        hypothesis: str,
        head_sha: str,
        base_sha: str,
    ) -> str:
        """Open one draft PR at the pushed branch head."""
        stamp = timestamp()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = int(
                connection.execute("SELECT COUNT(*) FROM pull_requests").fetchone()[0]
            )
            pr_id = f"PR{count + 1:06d}"
            connection.execute(
                """
                INSERT INTO pull_requests(
                    id, lane, branch, title, hypothesis, status, head_sha, base_sha,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?)
                """,
                (
                    pr_id,
                    lane,
                    branch,
                    title,
                    hypothesis,
                    head_sha,
                    base_sha,
                    stamp,
                    stamp,
                ),
            )
            event = self._event(
                connection,
                "pr_opened",
                lane=lane,
                payload={"pr_id": pr_id, "branch": branch, "head_sha": head_sha},
            )
        append_event(self.events, event)
        return pr_id

    def pr(self, pr_id: str) -> dict[str, object]:
        """Return one PR record."""
        with self.connect(readonly=True) as connection:
            row = connection.execute(
                "SELECT * FROM pull_requests WHERE id = ?", (pr_id,)
            ).fetchone()
        if row is None:
            raise KeyError(pr_id)
        return dict(row)

    def prs(self, *, status: str | None = None) -> list[dict[str, object]]:
        """List PRs in creation order."""
        query = "SELECT * FROM pull_requests"
        values: tuple[object, ...] = ()
        if status is not None:
            if status not in PR_STATES:
                raise ValueError(f"invalid PR status: {status}")
            query += " WHERE status = ?"
            values = (status,)
        query += " ORDER BY created_at, id"
        with self.connect(readonly=True) as connection:
            return [dict(row) for row in connection.execute(query, values)]

    def add_receipt(self, receipt: dict[str, object]) -> str:
        """Insert one immutable evaluation receipt."""
        receipt_id = cast("str", receipt["id"])
        fields = (
            "id",
            "lane",
            "role",
            "pr_id",
            "kind",
            "commit_sha",
            "tree_sha",
            "command_json",
            "cwd",
            "started_at",
            "finished_at",
            "exit_code",
            "stdout_path",
            "stderr_path",
            "stdout_sha256",
            "stderr_sha256",
            "environment_sha256",
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO receipts(
                    id, lane, role, pr_id, kind, commit_sha, tree_sha, command_json,
                    cwd, started_at, finished_at, exit_code, stdout_path, stderr_path,
                    stdout_sha256, stderr_sha256, environment_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(receipt.get(field) for field in fields),
            )
            event = self._event(
                connection,
                "evaluation_recorded",
                lane=cast("str", receipt["lane"]),
                payload={
                    "receipt_id": receipt_id,
                    "commit_sha": receipt["commit_sha"],
                    "kind": receipt["kind"],
                    "exit_code": receipt["exit_code"],
                    "pr_id": receipt.get("pr_id"),
                },
            )
        append_event(self.events, event)
        return receipt_id

    def receipts_after(self, rowid: int) -> tuple[list[dict[str, object]], int]:
        """Read newly inserted receipts for single-writer notification projection."""
        with self.connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT rowid AS receipt_rowid, * FROM receipts WHERE rowid>? ORDER BY rowid",
                (rowid,),
            ).fetchall()
        records = [dict(row) for row in rows]
        return records, max(
            [rowid, *(int(record["receipt_rowid"]) for record in records)]
        )

    def receipt(self, receipt_id: str) -> dict[str, object]:
        """Return one immutable evaluation receipt."""
        with self.connect(readonly=True) as connection:
            row = connection.execute(
                "SELECT * FROM receipts WHERE id = ?", (receipt_id,)
            ).fetchone()
        if row is None:
            raise KeyError(receipt_id)
        return dict(row)

    def ready_pr(
        self, *, pr_id: str, lane: str, head_sha: str, receipt_id: str
    ) -> None:
        """Freeze a draft head, superseding an older queued head from this lane."""
        stamp = timestamp()
        events: list[dict[str, object]] = []
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM pull_requests WHERE id = ?", (pr_id,)
            ).fetchone()
            if row is None or row["lane"] != lane or row["status"] != "draft":
                raise ValueError("only the owning lane may ready its draft PR")
            active = connection.execute(
                """
                SELECT id FROM pull_requests
                WHERE lane = ? AND status='reviewing' AND id != ?
                """,
                (lane, pr_id),
            ).fetchone()
            if active is not None:
                raise ValueError(f"{lane} already has active PR {active['id']}")
            receipt = connection.execute(
                "SELECT * FROM receipts WHERE id = ?", (receipt_id,)
            ).fetchone()
            if (
                receipt is None
                or receipt["lane"] != lane
                or receipt["commit_sha"] != head_sha
                or receipt["exit_code"] != 0
                or receipt["kind"] != "provisional"
            ):
                raise ValueError(
                    "ready requires a successful head-bound provisional receipt"
                )
            superseded = connection.execute(
                """
                SELECT id FROM pull_requests
                WHERE lane=? AND status='ready' AND id!=?
                ORDER BY ready_at, id
                """,
                (lane, pr_id),
            ).fetchall()
            for old in superseded:
                reason = f"superseded by newer candidate {pr_id}"
                connection.execute(
                    """
                    UPDATE pull_requests
                    SET status='rejected', rejection_reason=?, updated_at=? WHERE id=?
                    """,
                    (reason, stamp, old["id"]),
                )
                events.append(
                    self._event(
                        connection,
                        "pr_superseded",
                        lane=lane,
                        payload={"pr_id": old["id"], "replacement_pr_id": pr_id},
                    )
                )
            connection.execute(
                """
                UPDATE pull_requests
                SET status='ready', head_sha=?, provisional_receipt_id=?, ready_at=?,
                    updated_at=?
                WHERE id=?
                """,
                (head_sha, receipt_id, stamp, stamp, pr_id),
            )
            events.append(
                self._event(
                    connection,
                    "pr_ready",
                    lane=lane,
                    payload={
                        "pr_id": pr_id,
                        "head_sha": head_sha,
                        "receipt_id": receipt_id,
                    },
                )
            )
        for event in events:
            append_event(self.events, event)

    def active_review(self) -> dict[str, object] | None:
        """Return the unique active FIFO review, if any."""
        with self.connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT * FROM pull_requests WHERE status='reviewing' ORDER BY ready_at, id"
            ).fetchall()
        if len(rows) > 1:
            raise RuntimeError("more than one PR is under review")
        return dict(rows[0]) if rows else None

    def activate_next_pr(self) -> dict[str, object] | None:
        """Move the oldest ready PR into review when no review is active."""
        with self.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM pull_requests WHERE status='reviewing'"
            ).fetchone():
                return None
            row = connection.execute(
                """
                SELECT * FROM pull_requests WHERE status='ready'
                ORDER BY ready_at, id LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            stamp = timestamp()
            connection.execute(
                "UPDATE pull_requests SET status='reviewing', updated_at=? WHERE id=?",
                (stamp, row["id"]),
            )
            event = self._event(
                connection,
                "pr_review_started",
                lane=row["lane"],
                payload={"pr_id": row["id"], "head_sha": row["head_sha"]},
            )
            updated = dict(row)
            updated.update(status="reviewing", updated_at=stamp)
        append_event(self.events, event)
        return updated

    def activate_pr(self, pr_id: str) -> dict[str, object] | None:
        """Atomically activate one runtime-selected ready PR."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM pull_requests WHERE status='reviewing'"
            ).fetchone():
                return None
            row = connection.execute(
                "SELECT * FROM pull_requests WHERE id=? AND status='ready'", (pr_id,)
            ).fetchone()
            if row is None:
                return None
            stamp = timestamp()
            connection.execute(
                "UPDATE pull_requests SET status='reviewing', updated_at=? WHERE id=?",
                (stamp, pr_id),
            )
            event = self._event(
                connection,
                "pr_fast_path_started",
                lane=row["lane"],
                payload={"pr_id": pr_id, "head_sha": row["head_sha"]},
            )
            updated = dict(row)
            updated.update(status="reviewing", updated_at=stamp)
        append_event(self.events, event)
        return updated

    def reject_pr(self, *, pr_id: str, reason: str) -> dict[str, object]:
        """Close the active PR and release the author's ready slot."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM pull_requests WHERE id = ?", (pr_id,)
            ).fetchone()
            if row is None or row["status"] != "reviewing":
                raise ValueError("only the active review may be rejected")
            stamp = timestamp()
            connection.execute(
                """
                UPDATE pull_requests SET status='rejected', rejection_reason=?, updated_at=?
                WHERE id=?
                """,
                (reason, stamp, pr_id),
            )
            event = self._event(
                connection,
                "pr_rejected",
                lane=row["lane"],
                payload={"pr_id": pr_id, "reason": reason},
            )
            updated = dict(row)
            updated.update(status="rejected", rejection_reason=reason, updated_at=stamp)
        append_event(self.events, event)
        return updated

    def staging_receipts(self, pr_id: str, commit_sha: str) -> list[dict[str, object]]:
        """Return successful staging receipts for one exact review commit."""
        with self.connect(readonly=True) as connection:
            rows = connection.execute(
                """
                SELECT * FROM receipts
                WHERE pr_id=? AND commit_sha=? AND kind='staging' AND exit_code=0
                ORDER BY finished_at, id
                """,
                (pr_id, commit_sha),
            ).fetchall()
        return [dict(row) for row in rows]

    def qualifying_receipts(
        self, pr_id: str, commit_sha: str
    ) -> list[dict[str, object]]:
        """Return staging evidence or the frozen head's trusted provisional receipt."""
        staging = self.staging_receipts(pr_id, commit_sha)
        if staging:
            return staging
        with self.connect(readonly=True) as connection:
            row = connection.execute(
                """
                SELECT receipts.* FROM pull_requests
                JOIN receipts ON receipts.id=pull_requests.provisional_receipt_id
                WHERE pull_requests.id=? AND receipts.kind='provisional'
                    AND receipts.exit_code=0
                    AND receipts.commit_sha=pull_requests.head_sha
                """,
                (pr_id,),
            ).fetchone()
        return [dict(row)] if row is not None else []

    def finalize_merge(
        self,
        *,
        pr_id: str,
        prior_main_sha: str,
        merge_sha: str,
        comparison: dict[str, object],
    ) -> dict[str, object]:
        """Close one observed main merge and append its official ledger row."""
        receipts = self.qualifying_receipts(pr_id, merge_sha)
        if not receipts:
            raise ValueError("official merge has no qualifying evaluation receipt")
        stamp = timestamp()
        receipt_ids = [cast("str", receipt["id"]) for receipt in receipts]
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM pull_requests WHERE id=?", (pr_id,)
            ).fetchone()
            if row is None or row["status"] not in {"reviewing", "merged"}:
                raise ValueError("official merge does not match the active PR")
            existing = connection.execute(
                "SELECT 1 FROM official_ledger WHERE pr_id=? AND merge_sha=?",
                (pr_id, merge_sha),
            ).fetchone()
            if existing is not None:
                return dict(row)
            connection.execute(
                """
                UPDATE pull_requests SET status='merged', merge_sha=?, updated_at=? WHERE id=?
                """,
                (merge_sha, stamp, pr_id),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO official_ledger(
                    pr_id, prior_main_sha, merge_sha, receipt_ids_json,
                    comparison_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    pr_id,
                    prior_main_sha,
                    merge_sha,
                    canonical_json(receipt_ids),
                    canonical_json(comparison),
                    stamp,
                ),
            )
            event = self._event(
                connection,
                "pr_merged",
                lane=row["lane"],
                payload={
                    "pr_id": pr_id,
                    "prior_main_sha": prior_main_sha,
                    "merge_sha": merge_sha,
                    "receipt_ids": receipt_ids,
                },
            )
            updated = dict(row)
            updated.update(status="merged", merge_sha=merge_sha, updated_at=stamp)
        append_event(self.events, event)
        return updated

    def enqueue_report(self, report: dict[str, object]) -> None:
        """Retain a legacy immutable report-queue record for state compatibility."""
        report_id = report.get("report_id")
        if not isinstance(report_id, str):
            raise TypeError("the report queue requires a report_id")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO report_review_queue(
                    report_id, payload_json, status, created_at
                ) VALUES (?, ?, 'pending', ?)
                """,
                (report_id, canonical_json(report), timestamp()),
            )

    def pending_reports(self, limit: int = 6) -> list[dict[str, object]]:
        """Return pending records from the legacy report queue."""
        with self.connect(readonly=True) as connection:
            rows = connection.execute(
                """
                SELECT report_id, payload_json FROM report_review_queue
                WHERE status='pending' ORDER BY created_at, report_id LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {"report_id": row["report_id"], "report": json.loads(row["payload_json"])}
            for row in rows
        ]

    def mark_reports_reviewed(self, report_ids: Sequence[str]) -> None:
        """Acknowledge an exact batch from the legacy report queue."""
        if not report_ids:
            return
        with self.connect() as connection:
            connection.executemany(
                """
                UPDATE report_review_queue SET status='reviewed', reviewed_at=?
                WHERE report_id=? AND status='pending'
                """,
                [(timestamp(), report_id) for report_id in report_ids],
            )

    def add_fact(self, proposal: dict[str, object]) -> dict[str, object]:
        """Insert one evidence-backed atomic fact and dependency edges."""
        identity = {
            key: proposal.get(key)
            for key in ("statement", "proof", "scope", "evidence", "dependencies")
        }
        fact_id = content_id("F", identity)
        dependencies = list(cast("list[str]", proposal.get("dependencies", [])))
        contradicts = list(cast("list[str]", proposal.get("contradicts", [])))
        status = "conflicted" if contradicts else "verified"
        stamp = timestamp()
        with self.connect() as connection:
            known: dict[str, str] = {}
            for reference in dict.fromkeys([*dependencies, *contradicts]):
                row = connection.execute(
                    "SELECT id, status FROM facts WHERE id=?", (reference,)
                ).fetchone()
                if row is not None:
                    known[row["id"]] = row["status"]
            missing = [
                fact_id
                for fact_id in [*dependencies, *contradicts]
                if fact_id not in known
            ]
            if missing:
                raise ValueError(f"fact proposal references unknown facts: {missing}")
            inactive_dependencies = [
                dependency
                for dependency in dependencies
                if known.get(dependency) != "verified"
            ]
            if inactive_dependencies:
                raise ValueError(
                    "fact proposal depends on non-verified facts: "
                    f"{inactive_dependencies}"
                )
            if fact_id in dependencies or fact_id in contradicts:
                raise ValueError("a fact cannot depend on or contradict itself")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO facts(
                    id, statement, importance, proof, scope_json, evidence_json,
                    dependencies_json, contradicts_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fact_id,
                    proposal["statement"],
                    proposal["importance"],
                    proposal["proof"],
                    canonical_json(proposal.get("scope", [])),
                    canonical_json(proposal.get("evidence", [])),
                    canonical_json(dependencies),
                    canonical_json(contradicts),
                    status,
                    stamp,
                    stamp,
                ),
            )
            created = cursor.rowcount == 1
            for dependency in dependencies:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO fact_edges(source_id, target_id, kind)
                    VALUES (?, ?, 'depends_on')
                    """,
                    (fact_id, dependency),
                )
            if contradicts:
                connection.execute(
                    """
                    UPDATE facts SET status='conflicted', updated_at=?
                    WHERE id=? AND status!='revoked'
                    """,
                    (stamp, fact_id),
                )
            for contradicted in contradicts:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO fact_edges(source_id, target_id, kind)
                    VALUES (?, ?, 'contradicts')
                    """,
                    (fact_id, contradicted),
                )
                pending = [(contradicted, True)]
                visited: set[str] = set()
                while pending:
                    current, direct = pending.pop()
                    if current in visited:
                        continue
                    visited.add(current)
                    connection.execute(
                        """
                        UPDATE facts SET status=?, updated_at=?
                        WHERE id=? AND status!='revoked'
                        """,
                        ("conflicted" if direct else "stale", stamp, current),
                    )
                    pending.extend(
                        (row["source_id"], False)
                        for row in connection.execute(
                            """
                            SELECT source_id FROM fact_edges
                            WHERE target_id=? AND kind='depends_on'
                            """,
                            (current,),
                        )
                    )
            if created or contradicts:
                event = self._event(
                    connection,
                    "fact_recorded" if created else "fact_conflict_recorded",
                    lane=None,
                    payload={"fact_id": fact_id, "status": status},
                )
        if not created and not contradicts:
            return self.fact(fact_id, include_inactive=True)
        append_event(self.events, event)
        return self.fact(fact_id, include_inactive=True)

    def mark_fact_stale(self, fact_id: str) -> set[str]:
        """Quarantine one fact and its currently visible dependents."""
        affected: set[str] = set()
        pending = [fact_id]
        stamp = timestamp()
        with self.connect() as connection:
            while pending:
                current = pending.pop()
                if current in affected:
                    continue
                row = connection.execute(
                    "SELECT id, status FROM facts WHERE id=?", (current,)
                ).fetchone()
                if row is None:
                    raise KeyError(current)
                if row["status"] == "revoked":
                    raise ValueError(f"revoked fact cannot be marked stale: {current}")
                affected.add(current)
                connection.execute(
                    "UPDATE facts SET status='stale', updated_at=? WHERE id=?",
                    (stamp, current),
                )
                pending.extend(
                    row["source_id"]
                    for row in connection.execute(
                        """
                        SELECT source_id FROM fact_edges
                        WHERE target_id=? AND kind='depends_on'
                        """,
                        (current,),
                    )
                )
            event = self._event(
                connection,
                "facts_marked_stale",
                lane=None,
                payload={"fact_ids": sorted(affected)},
            )
        append_event(self.events, event)
        return affected

    def fact(
        self, fact_id: str, *, include_inactive: bool = False
    ) -> dict[str, object]:
        """Read one fact, hiding non-verified truth by default."""
        query = "SELECT * FROM facts WHERE id=?"
        values: tuple[object, ...] = (fact_id,)
        if not include_inactive:
            query += " AND status='verified'"
        with self.connect(readonly=True) as connection:
            row = connection.execute(query, values).fetchone()
        if row is None:
            raise KeyError(fact_id)
        return self._decode_knowledge_row(dict(row))

    def revoke_fact(self, fact_id: str) -> set[str]:
        """Revoke one fact and every verified dependent transitively."""
        revoked: set[str] = set()
        pending = [fact_id]
        stamp = timestamp()
        with self.connect() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM facts WHERE id=?", (fact_id,)
                ).fetchone()
                is None
            ):
                raise KeyError(fact_id)
            while pending:
                current = pending.pop()
                if current in revoked:
                    continue
                revoked.add(current)
                connection.execute(
                    "UPDATE facts SET status='revoked', updated_at=? WHERE id=?",
                    (stamp, current),
                )
                pending.extend(
                    row["source_id"]
                    for row in connection.execute(
                        """
                        SELECT source_id FROM fact_edges
                        WHERE target_id=? AND kind='depends_on'
                        """,
                        (current,),
                    )
                )
            event = self._event(
                connection,
                "facts_revoked",
                lane=None,
                payload={"fact_ids": sorted(revoked)},
            )
        append_event(self.events, event)
        return revoked

    def add_experience(
        self,
        *,
        title: str,
        summary: str,
        scope: Sequence[str],
        evidence: Sequence[str],
        pr_id: str | None,
        commit_sha: str | None,
    ) -> dict[str, object]:
        """Publish an immediately visible, machine-evidenced success skeleton."""
        identity = {
            "title": title,
            "summary": summary,
            "scope": list(scope),
            "evidence": list(evidence),
            "pr_id": pr_id,
            "commit_sha": commit_sha,
        }
        experience_id = content_id("E", identity)
        stamp = timestamp()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO experiences(
                    id, title, summary, method, why_it_worked, limitations_json,
                    scope_json, evidence_json, pr_id, commit_sha, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, '', '', '[]', ?, ?, ?, ?, 'accepted', ?, ?)
                """,
                (
                    experience_id,
                    title,
                    summary,
                    canonical_json(list(scope)),
                    canonical_json(list(evidence)),
                    pr_id,
                    commit_sha,
                    stamp,
                    stamp,
                ),
            )
            created = cursor.rowcount == 1
            if created:
                event = self._event(
                    connection,
                    "experience_recorded",
                    lane=None,
                    payload={"experience_id": experience_id, "pr_id": pr_id},
                )
        if not created:
            return self.experience(experience_id)
        append_event(self.events, event)
        return self.experience(experience_id)

    def compact_experiences(self, *, limit: int = 12) -> list[str]:
        """Keep only the newest compact success cards active in the hot index."""
        if limit < 1:
            raise ValueError("experience limit must be positive")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM experiences WHERE status='accepted'
                ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET ?
                """,
                (limit,),
            ).fetchall()
            archived = [cast("str", row["id"]) for row in rows]
            if archived:
                connection.executemany(
                    "UPDATE experiences SET status='archived', updated_at=? WHERE id=?",
                    [(timestamp(), experience_id) for experience_id in archived],
                )
                event = self._event(
                    connection,
                    "knowledge_digest_compacted",
                    lane=None,
                    payload={"archived_experience_ids": archived, "limit": limit},
                )
        if archived:
            append_event(self.events, event)
        return archived

    def enrich_experience(
        self,
        experience_id: str,
        *,
        method: str,
        why_it_worked: str,
        limitations: Sequence[str],
    ) -> None:
        """Apply semantic detail without reclassifying experience as fact."""
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE experiences SET method=?, why_it_worked=?, limitations_json=?,
                    updated_at=? WHERE id=? AND status='accepted'
                """,
                (
                    method,
                    why_it_worked,
                    canonical_json(list(limitations)),
                    timestamp(),
                    experience_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(experience_id)

    def experience(self, experience_id: str) -> dict[str, object]:
        """Read one accepted experience."""
        with self.connect(readonly=True) as connection:
            row = connection.execute(
                "SELECT * FROM experiences WHERE id=? AND status='accepted'",
                (experience_id,),
            ).fetchone()
        if row is None:
            raise KeyError(experience_id)
        return self._decode_knowledge_row(dict(row))

    def pending_experiences(self, limit: int = 20) -> list[dict[str, object]]:
        """Return accepted success skeletons awaiting semantic enrichment."""
        with self.connect(readonly=True) as connection:
            rows = connection.execute(
                """
                SELECT * FROM experiences
                WHERE status='accepted' AND (method='' OR why_it_worked='')
                ORDER BY created_at, id LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._decode_knowledge_row(dict(row)) for row in rows]

    @staticmethod
    def _decode_knowledge_row(row: dict[str, object]) -> dict[str, object]:
        for field in (
            "scope_json",
            "evidence_json",
            "dependencies_json",
            "contradicts_json",
            "limitations_json",
        ):
            if field in row:
                row[field.removesuffix("_json")] = json.loads(
                    cast("str", row.pop(field))
                )
        return row

    def search_knowledge(
        self, query: str, *, limit: int = 20
    ) -> list[dict[str, object]]:
        """Deterministically search visible facts and experiences."""
        terms = [term for term in query.casefold().split() if term]
        with self.connect(readonly=True) as connection:
            facts = [
                self._decode_knowledge_row(dict(row))
                for row in connection.execute(
                    "SELECT * FROM facts WHERE status='verified' ORDER BY created_at, id"
                )
            ]
            experiences = [
                self._decode_knowledge_row(dict(row))
                for row in connection.execute(
                    """
                    SELECT * FROM experiences WHERE status='accepted'
                    ORDER BY created_at, id
                    """
                )
            ]
        ranked: list[tuple[int, str, dict[str, object]]] = []
        for kind, records in (("fact", facts), ("experience", experiences)):
            for record in records:
                haystack = canonical_json(record).casefold()
                score = sum(haystack.count(term) for term in terms) if terms else 1
                if score:
                    ranked.append((score, kind, {"kind": kind, **record}))
        if terms:
            ranked.sort(key=lambda item: (-item[0], cast("str", item[2]["id"])))
        else:
            ranked.sort(
                key=lambda item: (
                    cast("str", item[2]["created_at"]),
                    cast("str", item[2]["id"]),
                ),
                reverse=True,
            )
        return [record for _score, _kind, record in ranked[:limit]]

    def knowledge_index(self) -> dict[str, object]:
        """Return the complete visible run-local knowledge view."""
        visible = self.search_knowledge("", limit=10_000)
        return {
            "version": timestamp(),
            "facts": [item for item in visible if item["kind"] == "fact"],
            "experiences": [item for item in visible if item["kind"] == "experience"],
        }

    @staticmethod
    def _decode_experiment(row: dict[str, object]) -> dict[str, object]:
        for field in (
            "parameters_json",
            "evidence_json",
            "limitations_json",
            "reopen_if_json",
        ):
            row[field.removesuffix("_json")] = json.loads(cast("str", row.pop(field)))
        return row

    def begin_experiment(
        self,
        *,
        lane: str,
        family: str,
        target: str,
        base_ref: str,
        hypothesis: str,
        parameters: object,
    ) -> dict[str, object]:
        """Open one soft lane-local experiment lease without suppressing other lanes."""
        stamp = timestamp()
        identity = {
            "lane": lane,
            "family": family,
            "target": target,
            "base_ref": base_ref,
            "hypothesis": hypothesis,
            "parameters": parameters,
            "created_at": stamp,
        }
        experiment_id = content_id("X", identity)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT id FROM experiments WHERE lane=? AND outcome='active'",
                (lane,),
            ).fetchone()
            if active is not None:
                raise ValueError(
                    f"{lane} already has active experiment {active['id']}; finish it first"
                )
            connection.execute(
                """
                INSERT INTO experiments(
                    id, lane, family, target, base_ref, hypothesis, parameters_json,
                    outcome, best_score, coverage_count, evidence_json,
                    limitations_json, reopen_if_json, next_frontier, report_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', NULL, 0, '[]', '[]', '[]',
                    '', NULL, ?, ?)
                """,
                (
                    experiment_id,
                    lane,
                    family,
                    target,
                    base_ref,
                    hypothesis,
                    canonical_json(parameters),
                    stamp,
                    stamp,
                ),
            )
            event = self._event(
                connection,
                "experiment_started",
                lane=lane,
                payload={
                    "experiment_id": experiment_id,
                    "family": family,
                    "target": target,
                    "base_ref": base_ref,
                },
            )
        append_event(self.events, event)
        return self.experiment(experiment_id)

    def finish_experiment(
        self,
        experiment_id: str,
        *,
        lane: str,
        outcome: str,
        best_score: float | None,
        coverage_count: int,
        evidence: Sequence[str],
        limitations: Sequence[str],
        reopen_if: Sequence[str],
        next_frontier: str,
    ) -> dict[str, object]:
        """Close one active record with explicit bounded evidence and reopening scope."""
        if outcome not in EXPERIMENT_OUTCOMES - {"active"}:
            raise ValueError(f"invalid experiment outcome: {outcome}")
        if coverage_count < 0:
            raise ValueError("coverage_count must be non-negative")
        if outcome == "exhausted" and coverage_count < 1:
            raise ValueError("exhausted requires a positive coverage_count")
        stamp = timestamp()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM experiments WHERE id=?", (experiment_id,)
            ).fetchone()
            if row is None or row["lane"] != lane or row["outcome"] != "active":
                raise ValueError(
                    "only the owning lane may finish its active experiment"
                )
            connection.execute(
                """
                UPDATE experiments SET outcome=?, best_score=?, coverage_count=?,
                    evidence_json=?, limitations_json=?, reopen_if_json=?,
                    next_frontier=?, updated_at=? WHERE id=?
                """,
                (
                    outcome,
                    best_score,
                    coverage_count,
                    canonical_json(list(evidence)),
                    canonical_json(list(limitations)),
                    canonical_json(list(reopen_if)),
                    next_frontier,
                    stamp,
                    experiment_id,
                ),
            )
            event = self._event(
                connection,
                "experiment_finished",
                lane=lane,
                payload={
                    "experiment_id": experiment_id,
                    "outcome": outcome,
                    "best_score": best_score,
                    "coverage_count": coverage_count,
                },
            )
        append_event(self.events, event)
        return self.experiment(experiment_id)

    def experiment(self, experiment_id: str) -> dict[str, object]:
        """Return one experiment-memory record."""
        with self.connect(readonly=True) as connection:
            row = connection.execute(
                "SELECT * FROM experiments WHERE id=?", (experiment_id,)
            ).fetchone()
        if row is None:
            raise KeyError(experiment_id)
        return self._decode_experiment(dict(row))

    def active_experiment(self, lane: str) -> dict[str, object] | None:
        """Return a lane's unique active soft lease."""
        with self.connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT * FROM experiments WHERE lane=? AND outcome='active'",
                (lane,),
            ).fetchall()
        if len(rows) > 1:
            raise RuntimeError(f"{lane} has multiple active experiments")
        return self._decode_experiment(dict(rows[0])) if rows else None

    def attach_experiment_report(self, lane: str, report_id: str) -> str | None:
        """Link a just-published report to the lane's most recently touched record."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id FROM experiments
                WHERE lane=? AND report_id IS NULL
                ORDER BY updated_at DESC, created_at DESC LIMIT 1
                """,
                (lane,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE experiments SET report_id=?, updated_at=? WHERE id=?",
                (report_id, timestamp(), row["id"]),
            )
            event = self._event(
                connection,
                "experiment_report_attached",
                lane=lane,
                payload={"experiment_id": row["id"], "report_id": report_id},
            )
        append_event(self.events, event)
        return cast("str", row["id"])

    def check_experiments(
        self,
        *,
        family: str,
        target: str,
        base_ref: str,
        parameters: object,
        limit: int = 3,
    ) -> dict[str, object]:
        """Classify an intent and return only its most relevant run-local records."""
        if not 1 <= limit <= 20:
            raise ValueError("experiment result limit must be between 1 and 20")
        with self.connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT * FROM experiments ORDER BY updated_at DESC, id"
            ).fetchall()
        records = [self._decode_experiment(dict(row)) for row in rows]
        exact_scope = [
            record
            for record in records
            if record["family"] == family and record["target"] == target
        ]
        exact_base = [
            record for record in exact_scope if record["base_ref"] == base_ref
        ]
        exact_parameters = canonical_json(parameters)
        exact = [
            record
            for record in exact_base
            if canonical_json(record["parameters"]) == exact_parameters
        ]
        terminal_exact = [record for record in exact if record["outcome"] != "active"]
        outcomes = {record["outcome"] for record in terminal_exact}
        positive = bool(outcomes & {"improved"})
        negative = bool(outcomes & {"neutral", "regressed", "exhausted"})
        if positive and negative:
            classification = "conflicted"
        elif any(record["outcome"] == "active" for record in exact):
            classification = "active"
        elif terminal_exact:
            classification = "covered"
        elif exact_base:
            classification = "partial"
        elif exact_scope:
            classification = "stale"
        else:
            classification = "unseen"

        terms = {
            item
            for item in re_split_words(
                " ".join((family, target, canonical_json(parameters)))
            )
            if item
        }
        ranked: list[tuple[int, str, dict[str, object]]] = []
        for record in records:
            haystack = set(re_split_words(canonical_json(record)))
            score = len(terms & haystack)
            score += 20 if record["family"] == family else 0
            score += 12 if record["target"] == target else 0
            score += 5 if record["base_ref"] == base_ref else 0
            if score:
                ranked.append((score, cast("str", record["updated_at"]), record))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return {
            "classification": classification,
            "intent": {
                "family": family,
                "target": target,
                "base_ref": base_ref,
                "parameters": parameters,
            },
            "records": [record for _score, _updated, record in ranked[:limit]],
        }

    def experiment_frontier(self) -> dict[str, object]:
        """Return a tiny prompt-safe board; terminal details stay on demand."""
        with self.connect(readonly=True) as connection:
            counts = {
                row["outcome"]: int(row["count"])
                for row in connection.execute(
                    "SELECT outcome, COUNT(*) AS count FROM experiments GROUP BY outcome"
                )
            }
            active = [
                self._decode_experiment(dict(row))
                for row in connection.execute(
                    """
                    SELECT * FROM experiments WHERE outcome='active'
                    ORDER BY created_at, id
                    """
                )
            ]
        return {"counts": counts, "active": active}

    def ledger(self) -> list[dict[str, object]]:
        """Return the immutable official-main history."""
        with self.connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT * FROM official_ledger ORDER BY sequence"
            ).fetchall()
        decoded: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            item["receipt_ids"] = json.loads(cast("str", item.pop("receipt_ids_json")))
            item["comparison"] = json.loads(cast("str", item.pop("comparison_json")))
            decoded.append(item)
        return decoded

    def telemetry(self) -> Iterable[dict[str, object]]:
        """Yield event-level research telemetry in order."""
        with self.connect(readonly=True) as connection:
            rows = connection.execute(
                "SELECT * FROM telemetry ORDER BY sequence"
            ).fetchall()
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(cast("str", item.pop("payload_json")))
            yield item


__all__ = [
    "EXPERIMENT_OUTCOMES",
    "FACT_STATES",
    "PR_STATES",
    "SCHEMA_VERSION",
    "CoordinationStore",
    "append_event",
    "canonical_json",
    "content_id",
    "timestamp",
]
