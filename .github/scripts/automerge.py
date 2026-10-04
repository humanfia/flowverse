# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pyyaml==6.0.3",
#     "semver==3.1.0",
# ]
# ///
"""Merges a pull request without review if, and only if, it is a safe version bump.

    uv run .github/scripts/automerge.py --pr 45                          # what it would do
    uv run .github/scripts/automerge.py --event "$GITHUB_EVENT_PATH" --apply   # CI does it
    uv run .github/scripts/automerge.py --fixture .github/scripts/fixtures/automerge.yaml

A pull request is merged without review when all of these hold, and waits for one otherwise:

1. Every file it changes is a manifest it adds, flows/<flow>/<version>/flow.yaml or
   flows/<owner>/<flow>/<version>/flow.yaml: nothing modified, deleted or renamed, and
   nothing else.
2. Each of those flows already has a version on the base branch, and the one added is newer
   than every one of them and was never there before: a version withdrawn is not published
   again without review.
3. Each manifest added is the same as the newest version's on the base branch in every key but
   version, ref and commit.
4. Every job of the validate workflow passed on the pull request's head commit, installing each
   version added with hmz among them.

Everything is read through the GitHub API with gh, at the head commit validate passed on and
against the base branch as it is now: nothing of the pull request is checked out or run. The
merge is a squash GitHub refuses if the head has moved since.

--event reads a workflow_run event of validate, finding the pull request it ran for, a fork's
too; --pr names one, and decides on validate's newest finished run of its head. Without --apply
it only says what it would do. With it, it merges and labels the pull request auto-merged, and
says why it did or did not in one comment, kept up to date. --fixture decides from files of the
facts as they would be read from GitHub, and fails if any case is decided otherwise than it
expects.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any

import semver
import yaml

WORKFLOW = ".github/workflows/validate.yml"
#: The jobs of validate that run whenever a manifest is added; each manifest also has its own
#: `install <flow> <version>`.
JOBS = ("lint", "validate", "verify", "index-ok")
#: What a version bump may change.
BUMPED = frozenset({"version", "ref", "commit"})
LABEL = "auto-merged"
#: What starts the one comment this leaves on a pull request.
MARK = "<!-- flowverse-automerge -->"
BOT = "github-actions[bot]"
#: More files than GitHub lists in a comparison.
MANY = 300
#: How many of the other files a pull request changes are named.
NAMED = 5
MANIFEST = re.compile(r"flows/((?:[a-z0-9-]+/)?[a-z][a-z0-9_]*)/([^/]+)/flow\.yaml")
RULE = (
    "Only a pull request that adds new versions of flows already listed, each never published "
    "before and the same as the newest version but for `version`, `ref` and `commit`, is "
    "merged without review once validate passes: see [Review and merge]"
    "(https://github.com/{repo}/blob/HEAD/CONTRIBUTING.md#review-and-merge)."
)
#: A key a manifest does not have, which no value it could have equals.
ABSENT = object()

type Facts = dict[str, Any]


def release(path: str) -> tuple[str, semver.Version] | None:
    """The flow, by its name in the index, and the version a manifest's path says it is."""
    found = MANIFEST.fullmatch(path)
    if not found or not semver.Version.is_valid(found[2]):
        return None
    version = semver.Version.parse(found[2])
    return None if version.build else (found[1], version)


def versions(paths: dict[str, Any], flow: str) -> dict[semver.Version, str]:
    """Each version of one flow among some paths, with its manifest's path."""
    return {
        place[1]: path
        for path in paths
        if (place := release(path)) is not None and place[0] == flow
    }


def read(text: str | None) -> dict[str, Any] | None:
    try:
        said = yaml.safe_load(text or "")
    except yaml.YAMLError:
        return None
    return said if isinstance(said, dict) else None


