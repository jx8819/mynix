#!/usr/bin/env python3
"""mesh-guardian — Xiaomi Mesh wired-backhaul recovery daemon.

Machine-specific values (AP IPs, passwords, email) come exclusively from
command-line flags so this program stays free of private deployment data.

State-machine per satellite AP:
  OK            → wired link_type from controller topology
  WIRELESS      → link_type != "wired" for at least --check-interval seconds
  REBOOTING     → reboot command sent; waiting up to --reboot-wait seconds
  COOLDOWN      → post-reboot grace period (switch restart / power cycle)
  LATCH         → failure count hit --max-failures; human intervention required

Suppression rules (no reboot issued when any holds):
  - AP unreachable (ping fails) — power cut or maintenance
  - AP offline for < --offline-grace seconds — transient / switch restart
  - AP already REBOOTING or in COOLDOWN
  - Daemon-wide LATCH active

Notification: sendmail (via postfix relay already configured on the host) with
  From: set to the configured sender so postfix generic map rewrites correctly.
"""

import argparse
import hashlib
import json
import logging
import os
import signal
import smtplib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from enum import Enum, auto
from pathlib import Path
from typing import Optional


# ──────────────────────────────────────────────
# Auth helpers  (mirrors the JS Encrypt object)
# ──────────────────────────────────────────────

def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


_XIAOQI_KEY = "a2ffa5c9be07488bbb04a3a47d3c5f6a"


def _make_nonce() -> str:
    import random
    ts = int(time.time())
    rnd = random.randint(0, 9999)
    # device_id is baked into the JS; use the same placeholder
    device_id = "0a:06:97:54:e5:d3"
    return f"0_{device_id}_{ts}_{rnd}"


def _hash_password(password: str, nonce: str, use_sha256: bool) -> str:
    if use_sha256:
        inner = _sha256(password + _XIAOQI_KEY)
        return _sha256(nonce + inner)
    inner = _sha1(password + _XIAOQI_KEY)
    return _sha1(nonce + inner)


# ──────────────────────────────────────────────
# HTTP helpers (no third-party deps)
# ──────────────────────────────────────────────

class APIError(Exception):
    pass


def _post_form(url: str, data: dict, timeout: int = 10) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise APIError(f"HTTP {e.code} from {url}") from e
    except Exception as e:
        raise APIError(str(e)) from e


def _get_json(url: str, timeout: int = 10) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        raise APIError(str(e)) from e


# ──────────────────────────────────────────────
# Xiaomi API
# ──────────────────────────────────────────────

class XiaomiClient:
    """Session towards one Xiaomi router."""

    def __init__(self, ip: str, password: str, timeout: int = 10):
        self.ip = ip
        self.password = password
        self.timeout = timeout
        self._token: Optional[str] = None

    def _base(self) -> str:
        return f"http://{self.ip}"

    def _login(self) -> str:
        # Check which hash mode the firmware uses
        info = _get_json(
            f"{self._base()}/cgi-bin/luci/api/xqsystem/init_info",
            timeout=self.timeout,
        )
        use_sha256 = info.get("newEncryptMode", 0) == 1
        nonce = _make_nonce()
        hashed = _hash_password(self.password, nonce, use_sha256)
        resp = _post_form(
            f"{self._base()}/cgi-bin/luci/api/xqsystem/login",
            {"username": "admin", "password": hashed, "logtype": "2", "nonce": nonce},
            timeout=self.timeout,
        )
        if resp.get("code") != 0:
            raise APIError(f"Login failed: {resp}")
        return resp["token"]

    def _ensure_token(self) -> str:
        if not self._token:
            self._token = self._login()
        return self._token

    def _api(self, path: str) -> str:
        tok = self._ensure_token()
        return f"{self._base()}/cgi-bin/luci/;stok={tok}{path}"

    def topology(self) -> dict:
        """Fetch /api/misystem/topo_graph from the controller AP."""
        try:
            return _get_json(self._api("/api/misystem/topo_graph"), timeout=self.timeout)
        except APIError:
            # Token may have expired; retry once
            self._token = None
            return _get_json(self._api("/api/misystem/topo_graph"), timeout=self.timeout)

    def reboot(self) -> None:
        """Send reboot to this AP (not the controller)."""
        try:
            url = self._api("/api/xqsystem/reboot") + "?client=web"
            _get_json(url, timeout=self.timeout)
        except APIError:
            self._token = None
            url = self._api("/api/xqsystem/reboot") + "?client=web"
            _get_json(url, timeout=self.timeout)


