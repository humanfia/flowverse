# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "hmz @ git+https://github.com/humanfia/humanize",
#     "jsonschema==4.26.0",
#     "license-expression==30.4.4",
#     "pyyaml==6.0.3",
#     "semver==3.1.0",
# ]
# ///
"""Loads one flow with hmz, laid out as hmz installs it, and lists the flows it offers.

    uv run .github/scripts/import_check.py <checkout of repo at commit> <flow.yaml>

The flow is copied into a scratch directory of flows the way hmz installs a release, beside the
releases of its dependencies that hmz would install with it (fetched from GitHub), then
imported, and each flow it offers is described.

Importing a flow runs its code. CI runs this only in a job of its own, with no secrets and a
token granted nothing; run it yourself only on a flow you would run anyway.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import validate


def place(source: Path, name: str, into: Path) -> Path:
    """Copies a flow as hmz installs a release: its directory whole under the flow's name,
    symlinks as they are, or a flow that is one file as that directory's entry point."""
    held = into / name
    if (source / "__init__.py").is_file():
        shutil.copytree(
            source,
            held,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git", "__pycache__"),
        )
    else:
        held.mkdir()
        shutil.copy2(source / f"{name}.py", held / "__init__.py")
    return held / "__init__.py"


def fetch(repo: str, commit: str, into: Path) -> Path:
    """A checkout of a public repository at one commit."""
    into.mkdir(parents=True)
    for args in (
        ["init", "-q"],
        ["fetch", "-q", "--depth", "1", f"https://github.com/{repo}", commit],
        ["checkout", "-q", "FETCH_HEAD"],
    ):
        subprocess.run(["git", *args], cwd=into, check=True)
    return into


def main(checkout: Path, path: Path) -> int:
    report = validate.Report()
    _, manifests = validate.read(report)
    manifest = manifests.get(path.resolve())
    if manifest is None:
        print(f"::error::{path} does not pass validation; run validate.py")
        return 1
    releases = {(m["name"], m["version"]): m for m in manifests.values()}
    name = manifest["name"]
    with tempfile.TemporaryDirectory() as scratch:
        flows, sources = Path(scratch, "flows"), Path(scratch, "sources")
        flows.mkdir()
        for need in validate.plan(releases, (name, manifest["version"]))[:-1]:
            one = releases[need]
            at = fetch(one["repo"], one["commit"], sources / one["name"])
            place(at / one.get("subdir", ""), one["name"], flows)
            print(f"beside it: {one['name']} {one['version']}")
        entry = place(checkout / manifest.get("subdir", ""), name, flows)

        from hmz.runtime.flowing.loading import module_of
        from hmz.sdk import Hmz

        module = module_of(entry, None)
        visible = sorted(sub for sub, one in module.flows().items() if not one.hidden)
        if not visible:
            print(f"::error::{name} defines no flow that hmz would list")
            return 1
        for sub in visible:
            declared = Hmz().flows.declared(f"{entry.parent}:{sub}")
            agents = ", ".join(role.name for role in declared.agents) or "-"
            envs = ", ".join(role.name for role in declared.envs) or "-"
            listed = name if sub == name else f"{name}:{sub}"
            print(f"{listed}  agents: {agents}  envs: {envs}")
            print(f"  {declared.description or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
