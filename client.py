#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# upload cmd
"""
Worker Client for centralized task management.
Runs as a daemon on Linux and Termux.
"""

import sys
import os
import time
import json
import logging
import argparse
import subprocess
import tempfile
import shutil
import signal
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
import fcntl

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---- Constants ------------------------------------------------------------
HOME = Path(os.environ.get("HOME", str(Path.home())))
CONFIG_PATH = HOME / ".worker_client.conf"
LOG_PATH = HOME / ".worker_client.log"
QUEUE_DIR = HOME / ".worker_client_queue"
LOCK_FILE = Path("/tmp/worker_client.lock")
PID_FILE = Path("/tmp/worker_client.pid")

# Default values
DEFAULT_POLL_INTERVAL = 2          # seconds between main loop iterations
MAX_BACKOFF = 60                   # maximum backoff delay in seconds
HEALTH_CHECK_INTERVAL = 60         # seconds between health checks
CANCEL_CHECK_INTERVAL = 2          # seconds between cancellation checks during task execution
MAX_REPORT_LENGTH = 1000           # max chars of output to include in report
TASK_TIMEOUT = None                # no timeout by default, but we handle cancellation

# ---- Logging setup --------------------------------------------------------
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

# ---- Configuration -------------------------------------------------------
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
    os.chmod(CONFIG_PATH, 0o600)  # only user read/write

# ---- Lock and PID management ---------------------------------------------
def acquire_lock() -> bool:
    """Try to acquire a lock using fcntl.flock. Return True if acquired."""
    try:
        lock_fd = open(LOCK_FILE, 'w')
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # write PID
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
        # keep the file descriptor open to hold the lock
        global _lock_fd
        _lock_fd = lock_fd
        return True
    except (IOError, OSError):
        return False

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
# -----Upload ---------
def handle_upload_command(session: requests.Session, config: Dict[str, str], task_id: int, command: str) -> None:
    """
    Handle upload command: upload x.jpg
    Extracts the file path from the command and uploads it.
    """
    # Parse command: "upload x.jpg" -> extract file path
    parts = command.strip().split(maxsplit=1)
    if len(parts) < 2:
        error_msg = "Upload command missing file path. Usage: upload <file_path>"
        logging.error(f"Task {task_id}: {error_msg}")
        send_report(session, config, task_id, error_msg)
        return
    
    file_path_str = parts[1].strip()
    file_path = Path(file_path_str)
    
    # Check if file exists
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
    
    # Notify server that task started
    if not start_task(session, config, task_id):
        logging.error(f"Task {task_id}: Failed to start task")
        return
    
    # Upload the file
    success = upload_file(session, config, task_id, file_path)
    
    if success:
        report_content = f"File uploaded successfully: {file_path.name} (Size: {file_path.stat().st_size} bytes)"
        logging.info(f"Task {task_id}: {report_content}")
    else:
        report_content = f"Failed to upload file: {file_path.name}"
        logging.warning(f"Task {task_id}: {report_content}")
        # Queue for retry
        queue_upload(task_id, file_path)
    
    # Send report
    if not send_report(session, config, task_id, report_content):
        logging.warning(f"Task {task_id}: Report failed, queuing.")
        queue_report(task_id, report_content)
# ---- Daemonisation -------------------------------------------------------
def daemonize() -> None:
    """
    Detach from terminal and run in background using nohup.
    Only call this if not in foreground and isatty.
    """
    if os.isatty(sys.stdin.fileno()):
        logging.info("Running in interactive terminal, daemonizing with nohup...")
        script_path = Path(__file__).resolve()
        # Re-execute with nohup and background
        cmd = f"nohup python3 {script_path} --foreground </dev/null >/dev/null 2>&1 &"
        os.system(cmd)
        sys.exit(0)

# ---- HTTP client with retries --------------------------------------------
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

# ---- API calls -----------------------------------------------------------
def health_check(session: requests.Session, config: Dict[str, str]) -> bool:
    """Check server health. Return True if ok."""
    url = f"{config['SERVER_URL']}/worker/health"
    params = {"token": config['TOKEN']}
    try:
        resp = session.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("status") == "ok"
        else:
            logging.warning(f"Health check failed: HTTP {resp.status_code}")
            return False
    except Exception as e:
        logging.warning(f"Health check exception: {e}")
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
            else:
                return None
        else:
            logging.warning(f"Get current task failed: HTTP {resp.status_code}")
            return None
    except Exception as e:
        logging.warning(f"Get current task exception: {e}")
        return None

