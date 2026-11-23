"""Small qTest API v3 client (standard library only).

Safety properties:
* A client built with ``allow_writes=False`` raises :class:`WriteBlocked` for
  every non-GET request.  The source client is always read-only and the target
  client is read-only during ``plan``/``preflight``, so a dry-run cannot change
  anything even if a phase has a bug.
* Redirects are refused (the Authorization header must not leave the host).
* Writes are retried only on 429/503 (the request was not processed), never on
  timeouts, to avoid creating duplicates.
"""
from __future__ import annotations

import json
import logging
import ssl
import time
import uuid
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import Instance
from .network import OFF, NetworkInterrupted, NetworkPolicy, format_seconds, is_unsent

log = logging.getLogger("qmig.client")

READ_RETRY_STATUS = (429, 500, 502, 503, 504)
WRITE_RETRY_STATUS = (429, 503)
MAX_PAGES = 100_000
MAX_NETWORK_CYCLES = 5


class ApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int | None, detail: str = "", *, ambiguous: bool = False):
        self.method, self.path, self.status, self.detail = method, path, status, detail
        self.ambiguous = ambiguous  # a write whose reply was lost: it may or may not have been executed
        label = f"HTTP {status}" if status else "connection error"
        super().__init__(f"{method} {path}: {label}{': ' + detail if detail else ''}")


