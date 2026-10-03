# /// script
# requires-python = ">=3.12"
# dependencies = ["hmz @ git+https://github.com/humanfia/humanize"]
# ///
"""Loads one flow with hmz, the way hmz loads an installed one, and lists the flows it offers.

    uv run .github/scripts/import_check.py <checkout of repo@commit> flows/<name>/<version>/flow.yaml

Importing a flow runs its code. CI runs this only in a job of its own, with no secrets and a
token that can do nothing; run it yourself only on a flow you would run anyway.
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
    held = checkout / said.get("subdir", "")
    with tempfile.TemporaryDirectory() as scratch:
        if (held / "__init__.py").is_file():
            # Kept under the flow's own name, as hmz keeps an installed flow: a directory is
            # imported as the module its name says.
            entry = Path(scratch, name, "__init__.py")
            shutil.copytree(held, entry.parent, ignore=shutil.ignore_patterns(".git"))
        else:
            entry = held / f"{name}.py"

        from hmz.runtime.flowing.loading import module_of
        from hmz.sdk import Hmz

        module = module_of(entry, None)
        visible = sorted(sub for sub, one in module.flows().items() if not one.hidden)
        if not visible:
            print(
                f"::error file={manifest}::{name} defines no flow that hmz would list"
            )
            return 1
        for sub in visible:
            declared = Hmz().flows.declared(f"{module.at}:{sub}")
            agents = ", ".join(role.name for role in declared.agents) or "-"
            envs = ", ".join(role.name for role in declared.envs) or "-"
            listed = name if sub == name else f"{name}:{sub}"
            print(
                f"{listed}  agents: {agents}  envs: {envs}  {declared.description or ''}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
