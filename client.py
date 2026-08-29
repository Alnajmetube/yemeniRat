#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Worker Client for centralized task management.
Runs as a background process on Linux and Termux.
"""

import sys
import os
import time
import json
import logging
import argparse
import subprocess
import shutil
import signal
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
import fcntl

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================ Constants ====================================
HOME = Path(os.environ.get("HOME", str(Path.home())))
CONFIG_PATH = HOME / ".worker_client.conf"
LOG_PATH = HOME / ".worker_client.log"
QUEUE_DIR = HOME / ".worker_client_queue"
LOCK_FILE = Path("/tmp/worker_client.lock")
PID_FILE = Path("/tmp/worker_client.pid")

DEFAULT_POLL_INTERVAL = 2
MAX_BACKOFF = 60
HEALTH_CHECK_INTERVAL = 60
CANCEL_CHECK_INTERVAL = 2
MAX_REPORT_LENGTH = 10000  # increased to accommodate longer outputs

_lock_fd = None  # file descriptor holding the lock


# ============================ Logging ======================================
def setup_logging(foreground: bool = False) -> None:
    """Configure logging to file and optionally to console."""
    log_format = '%(asctime)s - %(levelname)s - %(message)s'
    date_format = '%Y-%m-%d %H:%M:%S'
    handlers = [logging.FileHandler(LOG_PATH)]
    if foreground:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        datefmt=date_format,
        handlers=handlers
    )


# ============================ Configuration ===============================
def load_config() -> Dict[str, str]:
    """Load configuration from ~/.worker_client.conf."""
    if not CONFIG_PATH.exists():
        logging.error(f"Config file not found: {CONFIG_PATH}")
        sys.exit(1)
    config = {}
    with open(CONFIG_PATH, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, value = line.split('=', 1)
                config[key.strip()] = value.strip().strip('"\'')
    required = ['SERVER_URL', 'TOKEN']
    for key in required:
        if key not in config:
            logging.error(f"Missing required config key: {key}")
            sys.exit(1)
    return config


def write_config(url: str, token: str) -> None:
    """Write configuration file with secure permissions."""
    content = f'SERVER_URL="{url}"\nTOKEN="{token}"\n'
    with open(CONFIG_PATH, 'w') as f:
        f.write(content)
    os.chmod(CONFIG_PATH, 0o600)


# ============================ Lock and PID ================================
def acquire_lock() -> bool:
    """Try to acquire an exclusive lock using fcntl.flock."""
    global _lock_fd
    try:
        _lock_fd = open(LOCK_FILE, 'w')
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        return True
    except (IOError, OSError):
        return False


def release_lock() -> None:
    """Release the lock (close file descriptor)."""
    global _lock_fd
    if _lock_fd:
        try:
            _lock_fd.close()
        except Exception:
            pass
        _lock_fd = None


def write_pid() -> None:
    """Write current PID to PID file."""
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))
    os.chmod(PID_FILE, 0o644)


def remove_pid() -> None:
    """Remove PID file."""
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def read_pid() -> Optional[int]:
    """Read PID from file, return None if not exists or invalid."""
    try:
        with open(PID_FILE, 'r') as f:
            return int(f.read().strip())
    except Exception:
        return None


# ============================ HTTP Client =================================
def create_session() -> requests.Session:
    """Create a requests session with retry strategy."""
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


# ============================ API Calls ===================================
def health_check(session: requests.Session, config: Dict[str, str]) -> bool:
    """Check server health. Return True if status is 'active' or 'ok'."""
    url = f"{config['SERVER_URL']}/worker/health"
    params = {"token": config['TOKEN']}
    try:
        resp = session.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            status = data.get("status", "")
            return status in ("active", "ok")
        return False
    except Exception:
        return False


def get_current_task(session: requests.Session, config: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Fetch current task. Return task dict or None."""
    url = f"{config['SERVER_URL']}/worker/current-task"
    params = {"token": config['TOKEN']}
    try:
        resp = session.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            task = data.get("task")
            if task and task.get("status") == "pending":
                return task
        return None
    except Exception:
        return None


def start_task(session: requests.Session, config: Dict[str, str], task_id: int) -> bool:
    """Notify server that task is started. Return True if success."""
    url = f"{config['SERVER_URL']}/worker/start-task"
    data = {"token": config['TOKEN'], "task_id": task_id}
    try:
        resp = session.post(url, json=data, timeout=10)
        return resp.status_code == 200 and resp.json().get("status") == "running"
    except Exception:
        return False


