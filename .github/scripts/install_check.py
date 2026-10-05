# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "hmz @ git+https://github.com/humanfia/humanize",
# ]
# ///
"""Installs one version of a flow out of this index with hmz, as anybody would, and loads it.

    uv run .github/scripts/install_check.py flows/<flow>/<version>/flow.yaml
    uv run .github/scripts/install_check.py flows/<owner>/<flow>/<version>/flow.yaml

A clone of this checkout, as committed, is added as a flowverse to a humanize home made for the
run, and the version is installed out of it through hmz's SDK: fetched from GitHub at the
commit its manifest pins, with every flow it depends on at the version hmz picks. Then each flow
it offers is loaded and described.

Loading a flow runs its code. CI runs this only in a job of its own, with no secrets and a token
granted nothing; run it yourself only on a flow you would run anyway.
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
#: What this checkout is added as.
VERSE = "candidate"


def check(called: str, version: str) -> int:
    """Installs one version, by the flow's name in this index, and describes what it offers."""
    from hmz.sdk import Hmz

    hmz = Hmz()
    verse = hmz.verses.add(ROOT.as_uri(), VERSE)
    name = f"@{VERSE}/{called}"
    for one in hmz.verses.install(name, version):
        print(f"installed {one.name} {one.version}: {one.repo} at {one.commit}")
    offered = [
        one
        for one in hmz.verses.holds(verse)
        if one.name == name or one.name.startswith(f"{name}:")
    ]
    if not offered:
        print(f"::error::{called} {version} offers no flow that hmz would list")
        return 1
    for offer in offered:
        declared = hmz.flows.declared(offer.name)
        agents = ", ".join(role.name for role in declared.agents) or "-"
        envs = ", ".join(role.name for role in declared.envs) or "-"
        print(f"{offer.name}  agents: {agents}  envs: {envs}")
        print(f"  {declared.description or ''}")
    return 0


def main(manifest: Path) -> int:
    parts = manifest.resolve().relative_to(ROOT / "flows").parts
    called, version = "/".join(parts[:-2]), parts[-2]
    with tempfile.TemporaryDirectory() as scratch:
        os.environ["HUMANIZE_HOME"] = str(Path(scratch, "home"))
        os.chdir(scratch)  # away from any project's own flows
        try:
            return check(called, version)
        except Exception as error:  # noqa: BLE001 -- whatever stops hmz, as an annotation
            traceback.print_exc()
            said = f"{type(error).__name__}: {error}"
            said = said.translate(str.maketrans({"%": "%25", "\r": "%0D", "\n": "%0A"}))
            print(f"::error::hmz could not install and load {called} {version}: {said}")
            return 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
