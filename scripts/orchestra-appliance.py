#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(os.environ.get("ORCHESTRA_DATA_ROOT", "/var/lib/orchestra")).resolve()
APP = Path("/opt/orchestra/app")
STATUS = ROOT / "runtime/appliance/status.json"
DOCKER_ENV = {
    **os.environ,
    "DOCKER_HOST": "unix:///run/orchestra-docker/docker.sock",
    "DOCKER_CONTEXT": "default",
    "DOCKER_CONFIG": "/nonexistent/orchestra-empty-docker-config",
}
STOPPING = False
PUBLIC_ORIGIN = os.environ.get("ORCHESTRA_PUBLIC_ORIGIN", "http://127.0.0.1:8080")
PUBLIC_HOST = urlsplit(PUBLIC_ORIGIN).netloc


def log(component: str, message: str) -> None:
    print(f"[{component}] {message}", flush=True)


def run_checked(arguments: list[str], component: str = "init") -> None:
    log(component, "running " + " ".join(arguments))
    subprocess.run(arguments, check=True, env=DOCKER_ENV)


def wait_url(url: str, seconds: int, *, host: str | None = None) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(
                url,
                headers={"Host": host} if host is not None else {},
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.5)
    raise RuntimeError(f"readiness timeout: {url}")


def probe_controller(seconds: int) -> None:
    subprocess.run(
        [
            "python3", str(APP / "scripts/orchestra-controller-probe.py"),
            "--base-url", "http://127.0.0.1:8765", "--wait-seconds", str(seconds),
        ],
        env=DOCKER_ENV,
        check=True,
        stdout=subprocess.DEVNULL,
        timeout=seconds + 5,
    )


def initialize() -> None:
    if ROOT != Path("/var/lib/orchestra"):
        raise RuntimeError("canonical appliance data root must be /var/lib/orchestra")
    for relative in (
        "state/controller", "state/hermes-home", "state/sandboxes", "runtime/appliance",
        "runtime/supervisor", "workspaces", "project-data", "backups", "logs",
    ):
        (ROOT / relative).mkdir(parents=True, exist_ok=True, mode=0o750)
    secrets = ROOT / "secrets"
    secrets.mkdir(mode=0o700, exist_ok=True)
    secrets.chmod(0o700)
    repository = ROOT / "repo"
    if repository.is_symlink() and repository.resolve() == APP:
        pass
    elif not repository.exists():
        repository.symlink_to(APP, target_is_directory=True)
    else:
        raise RuntimeError("data-root repo path is not the packaged application")
    for _ in range(90):
        result = subprocess.run(
            ["docker", "info"], env=DOCKER_ENV,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if result.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("private runtime readiness timeout")
    commands = (
        ["python3", str(APP / "scripts/orchestra-controller-session.py"), "ensure"],
        ["python3", str(APP / "scripts/orchestra-db.py"), "migrate"],
        ["python3", str(APP / "scripts/orchestra-db.py"), "integrity"],
        ["python3", str(APP / "scripts/orchestra-controller-operator.py"), "ensure"],
        ["python3", str(APP / "scripts/orchestra-roles.py"), "sync"],
        ["python3", str(APP / "scripts/orchestra-registry.py"), "validate"],
        ["python3", str(APP / "scripts/orchestra-registry.py"), "sync"],
    )
    for command in commands:
        run_checked(command)


def output_reader(component: str, stream: object) -> None:
    for line in stream:  # type: ignore[union-attr]
        log(component, line.rstrip("\n"))


def spawn(component: str, arguments: list[str]) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        arguments, env=DOCKER_ENV, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True,
    )
    assert process.stdout is not None
    threading.Thread(target=output_reader, args=(component, process.stdout), daemon=True).start()
    log(component, f"started pid={process.pid}")
    return process


def write_status(processes: dict[str, subprocess.Popen[str]], ready: bool) -> None:
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATUS.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "ready": ready,
        "pid": os.getpid(),
        "children": {name: process.pid for name, process in processes.items()},
    }, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(STATUS)


def terminate(processes: dict[str, subprocess.Popen[str]]) -> None:
    global STOPPING
    STOPPING = True
    write_status(processes, False)
    log("appliance", "forwarding SIGTERM to children")
    for process in processes.values():
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and any(p.poll() is None for p in processes.values()):
        time.sleep(0.1)
    for process in processes.values():
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def run() -> None:
    initialize()
    processes: dict[str, subprocess.Popen[str]] = {}
    signal.signal(signal.SIGTERM, lambda *_: terminate(processes))
    signal.signal(signal.SIGINT, lambda *_: terminate(processes))
    processes["controller"] = spawn("controller", [
        "python3", str(APP / "scripts/orchestra-controller-api.py"), "serve",
        "--host", "127.0.0.1", "--port", "8765", "--log-level", "INFO",
    ])
    probe_controller(30)
    processes["console"] = spawn("console", [
        "python3", str(APP / "scripts/orchestra-console.py"), "serve",
        "--root", str(APP / "console/dist"), "--host", "0.0.0.0", "--port", "8080",
        "--public-bind",
        "--max-connections", "16", "--controller-host", "127.0.0.1",
        "--controller-port", "8765", "--controller-origin",
        os.environ.get("ORCHESTRA_PUBLIC_ORIGIN", "http://127.0.0.1:8080"),
        "--controller-timeout", "5",
    ])
    wait_url("http://127.0.0.1:8080/", 30, host=PUBLIC_HOST)
    processes["supervisor"] = spawn("supervisor", [
        "python3", str(APP / "scripts/orchestra-supervisor.py"), "run",
    ])
    processes["orchestrator"] = spawn("orchestrator", [
        "python3", str(APP / "scripts/orchestra-orchestrator.py"), "daemon",
    ])
    processes["notifier"] = spawn("notifier", [
        "python3", str(APP / "scripts/orchestra-notifier.py"), "daemon",
    ])
    write_status(processes, True)
    log("appliance", "ready")
    while not STOPPING:
        for name, process in processes.items():
            returncode = process.poll()
            if returncode is not None:
                log("appliance", f"mandatory component {name} exited rc={returncode}")
                terminate(processes)
                raise SystemExit(returncode or 1)
        time.sleep(0.25)
    for process in processes.values():
        process.wait()


def health() -> None:
    try:
        payload = json.loads(STATUS.read_text(encoding="utf-8"))
        if payload.get("ready") is not True:
            raise RuntimeError("appliance is not ready")
        for pid in payload.get("children", {}).values():
            os.kill(int(pid), 0)
        probe_controller(2)
        wait_url("http://127.0.0.1:8080/", 2, host=PUBLIC_HOST)
        subprocess.run(["docker", "info"], env=DOCKER_ENV, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        log("health", str(error))
        raise SystemExit(1) from error


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "run":
        run()
    elif command == "health":
        health()
    else:
        raise SystemExit("usage: orchestra-appliance.py [run|health]")
