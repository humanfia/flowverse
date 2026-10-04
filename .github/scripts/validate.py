# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "jsonschema==4.26.0",
#     "license-expression==30.4.4",
#     "pyyaml==6.0.3",
#     "semver==3.1.0",
# ]
# ///
"""Checks the index: every manifest under flows/, and optionally what each one points at.

    uv run .github/scripts/validate.py                               # every manifest, offline
    uv run .github/scripts/validate.py --base origin/main            # + published versions kept
    uv run .github/scripts/validate.py --base origin/main --network  # + changed ones vs GitHub

Offline checks run on every manifest: the schema, the directory names, SemVer, reserved names,
licenses, and dependencies, down to whether hmz could install each release. With --base, every
version directory that exists at that revision must be left exactly as it was, unless
--allow-modify is given or, in GitHub Actions, a maintainer labelled the pull request
`allow-modify`; and only the manifests added or changed since --base are "in scope". With
--network, the in-scope manifests (all of them without --base) are checked against the GitHub
API: the repository is public, `ref` resolves to `commit` and holds it, and `subdir` holds the
flow at that commit. GITHUB_TOKEN or GH_TOKEN is sent when set; it is only used to read.

In GitHub Actions the in-scope manifests are written to $GITHUB_OUTPUT as `manifests`, a JSON
list of {path, name, version, repo, commit, subdir} objects.
"""

from __future__ import annotations

import argparse
import graphlib
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import jsonschema
import semver
import yaml
from license_expression import LicenseSymbol, get_spdx_licensing

ROOT = Path(__file__).resolve().parents[2]
FLOWS = "flows"
MANIFEST = "flow.yaml"
SCHEMA = ROOT / "schema" / "flow.schema.json"
NAME = re.compile(r"[a-z][a-z0-9_]*")
#: What no value may hold: control characters and line separators. The schema's patterns,
#: run by Python's `re`, let a trailing newline through.
BREAK = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
#: The flows built into humanize. An index flow of one of these names would shadow a builtin.
RESERVED = frozenset(
    {
        "chat",
        "ralph_loop",
        "goal",
        "flame_chase",
        "stateful_ralph",
        "continue_loop",
        "rlar",
    }
)
ALLOW_MODIFY = "allow-modify"
API = "https://api.github.com"
#: What git says `before` is for a push that created the branch.
NO_COMMIT = "0" * 40
#: Errors asking GitHub can end in, once retried.
UNREACHABLE = (OSError, ValueError, http.client.HTTPException)

type Release = tuple[str, str]  # (name, version)


class Report:
    """Errors and warnings, by file, written as annotations inside GitHub Actions."""

    def __init__(self) -> None:
        self.errors = 0

    def _say(self, level: str, where: str, message: str) -> None:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            escapes = {"%": "%25", "\r": "%0D", "\n": "%0A"}
            message = message.translate(str.maketrans(escapes))
            where = where.translate(str.maketrans({**escapes, ":": "%3A", ",": "%2C"}))
            print(f"::{level} file={where}::{message}")
        else:
            print(f"{level}: {where}: {message}")

    def error(self, where: str, message: str) -> None:
        self.errors += 1
        self._say("error", where, message)

    def warning(self, where: str, message: str) -> None:
        self._say("warning", where, message)


class _Loader(yaml.SafeLoader):
    """The safe loader, refusing a key written twice rather than keeping the last one."""


def _mapping(loader: _Loader, node: yaml.MappingNode, deep: bool = False) -> Any:
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            continue  # refused by the schema, or as unhashable by construct_mapping
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"the key {key!r} is written twice", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep)


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def git(*args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    )
    return done.stdout


def version_of(text: str) -> semver.Version | None:
    """The version a directory is named, if it is SemVer 2.0.0 without build metadata."""
    try:
        version = semver.Version.parse(text)
    except ValueError:
        return None
    return None if version.build else version


