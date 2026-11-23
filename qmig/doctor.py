"""Environment self-check and automatic first-time setup (same on Windows and macOS)."""
from __future__ import annotations

import platform
import ssl
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from .client import ApiError, QtestClient
from .config import ENV_TEMPLATE, ConfigError, Paths, instance_names, load_instance
from .db import StateDb

OK, WARN, FAIL = "OK", "WARN", "FAIL"


def _line(status: str, name: str, detail: str) -> tuple[str, str, str]:
    return status, name, detail


def setup(paths: Paths) -> list[str]:
    """Create local folders, the SQLite database and a .env credentials template (never overwrites)."""
    done = []
    for folder in (paths.db.parent, paths.runs):
        folder.mkdir(parents=True, exist_ok=True)
        done.append(f"folder ready: {folder}")
    StateDb(paths.db).close()
    done.append(f"database ready: {paths.db}")
    env_file = paths.root / ".env"
    if env_file.exists():
        done.append(f"credentials file exists: {env_file}")
    else:
        env_file.write_text(ENV_TEMPLATE, encoding="utf-8")
        done.append(f"created credentials template: {env_file}  <- fill in the tokens")
    return done


def run_checks(paths: Paths, names: list[str], *, online: bool = True) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    add = results.append

    ok_python = sys.version_info >= (3, 10)
    add(_line(OK if ok_python else FAIL, "python",
              f"{platform.python_version()} on {platform.system()} {platform.machine()}"
              + ("" if ok_python else " - Python 3.10+ required")))

    try:
        with tempfile.NamedTemporaryFile(dir=paths.root, prefix=".qmig-write-test-"):
            pass
        add(_line(OK, "working folder", f"{paths.root} is writable"))
    except OSError as exc:
        add(_line(FAIL, "working folder", f"{paths.root} is not writable: {exc}"))

    try:
        db = StateDb(paths.db)
        version = db.conn.execute("select sqlite_version()").fetchone()[0]
        db.close()
        add(_line(OK, "database", f"SQLite {version} at {paths.db}"))
    except Exception as exc:  # noqa: BLE001 - report any failure
        add(_line(FAIL, "database", f"cannot open {paths.db}: {exc}"))

    cafile = ssl.get_default_verify_paths().cafile
    try:
        import certifi  # type: ignore  # noqa: F401
        add(_line(OK, "tls certificates", "certifi bundle in use"))
    except ImportError:
        add(_line(OK if cafile or platform.system() == "Windows" else WARN, "tls certificates",
                  f"system store ({cafile or 'default'}); on macOS python.org builds run "
                  "'Install Certificates.command' or `pip install certifi` if HTTPS fails"))
    proxies = {k: v for k, v in urllib.request.getproxies().items() if k in ("http", "https")}
    add(_line(OK, "proxy", ", ".join(f"{k}={v}" for k, v in proxies.items()) if proxies else "none detected"))

    known = instance_names()
    if not known:
        add(_line(FAIL, "credentials", "no QMIG_<NAME>_URL found in .env or the environment (see .env.example)"))
        return results
    add(_line(OK, "credentials", f".env / environment (instances: {', '.join(known)})"))

    for name in names:
        try:
            instance = load_instance(name)
        except ConfigError as exc:
            add(_line(FAIL, f"instance {name}", str(exc)))
            continue
        add(_line(OK, f"instance {name}", f"{instance.web_url} (token configured)"))
        if not online:
            continue
        client = QtestClient(instance, max_attempts=2, timeout=20)
        started = time.monotonic()
        try:
            client.request("GET", "/projects", {"page": 1, "pageSize": 1})
            add(_line(OK, f"connection {name}", f"API reachable and token accepted ({(time.monotonic() - started) * 1000:.0f} ms)"))
        except ApiError as exc:
            hint = {401: "token rejected", 403: "token not allowed to list projects"}.get(exc.status or 0, str(exc))
            add(_line(FAIL, f"connection {name}", hint))
    return results
