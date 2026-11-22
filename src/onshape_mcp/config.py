"""Configuration: which CAD backend, which runner, which budget.

Plain TOML, merged from up to three files, lowest priority first:

1. ``~/.config/onshape-mcp/config.toml``         machine-wide (credentials, runner)
2. the nearest ``onshape-mcp.toml`` at or above the working directory   (the design project: name, budget)
3. the file named by ``$ONSHAPE_MCP_CONFIG``

Later files override earlier ones key by key. See ``config.example.toml``.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

FILE_NAME = "onshape-mcp.toml"
ENV_VAR = "ONSHAPE_MCP_CONFIG"
USER_FILE = "~/.config/onshape-mcp/config.toml"
PATH_KEYS = {"credentials_file", "ledger", "dir"}  # relative values are resolved against the file that sets them

DEFAULTS: dict[str, dict] = {
    "project": {"name": "default", "budget": 0},  # budget 0: nothing is spent until the owner writes a number
    "backend": {"type": "onshape"},
    "runner": {"type": "local"},
    "store": {"dir": "~/.cache/onshape-mcp"},
}


class ConfigError(RuntimeError):
    """The configuration is missing something or names something that does not exist."""


@dataclass
class Config:
    data: dict[str, dict]
    files: list[Path] = field(default_factory=list)

    def section(self, name: str) -> dict:
        return self.data.get(name, {})


def find_files(cwd: Path | None = None, env=os.environ) -> list[Path]:
    out = []
    user = Path(USER_FILE).expanduser()
    if user.is_file():
        out.append(user)
    here = (cwd or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        if (folder / FILE_NAME).is_file():
            out.append(folder / FILE_NAME)
            break
    if env.get(ENV_VAR):
        named = Path(env[ENV_VAR]).expanduser()
        if not named.is_file():
            raise ConfigError(f"${ENV_VAR} points to a missing file: {named}")
        out.append(named.resolve())
    return out


def _resolve(value: str, base: Path) -> str:
    p = Path(value).expanduser()
    return str(p if p.is_absolute() else (base / p).resolve())


def load(cwd: Path | None = None, env=os.environ) -> Config:
    data = {name: dict(values) for name, values in DEFAULTS.items()}
    files = find_files(cwd, env)
    for f in files:
        try:
            with open(f, "rb") as fh:
                layer = tomllib.load(fh)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{f}: {e}") from None
        for section, values in layer.items():
            if not isinstance(values, dict):
                raise ConfigError(f"{f}: [{section}] must be a table")
            for key, value in values.items():
                if key in PATH_KEYS and isinstance(value, str):
                    value = _resolve(value, f.parent)
                data.setdefault(section, {})[key] = value
    data["store"]["dir"] = str(Path(data["store"]["dir"]).expanduser())
    budget = data["project"].get("budget")
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 0:
        raise ConfigError("[project] budget must be a whole number of requests, 0 or more")
    return Config(data, files)
