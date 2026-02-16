#!/usr/bin/env python3
"""
Cron-friendly watchdog:

- HTTP GET http://localhost:8080/snapshot
- If not 200 OK: `systemctl restart crowsnest`, wait, then re-check
- Repeat up to 5 times, increasing wait by +30s each time (30, 60, 90, 120, 150)

State in /tmp:
- STATUS_FILE: records whether the last run succeeded and details
- THROTTLE_FILE: if the *previous execution failed*, only run 1 in 10 cron invocations
- LOCK_FILE: prevents overlapping runs

Adjust constants below if you want different file names/paths.
"""

import json
import os
import time
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone

# --- Config ---
URL = "http://localhost:8080/snapshot"
TIMEOUT_SECONDS = 5

MAX_RETRIES = 5
BASE_WAIT_SECONDS = 30
WAIT_INCREMENT_SECONDS = 30

STATUS_FILE = "/tmp/crowsnest_watchdog_status.json"
THROTTLE_FILE = "/tmp/crowsnest_watchdog_throttle.json"
LOCK_FILE = "/tmp/crowsnest_watchdog.lock"

SERVICE_NAME = "crowsnest"
RESTART_CMD = ["systemctl", "restart", SERVICE_NAME]

# If last run failed, only proceed on 1 out of every N cron invocations
RUN_EVERY_N_AFTER_FAILURE = 10


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def http_get_status(url: str, timeout: int) -> tuple[int | None, str | None]:
    """
    Returns (status_code, error_string).
    status_code is None if we couldn't connect / request failed before response.
    """
    try:
        print(f"Testing request at {url}")
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            print("Response OK")
            return int(getattr(resp, "status", 200)), None
    except urllib.error.HTTPError as e:
        # Server responded with an HTTP error status code (e.g. 404/500)
        print(f"Failed request with error code {e.code}")
        return int(e.code), f"HTTPError: {e}"
    except Exception as e:
        # Connection refused, timeout, DNS, etc.
        print(f"Failed request with network error {e}")
        return None, f"RequestError: {e}"


def restart_service() -> tuple[bool, str]:
    try:
        print("Restarting service...")
        cp = subprocess.run(RESTART_CMD, capture_output=True, text=True)
        if cp.returncode == 0:
            return True, "ok"
        msg = (cp.stderr or cp.stdout or "").strip()
        return False, f"systemctl failed rc={cp.returncode}: {msg}"
    except Exception as e:
        return False, f"systemctl exception: {e}"


def read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_json_atomic(path: str, data: dict) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def should_throttle() -> tuple[bool, dict]:
    """
    Returns (skip_run, throttle_state).
    If the previous execution failed, we run only 1 in RUN_EVERY_N_AFTER_FAILURE invocations.
    """
    state = read_json(THROTTLE_FILE)
    last_failed = bool(state.get("last_failed", False))

    if not last_failed:
        # No throttling; reset counter to keep state tidy
        state["counter"] = 0
        return False, state

    counter = int(state.get("counter", 0)) + 1
    state["counter"] = counter

    # Run only when counter % N == 0
    if counter % RUN_EVERY_N_AFTER_FAILURE != 0:
        return True, state

    return False, state


def update_throttle_state(state: dict, last_failed: bool) -> None:
    state["last_failed"] = bool(last_failed)
    if not last_failed:
        state["counter"] = 0
    state["updated_at"] = utc_now_iso()
    write_json_atomic(THROTTLE_FILE, state)


def write_status(succeeded: bool, details: dict) -> None:
    payload = {
        "updated_at": utc_now_iso(),
        "succeeded": bool(succeeded),
        **details,
    }
    write_json_atomic(STATUS_FILE, payload)


def main() -> int:
    # --- Lock to avoid overlapping cron runs ---
    try:
        import fcntl  # POSIX only
        lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another instance is running
            return 0
    except Exception:
        # If locking fails for any reason, proceed (best effort)
        lock_fd = None

    # --- Throttle if last execution failed ---
    skip, throttle_state = should_throttle()
    if skip:
        print("Skipping run due to previous failures")
        update_throttle_state(throttle_state, last_failed=True)
        write_status(
            succeeded=False,
            details={
                "skipped_due_to_throttle": True,
                "note": f"Previous run failed; running only 1 in {RUN_EVERY_N_AFTER_FAILURE} invocations.",
            },
        )
        return 0

    # --- Main logic: check, restart on non-200, retry with increasing waits ---
    attempts = []
    wait = BASE_WAIT_SECONDS
    succeeded = False
    restart_errors = []

    for i in range(1, MAX_RETRIES + 1):
        code, err = http_get_status(URL, TIMEOUT_SECONDS)
        attempts.append(
            {
                "try": i,
                "http_status": code,
                "http_error": err,
                "checked_at": utc_now_iso(),
            }
        )

        if code == 200:
            succeeded = True
            break

        ok, msg = restart_service()
        attempts[-1]["restart_called"] = True
        attempts[-1]["restart_ok"] = ok
        attempts[-1]["restart_msg"] = msg
        if not ok:
            restart_errors.append(msg)

        # wait before next check (except after the last attempt)
        if i < MAX_RETRIES:
            print(f"Sleeping for {wait} seconds")
            time.sleep(wait)
            wait += WAIT_INCREMENT_SECONDS

    # --- Persist status + throttle state ---
    write_status(
        succeeded=succeeded,
        details={
            "url": URL,
            "service": SERVICE_NAME,
            "max_retries": MAX_RETRIES,
            "base_wait_seconds": BASE_WAIT_SECONDS,
            "wait_increment_seconds": WAIT_INCREMENT_SECONDS,
            "attempts": attempts,
            "restart_errors": restart_errors,
            "skipped_due_to_throttle": False,
        },
    )
    update_throttle_state(throttle_state, last_failed=(not succeeded))

    # release lock
    if lock_fd is not None:
        try:
            os.close(lock_fd)
        except Exception:
            pass

    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
