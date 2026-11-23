"""Per-run output directory, logging and event journal.

Every command that does work creates ``output/runs/<source>-<id>__<target>-<id>/<run_id>/`` containing:

* ``run.log``      full DEBUG log (every API call with status and timing)
* ``events.jsonl`` one JSON line per action (folder/test case created, skipped, failed ...)
* reports written at the end (see report.py)

The console shows the same run at INFO level so progress is visible live.
Tokens are masked in both outputs.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Paths
from .runindex import clean_label, pair_folder, write_run_info


class _Redact(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 8]  # short strings would mangle normal text

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            message = record.getMessage()
            for secret in self.secrets:
                message = message.replace(secret, "***")
            record.msg, record.args = message, None
        return True


def new_run_id(mode: str, label: str = "") -> str:
    stamp = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{mode}"
    return f"{stamp}-{label}" if label else stamp


def unique_run_id(paths: Paths, mode: str, label: str = "") -> str:
    """A run id no folder uses yet (ids have one-second resolution; a batch makes many runs per second)."""
    moment = datetime.now(timezone.utc)
    while True:
        stamp = f"{moment.strftime('%Y%m%d-%H%M%S')}-{mode}"
        run_id = f"{stamp}-{label}" if label else stamp
        if not (paths.runs / run_id).exists() and not any(paths.runs.glob(f"*/{run_id}")):
            return run_id
        moment += timedelta(seconds=1)


class RunContext:
    def __init__(self, paths: Paths, mode: str, secrets: list[str] | None = None, run_id: str | None = None,
                 pair: tuple[str, int, str, int] | None = None, label: str | None = None):
        self.mode = mode
        self.label = clean_label(label)
        self.run_id = run_id or unique_run_id(paths, mode, self.label)
        self.dir: Path = (paths.runs / pair_folder(*pair) / self.run_id) if pair else (paths.runs / self.run_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        if pair:
            write_run_info(self.dir, {"run_id": self.run_id, "mode": mode, "label": self.label, "source": pair[0],
                                      "source_project": pair[1], "target": pair[2], "target_project": pair[3],
                                      "started": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        self._events = open(self.dir / "events.jsonl", "a", encoding="utf-8")
        self.counts: dict[str, int] = {}
        self._configure_logging(secrets or [])
        self.log = logging.getLogger("qmig")

    def _configure_logging(self, secrets: list[str]) -> None:
        root = logging.getLogger("qmig")
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        root.setLevel(logging.DEBUG)
        root.propagate = False
        redact = _Redact(secrets)
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.INFO)
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        console.addFilter(redact)
        logfile = logging.FileHandler(self.dir / "run.log", encoding="utf-8")
        logfile.setLevel(logging.DEBUG)
        logfile.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s"))
        logfile.addFilter(redact)
        root.addHandler(console)
        root.addHandler(logfile)

    def event(self, entity: str, action: str, **fields) -> None:
        """Append one action to events.jsonl and bump the in-memory counter."""
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "entity": entity, "action": action, **fields}
        self._events.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._events.flush()
        key = f"{entity}.{action}"
        self.counts[key] = self.counts.get(key, 0) + 1

    def write_json(self, name: str, data) -> Path:
        path = self.dir / name
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def close(self) -> None:
        self._events.close()
        root = logging.getLogger("qmig")
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
