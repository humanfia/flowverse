# Contributing to flowverse

Thank you for sharing a flow. This index lists flows; it does not hold them. You publish your
flow in a GitHub repository of your own, then open a pull request here adding one small
manifest that says where a released version of it is. Maintainers review it, CI checks it, and
once it is merged every hmz can install it.

How to write a flow is humanize's documentation:
[Your first flow](https://docs.humanfia.ai/humanize/weaver/writing-a-flow),
[Testing a flow](https://docs.humanfia.ai/humanize/weaver/testing-flows) and
[Flowverses](https://docs.humanfia.ai/humanize/weaver/flowverses). This guide covers what
happens after.

- [Submitting a flow](#submitting-a-flow)
- [Naming](#naming)
- [The manifest](#the-manifest)
- [Pinning to a commit](#pinning-to-a-commit)
- [Dependencies](#dependencies)
- [What CI checks](#what-ci-checks)
- [Review and merge](#review-and-merge)
- [New versions, withdrawals and removals](#new-versions-withdrawals-and-removals)
- [Changing the index itself](#changing-the-index-itself)

## Submitting a flow

1. **Publish the flow in its own public GitHub repository.** Keep the flow in a `<name>/`
   directory, with its tests, README and LICENSE beside it at the root:

   ```text
   flow-my-review/
   ├── my_review/              the flow: what `subdir` names
   │   ├── __init__.py         its @flow functions
   │   ├── _prompts.py         anything it imports, by plain name
   │   └── skills/             skills its roles carry, if any
   ├── tests/
   ├── README.md
   └── LICENSE
   ```

   Variants of one flow belong in one module, as several `@flow`s (`my_review:strict`), not
   in several repositories or several index entries.

2. **Test it with hmz** by path: `hmz exec -f ./my_review …`, and with its tests.

3. **Tag a release** with a [SemVer 2.0.0](https://semver.org) version and push the tag:

   ```sh
   git tag v0.1.0 && git push origin v0.1.0
   ```

4. **Add the manifest** at `flows/<name>/<version>/flow.yaml` in a fork of this repository,
   [as below](#the-manifest). The commit your tag points at:

   ```sh
   gh api repos/<owner>/<repo>/commits/v0.1.0 --jq .sha
   ```

5. **Check it** the way CI will ([uv](https://docs.astral.sh/uv/) is the one requirement):

   ```sh
   uv run .github/scripts/validate.py --base origin/main --network
   ```

6. **Open a pull request** with one flow and one version in it, titled in the
   [Conventional Commits](https://www.conventionalcommits.org) style:
   `feat(my_review): add my_review 0.1.0`, or `feat(my_review): 0.2.0` for a later version.
   Fill in the checklist the template gives you.

## Naming

- A name is lowercase letters, digits and underscores, starting with a letter
  (`[a-z][a-z0-9_]*`), at most 64 characters. It is the directory's name, the manifest's
  `name`, and what people install and run.
- These names belong to the flows built into humanize and are never accepted: `chat`,
  `ralph_loop`, `goal`, `flame_chase`, `stateful_ralph`, `continue_loop`, `rlar`.
- Names are first come, first served, and go to the first flow merged under them. A name that
  impersonates another project or claims an affiliation it does not have is refused, as is a
  name taken to hold it rather than to publish a flow.
- Maintainers may ask for a name to be changed before merging. A published flow keeps its
  name.

## The manifest

```yaml
name: my_review
version: 0.1.0
description: Reviews a change twice, the second time with fresh eyes.
repo: octocat/flow-my-review
ref: v0.1.0
commit: 0123456789abcdef0123456789abcdef01234567
subdir: my_review
license: MIT
dependencies:
  humanize1: ">=0.1.0,<0.2.0"
```

| Key | Required | Value |
| --- | :-: | --- |
| `name` | yes | The flow's name; equal to the `<name>` directory. |
| `version` | yes | A SemVer 2.0.0 version without build metadata (`1.2.0`, `2.0.0-rc.1`); equal to the `<version>` directory. |
| `description` | yes | One line, at most 200 characters, saying what the flow does. |
| `repo` | yes | The public GitHub repository holding the flow, as `owner/repo`. |
| `ref` | yes | The tag the release is cut from. A branch or a commit is accepted, a tag is expected. |
| `commit` | yes | The full 40-character commit `ref` resolved to when the version was submitted. |
| `subdir` | no | The directory holding the flow: a package with `__init__.py`, or a `<name>.py` file. Left out, the root of the repository. |
| `license` | yes | The flow's license as an [SPDX identifier](https://spdx.org/licenses/), spelled as SPDX spells it: `Apache-2.0`, `MIT`, `GPL-3.0-only`. |
| `dependencies` | no | Other flows of this index the flow calls, each with a version range; see [Dependencies](#dependencies). |

No other key is accepted. Quote a value YAML would read as something other than a string.
[schema/flow.schema.json](schema/flow.schema.json) is the same contract as a JSON Schema, for
editors.

A version directory holds `flow.yaml` and nothing else, and `flows/<name>/` holds version
directories and nothing else.

## Pinning to a commit

hmz installs `commit`, never `ref`: a version is the same code for everybody who installs it,
whatever later happens to the tag. `ref` says where the commit came from, and CI checks that it
resolved to `commit` when the pull request was opened.

A weekly run checks every manifest again. A repository that went private or away, or a tag
that has moved, fails it, and maintainers look into it: a moved tag on a published version is
treated as a possible compromise until shown otherwise. Do not move a tag you released; tag a
new version.

## Dependencies

A flow that calls another flow of this index (`load("humanize1:rlcr")`) names it, with the
versions it works with:

```yaml
dependencies:
  humanize1: ">=0.1.0,<0.2.0"
```

A range is comparisons (`<`, `<=`, `>`, `>=`, `==`, `!=`) of full versions, joined by commas,
all of which must hold; a bare version means `==`. Each dependency must be a flow in this
index with at least one version in range, and dependencies may not form a cycle. The flows
built into humanize are always there and are never listed.

## What CI checks

Every pull request runs [validate](.github/workflows/validate.yml), and its `index-ok` check
sums up the rest:

| Job | Checks | On |
| --- | --- | --- |
| lint | YAML style (yamllint), workflows (actionlint, zizmor), scripts (ruff) | everything |
| validate | the schema; names and versions match their directories; SemVer; reserved names; nothing else in `flows/`; licenses; dependencies; published versions untouched | every manifest |
| verify | `repo` is public; `ref` resolves to `commit`; `subdir` holds the flow at that commit | the manifests you added or changed |
| import-check | hmz installs from `humanize`'s main branch, loads the flow from `subdir` at `commit`, and lists the flows it offers | the manifests you added or changed |

`import-check` runs your flow's module, so it runs isolated: no secrets, a token that can do
nothing, no credentials kept. A [labeler](.github/workflows/labeler.yml) marks each pull
request `new-flow`, `new-version`, `modify` or `infra`.

## Review and merge

- Maintainers ([CODEOWNERS](.github/CODEOWNERS)) review every pull request, and merge it once
  one of them has approved it and `index-ok` is green.
- A new flow gets the closest look: its code at `commit`, what it runs, what it fetches, and
  what its prompts ask agents to do. A new version is reviewed as the difference from the
  previous one (`https://github.com/<owner>/<repo>/compare/<old commit>...<new commit>`).
- A pull request is refused if the flow hides what it does, sends data anywhere its README
  does not say, downloads and runs code it does not pin, or breaks its license or anyone
  else's.
- Pull requests are squash-merged.

## New versions, withdrawals and removals

- **A new version** is a new directory, `flows/<name>/<new version>/`, in a pull request of
  its own. hmz tells users who installed an older version that it exists.
- **A published version is never edited.** CI refuses a change to, or the deletion of, a
  version directory already on `main`, unless a maintainer labels the pull request
  `allow-modify`. Correct a mistake by publishing a new version.
- **Withdrawing a version** you published, because it is broken or unsafe: open a pull request
  deleting its directory and say why; a maintainer applies `allow-modify`. hmz stops offering
  it. Copies already installed stay where they are, so publish a fixed version too.
- **Removal by maintainers.** Maintainers remove every version of a flow, without notice,
  when it is malicious or compromised, when its repository disappears or stops being public,
  when a published tag is moved, or when it breaks a license. The name may then be withheld
  from reuse. Report such a flow as [SECURITY.md](SECURITY.md) says, not in a public issue.

## Changing the index itself

The schema, scripts, workflows and documentation are changed like any code: open an issue first
for anything large, follow Conventional Commits, and run what CI runs:

```sh
uv run .github/scripts/validate.py
uvx yamllint --strict .
uvx --from actionlint-py actionlint
uvx zizmor .
uvx ruff check .github/scripts && uvx ruff format --check .github/scripts
```

Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).
