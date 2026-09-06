"""Atomic DAG and wiki persistence for an observable long-running flow."""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
from typing import TYPE_CHECKING, Any

from .models import NodeRecord, NodeStatus, ProvedTheorem

if TYPE_CHECKING:
    from pathlib import Path


def now() -> str:
    """Return one stable UTC timestamp for status records."""
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug(value: str, *, fallback: str = "theorem") -> str:
    """Turn a model-provided name into a safe file component."""
    made = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return made[:80] or fallback


def atomic_text(path: Path, content: str) -> None:
    """Replace one small control artifact without exposing a partial write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


class Store:
    """The single writer of node state, rendered DAGs, and theorem wiki pages."""

    def __init__(self, root: Path, wiki: Path, task: str) -> None:
        self.root = root
        self.wiki = wiki
        self.task = task
        self.nodes: dict[str, NodeRecord] = {}
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.wiki.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        path = self.root / "dag.json"
        if not path.is_file():
            return
        try:
            held = json.loads(path.read_text(encoding="utf-8"))
            records = held.get("nodes", [])
            self.nodes = {
                record.id: record
                for one in records
                for record in [NodeRecord.model_validate(one)]
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self.nodes = {}

    def ensure(
        self,
        node_id: str,
        *,
        parent: str | None,
        depth: int,
        title: str,
        statement: str,
        lean_statement: str = "",
        lean_name: str = "",
        depends_on: list[str] | None = None,
    ) -> NodeRecord:
        """Return an existing node or durably add it to the graph."""
        with self._lock:
            found = self.nodes.get(node_id)
            if found is not None:
                return found
            record = NodeRecord(
                id=node_id,
                parent=parent,
                depth=depth,
                title=title,
                statement=statement,
                lean_statement=lean_statement,
                lean_name=lean_name,
                depends_on=depends_on or [],
                updated_at=now(),
            )
            self.nodes[node_id] = record
            if parent and parent in self.nodes:
                parent_record = self.nodes[parent]
                if node_id not in parent_record.children:
                    parent_record.children.append(node_id)
            self.render()
            return record

    def update(
        self, node_id: str, status: NodeStatus, message: str = "", **fields: Any
    ) -> None:
        """Persist one status transition and immediately redraw the live DAG."""
        with self._lock:
            record = self.nodes[node_id]
            # Comparator + independent reviewer approval is a permanent checkpoint.
            # A later integration conflict is about composing Git histories; it must
            # never send accepted mathematics back through planning, prose, splitting,
            # or Lean proving.  Keep this invariant here at the persistence boundary so
            # stale supervisors cannot accidentally erase it.
            if record.status == "proved" and status != "proved":
                print(
                    f"[DAG] {node_id}: proved — ignored regressive transition to {status}"
                )
                return
            if (
                record.status == "integrating"
                and record.candidate_commit
                and status not in {"integrating", "proved"}
            ):
                print(
                    f"[DAG] {node_id}: integrating — retained accepted candidate; "
                    f"ignored regressive transition to {status}"
                )
                return
            record.status = status
            record.message = message
            record.updated_at = now()
            for name, value in fields.items():
                setattr(record, name, value)
            self.render()
            print(f"[DAG] {node_id}: {status}" + (f" — {message}" if message else ""))

    def render(self) -> None:
        """Write machine-readable state, Mermaid, and a compact Markdown status view."""
        with self._lock:
            ordered = sorted(self.nodes.values(), key=lambda one: (one.depth, one.id))
            payload = {
                "updated_at": now(),
                "task": self.task,
                "nodes": [one.model_dump(mode="json") for one in ordered],
            }
            atomic_text(
                self.root / "dag.json",
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            )
            mermaid = [
                "flowchart TD",
                (
                    '  legend["Every solid arrow A --&gt; B means A depends on B<br/>'
                    'B must be proved before A can finish"]'
                ),
            ]
            for record in ordered:
                label = self._label(record, self._scheduling(record))
                mermaid.append(f'  {self._mermaid_id(record.id)}["{label}"]')
            # Use one direction and one line style everywhere: the arrow starts at
            # the dependent theorem and points toward what it needs. A parent
            # depends on every decomposition child; a node depends on every
            # explicit prerequisite in ``depends_on``.
            edges: set[tuple[str, str]] = set()
            for record in ordered:
                edges.update(
                    (record.id, child)
                    for child in record.children
                    if child in self.nodes
                )
                if (
                    record.parent
                    and record.parent in self.nodes
                    and record.id not in self.nodes[record.parent].children
                ):
                    edges.add((record.parent, record.id))
                edges.update(
                    (record.id, dependency)
                    for dependency in record.depends_on
                    if dependency in self.nodes
                )
            mermaid.extend(
                f"  {self._mermaid_id(dependent)} --> {self._mermaid_id(dependency)}"
                for dependent, dependency in sorted(edges)
            )
            diagram = "\n".join(mermaid) + "\n"
            atomic_text(self.root / "dag.mmd", diagram)
            rows = [
                "# Recursive Lean proof DAG",
                "",
                f"Updated: {payload['updated_at']}",
                "",
                "```mermaid",
                diagram.rstrip(),
                "```",
                "",
                "| Node | Depth | Status | Scheduling | Theorem | Message |",
                "| --- | ---: | --- | --- | --- | --- |",
            ]
            rows.extend(
                (
                    (
                        "| {node} | {depth} | {status} | {scheduling} | "
                        "{theorem} | {message} |"
                    ).format(
                        node=self._cell(record.id),
                        depth=record.depth,
                        status=record.status,
                        scheduling=self._cell(self._scheduling(record)),
                        theorem=self._cell(record.lean_name or "—"),
                        message=self._cell(record.message or "—"),
                    )
                )
                for record in ordered
            )
            atomic_text(self.root / "DAG.md", "\n".join(rows) + "\n")

    def publish(
        self,
        node: NodeRecord,
        theorem: ProvedTheorem,
        *,
        plan: str,
        natural: str,
        comparator_log: str,
    ) -> Path:
        """Write or update one theorem page and rebuild the wiki index."""
        page = self.wiki / f"{slug(theorem.name)}.md"
        content = f"""# `{theorem.name}`

