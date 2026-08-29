"""
Worker Manager API Client
-------------------------

HTTPX helper module for communicating with the Worker Task Manager API.

This module only provides communication utilities.
Application logic, command execution, scheduling, UI, etc. should be
implemented by the caller.

Requirements:
    pip install httpx
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, Optional

import httpx

HOME = Path(os.environ.get("HOME", str(Path.home())))
CONFIG_FILE = HOME / ".worker_config.json"
QUEUE_DIR = HOME /".worker" / ".worker_client_queue"

def load_config():
    """Load configuration from file or return defaults."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_config(url, token):
    """Save configuration to file."""
    config = {
        "url": url,
        "token": token
    }
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)

class WorkerAPIError(Exception):
    """Raised when the Worker Manager API returns an error."""


class WorkerAPI:
    """
    Small HTTPX client for the Worker Task Manager API.

    Example:

        api = WorkerAPI(
            base_url="http://127.0.0.1:8089",
            token="YOUR_TOKEN",
        )

        print(api.health())
        task = api.current_task()
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
        )

    # =========================================================
    # Internal helpers
    # =========================================================

    def close(self) -> None:
        """Close the HTTPX client."""
        self.client.close()

    def __enter__(self) -> "WorkerAPI":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _handle_response(
        self,
        response: httpx.Response,
    ) -> dict[str, Any]:
        """
        Validate API response and return decoded JSON.
        """
        try:
            data = response.json()
        except ValueError:
            data = {
                "success": False,
                "detail": response.text,
            }

        if response.is_error:
            detail = data.get(
                "detail",
                response.text or "Unknown API error.",
            )
            print(detail)
            raise WorkerAPIError(
                f"HTTP {response.status_code}: {detail}"
            )

        return data

    def _get(
        self,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        response = self.client.get(
            path,
            **kwargs,
        )
        return self._handle_response(response)

    def _post(
        self,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        response = self.client.post(
            path,
            **kwargs,
        )
        return self._handle_response(response)

    # =========================================================
    # Worker
    # =========================================================

    def health(self) -> dict[str, Any]:
        """
        Check worker authentication and server connectivity.

        API:
            GET /worker/health
        """
        return self._get(
            "/worker/health",
            params={
                "token": self.token,
            },
        )

    def current_task(self) -> Optional[dict[str, Any]]:
        """
        Get the current pending/running task.

        API:
            GET /worker/current-task

        Returns:
            Task dictionary or None.
        """
        data = self._get(
            "/worker/current-task",
            params={
                "token": self.token,
            },
        )

        return data.get("task")

    # =========================================================
    # Task control
    # =========================================================

    def start_task(
        self,
        task_id: int,
    ) -> dict[str, Any]:
        """
        Mark a pending task as running.

        API:
            POST /worker/start-task
        """
        return self._post(
            "/worker/start-task",
            json={
                "token": self.token,
                "task_id": task_id,
            },
        )

    def is_cancelled(
        self,
        task_id: int,
    ) -> bool:
        """
        Check whether a task cancellation was requested.

        API:
            GET /worker/is-cancelled
        """
        data = self._get(
            "/worker/is-cancelled",
            params={
                "token": self.token,
                "task_id": task_id,
            },
        )

        return bool(data.get("cancelled", False))

    def report(
        self,
        task_id: int,
        content: str,
    ) -> dict[str, Any]:
        """
        Send a text report for a task.

        The server automatically completes the task after
        accepting the report.

        API:
            POST /worker/report
        """
        return self._post(
            "/worker/report",
            json={
                "token": self.token,
                "task_id": task_id,
                "content": content,
            },
        )

    # =========================================================
    # File upload
    # =========================================================

    def upload(
        self,
        task_id: int,
        file_path: str | Path,
    ) -> dict[str, Any]:
        """
        Upload a file related to a task.

        API:
            POST /worker/upload
        """
        path = Path(file_path)

        if not path.is_file():
            raise FileNotFoundError(
                f"File not found: {path}"
            )

        with path.open("rb") as file_obj:
            response = self.client.post(
                "/worker/upload",
                data={
                    "token": self.token,
                    "task_id": str(task_id),
                },
                files={
                    "file": (
                        path.name,
                        file_obj,
                        "application/octet-stream",
                    )
                },
            )

        return self._handle_response(response)

    # =========================================================
    # Convenience
    # =========================================================

    def wait_for_task(
        self,
        task_id: int,
        interval: float = 1.0,
        timeout: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        """
        Poll the task until it is no longer pending/running.

        This is only a communication helper.
        It does not execute anything.

        Returns:
            Final task dictionary.

        Raises:
            TimeoutError:
                If timeout is reached.
        """
        import time

        started = time.monotonic()

        while True:
            task = self.current_task()

            if task is None:
                return None

            if task.get("id") != task_id:
                time.sleep(interval)
                continue

            status = task.get("status")

            if status not in {"pending", "running"}:
                return task

            if timeout is not None:
                elapsed = time.monotonic() - started

                if elapsed >= timeout:
                    raise TimeoutError(
                        f"Task {task_id} did not finish "
                        f"within {timeout} seconds."
                    )

            time.sleep(interval)

    # =========================================================
    # Raw access
    # =========================================================

    def get(
        self,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Generic GET helper for future/custom endpoints.
        """
        return self._get(path, **kwargs)

    def post(
        self,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Generic POST helper for future/custom endpoints.
        """
        return self._post(path, **kwargs)



def queue_report(task_id: int, content: str) -> None:
    """Store a failed report to queue."""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    queue_file = QUEUE_DIR / f"report_{task_id}.json"
    with open(queue_file, 'w') as f:
        json.dump({"task_id": task_id, "content": content}, f)


def execute_task(task: Dict[str, Any], api: WorkerAPI) -> None:
    task_id = task['id']
    command = task['content']

    """if command.strip().lower().startswith('upload'):
        handle_upload_command(session, config, task_id, command)
        return"""
    if task["status"] == "pending":
        api.start_task(task["id"])

    # --- معالجة أمر الرفع ---
    if command.lower().startswith('upload'):
        # استخراج المسار: نفترض أن التنسيق "upload <path>"
        parts = command.split(maxsplit=1)
        if len(parts) < 2:
            report_content = "خطأ: لم يتم تحديد مسار الملف بعد الأمر 'upload'."
            if not api.report(task_id, report_content):
                queue_report(task_id, report_content)
            return

        file_path = parts[1].strip()
        # تدعيم المسارات النسبية (نسبة إلى المجلد الحالي أو HOME)
        path_obj = Path(file_path).expanduser().resolve()

        if not path_obj.is_file():
            report_content = f"error uploading {path_obj}"
            if not api.report(task_id, report_content):
                queue_report(task_id, report_content)
            return

        # محاولة الرفع
        try:
            

            upload_result = api.upload(task_id, path_obj)
            # نأخذ رسالة النجاح من الرد (إن وُجدت)
            report_content = f"file uploaded: {path_obj.name}"
            # يمكن إضافة تفاصيل إضافية من upload_result إذا أردت
        except Exception as e:
            report_content = f"unuploaded: {str(e)}"

        # إرسال التقرير النهائي
        if not api.report(task_id, report_content):
            queue_report(task_id, report_content)
        return


    proc = None
    report_content = ""
    
    try:
        # استخدام Popen مع قراءة متوازية للمخرجات
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1  # خطي
        )
        
        # استخدام خيوط لقراءة المخرجات بشكل متوازي
        import threading
        output_lines = []
        error_lines = []
        
        def read_output(pipe, lines_list):
            for line in iter(pipe.readline, ''):
                lines_list.append(line)
        
        stdout_thread = threading.Thread(target=read_output, args=(proc.stdout, output_lines))
        stderr_thread = threading.Thread(target=read_output, args=(proc.stderr, error_lines))
        
        stdout_thread.start()
        stderr_thread.start()
        
        # التحقق من الإلغاء والانتهاء
        import time
        while True:
            retcode = proc.poll()
            if retcode is not None:
                break
                
            # فحص الإلغاء
            if api.is_cancelled(task_id=task["id"]):
                proc.terminate()
                time.sleep(1)
                if proc.poll() is None:
                    proc.kill()
                report_content = "Task cancelled by server."
                api.report(task["id"], report_content)
                return
                
            time.sleep(2)
        
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        
        # تجميع المخرجات
        full_output = ''.join(output_lines)
        full_error = ''.join(error_lines)
        
        # بناء التقرير
        if full_output or full_error:
            report_content = full_output
            if full_error:
                if report_content:
                    report_content += "\n--- STDERR ---\n"
                report_content += full_error
        else:
            report_content = "(empty output)"
        
        # إضافة رمز الخروج
        if proc.returncode != 0:
            report_content += f"\n[Exit code: {proc.returncode}]"
        

    except subprocess.TimeoutExpired:
        report_content = "Process timeout"
        if proc:
            proc.kill()
    except Exception as e:
        report_content = f"Execution error: {str(e)}"
    finally:
        if proc and proc.poll() is None:
            proc.kill()
            proc.wait()

    # إرسال التقرير
    if report_content:
        if len(report_content) > 10000:
            report_content = report_content[:10000] + "\n... (truncated)"
        
        if not api.report(task["id"], report_content):
            queue_report(task_id, report_content)



def run_worker_loop(api):
    """Main worker loop."""
    print(f"🔄 Worker started with URL: {api.base_url}")
    print("Press Ctrl+C to stop\n")
    
    while True:
        try:
            task = api.current_task()
            if task:
                print(f"📋 Processing task #{task.get('id')}...")
                execute_task(task, api)
            else:
                import time
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n🛑 Worker stopped by user.")
            break
        except Exception as e:
            print(f"⚠️ Error: {e}")
            import time
            time.sleep(3)

def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Worker Task Manager Client",
        epilog="If --install is used, configuration will be saved and script will exit."
    )
    
    parser.add_argument(
        '--install',
        action='store_true',
        help='Install/update configuration and exit'
    )
    
    parser.add_argument(
        '--url',
        type=str,
        help='Worker API URL (e.g., http://127.0.0.1:8089)'
    )
    
    parser.add_argument(
        '--token',
        type=str,
        help='Authentication token'
    )
    
    return parser.parse_args()

def main():
    args = parse_arguments()
    
    # Handle installation/update
    if args.install:
        if not args.url or not args.token:
            sys.exit(1)
        
        save_config(args.url, args.token)
    
    # Load configuration
    config = load_config()
    
    if not config or not config.get('url') or not config.get('token'):
        print("❌ Error: Configuration not found or incomplete.")
        print("Please run: python script.py --install --url <URL> --token <TOKEN>")
        sys.exit(1)
    
    # Create API client
    api = WorkerAPI(
        base_url=config['url'],
        token=config['token']
    )
    
    # Run the main loop
    run_worker_loop(api)

if __name__ == "__main__":
    main()