def layout(report: Report) -> dict[str, dict[str, Path]]:
    """Each manifest by flow and version, once flows/ is checked to hold nothing else."""
    found: dict[str, dict[str, Path]] = {}
    top = ROOT / FLOWS
    if not top.is_dir():
        return found
    for flow in sorted(top.iterdir()):
        where = rel(flow)
        if flow.is_symlink() or not flow.is_dir():
            report.error(
                where, "flows/ holds flow directories, flows/<name>/, and nothing else"
            )
            continue
        if not NAME.fullmatch(flow.name):
            report.error(
                where,
                f"{flow.name!r} is not a flow name: lowercase letters, digits and "
                "underscores, starting with a letter",
            )
            continue
        if flow.name in RESERVED:
            report.error(
                where, f"{flow.name!r} is a flow built into humanize, and is reserved"
            )
            continue
        for release in sorted(flow.iterdir()):
            at = rel(release)
            if release.is_symlink() or not release.is_dir():
                report.error(at, f"{where}/ holds version directories and nothing else")
                continue
            if version_of(release.name) is None:
                report.error(
                    at,
                    f"{release.name!r} is not a version: SemVer 2.0.0 such as 1.2.0 or "
                    "2.0.0-rc.1, without build metadata",
                )
                continue
            held = sorted(one.name for one in release.iterdir())
            for extra in held:
                if extra != MANIFEST:
                    report.error(
                        f"{at}/{extra}",
                        f"a version directory holds its {MANIFEST} and nothing else; the "
                        "flow itself lives in its own repository",
                    )
            manifest = release / MANIFEST
            if manifest.is_symlink() or not manifest.is_file():
                report.error(at, f"a version directory holds a {MANIFEST} file")
                continue
            found.setdefault(flow.name, {})[release.name] = manifest
    return found


def explain(error: jsonschema.ValidationError) -> str:
    """One schema error, said plainly."""
    field = ".".join(str(part) for part in error.absolute_path) or "the manifest"
    if error.validator == "additionalProperties" and not error.absolute_path:
        known = error.schema["properties"]
        unknown = sorted(str(key) for key in error.instance if key not in known)
        return (
            f"unknown key(s): {', '.join(unknown)}; a manifest holds only "
            + ", ".join(known)
        )
    if error.validator == "pattern":
        said = error.schema.get("description")
        return f"{field}: {error.instance!r} is not valid" + (
            f": {said}" if said else ""
        )
    if error.validator == "type" and error.validator_value == "string":
        return f"{field}: {error.instance!r} is not a string; quote it"
    return f"{field}: {error.message}"


def check_manifest(
    report: Report, path: Path, flow: str, version: str, schema: Any, licensing: Any
) -> dict[str, Any] | None:
    """The manifest, if it reads, fits the schema, and matches its directories."""
    where = rel(path)
    try:
        manifest = yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        report.error(where, f"does not read as YAML: {error}")
        return None
    if not isinstance(manifest, dict):
        report.error(where, "a manifest is a mapping of keys to values")
        return None
    deps = manifest.get("dependencies")
    texts = [*manifest.values(), *(deps.values() if isinstance(deps, dict) else [])]
    if any(isinstance(text, str) and BREAK.search(text) for text in texts):
        report.error(where, "a value holds a line break or another control character")
        return None
    errors = sorted(
        schema.iter_errors(manifest),
        key=lambda one: [str(part) for part in one.absolute_path],
    )
    for error in errors:
        report.error(where, explain(error))
    if errors:
        return None
    ok = True
    if manifest["name"] != flow:
        report.error(where, f"name is {manifest['name']!r}, in flows/{flow}/")
        ok = False
    if manifest["version"] != version:
        report.error(
            where, f"version is {manifest['version']!r}, in flows/{flow}/{version}/"
        )
        ok = False
    said = manifest["license"]
    checked = licensing.validate(said)
    if checked.errors or not isinstance(licensing.parse(said), LicenseSymbol):
        report.error(
            where,
            f"license: {said!r} is not an SPDX license identifier (spdx.org/licenses)",
        )
        ok = False
    elif checked.normalized_expression != said:
        report.error(
            where,
            f"license: {said!r} is spelled {checked.normalized_expression!r} by SPDX",
        )
        ok = False
    return manifest if ok else None


def read(
    report: Report,
) -> tuple[dict[str, dict[str, Path]], dict[Path, dict[str, Any]]]:
    """Every manifest's place, and every manifest that passes the offline checks."""
    schema = jsonschema.Draft202012Validator(
        json.loads(SCHEMA.read_text(encoding="utf-8"))
    )
    licensing = get_spdx_licensing()
    index = layout(report)
    manifests = {
        path: manifest
        for flow, releases in index.items()
        for version, path in releases.items()
        if (manifest := check_manifest(report, path, flow, version, schema, licensing))
    }
    return index, manifests