class WriteBlocked(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiError(req.get_method(), req.full_url, code, "redirect refused (would leak credentials)")


def _ssl_context() -> ssl.SSLContext:
    """Default context; uses certifi's bundle when installed (helps python.org builds on macOS)."""
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def items_of(payload: Any) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        return payload["items"]
    return []


class QtestClient:
    def __init__(
        self,
        instance: Instance,
        *,
        allow_writes: bool = False,
        timeout: int = 60,
        page_size: int = 100,
        max_attempts: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        network: NetworkPolicy = OFF,
    ):
        self.instance = instance
        self.name = instance.name
        self.allow_writes = allow_writes
        self.timeout = timeout
        self.page_size = page_size
        self.max_attempts = max_attempts
        self._sleep = sleep
        self.network = network
        self._opener = build_opener(_NoRedirect(), *self._tls_handlers())
        self.stats = {"requests": 0, "retries": 0, "errors": 0, "writes": 0, "blocked_writes": 0,
                      "network_interruptions": 0, "network_wait_seconds": 0}
        self.warnings: list[str] = []

    @staticmethod
    def _tls_handlers():
        from urllib.request import HTTPSHandler

        return (HTTPSHandler(context=_ssl_context()),)

    # ------------------------------------------------------------------ core
    def request(self, method: str, path: str, params: dict | None = None, body: Any = None, *,
                raw: bool = False, data: bytes | None = None, extra_headers: dict | None = None):
        """Return ``(parsed_json_or_None, response_headers)``.

        ``raw=True`` returns the response bytes (file download); ``data`` sends a ready-made body
        (file upload) together with ``extra_headers``.
        """
        method = method.upper()
        if method != "GET" and not self.allow_writes:
            self.stats["blocked_writes"] += 1
            raise WriteBlocked(f"[{self.name}] {method} {path} blocked: client is read-only")
        url = self.instance.base_url + path
        if params:
            url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
        payload = json.dumps(body).encode("utf-8") if body is not None else data
        headers = {"Authorization": f"Bearer {self.instance.token}",
                   "Accept": "application/octet-stream" if raw else "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra_headers or {})
        want_bytes = raw
        retry_status = READ_RETRY_STATUS if method == "GET" else WRITE_RETRY_STATUS

        attempt = cycles = 0
        while attempt < self.max_attempts:
            attempt += 1
            started = time.monotonic()
            self.stats["requests"] += 1
            if method != "GET":
                self.stats["writes"] += 1
            try:
                req = Request(url, data=payload, headers=headers, method=method)
                with self._opener.open(req, timeout=self.timeout) as resp:
                    content = resp.read()
                    resp_headers = resp.headers
                    status = resp.status
                log.debug("[%s] %s %s -> %s %.0fms", self.name, method, path, status,
                          (time.monotonic() - started) * 1000)
                return (content if want_bytes else (json.loads(content) if content.strip() else None)), resp_headers
            except HTTPError as exc:
                detail = exc.read()[:500].decode("utf-8", "replace").strip()
                status = exc.code
                retry_after = exc.headers.get("Retry-After", "")
                exc.close()
                log.debug("[%s] %s %s -> HTTP %s %.0fms", self.name, method, path, status,
                          (time.monotonic() - started) * 1000)
                if status in retry_status and attempt < self.max_attempts:
                    self._backoff(method, path, attempt, f"HTTP {status}", retry_after)
                    continue
                self.stats["errors"] += 1
                raise ApiError(method, path, status, detail) from None
            except (URLError, TimeoutError, ConnectionError, ssl.SSLError) as exc:
                if self.network.enabled and cycles < MAX_NETWORK_CYCLES:  # a request that keeps killing the connection fails on its own
                    cycles += 1
                    self._wait_for_network(method, path, exc)  # raises NetworkInterrupted when it does not come back
                    if method == "GET" or is_unsent(exc):
                        attempt -= 1  # the wait is not a failed attempt; ask again
                        continue
                    self.stats["errors"] += 1  # a write that may have been executed: never repeat it blindly
                    raise ApiError(method, path, None, f"connection lost while writing ({getattr(exc, 'reason', exc)}); "
                                   "the write may have been executed", ambiguous=True) from None
                if method == "GET" and attempt < self.max_attempts:
                    self._backoff(method, path, attempt, f"connection problem ({exc})", "")
                    continue
                self.stats["errors"] += 1
                reason = getattr(exc, "reason", exc)
                raise ApiError(method, path, None, str(reason)) from None
        raise ApiError(method, path, None, "retries exhausted")

    def _probe(self) -> bool:
        """True when the instance answers at all (any HTTP status counts: the network is back)."""
        try:
            req = Request(self.instance.base_url + "/projects?pageSize=1",
                          headers={"Authorization": f"Bearer {self.instance.token}", "Accept": "application/json"})
            with self._opener.open(req, timeout=min(self.timeout, 20)) as resp:
                resp.read(1)
            return True
        except HTTPError as exc:
            exc.close()
            return True
        except (URLError, TimeoutError, ConnectionError, ssl.SSLError, OSError):
            return False

    def _wait_for_network(self, method: str, path: str, exc: BaseException) -> None:
        why = str(getattr(exc, "reason", exc))
        log.warning("[%s] %s %s: connection problem (%s); waiting for the network, up to %d retries",
                    self.name, method, path, why, self.network.retries)
        waited = 0.0
        for number in range(1, self.network.retries + 1):
            delay = self.network.delay(number)
            log.warning("[%s] network wait %d/%d: next check in %s", self.name, number, self.network.retries,
                        format_seconds(delay))
            self._sleep(delay)
            waited += delay
            if self._probe():
                self.stats["network_interruptions"] += 1
                self.stats["network_wait_seconds"] += round(waited)
                log.warning("[%s] network is back after %s; continuing", self.name, format_seconds(waited))
                return
        self.stats["network_interruptions"] += 1
        self.stats["network_wait_seconds"] += round(waited)
        raise NetworkInterrupted(self.name, self.network.retries, waited, why)

    def _backoff(self, method: str, path: str, attempt: int, why: str, retry_after: str) -> None:
        delay = min(int(retry_after), 120) if retry_after.isdigit() else min(2 ** attempt, 30)
        self.stats["retries"] += 1
        log.warning("[%s] %s %s: %s, retry %d/%d in %ss", self.name, method, path, why,
                    attempt, self.max_attempts - 1, delay)
        self._sleep(delay)

    # ------------------------------------------------------------ pagination
    def paginate(self, path: str, params: dict | None = None) -> list[dict]:
        """Fetch every page; tolerate qTest's unstable page order.

        Items are de-duplicated by ``id``.  If a page repeats ids (which means
        another item was probably skipped), the whole listing is refetched and
        merged, up to three times.  A page with no new ids stops the loop, which
        also protects against endpoints that ignore ``page``.
        """
        merged: dict[Any, dict] = {}
        without_id: list[dict] = []
        expected_total: int | None = None
        for refetch in range(4):
            repeated = 0
            for page in range(1, MAX_PAGES):
                payload, headers = self.request(
                    "GET", path, {**(params or {}), "page": page, "pageSize": self.page_size})
                total = headers.get("X-Item-Total") if headers else None
                if total is not None and str(total).isdigit():
                    expected_total = int(total)
                items = items_of(payload)
                new = 0
                for item in items:
                    if not isinstance(item, dict) or item.get("id") is None:
                        without_id.append(item)
                        new += 1
                    elif item["id"] in merged:
                        repeated += 1
                    else:
                        merged[item["id"]] = item
                        new += 1
                if not items or new == 0 or (expected_total is not None and len(merged) >= expected_total):
                    break
                if len(items) < self.page_size and expected_total is None:
                    break
            if repeated == 0:
                break
            log.warning("[%s] GET %s: %d repeated item(s) across pages, refetching (%d/3)",
                        self.name, path, repeated, refetch + 1)
        if expected_total is not None and len(merged) < expected_total:
            message = f"GET {path}: received {len(merged)} of {expected_total} items (listing may be incomplete)"
            self.warnings.append(message)
            log.warning("[%s] %s", self.name, message)
        return list(merged.values()) + without_id

    # ------------------------------------------------------------- endpoints
    def get_project(self, project_id: int) -> dict:
        return self.request("GET", f"/projects/{project_id}")[0] or {}

    def get_module(self, project_id: int, module_id: int) -> dict:
        return self.request("GET", f"/projects/{project_id}/modules/{module_id}")[0] or {}

    def list_modules(self, project_id: int, parent_id: int | None = None, *, descendants: bool = False) -> list[dict]:
        params: dict[str, Any] = {}
        if parent_id is not None:
            params["parentId"] = parent_id
        if descendants:
            params["expand"] = "descendants"
        return self.paginate(f"/projects/{project_id}/modules", params)

    def list_testcases(self, project_id: int, module_id: int, *, expand: bool = True) -> list[dict]:
        params: dict[str, Any] = {"parentId": module_id}
        if expand:
            params.update({"expandProps": "true", "expandSteps": "true"})
        return self.paginate(f"/projects/{project_id}/test-cases", params)

    def test_case_fields(self, project_id: int) -> list[dict]:
        payload, _ = self.request("GET", f"/projects/{project_id}/settings/test-cases/fields")
        return items_of(payload)

    def create_test_case_field(self, project_id: int, body: dict) -> dict:
        """Create a custom test case field in one project (POST ``.../settings/test-cases/fields``)."""
        return self.request("POST", f"/projects/{project_id}/settings/test-cases/fields", None, body)[0] or {}

    def execution_statuses(self, project_id: int) -> list[dict]:
        return items_of(self.request("GET", f"/projects/{project_id}/test-runs/execution-statuses")[0])

    def list_requirements(self, project_id: int) -> list[dict]:
        """All requirements with their ``external_properties`` (the Jira issue key lives there)."""
        return self.paginate(f"/projects/{project_id}/requirements", {"includeExternalProperties": "true"})

    def get_requirement(self, project_id: int, requirement_id: int) -> dict | None:
        try:
            return self.request("GET", f"/projects/{project_id}/requirements/{requirement_id}",
                                {"includeExternalProperties": "true"})[0] or None
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise

    def linked_artifacts(self, project_id: int, object_type: str, ids: list[int]) -> dict[int, list[dict]]:
        """Objects linked to the given objects: ``{object id: [linked object, ...]}``.

        ``object_type`` is the type of the *given* ids (``test-cases`` returns the requirements, runs ...
        linked to those test cases).  The endpoint takes many ids at once; callers batch by 100.
        """
        payload, _ = self.request("GET", f"/projects/{project_id}/linked-artifacts",
                                  {"type": object_type, "ids": ",".join(str(i) for i in ids)})
        return {row["id"]: list(row.get("objects") or []) for row in items_of(payload) if isinstance(row, dict) and "id" in row}

    def link_testcases_to_requirement(self, project_id: int, requirement_id: int, testcase_ids: list[int]) -> list[dict]:
        """Link test cases to a requirement (POST ``.../requirements/{id}/link?type=test-cases``, qTest 7.6+)."""
        payload, _ = self.request("POST", f"/projects/{project_id}/requirements/{requirement_id}/link",
                                  {"type": "test-cases"}, list(testcase_ids))
        return items_of(payload)

    def list_releases(self, project_id: int) -> list[dict]:
        return self.paginate(f"/projects/{project_id}/releases")

    def list_builds(self, project_id: int, release_id: int) -> list[dict]:
        return self.paginate(f"/projects/{project_id}/builds", {"releaseId": release_id})

    def list_cycles(self, project_id: int, parent_id: int, parent_type: str) -> list[dict]:
        return self.paginate(f"/projects/{project_id}/test-cycles", {"parentId": parent_id, "parentType": parent_type})

    def list_suites(self, project_id: int, parent_id: int, parent_type: str) -> list[dict]:
        return self.paginate(f"/projects/{project_id}/test-suites", {"parentId": parent_id, "parentType": parent_type})

    def list_runs(self, project_id: int, parent_id: int, parent_type: str) -> list[dict]:
        return self.paginate(f"/projects/{project_id}/test-runs", {"parentId": parent_id, "parentType": parent_type})

    def run_field_id(self, project_id: int, label: str) -> int | None:
        """Id of a test run field (for example 'Assigned To') from the project's run field settings, None if unavailable."""
        try:
            fields = items_of(self.request("GET", f"/projects/{project_id}/settings/test-runs/fields")[0])
        except ApiError:
            return None
        return next((f["id"] for f in fields if str(f.get("label") or "").strip().casefold() == label.casefold()), None)

    def list_logs(self, project_id: int, run_id: int) -> list[dict]:
        return self.paginate(f"/projects/{project_id}/test-runs/{run_id}/test-logs")

    def list_steps(self, project_id: int, test_case_id: int) -> list[dict]:
        return self.paginate(f"/projects/{project_id}/test-cases/{test_case_id}/test-steps")

    def test_case_steps(self, project_id: int, test_case_id: int) -> list[dict]:
        """Steps of one test case with their ids and files, by order (the bulk test case listing leaves the files out;
        HS and HCSC send the steps only with ``expand=teststep``)."""
        try:
            payload = self.request("GET", f"/projects/{project_id}/test-cases/{test_case_id}", {"expand": "teststep"})[0] or {}
        except ApiError as exc:
            if exc.status == 404:
                return []
            raise
        steps = [s for s in payload.get("test_steps") or [] if isinstance(s, dict)]
        steps.sort(key=lambda s: (s.get("order") is None, s.get("order") or 0))
        return steps

    def get_run(self, project_id: int, run_id: int) -> dict:
        return self.request("GET", f"/projects/{project_id}/test-runs/{run_id}")[0] or {}

    def list_attachments(self, project_id: int, object_type: str, object_id: int) -> list[dict]:
        """Attachment metadata of an object (``test-cases``, ``test-logs`` ...); a missing object has none."""
        try:
            return items_of(self.request("GET", f"/projects/{project_id}/{object_type}/{object_id}/attachments")[0])
        except ApiError as exc:
            if exc.status == 404:
                return []
            raise

    def download_attachment(self, project_id: int, object_type: str, object_id: int, attachment_id: int) -> bytes:
        path = f"/projects/{project_id}/{object_type}/{object_id}/attachments/{attachment_id}"
        return self.request("GET", path, raw=True)[0] or b""

    def upload_attachment(self, project_id: int, object_type: str, object_id: int, name: str,
                          content_type: str, content: bytes) -> dict:
        """Attach a file to an object (multipart upload to ``attachments``).

        ``blob-handles`` is not used: on the HCSC instance it stored the raw multipart body as the file
        and attached nothing, while ``attachments`` stores the file correctly.
        """
        boundary = uuid.uuid4().hex
        filename = name.replace('"', "'").replace("\r", " ").replace("\n", " ")
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n").encode("utf-8")
        body = head + content + f"\r\n--{boundary}--\r\n".encode("ascii")
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}", "File-Name": quote(filename)}
        return self.request("POST", f"/projects/{project_id}/{object_type}/{object_id}/attachments",
                            data=body, extra_headers=headers)[0] or {}

    def create_module(self, project_id: int, name: str, parent_id: int | None, description: str = "") -> dict:
        body: dict[str, Any] = {"name": name}
        if description:
            body["description"] = description
        params = {"parentId": parent_id} if parent_id else None
        return self.request("POST", f"/projects/{project_id}/modules", params, body)[0] or {}

    def create_testcase(self, project_id: int, parent_id: int, payload: dict) -> dict:
        body = {**payload, "parent_id": parent_id}
        return self.request("POST", f"/projects/{project_id}/test-cases", {"parentId": parent_id}, body)[0] or {}

    def approve_testcase(self, project_id: int, test_case_id: int) -> dict:
        return self.request("PUT", f"/projects/{project_id}/test-cases/{test_case_id}/approve")[0] or {}

    def create_release(self, project_id: int, payload: dict) -> dict:
        return self.request("POST", f"/projects/{project_id}/releases", None, payload)[0] or {}

    def update_release(self, project_id: int, release_id: int, payload: dict) -> dict:
        return self.request("PUT", f"/projects/{project_id}/releases/{release_id}", None, payload)[0] or {}

    def create_build(self, project_id: int, release_id: int, payload: dict) -> dict:
        return self.request("POST", f"/projects/{project_id}/builds", {"releaseId": release_id}, payload)[0] or {}

    def create_cycle(self, project_id: int, parent_id: int, parent_type: str, payload: dict) -> dict:
        return self.request("POST", f"/projects/{project_id}/test-cycles",
                            {"parentId": parent_id, "parentType": parent_type}, payload)[0] or {}

    def create_suite(self, project_id: int, parent_id: int, parent_type: str, payload: dict) -> dict:
        return self.request("POST", f"/projects/{project_id}/test-suites",
                            {"parentId": parent_id, "parentType": parent_type}, payload)[0] or {}

    def create_run(self, project_id: int, parent_id: int, parent_type: str, payload: dict) -> dict:
        return self.request("POST", f"/projects/{project_id}/test-runs",
                            {"parentId": parent_id, "parentType": parent_type}, payload)[0] or {}

    def create_log(self, project_id: int, run_id: int, payload: dict) -> dict:
        return self.request("POST", f"/projects/{project_id}/test-runs/{run_id}/test-logs", None, payload)[0] or {}
