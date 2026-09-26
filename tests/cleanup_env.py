"""A directory on this machine as an environment driver, for the cleanup flows' tests.

Real processes and real files, so the flows' tree and git work runs against real
repositories while their agents are the fake kit's. `local` is humanize's own local
driver where the runtime has one, and a small stand-in for it where it does not yet.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from hmz.flows import (
    EnvBackendKind,
    EnvCommandTimeout,
    EnvError,
    EnvFileNotFound,
    EnvPermissionDenied,
    UnsupportedOperation,
)
from hmz.runtime.flowing.environments import local_env
from hmz.runtime.flowing.spi import ENV_CAPABILITIES, Placement


def local(workdir: Path) -> Any:
    try:
        return local_env(workdir)
    except NotImplementedError:
        return LocalDir(workdir)


def _failed(error: OSError, path: Path) -> EnvError:
    if isinstance(error, FileNotFoundError):
        return EnvFileNotFound(f"{path}: no such file")
    if isinstance(error, PermissionError):
        return EnvPermissionDenied(f"{path}: {error.strerror}")
    return EnvError(f"{path}: {error}")


class LocalDir:
    backend = EnvBackendKind.LOCAL
    provider = ""
    capabilities = ENV_CAPABILITIES
    cpu_count = os.cpu_count() or 1
    memory = 1 << 40
    gpu_count = 0
    gpu_memory = 0
    available = True

    def __init__(self, workdir: Path | str) -> None:
        self.workdir = PurePosixPath(workdir)

    def placement(self) -> Placement:
        return Placement(self.backend, self.provider, self.workdir)

    def _at(self, path: str | PurePosixPath) -> Path:
        return Path(self.workdir, path)

    async def derive_subdir(self, subdir: str | PurePosixPath) -> LocalDir:
        self._at(subdir).mkdir(parents=True, exist_ok=True)
        return LocalDir(self._at(subdir))

    async def exec(self, argv: Any, *, timeout: float) -> tuple[int, str, str]:
        command = ["bash", "-c", argv] if isinstance(argv, str) else list(argv)
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.workdir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            async with asyncio.timeout(timeout or None):
                out, err = await process.communicate()
        except BaseException as error:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            if isinstance(error, TimeoutError):
                raise EnvCommandTimeout(f"{argv!r} ran past {timeout}s") from None
            raise
        assert process.returncode is not None
        status = process.returncode
        return (
            128 - status if status < 0 else status,
            out.decode(errors="replace"),
            err.decode(errors="replace"),
        )

    async def read(self, path: str) -> bytes:
        try:
            return self._at(path).read_bytes()
        except OSError as error:
            raise _failed(error, self._at(path)) from None

    async def write(self, path: str, data: bytes) -> None:
        target = self._at(path)
        aside = target.with_name(f".{target.name}.writing")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            aside.write_bytes(data)
            aside.replace(target)
        except OSError as error:
            raise _failed(error, target) from None

    async def derive_worktree(self, *, ref: Any, dir: Any) -> LocalDir:  # noqa: A002
        raise UnsupportedOperation("not needed by the cleanup flows")

    async def derive_temp_clone(self, id: str, *, holder: object) -> LocalDir:  # noqa: A002
        raise UnsupportedOperation("not needed by the cleanup flows")

    async def destroy_temp_clone(self, id: str) -> None:  # noqa: A002
        raise UnsupportedOperation("not needed by the cleanup flows")

    async def derive_scratch(self, id: str) -> LocalDir:  # noqa: A002
        raise UnsupportedOperation("not needed by the cleanup flows")

    async def destroy_scratch(self, id: str) -> None:  # noqa: A002
        raise UnsupportedOperation("not needed by the cleanup flows")

    async def close(self) -> None:
        return
