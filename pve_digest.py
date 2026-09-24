#!/usr/bin/env python3
"""Proxmox VE LXC health digest -> ntfy.

Pulls the status of every LXC container on a Proxmox node via the PVE REST API,
builds a short phone-friendly summary, and POSTs it to an ntfy topic.

Standard library only, so it runs on a stock Proxmox host or any small LXC.
Configuration comes from environment variables (see pve-digest.env.example).
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

TIMEOUT = 20


# Config


def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"error: environment variable {name} is required")
    return value


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    pve_host: str
    pve_token_id: str
    pve_token_secret: str
    pve_node: str | None
    pve_verify_tls: bool
    pve_ca_bundle: str | None
    ntfy_url: str
    ntfy_topic: str
    ntfy_token: str | None
    ntfy_priority: str
    disk_warn_pct: float
    mem_warn_pct: float
    rrd_timeframe: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            pve_host=env("PVE_HOST", required=True).rstrip("/"),
            pve_token_id=env("PVE_TOKEN_ID", required=True),
            pve_token_secret=env("PVE_TOKEN_SECRET", required=True),
            pve_node=env("PVE_NODE") or None,
            pve_verify_tls=env_bool("PVE_VERIFY_TLS", True),
            pve_ca_bundle=env("PVE_CA_BUNDLE") or None,
            ntfy_url=env("NTFY_URL", "https://ntfy.your.domain").rstrip("/"), # Change this to your ntfy server URL.
            ntfy_topic=env("NTFY_TOPIC", required=True),
            ntfy_token=env("NTFY_TOKEN") or None,
            ntfy_priority=env("NTFY_PRIORITY", "default"),
            disk_warn_pct=float(env("DISK_WARN_PCT", "85")),
            mem_warn_pct=float(env("MEM_WARN_PCT", "90")),
            rrd_timeframe=env("RRD_TIMEFRAME", "week"),
        )


# PVE API


class ProxmoxClient:
    def __init__(self, cfg: Config):
        base = cfg.pve_host
        if not base.startswith(("http://", "https://")):
            base = f"https://{base}"
        if urllib.parse.urlparse(base).port is None:
            base = f"{base}:8006"
        self.base = f"{base}/api2/json"
        self.headers = {
            "Authorization": f"PVEAPIToken={cfg.pve_token_id}={cfg.pve_token_secret}",
            "Accept": "application/json",
        }
        if cfg.pve_ca_bundle:
            self.ctx = ssl.create_default_context(cafile=cfg.pve_ca_bundle)
        elif cfg.pve_verify_tls:
            self.ctx = ssl.create_default_context()
        else:
            # Proxmox ships with a self-signed cert; allow opting out of verification.
            self.ctx = ssl._create_unverified_context()

    def get(self, path: str, **params) -> object:
        url = f"{self.base}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=self.ctx) as resp:
                return json.load(resp)["data"]
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"PVE API {path}: HTTP {e.code} {e.reason}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"PVE API {path}: {e.reason}") from e


# Data


@dataclass
class Container:
    vmid: int
    name: str
    status: str
    uptime: int = 0
    cpu: float = 0.0  # fraction of allotted cores, instantaneous
    cpus: float = 0.0
    mem: int = 0
    maxmem: int = 0
    disk: int = 0
    maxdisk: int = 0
    onboot: bool = False
    cpu_avg: float | None = None  # from RRD over RRD_TIMEFRAME
    cpu_peak: float | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def mem_pct(self) -> float | None:
        return pct(self.mem, self.maxmem)

    @property
    def disk_pct(self) -> float | None:
        return pct(self.disk, self.maxdisk)


@dataclass
class NodeSummary:
    name: str
    uptime: int
    cpu: float
    mem: int
    maxmem: int
    rootfs_used: int
    rootfs_total: int


def pct(used: float, total: float) -> float | None:
    return (used / total * 100) if total else None


def resolve_node(client: ProxmoxClient, configured: str | None) -> str:
    nodes = client.get("/nodes")
    names = [n["node"] for n in nodes]
    if configured:
        if configured not in names:
            raise RuntimeError(f"node {configured!r} not found (have: {', '.join(names)})")
        return configured
    if len(names) == 1:
        return names[0]
    raise RuntimeError(f"multiple nodes found ({', '.join(names)}); set PVE_NODE")


def collect(client: ProxmoxClient, node: str, cfg: Config) -> tuple[NodeSummary, list[Container]]:
    ns = client.get(f"/nodes/{node}/status")
    node_summary = NodeSummary(
        name=node,
        uptime=int(ns.get("uptime", 0)),
        cpu=float(ns.get("cpu", 0)),
        mem=int(ns.get("memory", {}).get("used", 0)),
        maxmem=int(ns.get("memory", {}).get("total", 0)),
        rootfs_used=int(ns.get("rootfs", {}).get("used", 0)),
        rootfs_total=int(ns.get("rootfs", {}).get("total", 0)),
    )

    containers: list[Container] = []
    for raw in client.get(f"/nodes/{node}/lxc"):
        vmid = int(raw["vmid"])
        ct = Container(
            vmid=vmid,
            name=raw.get("name", f"ct{vmid}"),
            status=raw.get("status", "unknown"),
            uptime=int(raw.get("uptime") or 0),
            cpu=float(raw.get("cpu") or 0),
            cpus=float(raw.get("cpus") or 0),
            mem=int(raw.get("mem") or 0),
            maxmem=int(raw.get("maxmem") or 0),
            disk=int(raw.get("disk") or 0),
            maxdisk=int(raw.get("maxdisk") or 0),
        )

        try:
            conf = client.get(f"/nodes/{node}/lxc/{vmid}/config")
            ct.onboot = str(conf.get("onboot", "0")) == "1"
        except RuntimeError:
            pass  # config needs VM.Audit; skip onboot-based warnings if unavailable

        if ct.running:
            try:
                rrd = client.get(
                    f"/nodes/{node}/lxc/{vmid}/rrddata",
                    timeframe=cfg.rrd_timeframe,
                    cf="AVERAGE",
                )
                samples = [float(p["cpu"]) for p in rrd if p.get("cpu") is not None]
                if samples:
                    ct.cpu_avg = sum(samples) / len(samples)
                    ct.cpu_peak = max(samples)
            except RuntimeError:
                pass

        evaluate(ct, cfg)
        containers.append(ct)

    containers.sort(key=lambda c: c.vmid)
    return node_summary, containers


def evaluate(ct: Container, cfg: Config) -> None:
    if not ct.running:
        if ct.onboot:
            ct.warnings.append("stopped but set to start on boot")
        return
    if ct.disk_pct is not None and ct.disk_pct >= cfg.disk_warn_pct:
        ct.warnings.append(f"disk {ct.disk_pct:.0f}%")
    if ct.mem_pct is not None and ct.mem_pct >= cfg.mem_warn_pct:
        ct.warnings.append(f"memory {ct.mem_pct:.0f}%")


# Formatting


def human_bytes(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            if unit in ("B", "K", "M"):
                return f"{n:.0f}{unit}"
            return f"{n:.1f}{unit}".replace(".0", "")
        n /= 1024
    return f"{n:.1f}T"


def human_uptime(seconds: int) -> str:
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}%"


def build_digest(node: NodeSummary, containers: list[Container]) -> tuple[str, str, bool]:
    """Return (title, body, has_warnings)."""
    running = [c for c in containers if c.running]
    warned = [c for c in containers if c.warnings]

    title = f"{node.name}: {len(running)}/{len(containers)} LXCs running"
    if warned:
        title += f", {len(warned)} ⚠"

    lines = [
        f"Host · up {human_uptime(node.uptime)} · CPU {node.cpu * 100:.0f}%"
        f" · RAM {human_bytes(node.mem)}/{human_bytes(node.maxmem)}"
        f" ({fmt_pct(pct(node.mem, node.maxmem))})"
        f" · root {fmt_pct(pct(node.rootfs_used, node.rootfs_total))}",
    ]

    if warned:
        lines.append("")
        lines.append("Needs attention:")
        for c in warned:
            lines.append(f"⚠ {c.vmid} {c.name}: {', '.join(c.warnings)}")

    lines.append("")
    for c in containers:
        if c.running:
            lines.append(f"🟢 {c.vmid} {c.name} · up {human_uptime(c.uptime)}")
            if c.cpu_avg is not None:
                cpu = f"CPU {c.cpu_avg * 100:.0f}% avg/{c.cpu_peak * 100:.0f}% pk"
            else:
                cpu = f"CPU {c.cpu * 100:.0f}%"
            lines.append(
                f"   {cpu}"
                f" · RAM {human_bytes(c.mem)}/{human_bytes(c.maxmem)}"
                f" · Disk {human_bytes(c.disk)}/{human_bytes(c.maxdisk)}"
                f" ({fmt_pct(c.disk_pct)})"
            )
        else:
            icon = "🔴" if c.warnings else "⚪"
            lines.append(f"{icon} {c.vmid} {c.name} · {c.status}")

    if not containers:
        lines.append("No LXC containers found on this node.")

    return title, "\n".join(lines), bool(warned)


# ntfy


PRIORITIES = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5, "urgent": 5}


def publish(cfg: Config, title: str, body: str, priority: str, tags: list[str]) -> None:
    # JSON publishing (POST to the server root) keeps unicode in the title intact,
    # which plain HTTP headers can't carry.
    payload = {
        "topic": cfg.ntfy_topic,
        "title": title,
        "message": body,
        "priority": PRIORITIES.get(priority.lower(), 3) if not priority.isdigit() else int(priority),
        "tags": tags,
    }
    headers = {"Content-Type": "application/json"}
    if cfg.ntfy_token:
        headers["Authorization"] = f"Bearer {cfg.ntfy_token}"
    req = urllib.request.Request(
        cfg.ntfy_url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"ntfy: HTTP {e.code} {e.reason} {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"ntfy: {e.reason}") from e


# Main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="print the digest instead of sending it"
    )
    args = parser.parse_args()

    cfg = Config.from_env()

    try:
        client = ProxmoxClient(cfg)
        node = resolve_node(client, cfg.pve_node)
        node_summary, containers = collect(client, node, cfg)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        if not args.dry_run:
            # Tell the phone the digest itself is broken, rather than failing without notice.
            try:
                publish(cfg, "Proxmox digest failed", str(e), "high", ["rotating_light"])
            except RuntimeError as ne:
                print(f"error: {ne}", file=sys.stderr)
        return 1

    title, body, has_warnings = build_digest(node_summary, containers)

    if args.dry_run:
        print(title)
        print("-" * len(title))
        print(body)
        return 0

    priority = "high" if has_warnings else cfg.ntfy_priority
    tags = ["warning"] if has_warnings else ["white_check_mark"]
    try:
        publish(cfg, title, body, priority, tags)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"sent: {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
