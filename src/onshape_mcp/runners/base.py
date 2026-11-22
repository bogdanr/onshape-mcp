"""The compute plug. A runner executes a geometry job (a plain Python script) somewhere and returns its answer.

A job receives its input files, then its arguments, on the command line and prints exactly one JSON object.
That is the whole contract, so a job can be run by hand on any machine to see what it does.
"""
from __future__ import annotations

import json
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Sequence


class RunnerError(RuntimeError):
    """The job did not run or did not answer. The message says where and why."""


class Runner(ABC):
    name: str

    @abstractmethod
    def describe(self) -> str:
        """Where jobs run, for status lines: "local (python3)" or "ssh user@host"."""

    @abstractmethod
    def run(self, script: Path, inputs: Sequence[Path] = (), args: Sequence[str] = (), timeout: float = 300) -> dict:
        """Run one job and return the JSON object it printed."""


def finish(done: subprocess.CompletedProcess, where: str) -> dict:
    if done.returncode != 0:
        tail = done.stderr.decode("utf-8", "replace").strip()[-800:]
        raise RunnerError(f"The job failed on {where} (exit {done.returncode}): {tail or 'no error text'}")
    try:
        answer = json.loads(done.stdout)
    except ValueError:
        raise RunnerError(f"The job on {where} did not print JSON: {done.stdout[:200]!r}") from None
    if not isinstance(answer, dict):
        raise RunnerError(f"The job on {where} must print one JSON object")
    return answer