def start_task(session: requests.Session, config: Dict[str, str], task_id: int) -> bool:
    """Notify server that task is started. Return True if success."""
    url = f"{config['SERVER_URL']}/worker/start-task"
    data = {"token": config['TOKEN'], "task_id": task_id}
    try:
        resp = session.post(url, json=data, timeout=10)
        if resp.status_code == 200:
            resp_data = resp.json()
            return resp_data.get("status") == "running"
        else:
            logging.warning(f"Start task failed: HTTP {resp.status_code}")
            return False
    except Exception as e:
        logging.warning(f"Start task exception: {e}")
        return False

def is_cancelled(session: requests.Session, config: Dict[str, str], task_id: int) -> bool:
    """Check if task is cancelled. Return True if cancelled."""
    url = f"{config['SERVER_URL']}/worker/is-cancelled"
    params = {"token": config['TOKEN'], "task_id": task_id}
    try:
        resp = session.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("cancelled", False)
        else:
            logging.warning(f"Cancel check failed: HTTP {resp.status_code}")
            return False
    except Exception as e:
        logging.warning(f"Cancel check exception: {e}")
        return False

def send_report(session: requests.Session, config: Dict[str, str], task_id: int, content: str) -> bool:
    """Send task report. Return True if success."""
    url = f"{config['SERVER_URL']}/worker/report"
    data = {"token": config['TOKEN'], "task_id": task_id, "content": content}
    try:
        resp = session.post(url, json=data, timeout=10)
        if resp.status_code == 200:
            return True
        else:
            logging.warning(f"Send report failed: HTTP {resp.status_code}")
            return False
    except Exception as e:
        logging.warning(f"Send report exception: {e}")
        return False

def upload_file(session: requests.Session, config: Dict[str, str], task_id: int, file_path: Path) -> bool:
    """Upload a file. Return True if success."""
    url = f"{config['SERVER_URL']}/worker/upload"
    files = {'file': (file_path.name, open(file_path, 'rb'))}
    data = {'token': config['TOKEN'], 'task_id': task_id}
    try:
        resp = session.post(url, data=data, files=files, timeout=30)
        if resp.status_code == 200:
            return True
        else:
            logging.warning(f"Upload failed: HTTP {resp.status_code}")
            return False
    except Exception as e:
        logging.warning(f"Upload exception: {e}")
        return False
    finally:
        files['file'][1].close()  # close file

# ---- Queue management ----------------------------------------------------
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
                logging.info(f"Queued report for task {task_id} sent successfully.")
            else:
                logging.warning(f"Queued report for task {task_id} still failing.")
        except Exception as e:
            logging.error(f"Error processing queued report {queue_file}: {e}")
    # Process uploads
    for queue_file in QUEUE_DIR.glob("upload_*.file"):
        try:
            # extract task_id from filename
            task_id = int(queue_file.stem.split('_')[1])
            if upload_file(session, config, task_id, queue_file):
                queue_file.unlink()
                logging.info(f"Queued upload for task {task_id} sent successfully.")
            else:
                logging.warning(f"Queued upload for task {task_id} still failing.")
        except Exception as e:
            logging.error(f"Error processing queued upload {queue_file}: {e}")

# ---- Task execution ------------------------------------------------------
def execute_task(session: requests.Session, config: Dict[str, str], task: Dict[str, Any]) -> None:
    """
    Execute a single task: run command, handle cancellation, report, upload.
    """
    task_id = task['id']
    command = task['content']
    logging.info(f"Executing task {task_id}: {command}")
    
    # ---- NEW: Check for special commands ----
    # Check if command starts with "upload"
    if command.strip().lower().startswith('upload'):
        handle_upload_command(session, config, task_id, command)
        return
    
    # ---- Existing code for normal shell commands ----
    # Notify server that we started
    if not start_task(session, config, task_id):
        logging.error(f"Failed to start task {task_id}, aborting.")
        return

    # ... (rest of the existing execute_task code remains the same)
# ---- Main loop -----------------------------------------------------------
def run_forever(config: Dict[str, str]) -> None:
    """Main infinite loop: health check, get task, execute."""
    session = create_session()
    backoff = DEFAULT_POLL_INTERVAL
    last_health_check = 0

    while True:
        try:
            # Process queue first
            process_queue(session, config)

            # Health check every 60 seconds
            now = time.time()
            if now - last_health_check >= HEALTH_CHECK_INTERVAL:
                if health_check(session, config):
                    logging.debug("Health check OK")
                else:
                    logging.warning("Health check failed")
                last_health_check = now

            # Get current task
            task = get_current_task(session, config)
            if task:
                # Reset backoff on success
                backoff = DEFAULT_POLL_INTERVAL
                # Execute task
                execute_task(session, config, task)
            else:
                # No task, wait
                logging.debug("No pending task, sleeping...")
                time.sleep(backoff)
                # Increase backoff if no task? Not necessary, we only backoff on errors.
                # But we can keep backoff low when idle.
        except Exception as e:
            logging.error(f"Main loop exception: {e}", exc_info=True)
            # Backoff on failure
            backoff = min(backoff * 2, MAX_BACKOFF)
            logging.warning(f"Backing off for {backoff} seconds")
            time.sleep(backoff)

