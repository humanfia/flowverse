<!--
Adding a flow or a version of one? Tick every box below; CONTRIBUTING.md explains each:
https://github.com/humanfia/flowverse/blob/main/CONTRIBUTING.md
Changing the index itself (schema, scripts, workflows, docs)? Delete the checklist and say what
changes and why.

A new version of a flow already listed that changes nothing but version, ref and commit, at its
repository's own tag v<version> on a commit after the newest version's, is merged automatically
once every check passes; anything else waits for a maintainer.
-->

## What this adds

<!-- The flow, the version, and in a sentence what it does. For a new version: what changed. -->

## Checklist

- [ ] This pull request adds exactly one version of one flow: `flows/<owner>/<flow>/<version>/flow.yaml`,
      `<owner>` being the owner of the flow's repository, in lowercase.
- [ ] I wrote the flow or maintain it, or its author agreed to it being listed.
- [ ] The repository is public, and the version's tag is pushed.
- [ ] `commit` is the full commit the tag points at.
- [ ] I ran the flow with hmz at that commit, and its tests pass.
- [ ] The repository has a license, and `license` names it.
- [ ] `uv run .github/scripts/validate.py --base origin/main --network` passes.
- [ ] I read the [code of conduct](https://github.com/humanfia/flowverse/blob/main/CODE_OF_CONDUCT.md).
