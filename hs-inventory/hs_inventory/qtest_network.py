"""Network policy shared by qTest automation clients.

The internal fyiblue qTest endpoints are reachable directly from the
corporate network. Public qTestnet endpoints must continue to use the
corporate proxy, so only the internal hosts are added to NO_PROXY.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

_DEFAULT_DIRECT_HOSTS = ("qtest.test.fyiblue.com", "qtest.fyiblue.com")


def _merge_no_proxy(existing: str, hosts: tuple[str, ...]) -> str:
    """Return a comma-separated NO_PROXY value without duplicate hosts."""
    values = [item.strip() for item in existing.split(",") if item.strip()]
    known = {item.lower() for item in values}
    for host in hosts:
        if host.lower() not in known:
            values.append(host)
            known.add(host.lower())
    return ",".join(values)


def configure_qtest_proxy() -> None:
    """Configure proxy bypass for internal qTest hosts.

    ``QTEST_DIRECT_HOSTS`` can override the defaults with a comma-separated
    list. Existing NO_PROXY entries are preserved. The uppercase and lowercase
    variants are both updated because requests/urllib3 consults environment
    variables case-insensitively depending on platform and process setup.
    """
    configured = os.environ.get("QTEST_DIRECT_HOSTS", "")
    hosts = tuple(item.strip() for item in configured.split(",") if item.strip())
    if not hosts:
        hosts = _DEFAULT_DIRECT_HOSTS

    merged = _merge_no_proxy(
        os.environ.get("NO_PROXY") or os.environ.get("no_proxy", ""),
        hosts,
    )
    os.environ["NO_PROXY"] = merged
    os.environ["no_proxy"] = merged


def is_internal_qtest_url(url: str) -> bool:
    """Return whether *url* targets one of the configured direct hosts."""
    configured = os.environ.get("QTEST_DIRECT_HOSTS", "")
    hosts = tuple(item.strip().lower() for item in configured.split(",") if item.strip())
    if not hosts:
        hosts = tuple(host.lower() for host in _DEFAULT_DIRECT_HOSTS)
    return (urlparse(url).hostname or "").lower() in hosts


configure_qtest_proxy()

__all__ = ["configure_qtest_proxy", "is_internal_qtest_url"]
