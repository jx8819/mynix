#!/usr/bin/env python3
"""miloco-omp-agent — Miloco agent webhook → OMP RPC bridge.

Speaks Miloco's webhook protocol (see miloco.utils.agent_client) on one side and
drives a restricted `omp --mode rpc` child on the other.

Security model (the whole point of this bridge):
  * The OMP child runs with --no-tools --no-extensions --no-skills --no-rules
    --no-lsp, i.e. NO shell, NO filesystem, NO network tools.
  * Its only capabilities are the host tools registered here, and every one of
    them is a Mi Home device/scene call proxied to the Miloco backend.
  * Any did the household denies is rejected in this process, before the HTTP
    request to Miloco is built.

Webhook contract (miloco.utils.agent_client.call_agent_webhook):
  POST {action, payload} + Authorization: Bearer <agent.auth_bearer>
  200  {code: 0, message: "ok", data: ...}      (code != 0 -> AgentWebhookException)
  actions: "agent" | "get_trace" | "reset_sessions"
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("miloco-omp-agent")

MAX_TOOL_RESULT_BYTES = 96 * 1024  # stay well under the 1 MiB RPC frame cap
IDEMPOTENCY_TTL_S = 15 * 60
IDEMPOTENCY_MAX = 512
TRACE_TTL_S = 7 * 24 * 3600


# ── small helpers ────────────────────────────────────────────────────────────

def read_secret(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().strip()


def http_json(method: str, url: str, body=None, headers=None, timeout: float = 30.0):
    """Minimal JSON HTTP client (stdlib only). Returns (status, parsed_or_text)."""
    data = None
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except Exception as exc:  # transport-level
        return 0, f"{type(exc).__name__}: {exc}"
    try:
        return status, json.loads(raw)
    except Exception:
        return status, raw


# ── Miloco backend client (device/scene tools) ───────────────────────────────

class MilocoClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    def _get(self, path: str):
        return http_json("GET", self.base_url + path, headers=self.headers)

    def _post(self, path: str, body=None):
        return http_json("POST", self.base_url + path, body=body if body is not None else {},
                         headers=self.headers)

    def device_list(self):
        return self._get("/api/miot/device_list")

    def device_status(self, did: str):
        return self._get(f"/api/miot/devices/{urllib.parse.quote(did)}/status")

    def device_spec(self, did: str):
        return self._get(f"/api/miot/devices/{urllib.parse.quote(did)}/spec")

    def device_control(self, did: str, body: dict):
        return self._post(f"/api/miot/devices/{urllib.parse.quote(did)}/control", body)

    def home(self):
        return self._get("/api/miot/home")

    def scene_trigger(self, scene_id: str):
        return self._post(f"/api/miot/scenes/{urllib.parse.quote(scene_id)}/trigger")


# ── host tool surface ────────────────────────────────────────────────────────

def tool_defs(read_only: bool) -> list[dict]:
    devices = [
        {
            "name": "miot_device_list",
            "label": "List Mi Home devices",
            "description": "List every Mi Home device bound to the household (did, name, "
                           "room, category, online).",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "miot_device_status",
            "label": "Read device properties",
            "description": "Read the current property values of one Mi Home device.",
            "parameters": {
                "type": "object",
                "properties": {"did": {"type": "string", "description": "Device id (did)"}},
                "required": ["did"],
                "additionalProperties": False,
            },
        },
        {
            "name": "miot_device_spec",
            "label": "Read device spec",
            "description": "Read the property/action spec of one Mi Home device. Use this to "
                           "learn the exact iid values before controlling it.",
            "parameters": {
                "type": "object",
                "properties": {"did": {"type": "string", "description": "Device id (did)"}},
                "required": ["did"],
                "additionalProperties": False,
            },
        },
    ]
    if read_only:
        return devices
    return devices + [
        {
            "name": "miot_device_control",
            "label": "Control device property or action",
            "description": "Set a property, set several properties, or call an action on one "
                           "Mi Home device. iids are 'prop.{siid}.{piid}' / 'action.{siid}.{aiid}'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "did": {"type": "string", "description": "Device id (did)"},
                    "type": {"type": "string", "enum": ["set_property", "set_properties", "call_action"]},
                    "iid": {"type": "string", "description": "IID for set_property / call_action"},
                    "value": {"description": "Value for set_property"},
                    "properties": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"iid": {"type": "string"}, "value": {}},
                            "required": ["iid", "value"],
                            "additionalProperties": False,
                        },
                        "description": "For set_properties",
                    },
                    "params": {"type": "array", "items": {}, "description": "For call_action"},
                },
                "required": ["did", "type"],
                "additionalProperties": False,
            },
        },
        {
            "name": "miot_home_overview",
            "label": "Home overview incl. scenes",
            "description": "Household overview: devices, areas and the manual Mi Home scenes "
                           "(scene ids can be passed to miot_scene_trigger).",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "miot_scene_trigger",
            "label": "Trigger Mi Home scene",
            "description": "Trigger one manual Mi Home scene by id.",
            "parameters": {
                "type": "object",
                "properties": {"scene_id": {"type": "string", "description": "Scene id"}},
                "required": ["scene_id"],
                "additionalProperties": False,
            },
        },
    ]


class ToolExecutor:
    """Executes the Mi Home host tools, enforcing the household device policy."""

    def __init__(self, miloco: MilocoClient, policy: dict, read_only: bool):
        self.miloco = miloco
        self.allow_all = bool(policy.get("allowAll", True))
        self.allow_dids = set(policy.get("allowDids") or [])
        self.deny_dids = set(policy.get("denyDids") or [])
        self.read_only = read_only

    def _check_did(self, did: str) -> str | None:
        if did in self.deny_dids:
            return f"device {did} is denied by household policy"
        if not self.allow_all and did not in self.allow_dids:
            return f"device {did} is not in the household allow list"
        return None

    def execute(self, name: str, args: dict) -> tuple[bool, str]:
        try:
            return self._execute(name, args or {})
        except Exception as exc:
            log.exception("tool %s failed", name)
            return False, f"{type(exc).__name__}: {exc}"

    def _execute(self, name: str, args: dict) -> tuple[bool, str]:
        if name in ("miot_device_status", "miot_device_spec", "miot_device_control"):
            denied = self._check_did(str(args.get("did", "")))
            if denied:
                return False, denied
        if name == "miot_device_list":
            status, data = self.miloco.device_list()
        elif name == "miot_device_status":
            status, data = self.miloco.device_status(str(args["did"]))
        elif name == "miot_device_spec":
            status, data = self.miloco.device_spec(str(args["did"]))
        elif name == "miot_device_control":
            if self.read_only:
                return False, "device control is disabled (read-only mode)"
            body = {"type": args["type"]}
            for key in ("iid", "value", "properties", "params"):
                if key in args:
                    body[key] = args[key]
            status, data = self.miloco.device_control(str(args["did"]), body)
        elif name == "miot_home_overview":
            status, data = self.miloco.home()
        elif name == "miot_scene_trigger":
            if self.read_only:
                return False, "scene trigger is disabled (read-only mode)"
            status, data = self.miloco.scene_trigger(str(args["scene_id"]))
        else:
            return False, f"unknown tool {name}"

        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        if len(text.encode("utf-8")) > MAX_TOOL_RESULT_BYTES:
            text = text.encode("utf-8")[:MAX_TOOL_RESULT_BYTES].decode("utf-8", "ignore") + "…[truncated]"
        ok = 200 <= int(status) < 300
        return ok, text


# ── OMP RPC child session ────────────────────────────────────────────────────

class OmpSession:
    """One `omp --mode rpc` child per Miloco sessionKey, with JSONL framing."""

    def __init__(self, key: str, cfg: dict, tools: ToolExecutor, tool_defs_json: list[dict]):
        self.key = key
        self.cfg = cfg
        self.tools = tools
        self.tool_defs_json = tool_defs_json
        self.lock = threading.Lock()          # one turn at a time per session
        self.frame_q: list[dict] = []
        self.frame_cv = threading.Condition(self.lock)
        self.proc: subprocess.Popen | None = None
        self.reader: threading.Thread | None = None
        self.pending: dict[str, threading.Event] = {}
        self.responses: dict[str, dict] = {}
        self.turn_done = threading.Event()
        self.turn_error: str | None = None
        self.reply_text: list[str] = []
        self.stats: dict = {}
        self._boot()

    # ── process lifecycle ──
    def _boot(self) -> None:
        omp = self.cfg["omp"]
        state_dir = omp["stateDir"]
        session_dir = os.path.join(omp["sessionDir"], re.sub(r"[^A-Za-z0-9_.-]", "_", self.key))
        os.makedirs(session_dir, exist_ok=True)
        os.makedirs(omp["cwd"], exist_ok=True)
        os.makedirs(os.path.join(state_dir, "tmp"), exist_ok=True)

        env = dict(os.environ)
        env["HOME"] = omp.get("home") or state_dir
        env["TMPDIR"] = os.path.join(state_dir, "tmp")
        env.update(omp.get("env") or {})
        for var, path in (omp.get("apiKeyEnv") or {}).items():
            env[var] = read_secret(path)

        argv = [
            omp["command"], "--mode", "rpc",
            "--cwd", omp["cwd"],
            "--model", omp["model"],
            "--thinking", omp.get("thinking", "off"),
            "--session-dir", session_dir,
            "--system-prompt", omp["systemPromptFile"],
            "--no-tools", "--no-extensions", "--no-skills", "--no-rules", "--no-lsp", "--no-title",
        ]
        if omp.get("profile"):
            argv += ["--profile", omp["profile"]]
        argv += list(omp.get("extraArgs") or [])

        log.info("spawn omp session key=%s model=%s", self.key, omp["model"])
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env, cwd=omp["cwd"],
        )
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()

        # register the Mi Home host tools before the first prompt
        resp = self._request({"type": "set_host_tools", "tools": self.tool_defs_json}, timeout=60.0)
        if not resp.get("success"):
            raise RuntimeError(f"set_host_tools failed: {resp.get('error')}")

    def _read_loop(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                log.warning("non-json frame from omp: %.200s", line)
                continue
            self._handle_frame(frame)
        log.warning("omp stdout closed for session %s", self.key)

    def _send(self, obj: dict) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise RuntimeError("omp child is gone")
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def _request(self, obj: dict, timeout: float) -> dict:
        req_id = obj.get("id") or f"req_{time.time_ns()}"
        obj = dict(obj, id=req_id)
        event = threading.Event()
        self.pending[req_id] = event
        try:
            self._send(obj)
            if not event.wait(timeout):
                return {"success": False, "error": f"omp request {obj.get('type')} timed out"}
            return self.responses.pop(req_id, {"success": False, "error": "no response"})
        finally:
            self.pending.pop(req_id, None)

    def _handle_frame(self, frame: dict) -> None:
        ftype = frame.get("type")

        if ftype == "response":
            req_id = frame.get("id")
            event = self.pending.get(req_id)
            self.responses[req_id] = frame
            if event:
                event.set()
            return

        if ftype == "host_tool_call":
            threading.Thread(target=self._run_tool, args=(frame,), daemon=True).start()
            return

        if ftype == "host_tool_cancel":
            return

        if ftype == "extension_ui_request":
            try:
                self._send({"type": "extension_ui_response", "id": frame.get("id"), "cancelled": True})
            except Exception:
                pass
            return

        if ftype == "message_update":
            ev = frame.get("assistantMessageEvent") or {}
            if ev.get("type") == "text_delta":
                self.reply_text.append(ev.get("delta", ""))
            return

        if ftype == "turn_start":
            self.stats["llmCallCount"] = self.stats.get("llmCallCount", 0) + 1
            return

        if ftype == "tool_execution_start":
            self.stats["toolCallCount"] = self.stats.get("toolCallCount", 0) + 1
            self.stats.setdefault("_tool_t0", {})[frame.get("toolCallId") or frame.get("id")] = (
                time.monotonic(), frame.get("toolName") or frame.get("name"))
            return

        if ftype == "tool_execution_end":
            t0 = self.stats.get("_tool_t0", {}).pop(frame.get("toolCallId") or frame.get("id"), None)
            if t0:
                dt = (time.monotonic() - t0[0]) * 1000.0
                self.stats["toolTotalMs"] = self.stats.get("toolTotalMs", 0.0) + dt
                if dt > self.stats.get("toolMaxMs", 0.0):
                    self.stats["toolMaxMs"] = dt
                    self.stats["slowestToolName"] = t0[1]
            return

        if ftype == "agent_end":
            if frame.get("isTerminal") is not False:
                self.turn_done.set()
            return

    def _run_tool(self, frame: dict) -> None:
        name = frame.get("toolName")
        args = frame.get("arguments") or {}
        ok, text = self.tools.execute(name, args)
        log.info("tool %s -> ok=%s %.300s", name, ok, text)
        try:
            self._send({
                "type": "host_tool_result",
                "id": frame.get("id"),
                "result": {"content": [{"type": "text", "text": text}]},
                **({} if ok else {"isError": True}),
            })
        except Exception:
            log.exception("failed to deliver host_tool_result")

    # ── turn execution ──
    def run_turn(self, message: str, timeout_s: float) -> dict:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._boot()
            self.turn_done.clear()
            self.turn_error = None
            self.reply_text = []
            self.stats = {}
            started = time.monotonic()
            try:
                resp = self._request({"type": "prompt", "message": message}, timeout=30.0)
            except Exception as exc:
                return {"status": "error", "error": f"prompt failed: {exc}"}
            if not resp.get("success"):
                return {"status": "error", "error": resp.get("error", "prompt rejected")}

            if not self.turn_done.wait(timeout_s):
                try:
                    self._send({"type": "abort"})
                except Exception:
                    pass
                self._request({"type": "get_state"}, timeout=10.0)
                return {"status": "timeout"}

            duration_ms = (time.monotonic() - started) * 1000.0
            return {
                "status": "ok",
                "durationMs": duration_ms,
                "reply": "".join(self.reply_text),
                "stats": {k: v for k, v in self.stats.items() if not k.startswith("_")},
            }

    def reset(self, delete_transcript: bool) -> None:
        with self.lock:
            try:
                if self.proc and self.proc.poll() is None:
                    self.proc.terminate()
                    self.proc.wait(timeout=5)
            except Exception:
                if self.proc:
                    self.proc.kill()
            self.proc = None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


# ── bridge service ───────────────────────────────────────────────────────────

class Bridge:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.bearer = read_secret(cfg["authBearerFile"])
        self.miloco = MilocoClient(cfg["miloco"]["baseUrl"], read_secret(cfg["miloco"]["tokenFile"]))
        self.tools = ToolExecutor(self.miloco, cfg.get("tools", {}).get("devicePolicy", {}),
                                  bool(cfg.get("tools", {}).get("readOnly", False)))
        self.tool_defs_json = tool_defs(bool(cfg.get("tools", {}).get("readOnly", False)))
        self.sessions: dict[str, OmpSession] = {}
        self.sessions_lock = threading.Lock()
        self.traces: dict[str, dict] = {}
        self.idem: dict[str, tuple[float, dict]] = {}
        self.trace_dir = os.path.join(cfg["omp"]["stateDir"], "traces")
        os.makedirs(self.trace_dir, exist_ok=True)

    # ── session registry ──
    def session(self, key: str) -> OmpSession:
        with self.sessions_lock:
            sess = self.sessions.get(key)
            if sess is not None and sess.alive():
                return sess
            sess = OmpSession(key, self.cfg, self.tools, self.tool_defs_json)
            self.sessions[key] = sess
            return sess

    # ── actions ──
    def action_agent(self, payload: dict) -> dict:
        resolve_target = payload.get("resolveTarget")
        if resolve_target == "owner-channel":
            # no IM channel is bound to this bridge; report it structurally
            return {"status": "no-channel"}

        idem_key = payload.get("idempotencyKey") or payload.get("traceId")
        now = time.time()
        with self.sessions_lock:
            for k, (ts, _val) in list(self.idem.items()):
                if now - ts > IDEMPOTENCY_TTL_S or len(self.idem) > IDEMPOTENCY_MAX:
                    self.idem.pop(k, None)
            if idem_key and idem_key in self.idem:
                return self.idem[idem_key][1]

        message = payload.get("message") or ""
        session_key = payload.get("sessionKey") or "agent:main:miloco"
        lane = payload.get("lane") or "miloco-interactive"
        trace_id = payload.get("traceId") or f"run_{time.time_ns()}"
        timeout_ms = int(payload.get("timeoutMs") or self.cfg.get("defaultTurnTimeoutMs", 60000))
        timeout_s = max(5.0, timeout_ms / 1000.0)

        log.info("turn start session=%s lane=%s trace=%s timeout_ms=%d deliver=%s",
                 session_key, lane, trace_id, timeout_ms, payload.get("deliver"))
        sess = self.session(session_key)
        result = sess.run_turn(message, timeout_s)

        status = result.get("status", "error")
        data = {"runId": trace_id, "status": status}
        if status == "error":
            data["error"] = result.get("error", "unknown error")

        self._save_trace(trace_id, session_key, lane, message, result)
        if idem_key:
            with self.sessions_lock:
                self.idem[idem_key] = (time.time(), data)
        log.info("turn end session=%s trace=%s status=%s duration=%.0fms reply=%.300s",
                 session_key, trace_id, status, result.get("durationMs", 0.0),
                 (result.get("reply") or "").replace("\n", " "))
        return data

    def action_get_trace(self, payload: dict) -> dict:
        run_id = str(payload.get("runId") or "")
        trace = self.traces.get(run_id)
        if trace is None:
            path = os.path.join(self.trace_dir, f"{re.sub(r'[^A-Za-z0-9_.-]', '_', run_id)}.json")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    trace = json.load(fh)
        if trace is None:
            return {"status": "pending"}
        return {"status": "done", **trace}

    def action_reset_sessions(self, payload: dict) -> dict:
        keys = payload.get("sessionKeys") or []
        delete = bool(payload.get("deleteTranscript", True))
        reset = 0
        with self.sessions_lock:
            targets = [k for k in self.sessions if not keys or k in keys]
            for key in targets:
                sess = self.sessions.pop(key)
                sess.reset(delete)
                reset += 1
                if delete:
                    shutil.rmtree(
                        os.path.join(self.cfg["omp"]["sessionDir"],
                                     re.sub(r"[^A-Za-z0-9_.-]", "_", key)),
                        ignore_errors=True)
        log.info("reset_sessions keys=%s delete=%s reset=%d", keys, delete, reset)
        return {"reset": reset}

    def _save_trace(self, run_id, session_key, lane, message, result) -> None:
        stats = result.get("stats") or {}
        tool_total = float(stats.get("toolTotalMs", 0.0) or 0.0)
        duration = float(result.get("durationMs", 0.0) or 0.0)
        trace = {
            "runId": run_id,
            "query": message,
            "durationMs": duration,
            "llmCallCount": int(stats.get("llmCallCount", 0) or 0),
            "toolCallCount": int(stats.get("toolCallCount", 0) or 0),
            "llmTotalMs": max(0.0, duration - tool_total),
            "toolTotalMs": tool_total,
            "toolMaxMs": float(stats.get("toolMaxMs", 0.0) or 0.0),
            "slowestToolName": stats.get("slowestToolName"),
            "success": result.get("status") == "ok",
            "errorCount": 0 if result.get("status") == "ok" else 1,
            "errorMsg": result.get("error"),
            "jsonlPath": None,
            "sessionKey": session_key,
            "lane": lane,
            "reply": (result.get("reply") or "")[:2000],
        }
        self.traces[run_id] = trace
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)
        try:
            with open(os.path.join(self.trace_dir, f"{safe}.json"), "w", encoding="utf-8") as fh:
                json.dump(trace, fh, ensure_ascii=False)
            self._prune_traces()
        except Exception:
            log.exception("trace persist failed")

    def _prune_traces(self) -> None:
        cutoff = time.time() - TRACE_TTL_S
        for name in os.listdir(self.trace_dir):
            path = os.path.join(self.trace_dir, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass

    def dispatch(self, action: str, payload: dict) -> dict:
        if action == "agent":
            return self.action_agent(payload or {})
        if action == "get_trace":
            return self.action_get_trace(payload or {})
        if action == "reset_sessions":
            return self.action_reset_sessions(payload or {})
        raise ValueError(f"unknown action '{action}'")


class Handler(BaseHTTPRequestHandler):
    bridge: Bridge
    allowed_ips: list[str]
    server_version = "miloco-omp-agent/1"

    def log_message(self, fmt, *args):  # route access logs through logging
        log.info("http %s", fmt % args)

    def _deny(self, code: int, message: str) -> None:
        body = json.dumps({"code": 1, "message": message}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, data) -> None:
        body = json.dumps({"code": 0, "message": "ok", "data": data}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        peer = self.client_address[0]
        if self.allowed_ips and peer not in self.allowed_ips:
            log.warning("rejected source ip %s", peer)
            return False
        header = self.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        return hmac.compare_digest(token, self.bridge.bearer)

    def do_GET(self):
        if not self._authorized():
            return self._deny(401, "unauthorized")
        if self.path == "/health":
            return self._ok({"status": "ok", "sessions": list(self.bridge.sessions.keys())})
        return self._deny(404, "not found")

    def do_POST(self):
        if not self._authorized():
            return self._deny(401, "unauthorized")
        if self.path != "/webhook":
            return self._deny(404, "not found")
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 1024 * 1024:
            return self._deny(413, "payload too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._deny(400, "invalid JSON body")
        action = body.get("action")
        payload = body.get("payload") or {}
        try:
            data = self.bridge.dispatch(action, payload)
        except ValueError as exc:
            return self._deny(400, str(exc))
        except Exception as exc:
            log.exception("action %s failed", action)
            return self._deny(500, f"{type(exc).__name__}: {exc}")
        return self._ok(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="Miloco agent webhook → OMP RPC bridge")
    parser.add_argument("--config", required=True, help="JSON config file")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("MILOCO_OMP_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    with open(args.config, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    bridge = Bridge(cfg)
    Handler.bridge = bridge
    Handler.allowed_ips = list(cfg.get("allowedSourceIps") or [])

    listen = cfg.get("listen", {})
    server = ThreadingHTTPServer((listen.get("address", "127.0.0.1"), int(listen.get("port", 18125))), Handler)
    log.info("listening on %s:%d (allowed sources: %s)",
             listen.get("address", "127.0.0.1"), int(listen.get("port", 18125)),
             Handler.allowed_ips or "any")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