def is_cancelled(session: requests.Session, config: Dict[str, str], task_id: int) -> bool:
    """Check if task is cancelled. Return True if cancelled."""
    url = f"{config['SERVER_URL']}/worker/is-cancelled"
    params = {"token": config['TOKEN'], "task_id": task_id}
    try:
        resp = session.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("cancelled", False)
        return False
    except Exception:
        return False


def send_report(session: requests.Session, config: Dict[str, str], task_id: int, content: str) -> bool:
    """Send task report. Return True if success."""
    url = f"{config['SERVER_URL']}/worker/report"
    data = {"token": config['TOKEN'], "task_id": task_id, "content": content}
    try:
        resp = session.post(url, json=data, timeout=10)
        return resp.status_code == 200
    except Exception:
        return False


def upload_file(session: requests.Session, config: Dict[str, str], task_id: int, file_path: Path) -> bool:
    """Upload a file. Return True if success."""
    url = f"{config['SERVER_URL']}/worker/upload"
    try:
        with open(file_path, 'rb') as f:
            files = {'file': (file_path.name, f)}
            data = {'token': config['TOKEN'], 'task_id': task_id}
            resp = session.post(url, data=data, files=files, timeout=30)
            return resp.status_code == 200
    except Exception:
        return False


# ============================ Queue Management ============================
def queue_report(task_id: int, content: str) -> None:
    """Store a failed report to queue."""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    queue_file = QUEUE_DIR / f"report_{task_id}.json"
    with open(queue_file, 'w') as f:
        json.dump({"task_id": task_id, "content": content}, f)


def queue_upload(task_id: int, file_path: Path) -> None:
    """Store a failed upload to queue (copy file)."""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    queue_file = QUEUE_DIR / f"upload_{task_id}.file"
    shutil.copy2(file_path, queue_file)


def process_queue(session: requests.Session, config: Dict[str, str]) -> None:
    """Attempt to process queued reports and uploads."""
    if not QUEUE_DIR.exists():
        return
    # Process reports
    for queue_file in QUEUE_DIR.glob("report_*.json"):
        try:
            with open(queue_file, 'r') as f:
                data = json.load(f)
            task_id = data['task_id']
            content = data['content']
            if send_report(session, config, task_id, content):
                queue_file.unlink()
                logging.info(f"Queued report for task {task_id} sent.")
            else:
                logging.warning(f"Queued report for task {task_id} still failing.")
        except Exception as e:
            logging.error(f"Error processing queued report {queue_file}: {e}")
    # Process uploads
    for queue_file in QUEUE_DIR.glob("upload_*.file"):
        try:
            task_id = int(queue_file.stem.split('_')[1])
            if upload_file(session, config, task_id, queue_file):
                queue_file.unlink()
                logging.info(f"Queued upload for task {task_id} sent.")
            else:
                logging.warning(f"Queued upload for task {task_id} still failing.")
        except Exception as e:
            logging.error(f"Error processing queued upload {queue_file}: {e}")


# ============================ Upload Handler ==============================
def handle_upload_command(session: requests.Session, config: Dict[str, str],
                          task_id: int, command: str) -> None:
    """
    Handle upload command: upload <file_path>
    """
    parts = command.strip().split(maxsplit=1)
    if len(parts) < 2:
        error_msg = "Upload command missing file path. Usage: upload <file_path>"
        logging.error(f"Task {task_id}: {error_msg}")
        send_report(session, config, task_id, error_msg)
        return

    file_path_str = parts[1].strip()
    file_path = Path(file_path_str)

    if not file_path.exists():
        error_msg = f"File not found: {file_path_str}"
        logging.error(f"Task {task_id}: {error_msg}")
        send_report(session, config, task_id, error_msg)
        return

    if not file_path.is_file():
        error_msg = f"Not a file: {file_path_str}"
        logging.error(f"Task {task_id}: {error_msg}")
        send_report(session, config, task_id, error_msg)
        return

    logging.info(f"Task {task_id}: Uploading file {file_path.name}")

    if not start_task(session, config, task_id):
        logging.error(f"Task {task_id}: Failed to start task")
        return

    success = upload_file(session, config, task_id, file_path)
    if success:
        report_content = f"File uploaded successfully: {file_path.name} (Size: {file_path.stat().st_size} bytes)"
        logging.info(f"Task {task_id}: {report_content}")
    else:
        report_content = f"Failed to upload file: {file_path.name}"
        logging.warning(f"Task {task_id}: {report_content}")
        queue_upload(task_id, file_path)

    if not send_report(session, config, task_id, report_content):
        logging.warning(f"Task {task_id}: Report failed, queuing.")
        queue_report(task_id, report_content)


