"""Run jobs on another machine over SSH.

One connection per job: the script and its inputs travel as a tar stream on stdin, the job runs in a
throw-away folder, the JSON answer comes back on stdout, and the folder is removed. Needs key-based login
(no password prompt) and ``tar`` plus Python on the far side. Nothing is installed there.
"""
from __future__ import annotations

import io
import os
import re
import secrets
import shlex
import subprocess
import tarfile
from pathlib import Path
from typing import Sequence

from ..config import ConfigError
from .base import Runner, RunnerError, finish

SAFE_PATH = re.compile(r"^[A-Za-z0-9._/~-]+$")


class SshRunner(Runner):
    name = "ssh"

    def __init__(self, host: str, python: str = "python3", workdir: str = "onshape-mcp-jobs", ssh: str = "ssh",
                 connect_timeout: int = 10):
        if not host or host.startswith("-"):
            raise ConfigError("[runner] host must look like user@machine")
        for label, value in (("python", python), ("workdir", workdir)):
            if not SAFE_PATH.match(value):
                raise ConfigError(f"[runner] {label} may only contain letters, digits and . _ - / ~")
        self.host, self.python, self.workdir, self.ssh, self.connect_timeout = host, python, workdir, ssh, connect_timeout

    def describe(self) -> str:
        return f"ssh {self.host} ({self.python})"

    @staticmethod
    def _remote(path: str) -> str:
        """A path for the far shell: ~/x becomes "$HOME/x", a relative path stays relative to the home folder."""
        return f'"$HOME/{path[2:]}"' if path.startswith("~/") else shlex.quote(path)

    def command(self, job: str, script: str, inputs: Sequence[str], args: Sequence[str]) -> list[str]:
        workdir = self.workdir[2:] if self.workdir.startswith("~/") else self.workdir
        folder = f"{workdir}/{job}" if workdir.startswith("/") else f"$HOME/{workdir}/{job}"
        steps = [
            "set -e",
            f'd="{folder}"',
            'mkdir -p "$d"',
            "trap 'rm -rf \"$d\"' EXIT",
            'cd "$d"',
            "tar xf -",
            " ".join([self._remote(self.python), *(shlex.quote(a) for a in (script, *inputs, *args))]),
        ]
        return [self.ssh, "-o", "BatchMode=yes", "-o", f"ConnectTimeout={self.connect_timeout}", "--", self.host,
                "sh -c " + shlex.quote("; ".join(steps))]

    def run(self, script: Path, inputs: Sequence[Path] = (), args: Sequence[str] = (), timeout: float = 300) -> dict:
        files = [Path(script), *(Path(p) for p in inputs)]
        names = [files[0].name] + [f"{n}_{f.name}" for n, f in enumerate(files[1:], start=1)]  # inputs may share a name
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as tar:
            for f, name in zip(files, names):
                tar.add(f, arcname=name)
        job = f"job-{os.getpid()}-{secrets.token_hex(4)}"
        try:
            done = subprocess.run(self.command(job, names[0], names[1:], list(args)), input=stream.getvalue(),
                                  capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RunnerError(f"The job did not finish in {timeout:.0f} s on {self.describe()}") from None
        except OSError as e:
            raise RunnerError(f"Cannot start {self.ssh}: {e.strerror}") from None
        if done.returncode == 255:
            tail = done.stderr.decode("utf-8", "replace").strip()[-300:]
            raise RunnerError(f"Cannot reach {self.host} over SSH (is the machine on?): {tail}")
        return finish(done, self.describe())