def clauses(ranged: str) -> list[str]:
    """A range's clauses, read as hmz reads them: whitespace dropped, all of them to hold.

    Raises:
      ValueError: If a clause is empty, or not a comparison with a full version.
    """
    said = [re.sub(r"\s+", "", clause) for clause in ranged.split(",")]
    for clause in said:
        if not clause:
            raise ValueError("a clause is empty")
        semver.Version(0).match(clause)  # raises for what python-semver cannot compare
    return said


def newest(versions: list[semver.Version], ranged: str) -> semver.Version | None:
    """The version hmz picks in a range: the newest that is not a prerelease, else the newest."""
    fits = sorted(
        (one for one in versions if all(map(one.match, clauses(ranged)))), reverse=True
    )
    return next((one for one in fits if not one.prerelease), fits[0] if fits else None)


def plan(releases: dict[Release, dict[str, Any]], asked: Release) -> list[Release]:
    """What hmz installs for one release where nothing is installed: what it needs, then it.

    The same walk as hmz's installer: each dependency at the newest version in its range,
    once per flow, refusing a flow that needs itself or two ranges of one flow that no single
    version satisfies.

    Raises:
      ValueError: Saying why it cannot be installed.
    """
    versions: dict[str, list[semver.Version]] = {}
    for name, version in releases:
        versions.setdefault(name, []).append(semver.Version.parse(version))
    chosen: dict[str, str] = {}
    ordered: list[Release] = []

    def visit(one: Release, path: tuple[str, ...]) -> None:
        name, version = one
        chosen[name] = version
        for need, ranged in sorted(releases[one].get("dependencies", {}).items()):
            if need in (*path, name):
                raise ValueError(
                    f"{need} needs itself: {' -> '.join((*path, name, need))}"
                )
            if need in chosen:
                if not all(
                    map(semver.Version.parse(chosen[need]).match, clauses(ranged))
                ):
                    raise ValueError(
                        f"{name} {version} needs {need} {ranged}, and the rest of the "
                        f"install needs {need} {chosen[need]}"
                    )
                continue
            picked = newest(versions.get(need, []), ranged)
            if picked is None:
                raise ValueError(
                    f"{name} {version} needs {need} {ranged}; none is listed"
                )
            visit((need, str(picked)), (*path, name))
        ordered.append(one)

    visit(asked, ())
    return ordered


def check_dependencies(report: Report, manifests: dict[Path, dict[str, Any]]) -> None:
    """Each dependency is a flow here with a version in range, and each release installs.

    Flows may not depend on one another in a cycle, whatever their versions; and each release
    must be one hmz's installer can install where nothing else is, which is checked with the
    same walk it takes.
    """
    releases = {(m["name"], m["version"]): m for m in manifests.values()}
    versions: dict[str, list[semver.Version]] = {}
    for name, version in releases:
        versions.setdefault(name, []).append(semver.Version.parse(version))
    graph: dict[str, set[str]] = {}
    sound = True
    for path, manifest in manifests.items():
        node = graph.setdefault(manifest["name"], set())
        for dep, ranged in manifest.get("dependencies", {}).items():
            if dep == manifest["name"]:
                problem = f"{dep} depends on itself"
            elif dep in RESERVED:
                problem = f"{dep} is built into humanize, so is never listed"
            elif dep not in versions:
                problem = f"no flow is called {dep!r} in this index"
            else:
                node.add(dep)
                try:
                    found = newest(versions[dep], ranged)
                except ValueError as error:
                    problem = (
                        f"{dep}: {ranged!r} is not a range like >=0.1.0,<0.2.0: {error}"
                    )
                else:
                    there = ", ".join(map(str, sorted(versions[dep])))
                    problem = (
                        "" if found else f"no {dep} is {ranged!r}; there are {there}"
                    )
            if problem:
                report.error(rel(path), f"dependencies: {problem}")
                sound = False
    try:
        graphlib.TopologicalSorter(graph).prepare()
    except graphlib.CycleError as error:
        cycle = error.args[1]
        report.error(
            f"{FLOWS}/{cycle[0]}",
            "dependencies form a cycle: " + " -> ".join(reversed(cycle)),
        )
        sound = False
    if not sound:
        return
    for path, manifest in manifests.items():
        try:
            plan(releases, (manifest["name"], manifest["version"]))
        except ValueError as error:
            report.error(rel(path), f"dependencies: hmz could not install it: {error}")


def blob(data: bytes) -> str:
    """The id git gives a file of these bytes."""
    return hashlib.sha1(
        b"blob %d\0" % len(data) + data, usedforsecurity=False
    ).hexdigest()


