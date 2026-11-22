"""Run jobs on this machine."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Sequence

from .base import Runner, RunnerError, finish


class LocalRunner(Runner):
    name = "local"

    def __init__(self, python: str | None = None):
        self.python = python or sys.executable  # point this at a virtualenv that has the CAD libraries

    def describe(self) -> str:
        return f"local ({self.python})"

    def run(self, script: Path, inputs: Sequence[Path] = (), args: Sequence[str] = (), timeout: float = 300) -> dict:
        command = [self.python, str(script), *(str(p) for p in inputs), *args]
        try:
            done = subprocess.run(command, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RunnerError(f"The job did not finish in {timeout:.0f} s on {self.describe()}") from None
        except OSError as e:
            raise RunnerError(f"Cannot start {self.python}: {e.strerror}") from None
        return finish(done, self.describe())
