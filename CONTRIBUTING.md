# Contributing to flowverse

Thank you for sharing a flow. This index lists flows; it does not hold them. You publish your
flow in a GitHub repository of your own, then open a pull request here adding one small
manifest that says where a released version of it is. Maintainers review it, CI checks it, and
once it is merged every hmz can install it. A later version that changes nothing but where it
is, at the `v<version>` tag you released it with, merges itself once CI passes.

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

1. **Publish the flow in its own public GitHub repository.** Keep the flow in a `<flow>/`
   directory, with its tests, README and LICENSE beside it at the root:

   ```text
   my-review-flow/
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

2. **Test it with hmz** by path, `hmz exec -f ./my_review …` (`-f` takes a path when it
   starts with `.`, `/` or `~`, and a flow's name otherwise), and with its tests.

3. **Tag a release** with a [SemVer 2.0.0](https://semver.org) version and push the tag:

   ```sh
   git tag v0.1.0 && git push origin v0.1.0
   ```

4. **Add the manifest** at `flows/<owner>/<flow>/<version>/flow.yaml` in a fork of this
   repository, `<owner>` being the GitHub user or organization that owns the flow's
   repository, in lowercase ([Naming](#naming)), and the manifest [as below](#the-manifest).
   The commit your tag points at:

   ```sh
   gh api repos/<owner>/<repo>/commits/v0.1.0 --jq .sha
   ```

5. **Check it** the way CI will ([uv](https://docs.astral.sh/uv/) is the one requirement):

   ```sh
   uv run .github/scripts/validate.py --base origin/main --network
   ```

6. **Open a pull request** with one flow and one version in it, titled in the
   [Conventional Commits](https://www.conventionalcommits.org) style:
   `feat(octocat/my_review): add octocat/my_review 0.1.0`, or `feat(octocat/my_review): 0.2.0`
   for a later version. Fill in the checklist the template gives you.

## Naming

- **Your flows are in your namespace.** A flow is listed at `flows/<owner>/<flow>/`, where
  `<owner>` is the GitHub user or organization that owns its repository, in lowercase: a flow
  in `octocat/my-review-flow` is at `flows/octocat/my_review/`, and nowhere else. CI refuses a
  flow whose repository another owner has.
- **Only Humanfia's flows are listed by name alone**, at `flows/<flow>/`: those whose
  repository the [humanfia](https://github.com/humanfia) organization owns. There is no
  `flows/humanfia/`.
- A directory under `flows/` is one or the other: a flow of Humanfia's, holding versions, or
  an owner's namespace, holding flows. An owner whose name a flow of Humanfia's already has
  cannot list flows here, and the other way round; ask the maintainers.
- A flow's name is lowercase letters, digits and underscores, starting with a letter
  (`[a-z][a-z0-9_]*`), at most 64 characters. It is the `<flow>` directory and the manifest's
  `name`.
- hmz calls a flow what its directory says: `aot` for a flow of Humanfia's, `octocat/my_review`
  for yours, and `octocat/my_review:strict` for one of the flows inside it. A flowverse that
  somebody adds to hmz themselves has its flows called `@<flowverse>/<flow>` and
  `@<flowverse>/<owner>/<flow>`.
- These names belong to the flows built into humanize and are never accepted by name alone:
  `chat`, `ralph_loop`, `goal`, `flame_chase`, `stateful_ralph`, `continue_loop`, `rlar`.
  `octocat/chat` is fine.
- The names in your namespace are yours. A name that impersonates another project or claims an
  affiliation it does not have is refused, as is one taken to hold it rather than to publish a
  flow. Maintainers may ask for a name to be changed before merging; a published flow keeps
  its name.

## The manifest

```yaml
name: my_review
version: 0.1.0
description: Reviews a change twice, the second time with fresh eyes.
repo: octocat/my-review-flow
ref: v0.1.0
commit: 0123456789abcdef0123456789abcdef01234567
subdir: my_review
license: MIT
dependencies:
  humanize1: ">=0.1.0,<0.2.0"
```

| Key | Required | Value |
| --- | :-: | --- |
| `name` | yes | The flow's name; equal to the `<flow>` directory. |
| `version` | yes | A SemVer 2.0.0 version without build metadata (`1.2.0`, `2.0.0-rc.1`); equal to the `<version>` directory. |
| `description` | yes | One line, at most 200 characters, saying what the flow does. |
| `repo` | yes | The public GitHub repository holding the flow, as `owner/repo`; its owner is the `<owner>` directory (`humanfia` for a flow listed by name alone). |
| `ref` | yes | The tag the release is cut from. A branch or a commit is accepted, a tag is expected. |
| `commit` | yes | The full 40-character commit `ref` resolved to when the version was submitted. |
| `subdir` | no | The directory holding the flow: a package with `__init__.py`, or a `<flow>.py` file. Left out, the root of the repository. |
| `license` | yes | The flow's license as an [SPDX identifier](https://spdx.org/licenses/), spelled as SPDX spells it: `Apache-2.0`, `MIT`, `GPL-3.0-only`. |
| `dependencies` | no | Other flows of this index the flow calls, each by its name here with a version range; see [Dependencies](#dependencies). |

No other key is accepted. Quote a value YAML would read as something other than a string.
[schema/flow.schema.json](schema/flow.schema.json) is the same contract as a JSON Schema, for
editors.

A version directory holds `flow.yaml` and nothing else, a flow's directory holds version
directories and nothing else, and a namespace holds flow directories and nothing else.

## Pinning to a commit

hmz installs `commit`, never `ref`: a version is the same code for everybody who installs it,
whatever later happens to the tag. `ref` says where the commit came from, and CI checks that it
resolved to `commit` when the pull request was opened.

A weekly run checks every manifest again. A repository that went private or away, or a tag
that has moved, fails it, and maintainers look into it: a moved tag on a published version is
treated as a possible compromise until shown otherwise. Do not move a tag you released; tag a
new version.

## Dependencies

A flow that calls another flow of this index (`load("humanize1:rlcr")`) names it as this
index does, `humanize1` or `octocat/my_review`, with the versions it works with:

```yaml
dependencies:
  humanize1: ">=0.1.0,<0.2.0"
  octocat/my_review: ">=1.0.0,<2.0.0"
