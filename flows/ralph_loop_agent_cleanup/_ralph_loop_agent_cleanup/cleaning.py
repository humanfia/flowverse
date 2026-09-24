from __future__ import annotations

import time
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .config import Config
from .guard import guarded, limits
from .tree import (
    MIB,
    Measure,
    archive_history,
    delete_strays,
    drop_saved,
    erase_history,
    freeze_ignores,
    history_repo,
    link_history,
    measure,
    restore_tree,
    run_check,
    save_tree,
    tail,
    truncate_notes,
)

DELIVERY_TRIES = 3
PLACES_SHOWN = 8


class Cleaned(BaseModel):
    """The cleaner's account of an epoch; every field required, as a shape must be."""

    deleted: list[str] = Field(description="what was deleted, item by item, briefly")
    kept: list[str] = Field(
        description="what was kept as essence, item by item, briefly"
    )
    check_ran: bool = Field(description="whether the correctness check was run")
    check_passed: bool = Field(description="whether the correctness check passed")


def cleaning_prompt(held: Config) -> str:
    work_paths = ", ".join(f"`{path}`" for path in held.work_paths)
    parts = [
        (
            "You are this repository's cleaner, arriving with fresh eyes. Shrink it to"
            " its essence for the next engineer; do not improve, optimize, or refactor"
            " anything."
        ),
        (
            f"In the task work under {work_paths}: delete dead code -- code paths"
            " disabled by constant flags, commented-out code blocks, unused imports,"
            " functions and variables, and abandoned alternatives; delete from comments"
            " every experiment record -- what was tried, comparative results, benchmark"
            " numbers, and attempt histories; keep only comments that explain design"
            " intent, each cut to a single line. Never change externally visible behavior;"
            " if unsure whether something is dead, leave it."
        ),
        (
            "In the rest of the repository: delete every file past agents left behind;"
            " leave the task's own files untouched; write exactly one file, NEXT.md at the"
            f" tree's root, of at most {held.next_lines} lines, each line one direction"
            " worth exploring next, distilled from what you read -- no narratives, no"
            " history. Leave every .gitignore as it is. Never follow symlinks."
        ),
    ]
    if held.check_command:
        parts.append(
            f"Then run the correctness check {held.check_command!r} and restore"
            " whatever you broke."
        )
    parts.append(
        "Answer in shape: what you deleted, what you kept, whether the check ran and"
        " passed."
    )
    return "\n\n".join(parts)


def places(strays: list[str]) -> str:
    counted = Counter(
        rel.split("/", 1)[0] + ("/" if "/" in rel else "") for rel in strays
    )
    shown = [
        f"{place} ({count})" if place.endswith("/") else place
        for place, count in counted.most_common(PLACES_SHOWN)
    ]
    rest = len(counted) - len(shown)
    return ", ".join(shown) + (f", and {rest} more places" if rest > 0 else "")


def overages(found: Measure, held: Config) -> list[str]:
    overs = []
    if found.strays:
        overs.append(f"{len(found.strays)} stray file(s) in {places(found.strays)}")
    if found.notes_lines > held.next_lines:
        overs.append(f"NEXT.md has {found.notes_lines} lines, cap {held.next_lines}")
    if found.comment_count > held.comment_lines:
        overs.append(
            f"{found.comment_count} comment lines in the work paths, cap"
            f" {held.comment_lines}"
        )
    return overs


def repair_prompt(overs: list[str]) -> str:
    listed = "\n".join(f"- {over}" for over in overs)
    return (
        "Still over after your cleaning:\n"
        f"{listed}\n"
        "Cut again under the same rules. Never change externally visible behavior."
    )


def _measure(
    held: Config, root: Path, saved: Path, manifest: set[str], epoch: int
) -> tuple[Measure, list[str]]:
    if touched := freeze_ignores(root, saved):
        print(
            f"epoch {epoch}: put back the .gitignore files the cleaner changed: {touched}"
        )
    found = measure(root, manifest, held.work_paths)
    return found, overages(found, held)