def held_at(base: str) -> dict[str, dict[str, str]]:
    """Each version directory at `base`, with the id of every file in it."""
    held: dict[str, dict[str, str]] = {}
    for entry in git("ls-tree", "-r", "-z", base, "--", FLOWS).split("\0"):
        if entry:
            meta, path = entry.split("\t", 1)
            parts = path.split("/")
            if len(parts) > 3 and version_of(parts[2]) is not None:
                held.setdefault("/".join(parts[:3]), {})[path] = meta.split()[2]
    return held


def held_now(directory: str) -> dict[str, str]:
    """The same for one directory as it is now."""
    at = ROOT / directory
    return {
        rel(one): blob(one.read_bytes())
        for one in sorted(at.rglob("*") if at.is_dir() else [])
        if one.is_file()
    }


def github(path: str, accept: str = "application/vnd.github+json") -> Any:
    """GET from the GitHub API: the decoded JSON, or None for what is not there.

    "Not there" is missing (404), empty (409), gone (410), unreadable (422) or blocked (451).
    Rate limits are waited out for up to ten minutes, and server errors and other refusals
    (403) retried; anything lasting, or a bad token (401), raises `urllib.error.URLError`.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{API}/{path}", headers=headers)
    for attempt in range(5):
        wait = 5 * 2**attempt
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
            return (
                json.loads(body) if accept.endswith("json") else body.decode().strip()
            )
        except urllib.error.HTTPError as error:
            if error.code in (404, 409, 410, 422, 451):
                return None
            said = error.headers
            if error.code == 429 or said["x-ratelimit-remaining"] == "0":
                reset = float(said.get("x-ratelimit-reset") or time.time())
                wait = float(said.get("retry-after") or reset - time.time()) + 1
            elif "retry-after" in said:
                wait = float(said["retry-after"]) + 1
            if error.code == 401 or attempt == 4 or wait > 600:
                raise
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError):
            if attempt == 4:
                raise
        time.sleep(max(wait, 1))
    raise AssertionError


def labelled(number: int | str, repository: str) -> bool:
    """Whether a pull request carries allow-modify now."""
    labels = github(f"repos/{repository}/issues/{number}/labels?per_page=100") or []
    return ALLOW_MODIFY in {one["name"] for one in labels}


def allowed(base: str) -> bool:
    """Whether a maintainer's allow-modify covers what this GitHub Actions run checks.

    A pull request: only in the run that applying the label starts, so it covers the commits
    the maintainer saw; a push afterwards starts a run without it (and the labeler takes the
    label off). The merge queue: the pull request's label as it is now. A push to main: the
    label of any pull request merged by the commits since `base`.
    """
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not event_path or not repository:
        return False
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    if "pull_request" in event:
        label = (event.get("label") or {}).get("name")
        return event.get("action") == "labeled" and label == ALLOW_MODIFY
    if "merge_group" in event:
        queued = re.findall(r"/pr-(\d+)-", event["merge_group"].get("head_ref", ""))
        return any(labelled(number, repository) for number in queued)
    if event.get("after"):
        merged = {
            pull["number"]
            for sha in git("rev-list", f"{base}..HEAD").split()
            for pull in github(f"repos/{repository}/commits/{sha}/pulls") or []
        }
        return any(labelled(number, repository) for number in merged)
    return False


def check_published(report: Report, base: str, allow: bool) -> set[str]:
    """Version directories at `base` are as they were; returns the manifests new since."""
    before = held_at(base)
    touched = [
        directory
        for directory, files in sorted(before.items())
        if held_now(directory) != files
    ]
    if touched and not allow:
        try:
            allow = allowed(base)
        except UNREACHABLE as error:
            report.warning(
                FLOWS, f"the {ALLOW_MODIFY} label could not be read: {error}"
            )
    for directory in touched:
        what = "changed" if (ROOT / directory).exists() else "deleted"
        if allow:
            report.warning(
                directory, f"a published version is {what}, as {ALLOW_MODIFY} allows"
            )
        else:
            report.error(
                directory,
                f"a published version is {what}: a version is never edited, so publish a new "
                f"one instead (a maintainer applying {ALLOW_MODIFY} after the last push lets "
                "this through)",
            )
    old = {path: one for files in before.values() for path, one in files.items()}
    return {
        path
        for path, one in held_now(FLOWS).items()
        if path.endswith(f"/{MANIFEST}") and old.get(path) != one
    }


def check_remote(report: Report, path: Path, manifest: dict[str, Any]) -> None:
    """The repository is public, `ref` is at `commit`, and `subdir` holds the flow there."""
    where = rel(path)
    repo, ref, commit = manifest["repo"], manifest["ref"], manifest["commit"]
    found = github(f"repos/{repo}")
    if (
        found is None
        or found.get("private")
        or found.get("visibility") not in (None, "public")
    ):
        report.error(where, f"repo: {repo} is not a public repository on GitHub")
        return
    if found["full_name"].lower() != repo.lower():
        report.error(
            where, f"repo: {repo} has moved to {found['full_name']}; write that"
        )
        return
    if found.get("archived"):
        report.warning(where, f"repo: {repo} is archived")
    at = github(
        f"repos/{repo}/commits/{urllib.parse.quote(ref, safe='')}",
        accept="application/vnd.github.sha",
    )
    if at is None:
        report.error(where, f"ref: {repo} has no tag, branch or commit {ref!r}")
        return
    if at != commit:
        report.error(where, f"commit: {ref} of {repo} is {at}, not {commit}")
        return
    if re.fullmatch(r"[0-9a-f]{7,40}", ref) and commit.startswith(ref):
        # GitHub answers for a commit of any fork of the repository too: one named by itself
        # has to be shown to be the repository's own.
        default = found["default_branch"]
        compared = github(
            f"repos/{repo}/compare/{urllib.parse.quote(default, safe='')}...{commit}"
            "?per_page=1"
        )
        if (compared or {}).get("status") not in ("behind", "identical"):
            report.error(where, f"ref: {commit} is not on {repo}'s {default}; tag it")
            return
    subdir = manifest.get("subdir", "")
    shown = f"{subdir}/" if subdir else "the root"
    contents = f"repos/{repo}/contents" + (
        f"/{urllib.parse.quote(subdir)}" if subdir else ""
    )
    listed = github(f"{contents}?ref={commit}")
    if not isinstance(listed, list):
        report.error(where, f"subdir: {repo} has no directory {shown} at {commit}")
        return
    files = {entry["name"] for entry in listed if entry.get("type") == "file"}
    single = f"{manifest['name']}.py"
    if "__init__.py" not in files and single not in files:
        report.error(
            where, f"subdir: {shown} of {repo} has no __init__.py and no {single}"
        )
        return
    license_ = (github(f"repos/{repo}/license?ref={commit}") or {}).get("license") or {}
    detected = license_.get("spdx_id")
    if detected not in (None, "NOASSERTION", manifest["license"]):
        report.warning(
            where, f"license: {manifest['license']}, and GitHub reads {detected}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--base", help="the revision whose published versions must be kept"
    )
    parser.add_argument(
        "--allow-modify", action="store_true", help="let published versions change"
    )
    parser.add_argument(
        "--network", action="store_true", help="check in-scope manifests against GitHub"
    )
    args = parser.parse_args()

    report = Report()
    index, manifests = read(report)
    check_dependencies(report, manifests)

    in_scope = {rel(path) for path in manifests}
    base = args.base
    if base == NO_COMMIT:
        base = None  # a push that created the branch: nothing was published before it
    elif base:
        try:
            git("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
        except subprocess.CalledProcessError:
            report.error(FLOWS, f"--base {base} is not a commit of this clone")
            return 1
    if base:
        in_scope &= check_published(report, base, args.allow_modify)
    chosen = [
        (path, manifests[path]) for path in sorted(manifests) if rel(path) in in_scope
    ]
    if args.network:
        for path, manifest in chosen:
            try:
                check_remote(report, path, manifest)
            except UNREACHABLE as error:
                report.error(rel(path), f"GitHub could not be asked: {error}")

    listed = [
        {
            "path": rel(path),
            "name": manifest["name"],
            "version": manifest["version"],
            "repo": manifest["repo"],
            "commit": manifest["commit"],
            "subdir": manifest.get("subdir", ""),
        }
        for path, manifest in chosen
    ]
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as out:
            out.write(f"manifests={json.dumps(listed)}\n")
    total = sum(map(len, index.values()))
    print(
        f"{total} manifest(s) of {len(index)} flow(s); {len(listed)} in scope", end=""
    )
    print(" and checked against GitHub" if args.network else "", end="")
    print("".join(f"\n  {one['path']}" for one in listed) if base else "")
    if report.errors:
        print(f"{report.errors} error(s)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