# ============================ Task Execution ==============================
def execute_task(session: requests.Session, config: Dict[str, str], task: Dict[str, Any]) -> None:
    """Execute a single task: run command, handle cancellation, report, upload."""
    task_id = task['id']
    command = task['content']
    logging.info(f"Executing task {task_id}: {command}")

    # Special command: upload
    if command.strip().lower().startswith('upload'):
        handle_upload_command(session, config, task_id, command)
        return

    # Normal shell command
    if not start_task(session, config, task_id):
        logging.error(f"Failed to start task {task_id}, aborting.")
        return

    # Use Popen to allow periodic cancellation checks
    proc = None
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            executable='/bin/bash'  # or /bin/sh, works on Linux/Termux
        )

        # Poll for completion while checking cancellation
        while True:
            try:
                retcode = proc.poll()
                if retcode is not None:
                    break
                # Check cancellation
                if is_cancelled(session, config, task_id):
                    logging.info(f"Task {task_id} cancelled, terminating process.")
                    proc.terminate()
                    proc.wait(timeout=5)
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
                    report_content = "Task cancelled by server."
                    send_report(session, config, task_id, report_content)
                    return
                time.sleep(CANCEL_CHECK_INTERVAL)
            except Exception as e:
                logging.warning(f"Error during cancellation check: {e}")
                time.sleep(1)

        stdout, stderr = proc.communicate(timeout=5)
        returncode = proc.returncode

        # Build report - combine both outputs
        output_parts = []
        if stdout and stdout.strip():
            output_parts.append(stdout.strip())
        if stderr and stderr.strip():
            output_parts.append(stderr.strip())

        report_content = "\n".join(output_parts) if output_parts else "(empty output)"

        # Truncate if too long
        if len(report_content) > MAX_REPORT_LENGTH:
            report_content = report_content[:MAX_REPORT_LENGTH] + "\n... (truncated)"

        logging.info(f"Task {task_id} completed with exit code {returncode}")

    except Exception as e:
        logging.error(f"Task {task_id} execution error: {e}", exc_info=True)
        report_content = f"Execution error: {str(e)}"
    finally:
        if proc and proc.poll() is None:
            proc.kill()
            proc.wait()

    # Send report
    if not send_report(session, config, task_id, report_content):
        logging.warning(f"Task {task_id} report failed, queuing.")
        queue_report(task_id, report_content)


# ============================ Main Loop ===================================
def run_forever(config: Dict[str, str]) -> None:
    """Main infinite loop: health check, get task, execute."""
    session = create_session()
    backoff = DEFAULT_POLL_INTERVAL
    last_health_check = 0

    while True:
        try:
            # Process queue first
            process_queue(session, config)

            # Health check periodically (do not block on failure)
            now = time.time()
            if now - last_health_check >= HEALTH_CHECK_INTERVAL:
                if health_check(session, config):
                    logging.debug("Health check OK")
                else:
                    logging.warning("Health check failed (server may be down)")
                last_health_check = now

            # Get current task
            task = get_current_task(session, config)
            if task:
                backoff = DEFAULT_POLL_INTERVAL  # reset backoff
                execute_task(session, config, task)
            else:
                logging.debug("No pending task, sleeping...")
                time.sleep(backoff)

        except Exception as e:
            logging.error(f"Main loop exception: {e}", exc_info=True)
            backoff = min(backoff * 2, MAX_BACKOFF)
            logging.warning(f"Backing off for {backoff} seconds")
            time.sleep(backoff)


# ============================ Install / Uninstall / Stop ==================
def install(url: str, token: str) -> None:
    """Install the worker client: create config, add to .bashrc, start daemon."""
    logging.info(f"Installing with SERVER_URL={url}, TOKEN=...")

    # Create config
    write_config(url, token)
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)

    # Add to .bashrc if not already present
    bashrc = HOME / ".bashrc"
    script_path = Path(__file__).resolve()
    # Use a unique marker to avoid duplicates
    marker = "# WORKER_CLIENT_START"
    start_line = f'nohup python3 "{script_path}" >/dev/null 2>&1 & {marker}'

    if bashrc.exists():
        content = bashrc.read_text()
    else:
        content = ""

    if marker not in content:
        with open(bashrc, 'a') as f:
            f.write(f"\n# Worker client startup\n{start_line}\n")
        logging.info(f"Added startup line to {bashrc}")
    else:
        logging.info("Startup line already in .bashrc")

    # Start the worker in background now
    logging.info("Starting worker client in background...")
    os.system(f'nohup python3 "{script_path}" >/dev/null 2>&1 &')
    logging.info("Installation complete.")


