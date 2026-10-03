# /// script
# requires-python = ">=3.12"
# dependencies = ["hmz @ git+https://github.com/humanfia/humanize"]
# ///
"""Loads one flow with hmz, laid out as hmz installs it, and lists the flows it offers.

    uv run .github/scripts/import_check.py <checkout of repo at commit> <flow.yaml>

Importing a flow runs its code. CI runs this only in a job of its own, with no secrets and a
token granted nothing; run it yourself only on a flow you would run anyway.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import yaml


def main(checkout: Path, manifest: Path) -> int:
    said = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    name = said["name"]
    source = checkout / said.get("subdir", "")
    with tempfile.TemporaryDirectory() as scratch:
        # As hmz installs a release: the flow's directory copied whole under the flow's own
        # name, symlinks as they are, or a flow that is one file as that directory's entry.
        held = Path(scratch, name)
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

        from hmz.runtime.flowing.loading import module_of
        from hmz.sdk import Hmz

        module = module_of(held / "__init__.py", None)
        visible = sorted(sub for sub, one in module.flows().items() if not one.hidden)
        if not visible:
            print(f"::error::{name} defines no flow that hmz would list")
            return 1
        for sub in visible:
            declared = Hmz().flows.declared(f"{held}:{sub}")
            agents = ", ".join(role.name for role in declared.agents) or "-"
            envs = ", ".join(role.name for role in declared.envs) or "-"
            listed = name if sub == name else f"{name}:{sub}"
            print(f"{listed}  agents: {agents}  envs: {envs}")
            print(f"  {declared.description or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