def _clean(
    cleaner: Any,
    held: Config,
    root: Path,
    saved: Path,
    manifest: set[str],
    epoch: int,
) -> None:
    session = cleaner.new(cwd=str(root))
    with guarded(session, **limits(held, f"epoch {epoch} cleaner")) as watch:
        report = session(cleaning_prompt(held), suppress=True, schema=Cleaned)
    ended = watch.timed_out
    if report is None:
        print(f"epoch {epoch}: the cleaner answered nothing usable")
    else:
        said_check = (
            "ran and passed"
            if report.check_ran and report.check_passed
            else "ran and failed"
            if report.check_ran
            else "did not run"
        )
        print(
            f"epoch {epoch}: cleaner deleted -- "
            + ("; ".join(report.deleted) or "nothing")
        )
        print(
            f"epoch {epoch}: cleaner kept -- " + ("; ".join(report.kept) or "nothing")
        )
        print(f"epoch {epoch}: cleaner says its check {said_check}")

    found, overs = _measure(held, root, saved, manifest, epoch)
    print(f"epoch {epoch}: measured -- " + ("; ".join(overs) or "within every cap"))
    used = 0
    while overs and used < held.repairs and not ended:
        print(f"epoch {epoch}: repair {used + 1} of {held.repairs}")
        landed = False
        for _ in range(DELIVERY_TRIES):
            label = f"epoch {epoch} cleaner repair {used + 1}"
            with guarded(session, **limits(held, label)) as watch:
                said = session(repair_prompt(overs), suppress=True)
            ended = watch.timed_out
            if said or ended:
                landed = True
                break
            print(f"epoch {epoch}: a repair turn never landed; resting, then retrying")
            time.sleep(5)
        if not landed:
            print(
                f"epoch {epoch}: repair delivery gave out; falling to the mechanical cut"
            )
            break
        used += 1
        found, overs = _measure(held, root, saved, manifest, epoch)
    if overs:
        print(f"epoch {epoch}: the flow cuts mechanically")
        delete_strays(root, found.strays)
        truncate_notes(root, held.next_lines)
        if found.comment_count > held.comment_lines:
            print(
                f"epoch {epoch}: comment lines still {found.comment_count} against"
                f" a cap of {held.comment_lines} -- printed only, since no rule can"
                " tell a design comment from a narrative one"
            )
    elif used:
        print(f"epoch {epoch}: within every cap after {used} repair(s)")


def clean_epoch(
    cleaner: Any,
    held: Config,
    root: Path,
    manifest: set[str],
    store: Path,
    epoch: int,
) -> None:
    limit = int(held.max_tracked_file_mb * MIB)
    print(f"epoch {epoch}: saving the tree aside as the revert point")
    saved = save_tree(root, store)
    try:
        _clean(cleaner, held, root, saved, manifest, epoch)
        title = f"epoch {epoch}: distilled tree"
        if held.check_command:
            log = store / "checks" / f"epoch-{epoch:03d}.log"
            if run_check(root, held.check_command, log):
                print(f"epoch {epoch}: the check passed; the cleaning stands")
            else:
                restore_tree(root, saved)
                title = (
                    f"epoch {epoch}: the tree the coding turns left; the check failed,"
                    " so the cleaning was reverted"
                )
                print(
                    f"epoch {epoch}: the check failed -- this epoch's cleaning was"
                    f" reverted; its output is in {log}:\n{tail(log)}"
                )
        archived = archive_history(saved, store, epoch, limit)
        erased = bool(archived) and erase_history(root, store, epoch, limit, title)
        if not archived:
            print(
                f"epoch {epoch}: the history could not be archived, so it is kept as it"
                " is this epoch"
            )
        elif not erased:
            restore_tree(root, saved)
            print(
                f"epoch {epoch}: a git step failed; the tree, old history included, was"
                " restored from the revert point"
            )
    except BaseException:
        try:
            restore_tree(root, saved)
        except BaseException:
            print(
                f"epoch {epoch}: interrupted, and putting the tree back broke; the"
                f" revert point survives at {saved}"
            )
            raise
        drop_saved(saved)
        print(f"epoch {epoch}: interrupted; the tree was put back as it was before it")
        raise
    if erased:
        link_history(store, epoch)
        print(
            f"epoch {epoch}: one commit stands; the history it replaced is {archived}"
            f" in {history_repo(store)}"
        )
    drop_saved(saved)