# ──────────────────────────────────────────────
# Per-AP state machine
# ──────────────────────────────────────────────

class APState(Enum):
    OK        = auto()
    WIRELESS  = auto()   # currently on wireless backhaul
    REBOOTING = auto()   # reboot sent, waiting for recovery
    COOLDOWN  = auto()   # post-reboot grace, not yet stable
    LATCH     = auto()   # too many failures; stop touching


@dataclass
class APRecord:
    ip: str
    name: str
    state: APState = APState.OK
    state_since: float = field(default_factory=time.monotonic)
    consecutive_failures: int = 0
    last_reboot_at: float = 0.0
    # monotonic timestamp when AP was first seen offline/unreachable
    offline_since: Optional[float] = None

    def transition(self, new_state: APState) -> None:
        if self.state != new_state:
            logging.info("[%s] %s → %s", self.name, self.state.name, new_state.name)
            self.state = new_state
            self.state_since = time.monotonic()


# ──────────────────────────────────────────────
# Notification
# ──────────────────────────────────────────────

def send_mail(
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    sendmail_bin: str = "/run/wrappers/bin/sendmail",
) -> None:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)
    raw = msg.as_bytes()
    try:
        result = subprocess.run(
            [sendmail_bin, "-f", sender, recipient],
            input=raw,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            logging.error(
                "sendmail exited %d: %s", result.returncode, result.stderr.decode()
            )
        else:
            logging.info("Email sent to %s: %s", recipient, subject)
    except Exception as exc:
        logging.error("Failed to send email: %s", exc)


# ──────────────────────────────────────────────
# Ping helper
# ──────────────────────────────────────────────

def is_reachable(ip: str, timeout: int = 2) -> bool:
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), ip],
            capture_output=True,
            timeout=timeout + 2,
        )
        return result.returncode == 0
    except Exception:
        return False


# ──────────────────────────────────────────────
# State persistence (simple JSON file)
# ──────────────────────────────────────────────

