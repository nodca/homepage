#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(os.environ.get("SERVER_STATUS_CONFIG", "/etc/homepage-status/config.json"))
DEFAULT_REFRESH_INTERVAL = 15

METRICS_SCRIPT = r"""
set -eu

read -r _ user nice system idle iowait irq softirq steal _ < /proc/stat
sleep 0.5
read -r _ user2 nice2 system2 idle2 iowait2 irq2 softirq2 steal2 _ < /proc/stat

prev_idle=$((idle + iowait))
idle_now=$((idle2 + iowait2))
prev_non_idle=$((user + nice + system + irq + softirq + steal))
non_idle_now=$((user2 + nice2 + system2 + irq2 + softirq2 + steal2))

total_prev=$((prev_idle + prev_non_idle))
total_now=$((idle_now + non_idle_now))
total_delta=$((total_now - total_prev))
idle_delta=$((idle_now - prev_idle))

cpu_percent=$(awk -v total="$total_delta" -v idle="$idle_delta" 'BEGIN {
  if (total <= 0) {
    printf "0.0"
  } else {
    printf "%.1f", ((total - idle) * 100) / total
  }
}')

memory_percent=$(awk '
  /MemTotal:/ { total = $2 }
  /MemAvailable:/ { available = $2 }
  END {
    used = total - available
    if (total <= 0) {
      printf "0.0"
    } else {
      printf "%.1f", (used * 100) / total
    }
  }
' /proc/meminfo)

cpu_cores=$(nproc)
memory_total_gb=$(awk '
  /MemTotal:/ {
    printf "%.1f", $2 / 1024 / 1024
  }
' /proc/meminfo)

disk_percent=$(df -P / | awk 'NR == 2 { gsub(/%/, "", $5); printf "%.1f", $5 }')

printf '{"cpu":%s,"memory":%s,"disk":%s,"cpuCores":%s,"totalMemoryGb":%s}\n' \
  "$cpu_percent" \
  "$memory_percent" \
  "$disk_percent" \
  "$cpu_cores" \
  "$memory_total_gb"
"""


@dataclass
class AppState:
    config: dict[str, Any]
    payload: dict[str, Any]
    refresh_interval: int
    lock: threading.Lock


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        config = json.load(file)

    nodes = config.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("Config must include a non-empty 'nodes' array")

    refresh_interval = int(config.get("refresh_interval_seconds", DEFAULT_REFRESH_INTERVAL))
    if refresh_interval < 5:
        raise ValueError("refresh_interval_seconds must be at least 5 seconds")

    config["refresh_interval_seconds"] = refresh_interval
    config.setdefault("listen_host", "127.0.0.1")
    config.setdefault("listen_port", 19529)
    return config


def run_metrics_command(command: list[str], *, input_text: str | None = None) -> tuple[bool, dict[str, Any] | None, str | None]:
    completed = subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=12,
        check=False,
    )
    if completed.returncode != 0:
        error_message = completed.stderr.strip() or completed.stdout.strip() or f"Command failed with exit code {completed.returncode}"
        return False, None, error_message

    try:
        payload = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as exc:
        return False, None, f"Invalid metrics payload: {exc}"

    snapshot = {
        "metrics": {
            "cpu": float(payload.get("cpu", 0.0)),
            "memory": float(payload.get("memory", 0.0)),
            "disk": float(payload.get("disk", 0.0)),
        },
        "hardware": {
            "cpuCores": int(payload.get("cpuCores", 0)),
            "totalMemoryGb": float(payload.get("totalMemoryGb", 0.0)),
        },
    }
    return True, snapshot, None


def collect_node(node: dict[str, Any]) -> dict[str, Any]:
    public_data = {
        "id": node["id"],
        "name": node["name"],
        "region": node["region"],
        "flag": node["flag"],
    }

    if node.get("mode") == "local":
        success, snapshot, error_message = run_metrics_command(["bash", "-s"], input_text=METRICS_SCRIPT)
    else:
        ssh_target = node.get("ssh_target")
        if not ssh_target:
            return {
                **public_data,
                "status": "offline",
                "metrics": None,
                "updatedAt": None,
            }

        ssh_command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "StrictHostKeyChecking=yes",
            ssh_target,
            "bash -s",
        ]
        success, snapshot, error_message = run_metrics_command(ssh_command, input_text=METRICS_SCRIPT)

    if not success or snapshot is None:
        print(f"[warn] {node['id']}: {error_message or 'Unknown error'}", flush=True)
        return {
            **public_data,
            "status": "offline",
            "metrics": None,
            "hardware": None,
            "updatedAt": None,
        }

    return {
        **public_data,
        "status": "online",
        "metrics": snapshot["metrics"],
        "hardware": snapshot["hardware"],
        "updatedAt": now_iso(),
    }


def refresh_payload(state: AppState) -> None:
    nodes = state.config["nodes"]
    servers = [collect_node(node) for node in nodes]
    payload = {
        "generatedAt": now_iso(),
        "refreshIntervalSeconds": state.refresh_interval,
        "servers": servers,
    }

    with state.lock:
        state.payload = payload


def start_refresh_thread(state: AppState) -> threading.Event:
    stop_event = threading.Event()

    def run() -> None:
        while not stop_event.is_set():
            refresh_payload(state)
            stop_event.wait(state.refresh_interval)

    thread = threading.Thread(target=run, name="server-status-refresh", daemon=True)
    thread.start()
    return stop_event


class StatusHandler(BaseHTTPRequestHandler):
    state: AppState

    def do_GET(self) -> None:
        if self.path in ("/", "/server-status", "/api/server-status"):
            with self.state.lock:
                payload = dict(self.state.payload)
            self.respond_json(HTTPStatus.OK, payload)
            return

        if self.path == "/healthz":
            self.respond_json(HTTPStatus.OK, {"status": "ok", "generatedAt": now_iso()})
            return

        self.respond_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def respond_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_initial_payload(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "generatedAt": now_iso(),
        "refreshIntervalSeconds": config["refresh_interval_seconds"],
        "servers": [
            {
                "id": node["id"],
                "name": node["name"],
                "region": node["region"],
                "flag": node["flag"],
                "status": "offline",
                "metrics": None,
                "hardware": None,
                "updatedAt": None,
            }
            for node in config["nodes"]
        ],
    }


def main() -> None:
    config = load_config()
    state = AppState(
        config=config,
        payload=build_initial_payload(config),
        refresh_interval=config["refresh_interval_seconds"],
        lock=threading.Lock(),
    )

    stop_event = start_refresh_thread(state)

    StatusHandler.state = state
    server = ThreadingHTTPServer((config["listen_host"], int(config["listen_port"])), StatusHandler)

    try:
        server.serve_forever()
    finally:
        stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
