"""Minimal in-memory qTest API v3 used to test qmig end to end over real HTTP."""
from __future__ import annotations

import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def set_creds(case, src: "FakeQtest", tgt: "FakeQtest", *, hs_token: str = "Bearer src-token", hcsc_token: str = "tgt-token") -> None:
    """Point QMIG_HS_* / QMIG_HCSC_* at the two fake servers for the duration of one test."""
    import os
    values = {"QMIG_HS_URL": src.url, "QMIG_HS_TOKEN": hs_token, "QMIG_HCSC_URL": tgt.url, "QMIG_HCSC_TOKEN": hcsc_token}
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update(values)

    def restore():
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
    case.addCleanup(restore)


class FakeQtest:
    def __init__(self, token: str = "tok", page_limit: int | None = None):
        self.token = token
        self.page_limit = page_limit  # server-side cap on page size
        self.projects: dict[int, dict] = {}
        self.requests: list[tuple[str, str]] = []
        self.next_id = 1000
        self.fail_post_for_names: set[str] = set()
        self.drop_next = 0  # network fault: close the connection without answering the next N requests (any method)
        self.outage_after_lost_reply = 0  # after a lost reply, also drop this many following requests (a real outage)
        self.lose_reply_of: list[str] = []  # regexes of "METHOD path": execute that request once, then cut the connection before the reply
        self.drop_reply_for_names: set[str] = set()  # execute the POST, then cut the connection before the reply (once per name)
        self.fail_upload_names: set[str] = set()
        self.fail_approve_names: set[str] = set()
        self.approve_needs_requirement = False
        self.logs_need_approved = False
        self.refuse_step_logs = False
        self.refuse_run_properties = False
        self.forbid_file_lists = False
        self.refuse_assignee_values: list = []
        self.fail_link_for_requirements: set[int] = set()
        self.refuse_field_labels: set[str] = set()
        self.status_labels = {"300": "Planned", "301": "In Progress", "302": "Completed", "303": "Closed"}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def add_project(self, pid: int, name: str, fields: list[dict]) -> dict:
        project = {"id": pid, "name": name, "fields": fields, "modules": {}, "testcases": {},
                   "statuses": [], "releases": [], "builds": [], "cycles": [], "suites": [], "runs": [], "logs": [], "requirements": {}, "links": []}
        self.projects[pid] = project
        return project

    def add_module(self, pid: int, name: str, parent: int | None = None, mid: int | None = None) -> int:
        mid = mid or self._id()
        self.projects[pid]["modules"][mid] = {"id": mid, "name": name, "parent_id": parent, "description": ""}
        return mid

    def add_release(self, pid: int, name: str, status: str = "300", **extra) -> int:
        rid = extra.pop("id", None) or self._id()
        self.projects[pid]["releases"].append({
            "id": rid, "pid": f"RL-{rid}", "name": name, "description": "", "note": "",
            "start_date": "2026-02-09T18:00:00-06:00", "end_date": "2026-12-30T18:00:00-06:00", **extra,
            "properties": [{"field_id": 7001, "field_name": "Status", "field_value": status,
                            "field_value_name": self.status_labels[status]}]})
        return rid

    def add_build(self, pid: int, release_id: int, name: str) -> int:
        bid = self._id()
        self.projects[pid]["builds"].append({"id": bid, "name": name, "release_id": release_id})
        return bid

    def add_cycle(self, pid: int, name: str, parent_type: str, parent_id: int, **extra) -> int:
        cid = extra.pop("id", None) or self._id()
        self.projects[pid]["cycles"].append({"id": cid, "pid": f"CL-{cid}", "name": name, "parent_type": parent_type,
                                              "parent_id": parent_id, **extra})
        return cid

    def add_suite(self, pid: int, name: str, parent_type: str, parent_id: int, **extra) -> int:
        sid = extra.pop("id", None) or self._id()
        self.projects[pid]["suites"].append({"id": sid, "pid": f"TS-{sid}", "name": name, "parent_type": parent_type,
                                              "parent_id": parent_id, **extra})
        return sid

    def add_run(self, pid: int, name: str, parent_type: str, parent_id: int, testcase_id: int, files=(), **extra) -> int:
        rid = extra.pop("id", None) or self._id()
        if files:
            extra["attachments"] = [{"id": self._id(), "name": n, "content_type": "text/plain", "data": d} for n, d in files]
        self.projects[pid]["runs"].append({"id": rid, "pid": f"TR-{rid}", "name": name, "parent_type": parent_type,
                                            "parent_id": parent_id, "testCaseId": testcase_id, **extra})
        return rid

    def add_log(self, pid: int, run_id: int, status: tuple[int, str], start: str, end: str, tester: str = "", note: str = "",
                files: tuple[tuple[str, bytes], ...] = (), step_logs: tuple[tuple[int, str], ...] = ()) -> int:
        lid = self._id()
        props = [{"field_id": 1, "field_name": "Tester", "field_value": "9", "field_value_name": tester}] if tester else []
        self.projects[pid]["logs"].append({"id": lid, "run_id": run_id, "status": {"id": status[0], "name": status[1]},
                                           "exe_start_date": start, "exe_end_date": end, "note": note, "properties": props,
                                           "test_step_logs": [{"order": o, "status": {"id": 601 if n == "Passed" else 602, "name": n}, "actual_result": ""}
                                                              for o, n in step_logs],
                                           "attachments": [{"id": self._id(), "name": n, "content_type": "text/plain", "data": d}
                                                           for n, d in files]})
        return lid

    def add_testcase(self, pid: int, parent: int, name: str, **extra) -> int:
        tid = extra.pop("id", None) or self._id()
        self.projects[pid]["testcases"][tid] = {"id": tid, "pid": f"TC-{tid}", "name": name, "parent_id": parent,
                                                "description": "", "properties": [], "test_steps": [], "version": "0.1", **extra}
        return tid

    def add_requirement(self, pid: int, name: str, key: str | None = None, **extra) -> int:
        rid = extra.pop("id", None) or self._id()
        req = {"id": rid, "pid": f"RQ-{rid}", "name": name, "properties": [], **extra}
        if key:
            req["_external"] = {"id": key, "link": f"https://jira.example/browse/{key}", "unique_id": str(rid)}
        self.projects[pid]["requirements"][rid] = req
        return rid

    def add_link(self, pid: int, testcase_id: int, requirement_id: int) -> None:
        self.projects[pid]["links"].append((testcase_id, requirement_id))

    def links(self, pid: int) -> set[tuple[int, int]]:
        return set(self.projects[pid]["links"])

    def writes(self) -> list[tuple[str, str]]:
        return [r for r in self.requests if r[0] != "GET"]

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def _tree(self, pid: int, parent: int | None) -> list[dict]:
        out = []
        for module in self.projects[pid]["modules"].values():
            if module["parent_id"] == parent:
                out.append({**module, "children": self._tree(pid, module["id"])})
        return out

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status: int, body=None, headers=None):
                if self._lose:
                    return self._drop()
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, str(v))
                self.end_headers()
                self.wfile.write(data)

            _lose = False

            def _drop(self):
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            def _route(self, method: str):
                parts = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                fake.requests.append((method, parts.path))
                if fake.drop_next > 0:
                    fake.drop_next -= 1
                    return self._drop()
                self._lose = False
                for pattern in list(fake.lose_reply_of):
                    if re.search(pattern, f"{method} {parts.path}"):
                        fake.lose_reply_of.remove(pattern)
                        self._lose = True  # the request is executed, only its reply is lost
                        fake.drop_next = fake.outage_after_lost_reply
                        break
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    return self._send(401, {"message": "bad token"})
                length = int(self.headers.get("Content-Length") or 0)
                raw_body = self.rfile.read(length) if length else b""
                is_json = self.headers.get("Content-Type", "").startswith("application/json")
                body = json.loads(raw_body) if raw_body and is_json else None
                segs = parts.path.strip("/").split("/")  # api v3 projects ...
                if segs[:3] != ["api", "v3", "projects"]:
                    return self._send(404, {"message": "no route"})
                if len(segs) == 3:
                    return self._send(200, [{"id": p["id"], "name": p["name"]} for p in fake.projects.values()])
                pid = int(segs[3])
                project = fake.projects.get(pid)
                if project is None:
                    return self._send(404, {"message": "project not found"})
                rest = segs[4:]
                if not rest:
                    return self._send(200, {"id": pid, "name": project["name"], "start_date": project.get("start_date")})
                if rest == ["settings", "test-cases", "fields"]:
                    if method == "POST":
                        if fake.refuse_field_labels & {body.get("label")}:
                            return self._send(400, {"message": "cannot create field"})
                        fake.next_id += 1
                        field = {"id": fake.next_id, "label": body["label"], "required": body.get("required", False),
                                 "attribute_type": {1: "String", 3: "Number", 6: "LongText", 7: "Number"}[body["data_type"]],
                                 "data_type": body["data_type"], "system_field": False, "is_active": True,
                                 "allowed_values": [{"value": fake.next_id * 10 + i, "label": o["label"],
                                                     "is_default": o.get("is_default", False), "is_active": o.get("is_active", False)}
                                                    for i, o in enumerate(body.get("allowed_values") or [])]}
                        project["fields"].append(field)
                        return self._send(201, field)
                    return self._send(200, project["fields"])
                if rest == ["test-runs", "execution-statuses"]:
                    return self._send(200, {"items": project["statuses"]})
                if rest[0] == "releases":
                    return self._releases(method, project, rest, body)
                if rest in (["test-cycles"], ["test-suites"]):
                    return self._cycles(method, project, rest[0], query, body)
                if rest == ["settings", "test-runs", "fields"]:
                    return self._send(200, [{"id": 9001, "label": "Assigned To"}, {"id": 9002, "label": "Priority"}, {"id": 9003, "label": "Test Run Details"}])
                if rest == ["test-runs"]:
                    return self._runs(method, project, query, body)
                if rest[0] == "test-runs" and len(rest) >= 2 and rest[1] != "execution-statuses":
                    return self._run_detail(method, project, rest, query, body, raw_body)
                if rest[0] == "test-cases" and len(rest) == 3 and rest[2] == "test-steps":
                    tc = project["testcases"].get(int(rest[1]))
                    if tc is None:
                        return self._send(404, {"message": "no test case"})
                    if int(query.get("page") or 1) > 1:
                        return self._send(200, [])
                    return self._send(200, self._step_views(tc))
                if rest[0] == "test-steps" and len(rest) == 3 and rest[2] == "attachments":
                    return self._attachments(method, project, rest, raw_body)
                if rest[0] == "test-steps" and len(rest) == 4 and rest[2] == "attachments":
                    return self._attachment_file(project, rest)
                if rest[0] == "test-logs" and len(rest) == 3 and rest[2] == "attachments":
                    return self._attachments(method, project, rest, raw_body)
                if rest[0] == "test-logs" and len(rest) == 4 and rest[2] == "attachments":
                    return self._attachment_file(project, rest)
                if rest == ["builds"]:
                    return self._builds(method, project, query, body)
                if rest[0] == "modules":
                    return self._modules(method, project, rest, query, body)
                if rest[0] == "requirements":
                    return self._requirements(method, project, rest, query, body)
                if rest == ["linked-artifacts"]:
                    return self._linked(project, query)
                if rest[0] == "test-cases" and len(rest) == 3 and rest[2] == "approve" and method == "PUT":
                    tc = project["testcases"].get(int(rest[1]))
                    if tc is None:
                        return self._send(404, {"message": "no test case"})
                    if tc["version"].endswith(".0"):
                        return self._send(400, {"message": "Test case is already approved"})
                    if fake.approve_needs_requirement:
                        return self._send(400, {"message": "not allowed to approve a Test Case without any linked Requirement"})
                    if tc["name"] in fake.fail_approve_names:
                        return self._send(400, {"message": "cannot approve"})
                    tc["version"] = f"{int(tc['version'].split('.')[0]) + 1}.0"
                    return self._send(200, {"id": tc["id"], "version": tc["version"]})
                if rest[0] == "test-cases" and len(rest) == 2 and method == "GET":
                    tc = project["testcases"].get(int(rest[1]))
                    return self._send(200, {**{k: v for k, v in tc.items() if k != "attachments"}, "test_steps": self._step_views(tc)}) if tc else self._send(404, {"message": "no test case"})
                if rest[0] == "test-cases" and len(rest) == 3 and rest[2] in ("attachments", "blob-handles"):
                    return self._attachments(method, project, rest, raw_body)
                if rest[0] == "test-cases" and len(rest) == 4 and rest[2] == "attachments":
                    return self._attachment_file(project, rest)
                if rest[0] == "test-cases":
                    return self._testcases(method, project, query, body)
                return self._send(404, {"message": "no route"})

            def _requirement_view(self, req, with_external):
                view = {k: v for k, v in req.items() if k != "_external"}
                if with_external and "_external" in req:
                    view["external_properties"] = req["_external"]
                return view

            def _requirements(self, method, project, rest, query, body):
                with_external = query.get("includeExternalProperties") == "true"
                if method == "POST" and len(rest) == 3 and rest[2] == "link":
                    rid = int(rest[1])
                    if rid not in project["requirements"]:
                        return self._send(404, {"message": "no requirement"})
                    if rid in fake.fail_link_for_requirements:
                        return self._send(400, {"message": "cannot link"})
                    if query.get("type") != "test-cases" or not isinstance(body, list):
                        return self._send(400, {"message": "bad link request"})
                    if len(body) > 100:
                        return self._send(400, {"message": "too many ids"})
                    if any(t not in project["testcases"] for t in body):
                        return self._send(404, {"message": "no test case"})
                    for tc in body:
                        if (tc, rid) not in project["links"]:
                            project["links"].append((tc, rid))
                    return self._send(200, [{"id": rid, "objects": [{"id": t, "link_type": "is_covered_by"} for t in body]}])
                if method != "GET":
                    return self._send(404, {"message": "no route"})
                if len(rest) == 2:
                    req = project["requirements"].get(int(rest[1]))
                    if req is None:
                        return self._send(404, {"message": "no requirement"})
                    return self._send(200, self._requirement_view(req, with_external))
                items = sorted(project["requirements"].values(), key=lambda r: r["id"])
                size = int(query.get("pageSize", 20))
                if fake.page_limit:
                    size = min(size, fake.page_limit)
                page = int(query.get("page", 1))
                shown = [self._requirement_view(r, with_external) for r in items[(page - 1) * size: page * size]]
                return self._send(200, shown, {"X-Item-Total": len(items)})

            def _linked(self, project, query):
                if query.get("type") != "test-cases":
                    return self._send(400, {"message": "only test-cases supported"})
                out = []
                for tc in (int(i) for i in query["ids"].split(",")):
                    objs = [{"id": r, "pid": f"RQ-{r}", "link_type": "covers",
                             "self": f"{fake.url}/api/v3/projects/{project['id']}/requirements/{r}?includeExternalProperties=false"}
                            for t, r in project["links"] if t == tc]
                    objs.append({"id": 999000 + tc, "pid": f"TR-{tc}", "link_type": "is_associated_with",
                                 "self": f"{fake.url}/api/v3/projects/{project['id']}/test-runs/{999000 + tc}"})
                    out.append({"id": tc, "objects": objs})
                return self._send(200, out)

            def _releases(self, method, project, rest, body):
                if "releases" not in project:
                    return self._send(404, {"message": "no route"})
                if method == "POST" and len(rest) == 1:
                    if body["name"] in fake.fail_post_for_names:
                        return self._send(400, {"message": "invalid release"})
                    rid = fake._id()
                    release = {"id": rid, "pid": f"RL-{rid}", "name": body["name"], "description": body.get("description", ""),
                               "note": body.get("note", ""), "start_date": body.get("start_date"), "end_date": body.get("end_date"),
                               "properties": [{"field_id": 7001, "field_name": "Status", "field_value": "300",
                                               "field_value_name": "Planned"}]}
                    project["releases"].append(release)
                    return self._send(201, release)
                if method == "PUT" and len(rest) == 2:
                    release = next((r for r in project["releases"] if r["id"] == int(rest[1])), None)
                    if release is None:
                        return self._send(404, {"message": "no release"})
                    for prop in body.get("properties", []):
                        for have in release["properties"]:
                            if have["field_id"] == prop["field_id"]:
                                have["field_value"] = prop["field_value"]
                                have["field_value_name"] = fake.status_labels.get(prop["field_value"], "?")
                    return self._send(200, release)
                if method == "GET" and len(rest) == 1:
                    return self._send(200, project["releases"])  # ignores page, like the real endpoints
                return self._send(404, {"message": "no route"})

            def _cycles(self, method, project, route, query, body):
                store = project["cycles" if route == "test-cycles" else "suites"]
                parent_type, parent_id = query.get("parentType"), int(query.get("parentId") or 0)
                if method == "POST":
                    if body["name"] in fake.fail_post_for_names:
                        return self._send(400, {"message": "invalid name"})
                    item = {"id": fake._id(), "pid": f"X-{fake._id()}", "name": body["name"], "parent_type": parent_type,
                            "parent_id": parent_id, "description": body.get("description", "")}
                    store.append(item)
                    return self._send(201, item)
                if int(query.get("page") or 1) > 1:
                    return self._send(200, [])
                return self._send(200, [i for i in store if i["parent_type"] == parent_type and i["parent_id"] == parent_id])

            def _run_detail(self, method, project, rest, query, body, raw_body=b""):
                run = next((r for r in project["runs"] if r["id"] == int(rest[1])), None)
                if run is None:
                    return self._send(404, {"message": "no run"})
                logs = sorted((l for l in project["logs"] if l["run_id"] == run["id"]), key=lambda l: (l["exe_end_date"], l["id"]))
                if rest[2:3] == ["attachments"]:
                    return self._attachments(method, project, rest, raw_body) if len(rest) == 3 else self._attachment_file(project, rest)
                if len(rest) == 2:
                    status = logs[-1]["status"] if logs else {"id": 0, "name": "Unexecuted"}
                    tc = project["testcases"].get(run["testCaseId"]) or {}
                    return self._send(200, {**{k: v for k, v in run.items() if k != "attachments"}, "parentId": run["parent_id"], "parentType": run["parent_type"], "test_case_version": run.get("bound_version") or tc.get("version", ""), "properties": [{"field_name": "Status", "field_value": str(status["id"]),
                                                                    "field_value_name": status["name"]}]})
                if rest[2] != "test-logs":
                    return self._send(404, {"message": "no route"})
                if len(rest) == 4:
                    log = next((l for l in logs if l["id"] == int(rest[3])), None)
                    return self._send(200, {k: v for k, v in log.items() if k != "attachments"}) if log else self._send(404, {"message": "no log"})
                if method == "POST":
                    if fake.logs_need_approved and not (run.get("bound_version") or project["testcases"][run["testCaseId"]]["version"]).endswith(".0"):
                        return self._send(400, {"message": "Invalid test case version. Please specify an approved version."})
                    if body.get("status", {}).get("id") not in {x["id"] for x in project["statuses"]} or not body.get("exe_start_date"):
                        return self._send(400, {"message": "invalid test log"})
                    lid = fake.add_log(project["id"], run["id"], (body["status"]["id"], body["status"]["name"]),
                                       body["exe_start_date"], body.get("exe_end_date") or body["exe_start_date"], note=body.get("note", ""))
                    if fake.refuse_step_logs and body.get("test_step_logs"):
                        project["logs"] = [l for l in project["logs"] if l["id"] != lid]
                        return self._send(400, {"message": "invalid test_step_logs"})
                    sent = {sl["order"]: sl for sl in body.get("test_step_logs") or []}
                    tcs = project["testcases"][run["testCaseId"]]["test_steps"]
                    next(l for l in project["logs"] if l["id"] == lid)["test_step_logs"] = [
                        {"order": st.get("order") or i, "status": sent.get(st.get("order") or i, {}).get("status") or {"id": 603, "name": "Unexecuted"},
                         "actual_result": sent.get(st.get("order") or i, {}).get("actual_result", "")} for i, st in enumerate(tcs, 1)]
                    return self._send(201, {"id": lid})
                if int(query.get("page") or 1) > 1:
                    return self._send(200, [])
                return self._send(200, [{k: v for k, v in l.items() if k != "attachments"} for l in logs])

            def _runs(self, method, project, query, body):
                parent_type, parent_id = query.get("parentType"), int(query.get("parentId") or 0)
                if method == "POST":
                    tc = (body.get("test_case") or {}).get("id")
                    if body["name"] in fake.fail_post_for_names or tc not in project["testcases"]:
                        return self._send(400, {"message": "invalid test run"})
                    if fake.refuse_run_properties and body.get("properties"):
                        return self._send(400, {"message": "invalid run property"})
                    assign = [p for p in body.get("properties") or [] if p.get("field_id") == 9001]
                    if any(p.get("field_value") in fake.refuse_assignee_values for p in assign):
                        return self._send(400, {"message": 'Value "" for field Assigned To (id=9001) is invalid'})
                    blank = any(p.get("field_value") in ("", None, "[]") for p in assign)
                    item = {"id": fake._id(), "name": body["name"], "parent_type": parent_type, "parent_id": parent_id, "testCaseId": tc,
                            "assigned_to": "" if blank else "Token User",
                            "details": next((p["field_value"] for p in body.get("properties") or [] if p.get("field_id") == 9003), ""),
                            "bound_version": project["testcases"][tc]["version"]}
                    project["runs"].append(item)
                    return self._send(201, item)
                if int(query.get("page") or 1) > 1:
                    return self._send(200, [])
                return self._send(200, [{k: v for k, v in i.items() if k != "attachments"} for i in project["runs"]
                                        if i["parent_type"] == parent_type and i["parent_id"] == parent_id])

            def _builds(self, method, project, query, body):
                rid = int(query["releaseId"])
                if method == "POST":
                    build = {"id": fake._id(), "name": body["name"], "release_id": rid}
                    project["builds"].append(build)
                    return self._send(201, build)
                return self._send(200, [b for b in project["builds"] if b["release_id"] == rid])

            def _step_views(self, tc):
                return [{**{k: v for k, v in st.items() if k != "attachments"}, "id": tc["id"] * 100 + (st.get("order") or i),
                         "attachments": [{"id": a["id"], "name": a["name"], "content_type": a["content_type"]} for a in st.get("attachments", [])]}
                        for i, st in enumerate(tc["test_steps"], 1)]

            def _holder(self, project, rest):
                if rest[0] == "test-steps":
                    tc = project["testcases"].get(int(rest[1]) // 100)
                    return next((s for i, s in enumerate((tc or {}).get("test_steps", []), 1)
                                 if (tc["id"] * 100 + (s.get("order") or i)) == int(rest[1])), None)
                if rest[0] == "test-logs":
                    return next((l for l in project["logs"] if l["id"] == int(rest[1])), None)
                if rest[0] == "test-runs":
                    return next((r for r in project["runs"] if r["id"] == int(rest[1])), None)
                return project["testcases"].get(int(rest[1]))

            def _attachments(self, method, project, rest, raw_body):
                if fake.forbid_file_lists and method == "GET":
                    return self._send(403, {"message": "No permission"})
                tc = self._holder(project, rest)
                if tc is None:
                    return self._send(404, {"message": "no test case"})
                files = tc.setdefault("attachments", [])
                if method == "GET":
                    return self._send(200, [{"id": f["id"], "name": f["name"], "content_type": f["content_type"]}
                                            for f in files])
                if fake.fail_upload_names and any(n.encode() in raw_body for n in fake.fail_upload_names):
                    return self._send(400, {"message": "upload rejected"})
                head, _, rest_body = raw_body.partition(b"\r\n\r\n")
                name = head.split(b'filename="', 1)[1].split(b'"', 1)[0].decode("utf-8")
                ctype = head.split(b"Content-Type: ", 1)[1].split(b"\r\n", 1)[0].decode()
                data = rest_body.rsplit(b"\r\n--", 1)[0]
                new = {"id": fake._id(), "name": name, "content_type": ctype, "data": data}
                files.append(new)
                return self._send(201, {"id": new["id"], "name": name})

            def _attachment_file(self, project, rest):
                tc = self._holder(project, rest)
                f = next((x for x in (tc or {}).get("attachments", []) if x["id"] == int(rest[3])), None)
                if f is None:
                    return self._send(404, {"message": "no attachment"})
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(f["data"])))
                self.end_headers()
                self.wfile.write(f["data"])

            def _modules(self, method, project, rest, query, body):
                pid = project["id"]
                if method == "POST":
                    parent = int(query["parentId"]) if "parentId" in query else None
                    if parent is not None and parent not in project["modules"]:
                        return self._send(404, {"message": "parent not found"})
                    mid = fake.add_module(pid, body["name"], parent)
                    if body["name"] in fake.drop_reply_for_names:
                        fake.drop_reply_for_names.discard(body["name"])
                        fake.drop_next = fake.outage_after_lost_reply
                        return self._drop()
                    return self._send(201, project["modules"][mid])
                if len(rest) == 2:
                    module = project["modules"].get(int(rest[1]))
                    return self._send(200, module) if module else self._send(404, {"message": "no module"})
                parent = int(query["parentId"]) if "parentId" in query else None
                if query.get("expand") == "descendants":
                    items = fake._tree(pid, parent)
                else:
                    items = [m for m in project["modules"].values() if m["parent_id"] == parent]
                return self._send(200, items)  # ignores page on purpose, like some endpoints do

            def _testcases(self, method, project, query, body):
                pid = project["id"]
                if method == "POST":
                    if body["name"] in fake.fail_post_for_names:
                        return self._send(400, {"message": "invalid test case"})
                    parent = int(query["parentId"])
                    tid = fake.add_testcase(pid, parent, body["name"], description=body.get("description", ""),
                                            properties=body.get("properties", []),
                                            test_steps=body.get("test_steps", []))
                    if body["name"] in fake.drop_reply_for_names:
                        fake.drop_reply_for_names.discard(body["name"])
                        fake.drop_next = fake.outage_after_lost_reply
                        return self._drop()
                    return self._send(201, project["testcases"][tid])
                parent = int(query["parentId"])
                items = sorted((t for t in project["testcases"].values() if t["parent_id"] == parent),
                               key=lambda t: t["id"])
                size = int(query.get("pageSize", 20))
                if fake.page_limit:
                    size = min(size, fake.page_limit)
                page = int(query.get("page", 1))
                shown = [{**{k: v for k, v in t.items() if k != "attachments"},
                          "test_steps": [{k: v for k, v in s.items() if k != "attachments"} for s in t.get("test_steps", [])]}
                         for t in items[(page - 1) * size: page * size]]  # step files are only on the single test case
                return self._send(200, shown, {"X-Item-Total": len(items)})

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def do_PUT(self):
                self._route("PUT")

        return Handler
