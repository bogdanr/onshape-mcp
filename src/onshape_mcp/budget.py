"""Request ledger for metered CAD services.

Every request is counted *before* it is sent, because failed requests are billed too. The budget comes from
the config file, which only the owner edits; no tool can raise it. The ledger is a small JSON file:

    {"used_total": 12, "year_limit": 500, "projects": {"name": {"used": 12, "budget": 40}}, "log": [...]}
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path


class BudgetExceeded(RuntimeError):
    """Raised instead of sending. Nothing was spent."""


class Ledger:
    def __init__(self, path: Path, project: str, budget: int, year_limit: int | None = None, reserve: int = 20):
        self.path = Path(path)
        self.project = project
        self.budget = budget
        self.year_limit = year_limit  # None: use the ledger file's own "year_limit", if it has one
        self.reserve = reserve  # never spend the last few of the year without the owner saying so
        self.session_used = 0

    def _load(self) -> dict:
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        data.setdefault("used_total", 0)
        data.setdefault("projects", {})
        data.setdefault("log", [])
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, self.path)

    def _limit(self, data: dict) -> int | None:
        return self.year_limit if self.year_limit is not None else data.get("year_limit")

    def charge(self, method: str, path: str) -> None:
        """Count one request, or raise BudgetExceeded and leave the ledger untouched."""
        data = self._load()
        entry = data["projects"].setdefault(self.project, {"used": 0})
        entry["budget"] = self.budget
        if entry["used"] >= self.budget:
            raise BudgetExceeded(
                f"Project '{self.project}' has used {entry['used']} of its {self.budget} approved requests. "
                "Nothing was sent. Ask the owner; only they raise [project] budget in the config file."
            )
        limit = self._limit(data)
        if limit is not None and data["used_total"] >= limit - self.reserve:
            raise BudgetExceeded(
                f"The yearly total is {data['used_total']} of {limit} (the last {self.reserve} are held in reserve). "
                "Nothing was sent. Ask the owner before spending more."
            )
        entry["used"] += 1
        data["used_total"] += 1
        data["log"].append([datetime.date.today().isoformat(), self.project, method, path.split("?")[0][:120]])
        self._save(data)
        self.session_used += 1

    def status(self) -> dict:
        data = self._load()
        entry = data["projects"].get(self.project, {})
        return {
            "project": self.project,
            "used": entry.get("used", 0),
            "budget": self.budget,
            "year_used": data["used_total"],
            "year_limit": self._limit(data),
            "session_used": self.session_used,
        }

    def line(self) -> str:
        s = self.status()
        year = f" | year {s['year_used']}/{s['year_limit']}" if s["year_limit"] is not None else ""
        return f"project '{s['project']}' {s['used']}/{s['budget']}{year}"