def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save_state(path: Path, records: dict[str, APRecord], latch: bool) -> None:
    data = {
        "latch": latch,
        "aps": {
            ip: {
                "state": rec.state.name,
                "state_since": rec.state_since,
                "consecutive_failures": rec.consecutive_failures,
                "last_reboot_at": rec.last_reboot_at,
                "offline_since": rec.offline_since,
            }
            for ip, rec in records.items()
        },
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(path)


def _restore_state(path: Path, records: dict[str, APRecord]) -> None:
    """Restore per-AP counters from disk.
    LATCH is never restored — a service restart is the human's signal
    to try again from a clean slate."""
    saved = _load_state(path)
    for ip, s in saved.get("aps", {}).items():
        if ip not in records:
            continue
        rec = records[ip]
        try:
            rec.state = APState[s["state"]]
        except KeyError:
            rec.state = APState.OK
        # Never carry LATCH across restarts.
        if rec.state == APState.LATCH:
            rec.state = APState.OK
        rec.state_since = s.get("state_since", time.monotonic())
        rec.consecutive_failures = s.get("consecutive_failures", 0)
        rec.last_reboot_at = s.get("last_reboot_at", 0.0)
        rec.offline_since = s.get("offline_since")


# ──────────────────────────────────────────────
# Core daemon loop
# ──────────────────────────────────────────────

def run(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )

    # Password file (written by sops at runtime)
    password = Path(args.password_file).read_text().strip()

    # Controller client (always .100)
    controller = XiaomiClient(args.controller_ip, password, timeout=args.api_timeout)

    # One record + client per satellite
    satellites: list[tuple[str, str, XiaomiClient]] = []
    for spec in args.satellite:
        parts = spec.split("=", 1)
        if len(parts) != 2:
            sys.exit(f"--satellite must be NAME=IP, got: {spec!r}")
        name, ip = parts
        satellites.append((name, ip, XiaomiClient(ip, password, timeout=args.api_timeout)))

    records: dict[str, APRecord] = {
        ip: APRecord(ip=ip, name=name) for name, ip, _ in satellites
    }
    sat_clients: dict[str, XiaomiClient] = {ip: c for _, ip, c in satellites}

    state_path = Path(args.state_file)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    _restore_state(state_path, records)
    daemon_latch = False

    # Signal handler: graceful shutdown
    stop = False
    def _sig(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    logging.info(
        "mesh-guardian started. controller=%s satellites=%s",
        args.controller_ip,
        [f"{n}={ip}" for n, ip, _ in satellites],
    )

    while not stop:
        now_mono = time.monotonic()

        # ── Fetch topology ──────────────────────────────────────
        topo_ok = False
        wired_ips: set[str] = set()
        try:
            resp = controller.topology()
            if resp.get("code") == 0:
                topo_ok = True
                for leaf in resp.get("graph", {}).get("leafs", []):
                    if leaf.get("link_type") == "wired":
                        wired_ips.add(leaf.get("ip", ""))
        except APIError as e:
            logging.warning("Topology fetch failed: %s", e)

        if not topo_ok:
            logging.warning("Topology unavailable; skipping this cycle.")
            _save_state(state_path, records, daemon_latch)
            _sleep(args.check_interval, lambda: stop)
            continue

        # ── Per-AP state machine ────────────────────────────────
        for ip, rec in records.items():
            _tick_ap(
                rec=rec,
                ip=ip,
                client=sat_clients[ip],
                wired_ips=wired_ips,
                now_mono=now_mono,
                args=args,
                password=password,
                daemon_latch_ref=[daemon_latch],
            )
            if daemon_latch_ref := [daemon_latch]:
                daemon_latch = daemon_latch_ref[0]

        # ── Check if any AP just pushed us into LATCH ───────────
        latch_aps = [rec for rec in records.values() if rec.state == APState.LATCH]
        if latch_aps and not daemon_latch:
            daemon_latch = True
            names = ", ".join(rec.name for rec in latch_aps)
            body = (
                f"mesh-guardian on {socket.gethostname()} has reached the failure "
                f"limit ({args.max_failures} consecutive failures) for the following "
                f"Mesh satellite APs:\n\n  {names}\n\n"
                f"Automatic recovery has been suspended. Once you have investigated, "
                f"simply restart the service to resume monitoring:\n\n"
                f"  systemctl restart mesh-guardian\n\n"
                f"-- mesh-guardian"
            )
            send_mail(
                sender=args.mail_from,
                recipient=args.mail_to,
                subject=f"[mesh-guardian] Manual intervention required: {names}",
                body=body,
                sendmail_bin=args.sendmail,
            )
            logging.error("LATCH engaged. Sent alert to %s.", args.mail_to)

        _save_state(state_path, records, daemon_latch)
        _sleep(args.check_interval, lambda: stop)

    logging.info("mesh-guardian stopped.")


def _tick_ap(
    *,
    rec: APRecord,
    ip: str,
    client: XiaomiClient,
    wired_ips: set[str],
    now_mono: float,
    args: argparse.Namespace,
    password: str,
    daemon_latch_ref: list,
) -> None:
    daemon_latch = daemon_latch_ref[0]

    # ── LATCH: do nothing ─────────────────────────────────────
    if rec.state == APState.LATCH or daemon_latch:
        return

    is_wired = ip in wired_ips
    reachable = is_reachable(ip, timeout=args.ping_timeout)

    # Track unreachable / offline duration
    if not reachable:
        if rec.offline_since is None:
            rec.offline_since = now_mono
            logging.info("[%s] went unreachable", rec.name)
    else:
        if rec.offline_since is not None:
            logging.info(
                "[%s] came back online after %.0fs",
                rec.name,
                now_mono - rec.offline_since,
            )
        rec.offline_since = None

    offline_duration = (now_mono - rec.offline_since) if rec.offline_since else 0.0

    # ── REBOOTING: wait for wired recovery ───────────────────
    if rec.state == APState.REBOOTING:
        elapsed = now_mono - rec.state_since
        if is_wired:
            logging.info(
                "[%s] wired backhaul restored after %.0fs", rec.name, elapsed
            )
            rec.consecutive_failures = 0
            rec.transition(APState.COOLDOWN)
            return
        if elapsed >= args.reboot_wait:
            logging.warning(
                "[%s] still not wired after %.0fs reboot wait",
                rec.name,
                elapsed,
            )
            rec.consecutive_failures += 1
            if rec.consecutive_failures >= args.max_failures:
                rec.transition(APState.LATCH)
            else:
                # Try again next cycle (go back to WIRELESS so trigger fires)
                rec.transition(APState.WIRELESS)
        return

    # ── COOLDOWN: mandatory calm period after reboot ──────────
    if rec.state == APState.COOLDOWN:
        elapsed = now_mono - rec.state_since
        if elapsed >= args.cooldown:
            logging.info("[%s] cooldown finished", rec.name)
            rec.transition(APState.OK if is_wired else APState.WIRELESS)
        return

    # ── AP unreachable: do not trigger reboot ─────────────────
    if not reachable:
        if offline_duration < args.offline_grace:
            logging.debug(
                "[%s] unreachable for %.0f/%ds (offline grace)", rec.name,
                offline_duration, args.offline_grace,
            )
            # Suppress state changes while inside grace window
            return
        # Long absence — likely power cut or maintenance.  Stay put, no reboot.
        if rec.state != APState.WIRELESS:
            logging.info(
                "[%s] unreachable for %.0fs (offline grace exhausted); holding",
                rec.name, offline_duration,
            )
            rec.transition(APState.WIRELESS)
        return

    # ── Reachable: evaluate link type ─────────────────────────
    if is_wired:
        if rec.state != APState.OK:
            logging.info("[%s] link_type=wired; back to OK", rec.name)
            rec.consecutive_failures = 0
        rec.transition(APState.OK)
        return

    # link_type != "wired" and device is reachable
    if rec.state == APState.OK:
        rec.transition(APState.WIRELESS)
        logging.warning("[%s] switched to wireless backhaul", rec.name)

    # WIRELESS: check dwell time before rebooting
    wireless_secs = now_mono - rec.state_since
    if wireless_secs < args.wireless_dwell:
        logging.debug(
            "[%s] wireless for %.0f/%ds (dwell)", rec.name,
            wireless_secs, args.wireless_dwell,
        )
        return

    # Trigger reboot
    logging.warning(
        "[%s] wireless for %.0fs; rebooting (attempt %d/%d)",
        rec.name, wireless_secs,
        rec.consecutive_failures + 1, args.max_failures,
    )
    try:
        client.reboot()
        rec.last_reboot_at = now_mono
        rec.transition(APState.REBOOTING)
    except APIError as e:
        logging.error("[%s] reboot API failed: %s", rec.name, e)
        rec.consecutive_failures += 1
        if rec.consecutive_failures >= args.max_failures:
            rec.transition(APState.LATCH)


def _sleep(seconds: int, stop_fn) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if stop_fn():
            return
        time.sleep(min(1.0, deadline - time.monotonic()))


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Xiaomi Mesh wired-backhaul recovery guardian"
    )
    p.add_argument("--controller-ip", required=True,
                   help="IP of the controller (master) AP")
    p.add_argument("--satellite", metavar="NAME=IP", action="append", default=[],
                   help="Satellite AP; repeat for each unit (e.g. living-room=192.168.1.2)")
    p.add_argument("--password-file", required=True,
                   help="Path to file containing the AP admin password (sops-rendered)")

    # Timing
    p.add_argument("--check-interval", type=int, default=60,
                   help="Seconds between topology polls (default: 60)")
    p.add_argument("--wireless-dwell", type=int, default=120,
                   help="Seconds an AP must stay wireless before reboot is triggered (default: 120)")
    p.add_argument("--reboot-wait", type=int, default=180,
                   help="Seconds to wait for wired recovery after reboot (default: 180)")
    p.add_argument("--cooldown", type=int, default=300,
                   help="Seconds of calm after successful recovery before resuming normal checks (default: 300)")
    p.add_argument("--offline-grace", type=int, default=600,
                   help="Seconds an AP may be unreachable before we consider it a long-term absence (power cut / maintenance); no reboot is issued during this window or after (default: 600)")

    # Failure cap
    p.add_argument("--max-failures", type=int, default=3,
                   help="Consecutive failures before LATCH and email alert (default: 3)")

    # Mail
    p.add_argument("--mail-to", default="",
                   help="Alert recipient email address")
    p.add_argument("--mail-from", default="",
                   help="Sender address (must match postfix relay account)")
    p.add_argument("--sendmail", default="/run/wrappers/bin/sendmail",
                   help="Path to sendmail binary (default: /run/wrappers/bin/sendmail)")

    # Misc
    p.add_argument("--api-timeout", type=int, default=10,
                   help="HTTP timeout for Xiaomi API calls in seconds (default: 10)")
    p.add_argument("--ping-timeout", type=int, default=2,
                   help="Ping timeout per AP in seconds (default: 2)")
    p.add_argument("--state-file", default="/var/lib/mesh-guardian/state.json",
                   help="Path to JSON state persistence file")
    p.add_argument("--debug", action="store_true",
                   help="Enable verbose debug logging")

    args = p.parse_args()
    run(args)


if __name__ == "__main__":
    main()
