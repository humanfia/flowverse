"""The gates a draft is held to before a critic reads it: it loads, and it ends on its own.

Both are asked of humanize's own engine, in a process of its own per question (`smoke.py`
beside this): once to load the draft, then once per world to run it on fakes -- an agent
that never says done, one that says done at once, one that answers nothing. A world whose
process is still going past its clock is killed and counted against the draft.
"""

import asyncio
import contextlib
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

WORLDS = ("never-done", "always-done", "silent")
SMOKE = Path(__file__).with_name("smoke.py")
GRACE = 30.0
ENTRY = "__init__.py"


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    said: str


@dataclass(frozen=True)
class Checked:
    findings: tuple[Finding, ...]
    agents: tuple[str, ...]
    person: bool

    def blocking(self, *, strict: bool) -> list[Finding]:
        return [one for one in self.findings if one.severity == "error" or strict]

    def waived(self, *, strict: bool) -> list[Finding]:
        return [one for one in self.findings if one.severity != "error" and not strict]


def said(findings: list[Finding]) -> str:
    return "\n".join(f"- {one.severity}: {one.code}: {one.said}" for one in findings)


async def checked(
    files: dict[str, bytes], name: str, seconds: float, shown: str = ""
) -> Checked:
    holding = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix=".aot."))
    try:
        draft = holding / name
        await asyncio.to_thread(_written, draft, files)
        loaded = await _asked(draft, "", seconds)
        findings = list(loaded.findings)
        if not any(one.severity == "error" for one in findings):
            async with asyncio.TaskGroup() as group:
                runs = [group.create_task(_asked(draft, world, seconds)) for world in WORLDS]
            for run in runs:
                findings.extend(run.result().findings)
        return Checked(
            findings=tuple(
                Finding(one.severity, one.code, one.said.replace(str(draft), shown or name))
                for one in findings
            ),
            agents=tuple(
                str(one["name"]) for one in loaded.agents if not one["person"]
            ),
            person=any(one["person"] for one in loaded.agents),
        )
    finally:
        await asyncio.to_thread(shutil.rmtree, holding, True)


@dataclass(frozen=True)
class _Said:
    agents: list[dict[str, Any]]
    findings: list[Finding]


def _written(draft: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        at = draft / PurePosixPath(rel)
        at.parent.mkdir(parents=True, exist_ok=True)
        at.write_bytes(data)


async def _asked(draft: Path, world: str, seconds: float) -> _Said:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(SMOKE),
        str(draft),
        world,
        repr(seconds),
        cwd=draft.parent,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    where = f"under {world}" if world else "loading it"
    try:
        async with asyncio.timeout(seconds + GRACE):
            out, err = await process.communicate()
    except TimeoutError:
        return _only(
            "hung",
            f"{where}, it was still going after {seconds + GRACE:g}s and was killed -- "
            "something in it never yields: a loop with no turn and no await in it, or a "
            "blocking call",
        )
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
    try:
        report: dict[str, Any] = json.loads(out.decode().strip().splitlines()[-1])
    except (ValueError, IndexError):
        tail = "\n".join(err.decode(errors="replace").strip().splitlines()[-8:])
        return _only("crashed", f"{where}, the smoke run died:\n{tail}")
    return _Said(
        agents=report.get("agents", []),
        findings=[Finding(**one) for one in report.get("findings", [])],
    )


def _only(code: str, said_: str) -> _Said:
    return _Said(agents=[], findings=[Finding("error", code, said_)])
