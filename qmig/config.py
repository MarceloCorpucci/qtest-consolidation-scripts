"""Instance profiles and local paths.

Connection settings come from environment variables, usually from a local, git-ignored ``.env``
file in the root (copy ``.env.example``)::

    QMIG_HS_URL=https://healthspring.qtestnet.com
    QMIG_HS_TOKEN=<token>
    QMIG_HCSC_URL=https://hcsctest.qtestnet.com
    QMIG_HCSC_TOKEN=<token>

``--source hs --target hcsc`` selects the ``QMIG_HS_*`` and ``QMIG_HCSC_*`` variables, so any pair of
instances works.  Real environment variables win over ``.env``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

API_SUFFIX = "/api/v3"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Instance:
    name: str
    base_url: str  # always ends with /api/v3
    token: str = ""

    @property
    def web_url(self) -> str:
        return self.base_url[: -len(API_SUFFIX)]


@dataclass(frozen=True)
class Paths:
    root: Path
    db: Path
    runs: Path


def get_paths() -> Paths:
    """Resolve local paths. Root is ``$QMIG_HOME`` or the current directory."""
    root = Path(os.environ.get("QMIG_HOME") or Path.cwd()).resolve()
    return Paths(
        root=root,
        db=root / "data" / "qmig.sqlite",
        runs=root / "output" / "runs",
    )


def load_dotenv(path: Path) -> list[str]:
    """Load ``KEY=VALUE`` lines of a ``.env`` file into the environment (existing variables are kept).

    Supports comments, blank lines, an optional ``export `` prefix and single or double quotes.  Returns the
    names that were set.  A missing file is fine.
    """
    if not path.is_file():
        return []
    loaded: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigError(f"{path}:{number}: expected KEY=value")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def _env_instances() -> set[str]:
    return {m.group(1).lower().replace("_", "-") for k, v in os.environ.items()
            if v and (m := re.fullmatch(r"QMIG_(.+)_URL", k))}


def _env_name(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.upper())


def normalize_url(url: str, *, label: str = "url") -> str:
    """Validate an instance URL and return it with the /api/v3 suffix."""
    text = url.strip().rstrip("/")
    parts = urlsplit(text)
    local_http = (
        os.environ.get("QMIG_ALLOW_HTTP") == "1"
        and parts.scheme == "http"
        and parts.hostname in ("localhost", "127.0.0.1", "::1")
    )
    if parts.scheme != "https" and not local_http:
        raise ConfigError(f"{label} must be an https URL")
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError(f"{label} must be a plain host URL without credentials, query or fragment")
    path = parts.path.rstrip("/")
    if path not in ("", API_SUFFIX):
        raise ConfigError(f"{label} path must be empty or {API_SUFFIX}")
    return f"{parts.scheme}://{parts.netloc}{API_SUFFIX}"


def normalize_token(token: str, *, label: str = "token") -> str:
    value = token.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if not value or any(ch.isspace() for ch in value):
        raise ConfigError(f"{label} is empty or contains whitespace")
    return value


def load_instance(name: str, *, need_token: bool = True) -> Instance:
    key = name.strip().lower()
    env = _env_name(key)
    url = os.environ.get(f"QMIG_{env}_URL", "")
    token = os.environ.get(f"QMIG_{env}_TOKEN", "")
    if not url:
        raise ConfigError(f"Missing QMIG_{env}_URL (set it in .env or the environment; see .env.example)")
    base_url = normalize_url(url, label=f"QMIG_{env}_URL")
    if not token and not need_token:
        return Instance(key, base_url, "")
    if not token:
        raise ConfigError(f"Missing QMIG_{env}_TOKEN (set it in .env or the environment; see .env.example)")
    return Instance(key, base_url, normalize_token(token, label=f"QMIG_{env}_TOKEN"))


def instance_names() -> list[str]:
    return sorted(_env_instances())


ENV_TEMPLATE = """\
# qmig credentials - local only, never commit (.env is ignored by git).
# QMIG_<NAME>_URL / QMIG_<NAME>_TOKEN per qTest instance (name = the --source / --target value).
# Real environment variables win over this file.
# Tokens: qTest > profile > API & SDK > copy the token (with or without 'Bearer ').

QMIG_HS_URL=https://healthspring.qtestnet.com
QMIG_HS_TOKEN=

QMIG_HCSC_URL=https://hcsctest.qtestnet.com
QMIG_HCSC_TOKEN=
"""