def decide(facts: Facts) -> tuple[list[str], list[str]]:
    """Why a pull request is not a safe version bump, and the versions it adds.

    Args:
      facts: What GitHub says, as :func:`gather` reads it: `pull` (number, state, draft, base,
        head), `default` (the default branch), `run` (validate's: event, conclusion, head_sha,
        path), `jobs` (each of its jobs' conclusion, by name), `files` (each file changed at
        the run's head against the base branch, by its status), `truncated` (whether there
        were too many to list), `base` (each manifest on the base branch, by path, with its
        text where it was read), `head` (the text of each manifest added, by path) and
        `withdrawn` (each manifest added that the base branch had before, and has no more).

    Returns:
      Nothing in the first list for a pull request to merge, and why not otherwise; and each
      `<flow> <version>` it adds.
    """
    why: list[str] = []
    bumps: list[str] = []
    pull, run = facts["pull"], facts["run"]
    sha = run["head_sha"]
    if pull["state"] != "open":
        why.append("it is not open")
    if pull["draft"]:
        why.append("it is a draft")
    if pull["base"] != facts["default"]:
        why.append(f"it is not into {facts['default']}")
    if pull["head"] != sha:
        why.append(f"its head is {pull['head'][:7]}, and validate ran on {sha[:7]}")
    if run["event"] != "pull_request" or run["path"] != WORKFLOW:
        why.append(f"the run is not {WORKFLOW} on the pull request")
    if run["conclusion"] != "success":
        why.append(
            f"validate did not pass on {sha[:7]}: {run['conclusion'] or 'not run'}"
        )
    if facts.get("truncated"):
        why.append(f"it changes more than {MANY} files")
    if not facts["files"]:
        why.append("it changes nothing")
    jobs: dict[str, str] = facts["jobs"]
    needed = set(JOBS)
    others: list[str] = []
    for path, status in sorted(facts["files"].items()):
        place = release(path)
        if status != "added" or place is None:
            others.append(f"`{path}` is {status}")
            continue
        flow, version = place
        needed.add(f"install {flow} {version}")
        if path in facts["withdrawn"]:
            why.append(f"`{flow}` {version} was published before, and withdrawn")
            continue
        published = versions(facts["base"], flow)
        if not published:
            why.append(f"`{flow}` is a new flow")
            continue
        top = max(published)
        if version <= top:
            why.append(
                f"`{flow}` {version} is not newer than {top}, its newest version"
            )
            continue
        new, old = read(facts["head"].get(path)), read(facts["base"][published[top]])
        if new is None or old is None:
            why.append(f"`{path}` or {top}'s manifest does not read")
            continue
        changed = sorted(
            key
            for key in new.keys() | old.keys()
            if key not in BUMPED and new.get(key, ABSENT) != old.get(key, ABSENT)
        )
        if changed:
            why.append(f"`{flow}` {version} changes {', '.join(changed)} from {top}")
            continue
        bumps.append(f"{flow} {version}")
    if others:
        more = f", and {len(others) - NAMED} more" if len(others) > NAMED else ""
        why.append(
            f"{', '.join(others[:NAMED])}{more}: only new `flows/…/<version>/flow.yaml` "
            "files are merged without review"
        )
    failed = sorted(
        f"{name} ({said})" for name, said in jobs.items() if said != "success"
    )
    if failed:
        why.append(f"validate's jobs did not all pass: {', '.join(failed)}")
    missing = sorted(needed - jobs.keys())
    if missing and run["conclusion"] == "success":
        why.append(f"validate ran no {', '.join(missing)}")
    return why, bumps


def gh(*args: str) -> Any:
    """`gh api`, decoded: GET unless the arguments say otherwise."""
    done = subprocess.run(
        ["gh", "api", *args], capture_output=True, text=True, check=False
    )
    if done.returncode:
        raise RuntimeError(f"gh api {args[-1]}: {done.stderr.strip()}")
    return json.loads(done.stdout) if done.stdout.strip() else None


def every(path: str, key: str | None = None) -> list[Any]:
    """Every item of a listing, across its pages."""
    pages = gh("--paginate", "--slurp", path)
    return [one for page in pages for one in (page[key] if key else page)]


