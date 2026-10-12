"""Hardened Skill sandbox runner."""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from pathlib import PurePosixPath

import aiodocker


@dataclass(frozen=True)
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str


async def _run(image: str, host_source: PurePosixPath, bootstrap: str,
               arguments: list[str], timeout: float) -> SandboxResult:
    container = None
    completed = False
    async with aiodocker.Docker() as docker:
        try:
            container = await docker.containers.create({
                "Image": image,
                "Cmd": ["python", "-I", "-c", bootstrap, *arguments],
                "User": "65534:65534",
                "WorkingDir": "/tmp",
                "AttachStdout": True,
                "AttachStderr": True,
                "NetworkDisabled": True,
                "HostConfig": {
                    "NetworkMode": "none",
                    "ReadonlyRootfs": True,
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges"],
                    "PidsLimit": 32,
                    "Memory": 256 * 1024 * 1024,
                    "MemorySwap": 256 * 1024 * 1024,
                    "NanoCpus": 500_000_000,
                    "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=16m"},
                    "Mounts": [{"Type": "bind", "Source": str(host_source),
                                "Target": "/skill", "ReadOnly": True}],
                },
            })
            await container.start()
            outcome = await asyncio.wait_for(container.wait(), timeout=timeout)
            stdout = "".join(await container.log(stdout=True, tail=100))[:8192]
            stderr = "".join(await container.log(stderr=True, tail=100))[:8192]
            completed = True
            return SandboxResult(outcome["StatusCode"], stdout, stderr)
        finally:
            if container is not None:
                if completed:
                    await container.delete(force=True, v=True)
                else:
                    with suppress(Exception):
                        await container.delete(force=True, v=True)


def run_sandboxed(image: str, host_source: PurePosixPath, bootstrap: str,
                  arguments: list[str], timeout: float = 15) -> SandboxResult:
    return asyncio.run(_run(image, host_source, bootstrap, arguments, timeout))