def uninstall() -> None:
    """Uninstall: remove from .bashrc, delete config, stop worker."""
    logging.info("Uninstalling worker client...")

    # Stop the worker first
    stop_worker()

    # Remove from .bashrc
    bashrc = HOME / ".bashrc"
    if bashrc.exists():
        marker = "# WORKER_CLIENT_START"
        lines = bashrc.read_text().splitlines()
        new_lines = [line for line in lines if marker not in line]
        bashrc.write_text("\n".join(new_lines))
        logging.info(f"Removed startup line from {bashrc}")

    # Remove config and queue dir
    for p in [CONFIG_PATH, QUEUE_DIR]:
        try:
            if p.exists():
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                logging.info(f"Removed {p}")
        except Exception as e:
            logging.warning(f"Could not remove {p}: {e}")

    logging.info("Uninstall complete.")


def stop_worker() -> None:
    """Stop the running worker process."""
    pid = read_pid()
    if pid is None:
        logging.info("No PID file found; worker may not be running.")
        # Still clean up lock and pid files
        remove_pid()
        release_lock()
        return

    try:
        os.kill(pid, signal.SIGTERM)
        # Wait a bit for process to terminate
        time.sleep(2)
        # Check if still alive
        try:
            os.kill(pid, 0)
            logging.warning(f"Worker (PID {pid}) did not terminate, sending SIGKILL.")
            os.kill(pid, signal.SIGKILL)
            time.sleep(1)
        except OSError:
            pass  # process already dead
        logging.info(f"Worker (PID {pid}) stopped.")
    except OSError as e:
        logging.warning(f"Could not kill PID {pid}: {e}")

    # Clean up files
    remove_pid()
    release_lock()


# ============================ Main Entry ==================================
def main() -> None:
    parser = argparse.ArgumentParser(description="Worker Client for task management")
    parser.add_argument("--install", action="store_true", help="Install worker client")
    parser.add_argument("--uninstall", action="store_true", help="Uninstall worker client")
    parser.add_argument("--stop", action="store_true", help="Stop the running worker")
    parser.add_argument("--foreground", action="store_true", help="Run in foreground (with console logging)")
    parser.add_argument("--url", help="Server URL (required with --install)")
    parser.add_argument("--token", help="Authentication token (required with --install)")
    args = parser.parse_args()

    # Setup logging (will be reconfigured later for foreground)
    setup_logging(foreground=args.foreground)

    # Handle install
    if args.install:
        if not args.url or not args.token:
            logging.error("--install requires --url and --token")
            sys.exit(1)
        install(args.url, args.token)
        sys.exit(0)

    # Handle uninstall
    if args.uninstall:
        uninstall()
        sys.exit(0)

    # Handle stop
    if args.stop:
        stop_worker()
        sys.exit(0)

    # Normal execution: load config, acquire lock, run loop
    # If not foreground, we still log to file only (already set up)
    if not args.foreground:
        # Reconfigure logging to remove console handlers (in case we had any)
        # Actually setup_logging was called with foreground=False if not set,
        # but we might have called with foreground=True from args, so let's reset.
        # We'll just ensure that if not foreground, we have no console handler.
        # We'll reconfigure logging to file only.
        for handler in logging.root.handlers[:]:
            if isinstance(handler, logging.StreamHandler) and handler.stream == sys.stdout:
                logging.root.removeHandler(handler)
        # Add file handler if not present
        if not any(isinstance(h, logging.FileHandler) for h in logging.root.handlers):
            logging.root.addHandler(logging.FileHandler(LOG_PATH))
        logging.root.setLevel(logging.INFO)

    config = load_config()

    # Acquire lock – prevent multiple instances
    if not acquire_lock():
        logging.error("Another instance is already running. Exiting.")
        sys.exit(1)

    write_pid()
    logging.info(f"Worker started (PID {os.getpid()})")
    logging.info(f"Lock acquired, config loaded: SERVER_URL={config['SERVER_URL']}")

    # Set up signal handlers
    def signal_handler(sig, frame):
        logging.info(f"Received signal {sig}, shutting down...")
        remove_pid()
        release_lock()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        run_forever(config)
    except KeyboardInterrupt:
        logging.info("Received KeyboardInterrupt, exiting.")
    except Exception as e:
        logging.error(f"Unhandled exception: {e}", exc_info=True)
    finally:
        remove_pid()
        release_lock()
        logging.info("Worker stopped.")


if __name__ == "__main__":
    main()