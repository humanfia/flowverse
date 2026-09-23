#!/usr/bin/env python3
"""Run-local CLI for PRs, evaluation receipts, artifacts, and knowledge."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

try:  # The runtime copies this file beside the standalone storage module.
    from pfc_storage import CoordinationStore, canonical_json, content_id, timestamp
except ModuleNotFoundError:  # pragma: no cover - used from the source checkout
    from parallel_flame_chase_git_pr.storage import (
        CoordinationStore,
        canonical_json,
        content_id,
        timestamp,
    )

PROTECTED_PREFIXES = (".git", ".flowbench", ".pfc")
SECRET_MARKERS = (
    "AUTH",
    "COOKIE",
    "CREDENTIAL",
    "KEY",
    "PASSWORD",
    "SECRET",
    "TOKEN",
)
EXPERIMENT_TERMINAL_OUTCOMES = {
    "improved",
    "neutral",
    "regressed",
    "invalid",
    "exhausted",
}
CYCLES_PATTERN = re.compile(r"(?im)^\s*CYCLES\s*:\s*([0-9]+)\s*$")


def run_git(*arguments: str, cwd: Path | None = None, check: bool = True) -> str:
    """Run Git without a shell and return text output."""
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def git_config(name: str, cwd: Path) -> str | None:
    """Read an optional repository-local setting."""
    result = subprocess.run(
        ["git", "config", "--get", name],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    return value or None


def sha256_file(path: Path) -> str:
    """Hash one artifact without loading it into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_copy(source: Path, destination: Path) -> None:
    """Copy one file into an immutable content-addressed location."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if sha256_file(destination) != sha256_file(source):
            raise RuntimeError(f"object collision at {destination}")
        return
    descriptor, raw = tempfile.mkstemp(prefix=".pfc-object-", dir=destination.parent)
    temporary = Path(raw)
    os.close(descriptor)
    try:
        shutil.copyfile(source, temporary)
        temporary.chmod(0o444)
        with contextlib.suppress(FileExistsError):
            os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


class Context:
    """Resolved identity and paths for one invocation."""

    def __init__(self, arguments: argparse.Namespace) -> None:
        self.cwd = Path.cwd().resolve()
        configured_root = git_config("pfc.run-root", self.cwd)
        configured_lane = git_config("pfc.lane", self.cwd)
        raw_root = (
            arguments.run_root or os.environ.get("PFC_RUN_ROOT") or configured_root
        )
        raw_lane = arguments.lane or os.environ.get("PFC_LANE") or configured_lane
        if raw_root is None:
            raise ValueError(
                "cannot find this run; pass --run-root or work in a PFC clone"
            )
        if raw_lane is None:
            raise ValueError(
                "cannot find this role; pass --lane or work in a PFC clone"
            )
        self.root = Path(raw_root).expanduser().resolve()
        self.lane = raw_lane
        self.shared = self.root / "shared"
        self.store = CoordinationStore(
            self.shared / "coordination.sqlite",
            self.shared / "coordination-events.jsonl",
        )

    @property
    def is_lane(self) -> bool:
        return self.lane in set(self.store.meta("lanes"))

    @property
    def is_orchestrateor(self) -> bool:
        return self.lane == "orchestrateor"


def repository_root(context: Context) -> Path:
    try:
        return Path(run_git("rev-parse", "--show-toplevel", cwd=context.cwd)).resolve()
    except subprocess.CalledProcessError as why:
        raise ValueError("this command must run inside a PFC Git workspace") from why


def clean_head(repository: Path) -> tuple[str, str]:
    """Require a commit-backed, completely clean worktree."""
    status = run_git("status", "--porcelain", "--untracked-files=all", cwd=repository)
    if status:
        raise ValueError("evaluation and PR transitions require a clean worktree")
    return (
        run_git("rev-parse", "HEAD", cwd=repository),
        run_git("rev-parse", "HEAD^{tree}", cwd=repository),
    )


def workspace_identity(root: Path, patterns: list[str]) -> str:
    """Hash only frozen task paths for a non-Git Experiment Memory lane."""
    entries: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if allowed_path(relative, patterns):
            entries.append((relative, sha256_file(path)))
    return "workspace:" + hashlib.sha256(canonical_json(entries).encode()).hexdigest()


def current_base_ref(context: Context) -> str:
    """Resolve a Git commit or content identity without introducing Git inheritance."""
    if bool(context.store.meta("git_pr_enabled")):
        return clean_head(repository_root(context))[0]
    return workspace_identity(context.cwd, list(context.store.meta("allowed_paths")))


def environment_hash() -> str:
    visible = {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in SECRET_MARKERS)
    }
    return hashlib.sha256(canonical_json(visible).encode()).hexdigest()


def command_evaluate(context: Context, arguments: argparse.Namespace) -> int:
    """Record an external evaluator result against one clean commit/tree."""
    if not (context.is_lane or context.is_orchestrateor):
        raise ValueError("only lanes and the orchestrateor may record evaluations")
    if context.is_orchestrateor and not arguments.pr:
        raise ValueError("a staging evaluation requires --pr")
    if context.is_lane and arguments.pr:
        pr = context.store.pr(arguments.pr)
        if pr["lane"] != context.lane:
            raise ValueError("a lane may only bind evaluation to its own PR")
    if not arguments.command:
        raise ValueError("evaluate requires a command after --")

    git_enabled = bool(context.store.meta("git_pr_enabled"))
    repository = repository_root(context) if git_enabled else None
    if repository is None:
        commit_sha = workspace_identity(
            context.cwd, list(context.store.meta("allowed_paths"))
        )
        tree_sha = commit_sha.removeprefix("workspace:")
    else:
        commit_sha, tree_sha = clean_head(repository)
    started_at = timestamp()
    artifact_root = context.shared / "evaluations"
    artifact_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pfc-eval-", dir=artifact_root) as raw:
        temporary = Path(raw)
        stdout_file = temporary / "stdout"
        stderr_file = temporary / "stderr"
        with (
            stdout_file.open("wb") as stdout_handle,
            stderr_file.open("wb") as stderr_handle,
        ):
            result = subprocess.run(
                arguments.command,
                cwd=context.cwd,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                check=False,
            )
        finished_at = timestamp()
        exit_code = result.returncode
        changed = (
            workspace_identity(context.cwd, list(context.store.meta("allowed_paths")))
            != commit_sha
            if repository is None
            else bool(
                run_git(
                    "status", "--porcelain", "--untracked-files=all", cwd=repository
                )
                or run_git("rev-parse", "HEAD", cwd=repository) != commit_sha
            )
        )
        if changed:
            with stderr_file.open("ab") as handle:
                handle.write(
                    b"\nPFC recorder: evaluator changed the clean commit/worktree; "
                    b"this receipt is non-qualifying.\n"
                )
            exit_code = 125
        stdout_sha = sha256_file(stdout_file)
        stderr_sha = sha256_file(stderr_file)
        identity = {
            "nonce": os.urandom(16).hex(),
            "lane": context.lane,
            "pr_id": arguments.pr,
            "commit_sha": commit_sha,
            "tree_sha": tree_sha,
            "command": arguments.command,
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": exit_code,
            "stdout_sha256": stdout_sha,
            "stderr_sha256": stderr_sha,
        }
        receipt_id = content_id("R", identity)
        receipt_root = artifact_root / receipt_id
        receipt_root.mkdir(exist_ok=False)
        stdout_path = receipt_root / f"{stdout_sha}.stdout"
        stderr_path = receipt_root / f"{stderr_sha}.stderr"
        stdout_file.replace(stdout_path)
        stderr_file.replace(stderr_path)
        stdout_path.chmod(0o444)
        stderr_path.chmod(0o444)
        receipt_root.chmod(0o555)

    receipt = {
        "id": receipt_id,
        "lane": context.lane,
        "role": "orchestrateor" if context.is_orchestrateor else "lane",
        "pr_id": arguments.pr,
        "kind": "staging" if context.is_orchestrateor else "provisional",
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
        "command_json": canonical_json(arguments.command),
        "cwd": str(context.cwd),
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": exit_code,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "stdout_sha256": stdout_sha,
        "stderr_sha256": stderr_sha,
        "environment_sha256": environment_hash(),
    }
    context.store.add_receipt(receipt)
    with stdout_path.open("rb") as handle:
        shutil.copyfileobj(handle, sys.stdout.buffer)
    if stdout_path.stat().st_size:
        with stdout_path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                sys.stdout.buffer.write(b"\n")
    with stderr_path.open("rb") as handle:
        shutil.copyfileobj(handle, sys.stderr.buffer)
    sys.stdout.flush()
    sys.stderr.flush()
    print(
        canonical_json(
            {
                "receipt_id": receipt_id,
                "commit_sha": commit_sha,
                "exit_code": exit_code,
            }
        )
    )
    return exit_code


def pushed_head(repository: Path, branch: str) -> str:
    output = run_git(
        "ls-remote", "--heads", "origin", f"refs/heads/{branch}", cwd=repository
    )
    if not output:
        raise ValueError(f"branch {branch!r} is not pushed to origin")
    return output.split()[0]


def current_branch(repository: Path) -> str:
    branch = run_git("symbolic-ref", "--short", "HEAD", cwd=repository)
    if not branch:
        raise ValueError("PR commands require an attached branch")
    return branch


def allowed_path(path: str, patterns: list[str]) -> bool:
    import fnmatch

    canonical = PurePosixPath(path).as_posix()
    if any(
        canonical == prefix or canonical.startswith(f"{prefix}/")
        for prefix in PROTECTED_PREFIXES
    ):
        return False
    return any(
        pattern in {"**", canonical} or fnmatch.fnmatchcase(canonical, pattern)
        for pattern in patterns
    )


def validate_pr_paths(
    repository: Path, base: str, head: str, patterns: list[str]
) -> None:
    raw = subprocess.run(
        ["git", "diff", "--name-only", "-z", base, head],
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout
    paths = [os.fsdecode(value) for value in raw.split(b"\0") if value]
    rejected = [path for path in paths if not allowed_path(path, patterns)]
    if rejected:
        raise ValueError(f"PR changes protected or out-of-scope paths: {rejected}")


def verify_receipt_artifacts(
    context: Context, repository: Path, receipt_id: str, head_sha: str
) -> None:
    """Recheck the immutable provisional evidence before freezing a head."""
    receipt = context.store.receipt(receipt_id)
    if receipt["commit_sha"] != head_sha:
        raise ValueError("receipt is not bound to the current head")
    if receipt["tree_sha"] != run_git("rev-parse", "HEAD^{tree}", cwd=repository):
        raise ValueError("receipt tree does not match the current head")
    trusted_command = list(context.store.meta("trusted_evaluator_command"))
    recorded_command = json.loads(str(receipt["command_json"]))
    if trusted_command and recorded_command != trusted_command:
        raise ValueError(
            "receipt did not run the exact frozen official evaluator command"
        )
    evaluation_root = (context.shared / "evaluations").resolve()
    for path_field, hash_field in (
        ("stdout_path", "stdout_sha256"),
        ("stderr_path", "stderr_sha256"),
    ):
        path = Path(str(receipt[path_field]))
        try:
            resolved = path.resolve(strict=True)
        except OSError as why:
            raise ValueError(f"receipt artifact is missing: {path}") from why
        if (
            not resolved.is_relative_to(evaluation_root)
            or path.is_symlink()
            or not path.is_file()
            or sha256_file(path) != receipt[hash_field]
        ):
            raise ValueError(f"receipt artifact failed integrity validation: {path}")


def command_pr_open(context: Context, arguments: argparse.Namespace) -> int:
    if not context.is_lane:
        raise ValueError("only a research lane may open a PR")
    if not arguments.draft:
        raise ValueError("new PRs must be explicitly opened with --draft")
    repository = repository_root(context)
    head_sha, _tree = clean_head(repository)
    branch = current_branch(repository)
    prefix = f"{context.lane}/"
    if not branch.startswith(prefix) or branch == prefix:
        raise ValueError(f"branch must be under {prefix}*")
    if pushed_head(repository, branch) != head_sha:
        raise ValueError("push the exact current branch head before opening a PR")
    run_git("fetch", "origin", "main", cwd=repository)
    base_sha = run_git("merge-base", "origin/main", head_sha, cwd=repository)
    pr_id = context.store.create_pr(
        lane=context.lane,
        branch=branch,
        title=arguments.title,
        hypothesis=arguments.hypothesis,
        head_sha=head_sha,
        base_sha=base_sha,
    )
    print(canonical_json(context.store.pr(pr_id)))
    return 0


def command_pr_ready(context: Context, arguments: argparse.Namespace) -> int:
    if not context.is_lane:
        raise ValueError("only a research lane may mark its PR ready")
    repository = repository_root(context)
    head_sha, _tree = clean_head(repository)
    branch = current_branch(repository)
    pr = context.store.pr(arguments.pr_id)
    if pr["lane"] != context.lane or pr["branch"] != branch:
        raise ValueError("ready must run on the owning PR branch")
    if pushed_head(repository, branch) != head_sha:
        raise ValueError("push the exact current head before marking the PR ready")
    run_git("fetch", "origin", "main", cwd=repository)
    base = run_git("merge-base", "origin/main", head_sha, cwd=repository)
    patterns = list(context.store.meta("allowed_paths"))
    validate_pr_paths(repository, base, head_sha, patterns)
    verify_receipt_artifacts(context, repository, arguments.receipt, head_sha)
    context.store.ready_pr(
        pr_id=arguments.pr_id,
        lane=context.lane,
        head_sha=head_sha,
        receipt_id=arguments.receipt,
    )
    print(canonical_json(context.store.pr(arguments.pr_id)))
    return 0


def command_pr_list(context: Context, arguments: argparse.Namespace) -> int:
    print(
        json.dumps(
            context.store.prs(status=arguments.status), ensure_ascii=False, indent=2
        )
    )
    return 0


def command_pr_show(context: Context, arguments: argparse.Namespace) -> int:
    print(json.dumps(context.store.pr(arguments.pr_id), ensure_ascii=False, indent=2))
    return 0


def command_pr_reject(context: Context, arguments: argparse.Namespace) -> int:
    if not context.is_orchestrateor:
        raise ValueError("only the orchestrateor may reject an active PR")
    record = context.store.reject_pr(pr_id=arguments.pr_id, reason=arguments.reason)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def command_knowledge_status(context: Context, _arguments: argparse.Namespace) -> int:
    visible = context.store.search_knowledge("", limit=100_000)
    status = {
        "enabled": bool(context.store.meta("global_knowledge_enabled")),
        "verified_facts": sum(item["kind"] == "fact" for item in visible),
        "accepted_experiences": sum(item["kind"] == "experience" for item in visible),
        "pending_reports": len(context.store.pending_reports(limit=100_000)),
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


def command_knowledge_search(context: Context, arguments: argparse.Namespace) -> int:
    records = context.store.search_knowledge(arguments.query, limit=arguments.limit)
    print(json.dumps(records, ensure_ascii=False, indent=2))
    return 0


def command_knowledge_get(context: Context, arguments: argparse.Namespace) -> int:
    if arguments.knowledge_id.startswith("F"):
        record = {"kind": "fact", **context.store.fact(arguments.knowledge_id)}
    elif arguments.knowledge_id.startswith("E"):
        record = {
            "kind": "experience",
            **context.store.experience(arguments.knowledge_id),
        }
    else:
        raise ValueError("knowledge IDs begin with F (fact) or E (experience)")
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def require_experiment_memory(context: Context) -> None:
    if not bool(context.store.meta("experiment_memory_enabled")):
        raise ValueError("Experiment Memory Lite is disabled in this cell")
    if not context.is_lane:
        raise ValueError("only research lanes may update Experiment Memory Lite")


def experiment_parameters(raw: str) -> object:
    if len(raw.encode()) > 16_384:
        raise ValueError("experiment parameters exceed 16 KiB")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as why:
        raise ValueError("--parameters-json must be valid JSON") from why
    if not isinstance(value, (dict, list)):
        raise TypeError("--parameters-json must be an object or array")
    return value


def resolve_base(context: Context, raw: str) -> str:
    value = current_base_ref(context) if raw == "auto" else raw.strip()
    if not 1 <= len(value) <= 200:
        raise ValueError("experiment base reference must contain 1..200 characters")
    return value


def validate_intent_text(arguments: argparse.Namespace) -> None:
    for field, limit in (("family", 200), ("target", 500), ("hypothesis", 4000)):
        value = getattr(arguments, field).strip()
        if not value or len(value) > limit:
            raise ValueError(
                f"--{field.replace('_', '-')} must contain 1..{limit} characters"
            )
        setattr(arguments, field, value)


def command_experiment_check(context: Context, arguments: argparse.Namespace) -> int:
    require_experiment_memory(context)
    validate_intent_text(arguments)
    parameters = experiment_parameters(arguments.parameters_json)
    base_ref = resolve_base(context, arguments.base)
    result = context.store.check_experiments(
        family=arguments.family,
        target=arguments.target,
        base_ref=base_ref,
        parameters=parameters,
        limit=3,
    )
    context.store.record_telemetry(
        "experiment_intent_checked",
        {
            "classification": result["classification"],
            "family": arguments.family,
            "target": arguments.target,
            "base_ref": base_ref,
            "returned_ids": [item["id"] for item in result["records"]],
        },
        lane=context.lane,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_experiment_begin(context: Context, arguments: argparse.Namespace) -> int:
    require_experiment_memory(context)
    validate_intent_text(arguments)
    parameters = experiment_parameters(arguments.parameters_json)
    base_ref = resolve_base(context, arguments.base)
    checked = context.store.check_experiments(
        family=arguments.family,
        target=arguments.target,
        base_ref=base_ref,
        parameters=parameters,
        limit=3,
    )
    classification = str(checked["classification"])
    if classification in {"covered", "conflicted"} and not arguments.reopen_reason:
        raise ValueError(
            "covered/conflicted work may be reopened, but --reopen-reason is required"
        )
    record = context.store.begin_experiment(
        lane=context.lane,
        family=arguments.family,
        target=arguments.target,
        base_ref=base_ref,
        hypothesis=arguments.hypothesis,
        parameters=parameters,
    )
    if arguments.reopen_reason:
        context.store.record_telemetry(
            "experiment_scope_reopened",
            {
                "experiment_id": record["id"],
                "prior_classification": classification,
                "reason": arguments.reopen_reason,
            },
            lane=context.lane,
        )
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def evidence_score(
    context: Context, evidence: list[str], *, allow_failed: bool
) -> float | None:
    if not evidence:
        raise ValueError("experiment finish requires at least one evidence reference")
    scores: list[int] = []
    object_root = (context.shared / "objects" / "sha256").resolve()
    for reference in evidence:
        if reference.startswith("R"):
            receipt = context.store.receipt(reference)
            if receipt["lane"] != context.lane:
                raise ValueError("a lane may cite only its own evaluator receipt")
            if int(receipt["exit_code"]) != 0 and not allow_failed:
                raise ValueError("non-invalid outcomes require successful receipts")
            path = Path(str(receipt["stdout_path"]))
            if path.is_file() and not path.is_symlink():
                scores.extend(
                    int(value)
                    for value in CYCLES_PATTERN.findall(
                        path.read_text(encoding="utf-8", errors="replace")
                    )
                )
            continue
        if reference.startswith("sha256:"):
            digest = reference.removeprefix("sha256:")
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("invalid sha256 evidence reference")
            path = object_root / digest[:2] / digest
            if not path.is_file() or path.is_symlink() or sha256_file(path) != digest:
                raise ValueError("sha256 evidence object is missing or corrupt")
            continue
        raise ValueError("evidence must be an evaluator receipt ID or sha256:<digest>")
    return float(scores[-1]) if scores else None


def command_experiment_finish(context: Context, arguments: argparse.Namespace) -> int:
    require_experiment_memory(context)
    if arguments.outcome not in EXPERIMENT_TERMINAL_OUTCOMES:
        raise ValueError("invalid experiment outcome")
    derived_score = evidence_score(
        context, arguments.evidence, allow_failed=arguments.outcome == "invalid"
    )
    best_score = (
        arguments.best_score if arguments.best_score is not None else derived_score
    )
    if arguments.outcome == "improved" and best_score is None:
        raise ValueError("improved requires a measured --best-score or scored receipt")
    record = context.store.finish_experiment(
        arguments.experiment_id,
        lane=context.lane,
        outcome=arguments.outcome,
        best_score=best_score,
        coverage_count=arguments.coverage_count,
        evidence=arguments.evidence,
        limitations=arguments.limitation,
        reopen_if=arguments.reopen_if,
        next_frontier=arguments.next_frontier,
    )
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def command_experiment_show(context: Context, arguments: argparse.Namespace) -> int:
    require_experiment_memory(context)
    print(
        json.dumps(
            context.store.experiment(arguments.experiment_id),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_experiment_status(context: Context, _arguments: argparse.Namespace) -> int:
    require_experiment_memory(context)
    print(json.dumps(context.store.experiment_frontier(), ensure_ascii=False, indent=2))
    return 0


def command_artifact_put(context: Context, arguments: argparse.Namespace) -> int:
    source = Path(arguments.path).expanduser().resolve(strict=True)
    if not source.is_file() or source.is_symlink():
        raise ValueError("artifact put accepts one regular file")
    digest = sha256_file(source)
    destination = context.shared / "objects" / "sha256" / digest[:2] / digest
    atomic_copy(source, destination)
    record: dict[str, Any] = {
        "algorithm": "sha256",
        "digest": digest,
        "size": source.stat().st_size,
        "path": str(destination),
    }
    context.store.record_telemetry("artifact_stored", record, lane=context.lane)
    print(canonical_json(record))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="pfc")
    root.add_argument("--run-root")
    root.add_argument("--lane")
    commands = root.add_subparsers(dest="group", required=True)

    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--pr")
    evaluate.add_argument("command", nargs=argparse.REMAINDER)
    evaluate.set_defaults(handler=command_evaluate)

    pr = commands.add_parser("pr")
    pr_commands = pr.add_subparsers(dest="pr_command", required=True)
    pr_open = pr_commands.add_parser("open")
    pr_open.add_argument("--draft", action="store_true")
    pr_open.add_argument("--title", required=True)
    pr_open.add_argument("--hypothesis", required=True)
    pr_open.set_defaults(handler=command_pr_open)
    pr_ready = pr_commands.add_parser("ready")
    pr_ready.add_argument("pr_id")
    pr_ready.add_argument("--receipt", required=True)
    pr_ready.set_defaults(handler=command_pr_ready)
    pr_list = pr_commands.add_parser("list")
    pr_list.add_argument(
        "--status",
        choices=sorted({"draft", "ready", "reviewing", "merged", "rejected"}),
    )
    pr_list.set_defaults(handler=command_pr_list)
    pr_show = pr_commands.add_parser("show")
    pr_show.add_argument("pr_id")
    pr_show.set_defaults(handler=command_pr_show)
    pr_reject = pr_commands.add_parser("reject")
    pr_reject.add_argument("pr_id")
    pr_reject.add_argument("--reason", required=True)
    pr_reject.set_defaults(handler=command_pr_reject)

    knowledge = commands.add_parser("knowledge")
    knowledge_commands = knowledge.add_subparsers(
        dest="knowledge_command", required=True
    )
    knowledge_status = knowledge_commands.add_parser("status")
    knowledge_status.set_defaults(handler=command_knowledge_status)
    knowledge_search = knowledge_commands.add_parser("search")
    knowledge_search.add_argument("query", nargs="?", default="")
    knowledge_search.add_argument("--limit", type=int, default=20)
    knowledge_search.set_defaults(handler=command_knowledge_search)
    knowledge_get = knowledge_commands.add_parser("get")
    knowledge_get.add_argument("knowledge_id")
    knowledge_get.set_defaults(handler=command_knowledge_get)

    experiment = commands.add_parser("experiment")
    experiment_commands = experiment.add_subparsers(
        dest="experiment_command", required=True
    )
    experiment_check = experiment_commands.add_parser("check")
    experiment_begin = experiment_commands.add_parser("begin")
    for intent in (experiment_check, experiment_begin):
        intent.add_argument("--family", required=True)
        intent.add_argument("--target", required=True)
        intent.add_argument("--base", default="auto")
        intent.add_argument("--hypothesis", required=True)
        intent.add_argument("--parameters-json", required=True)
    experiment_check.set_defaults(handler=command_experiment_check)
    experiment_begin.add_argument(
        "--reopen-reason",
        choices=("changed-base", "new-range", "new-interaction", "evidence-gap"),
    )
    experiment_begin.set_defaults(handler=command_experiment_begin)
    experiment_finish = experiment_commands.add_parser("finish")
    experiment_finish.add_argument("experiment_id")
    experiment_finish.add_argument(
        "--outcome", required=True, choices=sorted(EXPERIMENT_TERMINAL_OUTCOMES)
    )
    experiment_finish.add_argument("--best-score", type=float)
    experiment_finish.add_argument("--coverage-count", type=int, default=0)
    experiment_finish.add_argument("--evidence", action="append", default=[])
    experiment_finish.add_argument("--limitation", action="append", default=[])
    experiment_finish.add_argument("--reopen-if", action="append", default=[])
    experiment_finish.add_argument("--next-frontier", default="")
    experiment_finish.set_defaults(handler=command_experiment_finish)
    experiment_show = experiment_commands.add_parser("show")
    experiment_show.add_argument("experiment_id")
    experiment_show.set_defaults(handler=command_experiment_show)
    experiment_status = experiment_commands.add_parser("status")
    experiment_status.set_defaults(handler=command_experiment_status)

    artifact = commands.add_parser("artifact")
    artifact_commands = artifact.add_subparsers(dest="artifact_command", required=True)
    artifact_put = artifact_commands.add_parser("put")
    artifact_put.add_argument("path")
    artifact_put.set_defaults(handler=command_artifact_put)
    return root


def main() -> int:
    arguments = parser().parse_args()
    if arguments.group == "evaluate" and arguments.command[:1] == ["--"]:
        arguments.command = arguments.command[1:]
    try:
        return int(arguments.handler(Context(arguments), arguments))
    except (
        KeyError,
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ) as why:
        print(f"pfc: {why}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