```

A range is comparisons (`<`, `<=`, `>`, `>=`, `==`, `!=`) of full versions, joined by commas,
all of which must hold; a bare version means `==`. Each dependency must be a flow in this
index with at least one version in range, and no flow may come to depend on itself, in any of
its versions. The flows built into humanize are always there and are never listed.

## What CI checks

Every pull request runs [validate](.github/workflows/validate.yml), and its `index-ok` check
sums up the rest:

| Job | Checks | On |
| --- | --- | --- |
| lint | YAML style (yamllint), workflows (actionlint, zizmor), scripts (ruff, and automerge's fixtures) | everything |
| validate | the schema; names and versions match their directories; SemVer; reserved names; who owns each repository; nothing else in `flows/`; licenses; dependencies; published versions untouched | every manifest |
| verify | `repo` is public; `ref` resolves to `commit`; `subdir` holds the flow at that commit | the manifests you added or changed |
| install | hmz, from `humanize`'s main branch, adds the pull request as a flowverse, installs the version with every flow it depends on, loads it and lists the flows it offers | the manifests you added or changed |

`install` runs your flow's module, so it runs isolated: no secrets, a token that can do
nothing, no credentials kept. A weekly run checks every manifest again, `install` included. A
[labeler](.github/workflows/labeler.yml) marks each pull request `new-flow`, `new-version`,
`modify` or `infra`.

## Review and merge

- **A version bump merges itself.** [automerge](.github/workflows/automerge.yml) squash-merges
  a pull request, and labels it `auto-merged`, when:
  1. every file it changes is a `flow.yaml` it adds in a new version directory: nothing
     modified, deleted or renamed, and nothing else;
  2. each of those flows already has a version on `main`, and the one added is newer than all
     of them and was never published before: a withdrawn version put back waits for review;
  3. each manifest added is the same as the newest version's in every key but `version`, `ref`
     and `commit`;
  4. each version added is one its repository's owner released after the newest: `ref` is the
     repository's own tag `v<version>` (or `<version>`), `commit` is what that tag points at,
     and that commit comes after the newest version's (GitHub compares it as `ahead`). A
     branch, a commit by itself, an older commit or a tag of another version waits for review;
  5. every job of validate passed on the pull request's head commit, `install` among them.

  It says in a comment why it did or did not. It reads the pull request through GitHub's API
  and runs none of it, and GitHub refuses the merge if anything was pushed after the checks
  ran.
- Maintainers ([CODEOWNERS](.github/CODEOWNERS)) review every other pull request, and merge it
  once one of them has approved it and `index-ok` is green.
- A new flow gets the closest look: its code at `commit`, what it runs, what it fetches, and
  what its prompts ask agents to do. A new version is reviewed as the difference from the
  previous one (`https://github.com/<owner>/<repo>/compare/<old commit>...<new commit>`).
- A pull request is refused if the flow hides what it does, sends data anywhere its README
  does not say, downloads and runs code it does not pin, or breaks its license or anyone
  else's.
- Pull requests are squash-merged.

## New versions, withdrawals and removals

- **A new version** is a new directory beside the others, `flows/<owner>/<flow>/<new
  version>/`, in a pull request of its own; one that changes nothing but `version`, `ref` and
  `commit`, at its own tag `v<version>` on a commit after the newest version's,
  [merges itself](#review-and-merge). hmz tells users who installed an older version
  that it exists.
- **A published version is never edited.** CI refuses a change to, or the deletion of, a
  version directory already on `main`, unless a maintainer labels the pull request
  `allow-modify`. The label covers the commits the maintainer reviewed: applying it starts the
  run that lets the change through, so it is applied last, and a push takes it off until the
  new commits are reviewed. Correct a mistake by publishing a new version.
- **Withdrawing a version** you published, because it is broken or unsafe: open a pull request
  deleting its directory and say why; a maintainer applies `allow-modify`. hmz stops offering
  it. Copies already installed stay where they are, so publish a fixed version too. Releases of
  other flows that only that version satisfies could no longer be installed, so CI refuses the
  withdrawal unless they are withdrawn with it.
- **Removal by maintainers.** Maintainers remove every version of a flow, without notice,
  when it is malicious or compromised, when its repository disappears or stops being public,
  when a published tag is moved, or when it breaks a license. The name may then be withheld
  from reuse. Report such a flow as [SECURITY.md](SECURITY.md) says, not in a public issue.

## Changing the index itself

The schema, scripts, workflows and documentation are changed like any code: open an issue first
for anything large, follow Conventional Commits, and run what CI runs:

```sh
uv run .github/scripts/validate.py
uv run .github/scripts/automerge.py --fixture .github/scripts/fixtures/automerge.yaml
uvx yamllint --strict .
uvx --from actionlint-py actionlint
uvx zizmor .
uvx ruff check .github/scripts && uvx ruff format --check .github/scripts
```

`uv run .github/scripts/automerge.py --pr <number>` says what automerge would do with a pull
request, and why, without doing it.

Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).