def text(repo: str, path: str, at: str) -> str:
    done = subprocess.run(
        [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github.raw+json",
            f"repos/{repo}/contents/{urllib.parse.quote(path)}?ref={at}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def gather(repo: str, pull: dict[str, Any], run: dict[str, Any]) -> Facts:
    """What GitHub says of a pull request and one run of validate, as :func:`decide` takes it."""
    sha = run["head_sha"]
    branch = urllib.parse.quote(pull["base"]["ref"], safe="")
    base = gh(f"repos/{repo}/branches/{branch}")["commit"]["sha"]
    compared = gh(f"repos/{repo}/compare/{base}...{sha}")
    files = {one["filename"]: one["status"] for one in compared["files"]}
    tree = gh(f"repos/{repo}/git/trees/{base}?recursive=1")
    listed: dict[str, str | None] = {
        one["path"]: None
        for one in tree["tree"]
        if one["type"] == "blob" and release(one["path"]) is not None
    }
    added: dict[str, str] = {}
    withdrawn: list[str] = []
    for path, status in files.items():
        place = release(path)
        if status != "added" or place is None:
            continue
        added[path] = text(repo, path, sha)
        # Not on the base branch now, yet in its history: published once, and withdrawn.
        if path not in listed and gh(
            f"repos/{repo}/commits?sha={base}&path={urllib.parse.quote(path)}&per_page=1"
        ):
            withdrawn.append(path)
        published = versions(listed, place[0])
        if published:
            newest = published[max(published)]
            listed[newest] = listed[newest] or text(repo, newest, base)
    jobs = (
        {
            one["name"]: one["conclusion"]
            for one in every(
                f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100",
                "jobs",
            )
        }
        if run.get("id")
        else {}
    )
    return {
        "pull": {
            "number": pull["number"],
            "state": pull["state"],
            "draft": pull["draft"],
            "base": pull["base"]["ref"],
            "head": pull["head"]["sha"],
        },
        "default": gh(f"repos/{repo}")["default_branch"],
        "run": {
            key: run.get(key) for key in ("event", "conclusion", "head_sha", "path")
        },
        "jobs": jobs,
        "files": files,
        "truncated": len(files) >= MANY or tree["truncated"],
        "base": listed,
        "head": added,
        "withdrawn": withdrawn,
    }


def pulls_of(repo: str, run: dict[str, Any]) -> list[int]:
    """The open pull requests a run of validate checked the head of.

    GitHub names them in the event only for branches of this repository; a fork's is looked up
    by the fork's owner and branch.
    """
    named = [one["number"] for one in run.get("pull_requests") or []]
    if named:
        return named
    fork = run.get("head_repository") or {}
    if not fork.get("full_name"):
        return []
    head = urllib.parse.quote(
        f"{fork['owner']['login']}:{run['head_branch']}", safe=":"
    )
    return [
        one["number"]
        for one in every(f"repos/{repo}/pulls?state=open&per_page=100&head={head}")
        if one["head"]["sha"] == run["head_sha"]
        and (one["head"]["repo"] or {}).get("full_name") == fork["full_name"]
    ]


def newest_run(repo: str, sha: str) -> dict[str, Any]:
    """validate's newest finished run on a pull request's head, or one that says there is none."""
    runs = gh(
        f"repos/{repo}/actions/workflows/{Path(WORKFLOW).name}/runs"
        f"?head_sha={sha}&event=pull_request&status=completed&per_page=1"
    )["workflow_runs"]
    if runs:
        return runs[0]
    return {
        "event": "pull_request",
        "conclusion": None,
        "head_sha": sha,
        "path": WORKFLOW,
    }


def say(repo: str, number: int, body: str) -> None:
    """Leaves one comment on a pull request, or brings the one left before up to date."""
    body = f"{MARK}\n{body}"
    mine = [
        one
        for one in every(f"repos/{repo}/issues/{number}/comments?per_page=100")
        if one["user"]["login"] == BOT and one["body"].startswith(MARK)
    ]
    if not mine:
        gh("-X", "POST", f"repos/{repo}/issues/{number}/comments", "-f", f"body={body}")
    elif mine[-1]["body"] != body:
        gh(
            "-X",
            "PATCH",
            f"repos/{repo}/issues/comments/{mine[-1]['id']}",
            "-f",
            f"body={body}",
        )


def handle(repo: str, number: int, run: dict[str, Any], apply: bool) -> int:
    pull = gh(f"repos/{repo}/pulls/{number}")
    sha = run["head_sha"]
    if pull["state"] != "open":
        print(f"#{number} is {pull['state']}")
        return 0
    if pull["head"]["sha"] != sha:
        print(f"#{number} is at {pull['head']['sha'][:7]} now; its own run decides")
        return 0
    why, bumps = decide(gather(repo, pull, run))
    if why:
        print(f"#{number}: waits for review, as")
        print("".join(f"  - {one}\n" for one in why), end="")
        body = (
            "Not merged automatically; a maintainer will review it, as\n\n"
            + "".join(f"- {one}\n" for one in why)
            + "\n"
            + RULE.format(repo=repo)
        )
        if apply:
            say(repo, number, body)
        return 0
    adds = ", ".join(f"`{one}`" for one in bumps)
    print(f"#{number}: merges, adding {adds}")
    if not apply:
        return 0
    merged = subprocess.run(
        ["gh", "pr", "merge", str(number), "--repo", repo, "--squash"]
        + ["--match-head-commit", sha],
        capture_output=True,
        text=True,
        check=False,
    )
    if merged.returncode:
        said = merged.stderr.strip()
        print(f"::error::#{number} could not be merged: {said}")
        say(repo, number, f"A safe version bump, which GitHub would not merge: {said}")
        return 1
    try:
        gh(
            "-X",
            "POST",
            f"repos/{repo}/issues/{number}/labels",
            "-f",
            f"labels[]={LABEL}",
        )
    except RuntimeError as error:
        print(f"::warning::#{number} was merged, and not labelled {LABEL}: {error}")
    say(
        repo,
        number,
        f"Merged automatically: it adds {adds}, each a new version of a flow already listed, "
        "never published before, and the same as its newest version but for `version`, `ref` "
        f"and `commit`, and every job of validate passed on {sha}, installing it with hmz "
        "among them.",
    )
    return 0


def fixtures(paths: list[str]) -> int:
    """Decides each case of some fixture files, and says whether each went as it expects."""
    wrong = 0
    for path in paths:
        said = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        for case in said["cases"]:
            expect, because = case.pop("expect", "wait"), case.pop("because", "")
            name = case.pop("name")
            why, _ = decide(said["facts"] | case)
            got = "wait" if why else "merge"
            ok = got == expect and (not because or any(because in one for one in why))
            wrong += not ok
            print(f"{'ok' if ok else 'WRONG'}  {name}: {got}")
            print("".join(f"      {one}\n" for one in why), end="")
    return 1 if wrong else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--event", help="a workflow_run event of validate, as JSON")
    which.add_argument("--pr", type=int, help="a pull request, by number")
    which.add_argument("--fixture", nargs="+", help="fixture files to decide")
    parser.add_argument(
        "--repo", default=os.environ.get("GITHUB_REPOSITORY", "humanfia/flowverse")
    )
    parser.add_argument("--apply", action="store_true", help="merge, label and comment")
    args = parser.parse_args()

    if args.fixture:
        return fixtures(args.fixture)
    if args.pr:
        sha = gh(f"repos/{args.repo}/pulls/{args.pr}")["head"]["sha"]
        return handle(args.repo, args.pr, newest_run(args.repo, sha), args.apply)
    run = json.loads(Path(args.event).read_text(encoding="utf-8"))["workflow_run"]
    numbers = pulls_of(args.repo, run)
    if not numbers:
        print(f"no open pull request is at {run['head_sha']}")
    return max((handle(args.repo, one, run, args.apply) for one in numbers), default=0)


if __name__ == "__main__":
    sys.exit(main())
