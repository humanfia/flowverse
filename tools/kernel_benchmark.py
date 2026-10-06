"""Run a task's existing evaluator locally or through an existing KCoral service.

This is a standalone command, usable from any flow's task or evaluator receipt.
KCoral owns the remote protocol, uploads, GPU scheduling and downloads.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import NoReturn


class Terminated(Exception):
    """The local runner was asked to stop by its owning flow."""


def terminate(_signum: int, _frame: object) -> NoReturn:
    raise Terminated


def relative_path(value: str) -> str:
    """Accept an artifact inside the benchmark bundle."""
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise argparse.ArgumentTypeError("artifact paths must stay inside the bundle")
    return str(path)


def positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("timeout must be positive")
    return number


def copy_artifact(source: Path, destination: Path) -> None:
    """Copy regular files and directories, refusing links and special files."""
    if source.is_symlink():
        raise ValueError(f"artifact is a symbolic link: {source}")
    if source.is_dir():
        destination.mkdir(parents=True)
        for child in source.iterdir():
            copy_artifact(child, destination / child.name)
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    else:
        raise ValueError(f"artifact is missing or not a regular file: {source}")


def run_command(command: list[str], cwd: Path, timeout: int | None = None) -> int:
    """Wait for a command and clean up its process group when the caller stops."""
    with subprocess.Popen(
        command, cwd=cwd, stdin=subprocess.DEVNULL, start_new_session=True
    ) as process:
        previous = signal.signal(signal.SIGTERM, terminate)
        try:
            code = process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            code = 124 if isinstance(error, subprocess.TimeoutExpired) else 130
            print("benchmark interrupted or timed out", file=sys.stderr)
        except Terminated:
            code = 143
        finally:
            # Also reap children an evaluator left behind on an ordinary exit.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            signal.signal(signal.SIGTERM, previous)
    return code if code >= 0 else 128 - code


def local(args: argparse.Namespace, command: list[str]) -> int:
    """Run in the source bundle, then keep requested artifacts even after failure."""
    code = run_command(command, args.bundle, args.timeout)
    if args.out:
        args.out.mkdir()
        for name in args.fetch:
            source = args.bundle / name
            # A link in any ancestor is a link too; do not copy outside the bundle.
            for parent in [source, *source.parents]:
                if parent == args.bundle:
                    break
                if parent.is_symlink():
                    raise ValueError(f"artifact path contains a symbolic link: {parent}")
            copy_artifact(source, args.out / args.bundle.name / name)
    return code


def snapshot(source: Path, destination: Path) -> None:
    """Send working changes and untracked inputs, respecting a project's gitignore."""
    excluded = {".git", ".venv", "venv", "node_modules", "__pycache__", ".humanize"}
    listing = (
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
                ".",
            ],
            capture_output=True,
            check=False,
        )
        if shutil.which("git")
        else None
    )
    if listing is not None and listing.returncode == 0:
        paths = [Path(os.fsdecode(name)) for name in listing.stdout.split(b"\0") if name]
    else:
        paths = []
        for parent, dirs, files in os.walk(source, followlinks=False):
            dirs[:] = [name for name in dirs if name not in excluded]
            for name in dirs:
                if (Path(parent) / name).is_symlink():
                    raise ValueError(f"input is a symbolic link: {Path(parent) / name}")
            paths.extend((Path(parent) / name).relative_to(source) for name in files)
    destination.mkdir()
    for name in dict.fromkeys(paths):
        if any(part in excluded for part in name.parts):
            continue
        path = source / name
        # Git still lists tracked files deleted in the working tree.
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"input is not a regular file: {path}")
        if any(parent.is_symlink() for parent in path.parents if parent != source):
            raise ValueError(f"input path contains a symbolic link: {path}")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def remote(args: argparse.Namespace, command: list[str]) -> int:
    """Use the official client over a fresh project snapshot, without execution retries."""
    executable = shutil.which("kcoral")
    if executable is None:
        raise ValueError("kcoral is not installed; install the KCoral client on PATH")
    if not (args.url or os.environ.get("KCORAL_URL", "").strip()):
        raise ValueError("the kcoral backend requires --url or KCORAL_URL")
    argv = [
        executable,
        "run",
        "shell",
        "--timeout",
        str(args.timeout),
    ]
    if args.url:
        argv.extend(["--url", args.url])
    if args.out:
        argv.extend(["--out", str(args.out)])
    for name in args.fetch:
        argv.extend(["--fetch", f"{args.bundle.name}/{name}"])
    # The uploaded directory keeps its name. Positional arguments preserve spaces,
    # shell metacharacters, and evaluator arguments without evaluating them as code.
    argv.extend(
        [
            "--",
            "sh",
            "-c",
            'cd "./$1" && shift && exec "$@"',
            "kernel-benchmark",
            args.bundle.name,
            *command,
        ]
    )
    with tempfile.TemporaryDirectory(prefix="hmz-kcoral-") as temporary:
        bundle = Path(temporary) / args.bundle.name
        snapshot(args.bundle, bundle)
        argv[5:5] = ["--send", str(bundle)]
        return run_command(argv, args.bundle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--backend",
        choices=("local", "kcoral"),
        default=os.environ.get("HMZ_BENCHMARK_BACKEND", "local"),
    )
    parser.add_argument(
        "--bundle",
        default=Path.cwd(),
        type=Path,
        help="project directory (default: current directory; respects .gitignore)",
    )
    parser.add_argument("--url", help="existing KCoral server or Router; else KCORAL_URL")
    parser.add_argument("--timeout", type=positive, default=300)
    parser.add_argument(
        "--fetch",
        action="append",
        type=relative_path,
        default=[],
        help="artifact relative to the bundle; repeatable",
    )
    parser.add_argument("--out", type=Path, help="new local artifact directory")
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--" not in raw:
        parser.error("separate the evaluator command with --")
    at = raw.index("--")
    args = parser.parse_args(raw[:at])
    command = raw[at + 1 :]
    if not command:
        parser.error("an evaluator command is required after --")
    if args.backend not in ("local", "kcoral"):
        parser.error("HMZ_BENCHMARK_BACKEND must be local or kcoral")
    if args.bundle.is_symlink():
        parser.error("the bundle must not be a symbolic link")
    args.bundle = args.bundle.resolve()
    if not args.bundle.is_dir() or not args.bundle.name:
        parser.error("the bundle must be a named directory")
    if bool(args.fetch) != bool(args.out):
        parser.error("--fetch and --out must be supplied together")
    # A requested directory and its child would try to save the same file twice.
    for index, name in enumerate(args.fetch):
        for other in args.fetch[:index]:
            left, right = PurePosixPath(name), PurePosixPath(other)
            if left.is_relative_to(right) or right.is_relative_to(left):
                parser.error("artifact paths must not overlap")
    if args.out:
        args.out = args.out.absolute()
        if args.out.exists() or args.out.is_symlink() or not args.out.parent.is_dir():
            parser.error("--out must be new and its parent must exist")
        if any(
            args.out.resolve().is_relative_to(args.bundle / name) for name in args.fetch
        ):
            parser.error("--out must not be inside an artifact being collected")
    if args.backend == "local" and args.url:
        parser.error("--url is only used by the kcoral backend")
    try:
        return remote(args, command) if args.backend == "kcoral" else local(args, command)
    except (OSError, ValueError) as error:
        print(f"kernel benchmark: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