- Status: comparator-approved and independently reviewed
- DAG node: `{node.id}`
- Recursion depth: {node.depth}
- Parent: `{node.parent or "none"}`
- Lean source: `{theorem.lean_file}`
- Isolated proof worktree: `{node.worktree or "not recorded"}`
- Proof branch: `{node.proof_branch or "not recorded"}`
- Proof base commit: `{node.proof_base_commit or "not recorded"}`
- Reviewed candidate commit: `{node.candidate_commit or "not recorded"}`
- Integrated problem commit: `{node.integrated_commit or "not recorded"}`
- Updated: {now()}

## Statement

{theorem.statement}

## Mathematical summary

{theorem.natural_summary}

## Node problem

{node.statement}

## Natural-language proof

{natural}

## Accepted plan

{plan}

## Comparator evidence

```text
{comparator_log.rstrip()}
```
"""
        atomic_text(page, content)
        self._wiki_index()
        return page

    def _wiki_index(self) -> None:
        pages = sorted(one for one in self.wiki.glob("*.md") if one.name != "README.md")
        rows = [
            "# Comparator-approved theorem wiki",
            "",
            "Every page here was emitted only after the configured comparator and a fresh",
            "Lean reviewer both passed.",
            "",
        ]
        rows.extend(f"- [{one.stem}]({one.name})" for one in pages)
        atomic_text(self.wiki / "README.md", "\n".join(rows) + "\n")

    @staticmethod
    def _mermaid_id(node_id: str) -> str:
        return "n_" + re.sub(r"[^A-Za-z0-9_]", "_", node_id)

    @staticmethod
    def _cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ")

    def _scheduling(self, record: NodeRecord) -> str:
        """Explain decomposition shape separately from dependency readiness."""
        shape = "decomposition leaf" if not record.children else "decomposed node"
        blocked = [
            dependency
            for dependency in record.depends_on
            if dependency in self.nodes and self.nodes[dependency].status != "proved"
        ]
        if blocked:
            return f"{shape}; blocked by: {', '.join(blocked)}"
        unfinished_children = [
            child
            for child in record.children
            if child in self.nodes and self.nodes[child].status != "proved"
        ]
        if unfinished_children:
            return f"{shape}; waiting for {len(unfinished_children)} child theorem(s)"
        if record.status == "proved":
            return f"{shape}; proved"
        return f"{shape}; dependency-ready"

    @staticmethod
    def _label(record: NodeRecord, scheduling: str) -> str:
        compact = record.title.replace('"', "'").replace("\n", " ")[:46]
        compact_schedule = scheduling.replace('"', "'")[:84]
        return f"{record.id}\\n{compact}\\n[{record.status}]\\n{compact_schedule}"