# ---- Install / Uninstall ------------------------------------------------
def install(url: str, token: str) -> None:
    """Install the worker client: create config, add to .bashrc, start daemon."""
    logging.info(f"Installing with SERVER_URL={url}, TOKEN=...")
    # Create config
    write_config(url, token)
    # Ensure queue dir exists
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)

    # Add to .bashrc if not already present
    bashrc = HOME / ".bashrc"
    if bashrc.exists():
        content = bashrc.read_text()
    else:
        content = ""
    # Line to add
    script_path = Path(__file__).resolve()
    start_line = f'( flock -n /tmp/worker_client.lock -c "python3 {script_path} --foreground" </dev/null >/dev/null 2>&1 & )'
    if start_line not in content:
        with open(bashrc, 'a') as f:
            f.write(f"\n# Worker client startup\n{start_line}\n")
        logging.info(f"Added startup line to {bashrc}")
    else:
        logging.info("Startup line already in .bashrc")

    # Start the daemon now
    logging.info("Starting worker client in background...")
    # Use nohup to start in background
    cmd = f"nohup python3 {script_path} --foreground </dev/null >/dev/null 2>&1 &"
    os.system(cmd)
    logging.info("Installation complete.")

def uninstall() -> None:
    """Uninstall: remove from .bashrc, delete config, lock, pid files."""
    logging.info("Uninstalling worker client...")
    # Remove from .bashrc
    bashrc = HOME / ".bashrc"
    if bashrc.exists():
        content = bashrc.read_text()
        script_path = Path(__file__).resolve()
        start_line = f'( flock -n /tmp/worker_client.lock -c "python3 {script_path} --foreground" </dev/null >/dev/null 2>&1 & )'
        # Remove line(s) containing that pattern
        lines = content.splitlines()
        new_lines = [line for line in lines if start_line not in line]
        bashrc.write_text("\n".join(new_lines))
        logging.info(f"Removed startup line from {bashrc}")

    # Remove files
    for p in [CONFIG_PATH, LOCK_FILE, PID_FILE]:
        try:
            if p.exists():
                p.unlink()
                logging.info(f"Removed {p}")
        except Exception as e:
            logging.warning(f"Could not remove {p}: {e}")

    # Stop running processes? We could kill processes with this script name, but risky.
    # Instead, we rely on lock file removal; next run will not start due to no config.
    logging.info("Uninstall complete. Running workers will continue until next restart.")
    # Optionally kill all worker processes with same script name (but not this one)
    # We can implement a gentle kill of other instances.
    # But we'll skip for safety.

# ---- Main entry point ---------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Worker Client for task management")
    parser.add_argument("--install", action="store_true", help="Install worker client")
    parser.add_argument("--uninstall", action="store_true", help="Uninstall worker client")
    parser.add_argument("--foreground", action="store_true", help="Run in foreground (for debugging)")
    parser.add_argument("--url", help="Server URL (required with --install)")
    parser.add_argument("--token", help="Authentication token (required with --install)")
    args = parser.parse_args()

    # Setup logging initially (may be reconfigured later)
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

    # Normal daemon mode
    if not args.foreground:
        # If running in terminal, daemonize
        daemonize()
        # After daemonize, we are in background, set up logging again (no console)
        setup_logging(foreground=False)
    else:
        # Foreground mode: logging to console as well
        setup_logging(foreground=True)

    # Load config
    config = load_config()

    # Acquire lock
    if not acquire_lock():
        logging.error("Another instance is already running. Exiting.")
        sys.exit(1)

    # Write PID
    write_pid()

    # Set up signal handlers for graceful exit
    def signal_handler(sig, frame):
        logging.info(f"Received signal {sig}, shutting down...")
        remove_pid()
        sys.exit(0)
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    logging.info("Worker started (PID %d)", os.getpid())
    logging.info("Lock acquired")
    logging.info(f"Config loaded: SERVER_URL={config['SERVER_URL']}, TOKEN=...")

    try:
        run_forever(config)
    except KeyboardInterrupt:
        logging.info("Received KeyboardInterrupt, exiting.")
    except Exception as e:
        logging.error(f"Unhandled exception: {e}", exc_info=True)
    finally:
        remove_pid()
        logging.info("Worker stopped.")

if __name__ == "__main__":
    main()