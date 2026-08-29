import os
import sqlite3
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Query, UploadFile, File
from pydantic import BaseModel

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, IntPrompt, Confirm
from rich.table import Table
from rich.text import Text
from rich.live import Live
from rich.spinner import Spinner

# ============================================================
# Configuration
# ============================================================

DB_FILE = "worker_manager.db"

API_HOST = "0.0.0.0"
API_PORT = 8089

UPLOADS_DIR = Path("uploads")

WORKER_TIMEOUT = 60
CLI_REFRESH_SECONDS = 3


# ============================================================
# Globals
# ============================================================

db_lock = threading.Lock()
console = Console()

app = FastAPI(
    title="Worker Task Manager",
    version="1.0.0",
)


# ============================================================
# Utilities
# ============================================================

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None

    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def worker_is_active(last_seen: Optional[str]) -> bool:
    dt = parse_datetime(last_seen)

    if not dt:
        return False

    age = (datetime.now(timezone.utc) - dt).total_seconds()

    return age < WORKER_TIMEOUT


def generate_worker_id() -> str:
    return secrets.token_hex(8)


def generate_worker_token() -> str:
    return secrets.token_urlsafe(32)


def get_connection():
    conn = sqlite3.connect(
        DB_FILE,
        check_same_thread=False,
    )

    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys = ON")

    return conn


# ============================================================
# Database
# ============================================================

def init_database():
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    with db_lock:
        conn = get_connection()

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                worker_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                token TEXT UNIQUE NOT NULL,
                last_seen TEXT,
                created_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                worker_id TEXT NOT NULL,

                content TEXT NOT NULL,

                status TEXT NOT NULL DEFAULT 'pending',

                cancel_requested INTEGER NOT NULL DEFAULT 0,

                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,

                FOREIGN KEY(worker_id)
                    REFERENCES workers(worker_id)
                    ON DELETE CASCADE
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                task_id INTEGER NOT NULL,

                report_type TEXT NOT NULL DEFAULT 'text',

                content TEXT,

                created_at TEXT NOT NULL,

                FOREIGN KEY(task_id)
                    REFERENCES tasks(id)
                    ON DELETE CASCADE
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                task_id INTEGER NOT NULL,

                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,

                size INTEGER NOT NULL,

                created_at TEXT NOT NULL,

                FOREIGN KEY(task_id)
                    REFERENCES tasks(id)
                    ON DELETE CASCADE
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tasks_worker
            ON tasks(worker_id)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tasks_status
            ON tasks(status)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_reports_task
            ON task_reports(task_id)
            """
        )

        conn.commit()
        conn.close()


# ============================================================
# Worker Database Functions
# ============================================================

def create_worker(name: str):
    worker_id = generate_worker_id()
    token = generate_worker_token()
    now = utc_now()

    with db_lock:
        conn = get_connection()

        conn.execute(
            """
            INSERT INTO workers (
                worker_id,
                name,
                token,
                last_seen,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                worker_id,
                name,
                token,
                now,
                now,
            ),
        )

        conn.commit()
        conn.close()

    return worker_id, token


def get_workers():
    with db_lock:
        conn = get_connection()

        rows = conn.execute(
            """
            SELECT
                worker_id,
                name,
                token,
                last_seen,
                created_at
            FROM workers
            ORDER BY id DESC
            """
        ).fetchall()

        conn.close()

    return rows


def get_worker_by_id(worker_id: str):
    with db_lock:
        conn = get_connection()

        row = conn.execute(
            """
            SELECT *
            FROM workers
            WHERE worker_id = ?
            """,
            (worker_id,),
        ).fetchone()

        conn.close()

    return row


def get_worker_by_token(token: str):
    with db_lock:
        conn = get_connection()

        row = conn.execute(
            """
            SELECT *
            FROM workers
            WHERE token = ?
            """,
            (token,),
        ).fetchone()

        conn.close()

    return row


def touch_worker(worker_id: str):
    with db_lock:
        conn = get_connection()

        conn.execute(
            """
            UPDATE workers
            SET last_seen = ?
            WHERE worker_id = ?
            """,
            (
                utc_now(),
                worker_id,
            ),
        )

        conn.commit()
        conn.close()


# ============================================================
# Task Database Functions
# ============================================================

def get_current_task(worker_id: str):
    """
    Return only the current task.

    A task is considered current when it is:
        pending
        running
    """

    with db_lock:
        conn = get_connection()

        row = conn.execute(
            """
            SELECT *
            FROM tasks
            WHERE worker_id = ?
              AND status IN ('pending', 'running')
            ORDER BY id DESC
            LIMIT 1
            """,
            (worker_id,),
        ).fetchone()

        conn.close()

    return row


def create_task(worker_id: str, content: str):
    content = content.strip()

    if not content:
        raise ValueError("Task cannot be empty.")

    with db_lock:
        conn = get_connection()

        current = conn.execute(
            """
            SELECT id
            FROM tasks
            WHERE worker_id = ?
              AND status IN ('pending', 'running')
            LIMIT 1
            """,
            (worker_id,),
        ).fetchone()

        if current:
            conn.close()
            raise RuntimeError(
                "Worker already has a pending/running task."
            )

        cursor = conn.execute(
            """
            INSERT INTO tasks (
                worker_id,
                content,
                status,
                cancel_requested,
                created_at
            )
            VALUES (?, ?, 'pending', 0, ?)
            """,
            (
                worker_id,
                content,
                utc_now(),
            ),
        )

        task_id = cursor.lastrowid

        conn.commit()
        conn.close()

    return task_id


def get_task_by_id(task_id: int):
    with db_lock:
        conn = get_connection()

        row = conn.execute(
            """
            SELECT *
            FROM tasks
            WHERE id = ?
            """,
            (task_id,),
        ).fetchone()

        conn.close()

    return row


def start_task(task_id: int, worker_id: str):
    with db_lock:
        conn = get_connection()

        row = conn.execute(
            """
            SELECT *
            FROM tasks
            WHERE id = ?
              AND worker_id = ?
            """,
            (
                task_id,
                worker_id,
            ),
        ).fetchone()

        if not row:
            conn.close()
            return False

        if row["status"] != "pending":
            conn.close()
            return False

        if row["cancel_requested"]:
            conn.execute(
                """
                UPDATE tasks
                SET
                    status = 'cancelled',
                    finished_at = ?
                WHERE id = ?
                """,
                (
                    utc_now(),
                    task_id,
                ),
            )

            conn.commit()
            conn.close()

            return False

        conn.execute(
            """
            UPDATE tasks
            SET
                status = 'running',
                started_at = ?
            WHERE id = ?
            """,
            (
                utc_now(),
                task_id,
            ),
        )

        conn.commit()
        conn.close()

    return True


def request_task_cancel(worker_id: str, task_id: int):
    with db_lock:
        conn = get_connection()

        row = conn.execute(
            """
            SELECT *
            FROM tasks
            WHERE id = ?
              AND worker_id = ?
            """,
            (
                task_id,
                worker_id,
            ),
        ).fetchone()

        if not row:
            conn.close()
            return False, "Task not found."

        if row["status"] not in ("pending", "running"):
            conn.close()
            return False, "Task is already finished."

        conn.execute(
            """
            UPDATE tasks
            SET cancel_requested = 1
            WHERE id = ?
            """,
            (task_id,),
        )

        if row["status"] == "pending":
            conn.execute(
                """
                UPDATE tasks
                SET
                    status = 'cancelled',
                    finished_at = ?
                WHERE id = ?
                """,
                (
                    utc_now(),
                    task_id,
                ),
            )

        conn.commit()
        conn.close()

    return True, "Cancellation requested."


def is_task_cancelled(worker_id: str, task_id: int):
    with db_lock:
        conn = get_connection()

        row = conn.execute(
            """
            SELECT
                status,
                cancel_requested
            FROM tasks
            WHERE id = ?
              AND worker_id = ?
            """,
            (
                task_id,
                worker_id,
            ),
        ).fetchone()

        conn.close()

    if not row:
        return True

    return bool(
        row["cancel_requested"]
        or row["status"] == "cancelled"
    )


def complete_task(worker_id: str, task_id: int):
    with db_lock:
        conn = get_connection()

        row = conn.execute(
            """
            SELECT *
            FROM tasks
            WHERE id = ?
              AND worker_id = ?
            """,
            (
                task_id,
                worker_id,
            ),
        ).fetchone()

        if not row:
            conn.close()
            return False, "Task not found."

        if row["status"] not in ("pending", "running"):
            conn.close()
            return False, "Task is not active."

        if row["cancel_requested"]:
            conn.execute(
                """
                UPDATE tasks
                SET
                    status = 'cancelled',
                    finished_at = ?
                WHERE id = ?
                """,
                (
                    utc_now(),
                    task_id,
                ),
            )

            conn.commit()
            conn.close()

            return False, "Task was cancelled."

        conn.execute(
            """
            UPDATE tasks
            SET
                status = 'completed',
                finished_at = ?
            WHERE id = ?
            """,
            (
                utc_now(),
                task_id,
            ),
        )

        conn.commit()
        conn.close()

    return True, "Task completed."


def add_report(
    task_id: int,
    content: str,
    report_type: str = "text",
):
    content = content.strip()

    if not content:
        content = "Blank"

    with db_lock:
        conn = get_connection()

        cursor = conn.execute(
            """
            INSERT INTO task_reports (
                task_id,
                report_type,
                content,
                created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                task_id,
                report_type,
                content,
                utc_now(),
            ),
        )

        report_id = cursor.lastrowid

        conn.commit()
        conn.close()

    return report_id


def get_task_reports(task_id: int):
    with db_lock:
        conn = get_connection()

        rows = conn.execute(
            """
            SELECT
                id,
                task_id,
                report_type,
                content,
                created_at
            FROM task_reports
            WHERE task_id = ?
            ORDER BY id ASC
            """,
            (task_id,),
        ).fetchall()

        conn.close()

    return rows


def get_recent_tasks(worker_id: str, limit: int = 3):
    with db_lock:
        conn = get_connection()

        rows = conn.execute(
            """
            SELECT *
            FROM tasks
            WHERE worker_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (
                worker_id,
                limit,
            ),
        ).fetchall()

        conn.close()

    return rows


# ============================================================
# File Database Functions
# ============================================================

def add_task_file(
    task_id: int,
    original_name: str,
    stored_name: str,
    size: int,
):
    with db_lock:
        conn = get_connection()

        cursor = conn.execute(
            """
            INSERT INTO task_files (
                task_id,
                original_name,
                stored_name,
                size,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                task_id,
                original_name,
                stored_name,
                size,
                utc_now(),
            ),
        )

        file_id = cursor.lastrowid

        conn.commit()
        conn.close()

    return file_id


def get_task_files(task_id: int):
    with db_lock:
        conn = get_connection()

        rows = conn.execute(
            """
            SELECT *
            FROM task_files
            WHERE task_id = ?
            ORDER BY id ASC
            """,
            (task_id,),
        ).fetchall()

        conn.close()

    return rows


# ============================================================
# Validation Helpers
# ============================================================

def authenticated_worker(token: str):
    worker = get_worker_by_token(token)

    if not worker:
        raise HTTPException(
            status_code=401,
            detail="Invalid token.",
        )

    touch_worker(worker["worker_id"])

    return worker


def validate_task_owner(
    token: str,
    task_id: int,
):
    worker = authenticated_worker(token)

    task = get_task_by_id(task_id)

    if not task:
        raise HTTPException(
            status_code=404,
            detail="Task not found.",
        )

    if task["worker_id"] != worker["worker_id"]:
        raise HTTPException(
            status_code=403,
            detail="Task does not belong to this worker.",
        )

    return worker, task


# ============================================================
# API Models
# ============================================================

class TaskCreate(BaseModel):
    token: str
    content: str


class TaskReport(BaseModel):
    token: str
    task_id: int
    content: str


class TaskAction(BaseModel):
    token: str
    task_id: int


# ============================================================
# API - Worker
# ============================================================

@app.get("/worker/health")
def worker_health(token: str = Query(...)):
    worker = authenticated_worker(token)

    return {
        "success": True,
        "worker_id": worker["worker_id"],
        "status": "active",
        "server_time": utc_now(),
    }


@app.get("/worker/current-task")
def worker_current_task(token: str = Query(...)):
    worker = authenticated_worker(token)

    task = get_current_task(worker["worker_id"])

    if not task:
        return {
            "task": None,
        }

    return {
        "task": {
            "id": task["id"],
            "content": task["content"],
            "status": task["status"],
            "cancel_requested": bool(
                task["cancel_requested"]
            ),
            "created_at": task["created_at"],
            "started_at": task["started_at"],
        }
    }


@app.post("/worker/start-task")
def worker_start_task(data: TaskAction):
    worker, task = validate_task_owner(
        data.token,
        data.task_id,
    )

    started = start_task(
        task["id"],
        worker["worker_id"],
    )

    if not started:
        raise HTTPException(
            status_code=409,
            detail="Task cannot be started.",
        )

    return {
        "success": True,
        "task_id": task["id"],
        "status": "running",
    }


@app.get("/worker/is-cancelled")
def worker_is_cancelled(
    token: str = Query(...),
    task_id: int = Query(...),
):
    worker, task = validate_task_owner(
        token,
        task_id,
    )

    cancelled = is_task_cancelled(
        worker["worker_id"],
        task["id"],
    )

    return {
        "task_id": task["id"],
        "cancelled": cancelled,
    }


@app.post("/worker/report")
def worker_report(data: TaskReport):
    worker, task = validate_task_owner(
        data.token,
        data.task_id,
    )

    # إضافة التقرير
    report_id = add_report(
        task["id"],
        data.content,
        "text",
    )

    # إنهاء المهمة تلقائياً بعد إضافة التقرير
    complete_task(worker["worker_id"], task["id"])

    return {
        "success": True,
        "report_id": report_id,
    }

@app.post("/worker/upload")
async def worker_upload(
    token: str = Form(...),
    task_id: int = Form(...),
    file: UploadFile = File(...),
):
    worker, task = validate_task_owner(
        token,
        task_id,
    )

    if task["status"] not in ("pending", "running"):
        raise HTTPException(
            status_code=400,
            detail="Files can only be uploaded for active tasks.",
        )

    task_dir = UPLOADS_DIR / worker["worker_id"] / str(task_id)

    task_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    original_name = Path(
        file.filename or "uploaded_file"
    ).name

    safe_name = (
        f"{secrets.token_hex(8)}_"
        f"{original_name}"
    )

    target = task_dir / safe_name

    size = 0

    with open(target, "wb") as output:
        while True:
            chunk = await file.read(1024 * 1024)

            if not chunk:
                break

            output.write(chunk)
            size += len(chunk)

    file_id = add_task_file(
        task["id"],
        original_name,
        safe_name,
        size,
    )

    return {
        "success": True,
        "file_id": file_id,
        "filename": original_name,
        "size": size,
    }

# ============================================================
# Admin API - optional
# ============================================================

@app.get("/admin/workers")
def admin_workers():
    rows = get_workers()

    return {
        "workers": [
            {
                "worker_id": row["worker_id"],
                "name": row["name"],
                "last_seen": row["last_seen"],
                "active": worker_is_active(
                    row["last_seen"]
                ),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    }


# ============================================================
# Server
# ============================================================

def start_api_server():
    config = uvicorn.Config(
        app,
        host=API_HOST,
        port=API_PORT,
        log_level="warning",
    )

    server = uvicorn.Server(config)

    server.run()


# ============================================================
# Rich CLI
# ============================================================

def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def header():
    console.print(
        Panel.fit(
            "[bold cyan]WORKER TASK MANAGER[/bold cyan]\n"
            "[dim]Local Task Management Server[/dim]",
            border_style="cyan",
            padding=(1, 3),
        )
    )


def show_workers():
    rows = get_workers()

    table = Table(
        title="Workers",
        box=box.ROUNDED,
        expand=True,
    )

    table.add_column("ID", style="cyan")
    table.add_column("Name", style="bold")
    table.add_column("Status")
    table.add_column("Last Seen")
    table.add_column("Created")

    for row in rows:
        active = worker_is_active(row["last_seen"])

        status = (
            "[green]ACTIVE[/green]"
            if active
            else "[red]INACTIVE[/red]"
        )

        last_seen = row["last_seen"] or "-"

        table.add_row(
            row["worker_id"],
            row["name"],
            status,
            last_seen[:19],
            row["created_at"][:19],
        )

    console.print(table)

    console.print()


def create_worker_cli():
    name = Prompt.ask(
        "[cyan]Worker name[/cyan]"
    ).strip()

    if not name:
        console.print(
            "[red]Name cannot be empty.[/red]"
        )
        return

    try:
        worker_id, token = create_worker(name)
    except sqlite3.IntegrityError:
        console.print(
            "[red]Failed to create worker.[/red]"
        )
        return

    console.print()

    console.print(
        Panel(
            f"[bold]Name:[/bold] {name}\n"
            f"[bold]Worker ID:[/bold] {worker_id}\n"
            f"[bold]Token:[/bold] {token}",
            title="Worker Created",
            border_style="green",
        )
    )

    console.print(
        "[yellow]Keep the token private.[/yellow]"
    )


def show_token_cli(worker):
    console.print()

    console.print(
        Panel(
            f"[bold cyan]{worker['token']}[/bold cyan]",
            title=f"Token - {worker['name']}",
            border_style="cyan",
        )
    )


def show_worker_details(worker):
    clear_screen()
    header()

    active = worker_is_active(worker["last_seen"])

    status = (
        "[green]ACTIVE[/green]"
        if active
        else "[red]INACTIVE[/red]"
    )

    # ========================================================
    # Worker information
    # ========================================================

    console.print(
        Panel(
            f"[bold]Name:[/bold] {worker['name']}\n"
            f"[bold]Worker ID:[/bold] {worker['worker_id']}\n"
            f"[bold]Status:[/bold] {status}\n"
            f"[bold]Last Seen:[/bold] "
            f"{(worker['last_seen'] or '-')[:19]}",
            title="Worker",
            border_style="blue",
        )
    )

    # ========================================================
    # Current Task
    # ========================================================

    current = get_current_task(
        worker["worker_id"]
    )

    if current:

        task_status = {
            "pending": "[yellow]PENDING[/yellow]",
            "running": "[cyan]RUNNING[/cyan]",
        }.get(
            current["status"],
            current["status"],
        )

        current_content = current["content"]

        reports = get_task_reports(
            current["id"]
        )

        report_text = "[dim]No report yet.[/dim]"

        if reports:
            latest_report = reports[-1]["content"]

            report_text = latest_report

        current_text = (
            f"[bold]Task #{current['id']}[/bold]\n"
            f"[bold]Status:[/bold] {task_status}\n"
            f"[bold]Created:[/bold] "
            f"{current['created_at'][:19]}\n\n"
            f"[bold cyan]Task:[/bold cyan]\n"
            f"{current_content}\n\n"
            f"[bold green]Latest Report:[/bold green]\n"
            f"{report_text}"
        )

        if current["cancel_requested"]:
            current_text += (
                "\n\n"
                "[bold red]Cancellation requested[/bold red]"
            )

        console.print(
            Panel(
                current_text,
                title="CURRENT TASK",
                border_style="yellow",
                padding=(1, 2),
            )
        )

    else:

        console.print(
            Panel(
                "[dim]No current task.[/dim]",
                title="CURRENT TASK",
                border_style="dim",
            )
        )

    # ========================================================
    # Last 3 Historical Tasks
    # ========================================================

    with db_lock:
        conn = get_connection()

        historical_tasks = conn.execute(
            """
            SELECT *
            FROM tasks
            WHERE worker_id = ?
              AND status IN ('completed', 'cancelled')
            ORDER BY id DESC
            LIMIT 3
            """,
            (
                worker["worker_id"],
            ),
        ).fetchall()

        conn.close()

    if historical_tasks:

        table = Table(
            title="LAST 3 TASKS",
            box=box.ROUNDED,
            expand=True,
        )

        table.add_column(
            "#",
            width=5,
            justify="center",
        )

        table.add_column(
            "Status",
            width=12,
        )

        table.add_column(
            "Created",
            width=20,
        )

        table.add_column(
            "Task",
            ratio=2,
        )

        table.add_column(
            "Report",
            ratio=2,
        )

        for task in historical_tasks:

            reports = get_task_reports(
                task["id"]
            )

            report = "-"

            if reports:
                report = reports[-1]["content"]

            task_text = task["content"]

            if len(task_text) > 80:
                task_text = task_text[:77] + "..."

            if len(report) > 100:
                report = report[:97] + "..."

            if task["status"] == "completed":
                status_text = "[green]COMPLETED[/green]"
            else:
                status_text = "[red]CANCELLED[/red]"

            table.add_row(
                str(task["id"]),
                status_text,
                task["created_at"][:19],
                task_text,
                report,
            )

        console.print(table)

    else:

        console.print(
            Panel(
                "[dim]No previous tasks.[/dim]",
                title="LAST 3 TASKS",
                border_style="dim",
            )
        )


# ------------------- NEW FUNCTIONS FOR INTERACTIVE SHELL -------------------

def wait_for_task_completion(worker_id: str, task_id: int, timeout: int = 60):
    """
    Wait for a task to complete (status 'completed' or 'cancelled').
    Displays a spinner, shows new reports as they arrive, and times out.
    Returns (success: bool, message: str).
    """
    start_time = time.time()
    last_report_count = 0

    with console.status("[bold cyan]Waiting for agent to process...[/bold cyan]") as status:
        while True:
            elapsed = int(time.time() - start_time)
            task = get_task_by_id(task_id)

            if not task:
                status.update("[red]Task not found![/red]")
                return False, "Task disappeared"

            # Update status line with current state and elapsed time
            status_text = f"Status: {task['status']} (elapsed {elapsed}s)"

            if task['status'] in ('completed', 'cancelled'):
                status.update(f"[bold green]Task {task['status']}[/bold green]")
                # Show final report if any
                reports = get_task_reports(task_id)
                if reports:
                    console.print()
                    console.print(Panel(
                        reports[-1]['content'],
                        title="Final Report",
                        border_style="green"
                    ))
                return True, task['status']

            # Check for new reports
            reports = get_task_reports(task_id)
            if len(reports) > last_report_count:
                # New reports arrived
                for i in range(last_report_count, len(reports)):
                    console.print()
                    console.print(Panel(
                        reports[i]['content'],
                        title=f"Report #{reports[i]['id']}",
                        border_style="cyan"
                    ))
                last_report_count = len(reports)

            # Update the spinner message
            status.update(f"[bold cyan]Waiting...[/bold cyan] {status_text}")

            # Timeout check
            if elapsed > timeout:
                status.update("[red]Timeout! Task not completed.[/red]")
                return False, "Timeout"

            time.sleep(1)


def interactive_shell(worker):
    """
    REPL for sending commands to the agent.
    Built-in commands: exit, help, cancel, clear, status.
    Any other input creates a new task and waits for completion.
    """
    clear_screen()
    header()
    console.print(Panel(
        f"Interactive shell for worker: [bold]{worker['name']}[/bold] (ID: {worker['worker_id']})",
        border_style="cyan"
    ))
    console.print("[dim]Type 'help' for commands, 'exit' to quit.[/dim]")
    console.print()

    while True:
        try:
            command = Prompt.ask("[bold cyan]>>>[/bold cyan]").strip()
        except (KeyboardInterrupt, EOFError):
            break

        if not command:
            continue

        # --- Built-in commands ---
        if command.lower() in ('quit'):
            break

        elif command.lower() == 'help':
            console.print(Panel(
                "[bold]Available built-in commands:[/bold]\n"
                "  exit, quit    - Exit the shell\n"
                "  help          - Show this help\n"
                "  cancel        - Cancel the current running task\n"
                "  clear         - Clear the screen\n"
                "  status        - Show current task status\n"
                "All other input will be sent to the agent as a new task.",
                title="Help",
                border_style="cyan"
            ))
            continue

        elif command.lower() == 'clear':
            clear_screen()
            header()
            console.print(Panel(
                f"Interactive shell for worker: [bold]{worker['name']}[/bold]",
                border_style="cyan"
            ))
            continue

        elif command.lower() == 'status':
            current = get_current_task(worker['worker_id'])
            if current:
                console.print(Panel(
                    f"Task #{current['id']}\n"
                    f"Status: {current['status']}\n"
                    f"Content: {current['content']}\n"
                    f"Cancel requested: {bool(current['cancel_requested'])}",
                    title="Current Task",
                    border_style="yellow"
                ))
            else:
                console.print("[dim]No current task.[/dim]")
            continue

        elif command.lower() == 'cancel':
            current = get_current_task(worker['worker_id'])
            if not current:
                console.print("[yellow]No current task to cancel.[/yellow]")
                continue
            console.print(f"Requesting cancellation of task #{current['id']}...")
            success, msg = request_task_cancel(worker['worker_id'], current['id'])
            if success:
                console.print("[green]Cancellation requested.[/green]")
            else:
                console.print(f"[red]{msg}[/red]")
            continue

        # --- Send as task to agent ---
        console.print("[dim]Sending command to agent...[/dim]")
        try:
            task_id = create_task(worker['worker_id'], command)
        except RuntimeError as e:
            console.print(f"[red]Error: {e}[/red]")
            continue

        # Wait for the task to finish
        success, result = wait_for_task_completion(worker['worker_id'], task_id, timeout=60)
        if success:
            console.print(f"[green]Task {result}.[/green]")
        else:
            console.print(f"[red]Task did not complete: {result}[/red]")
            # Offer to cancel the hanging task
            if Confirm.ask("Cancel the task?"):
                request_task_cancel(worker['worker_id'], task_id)

# ------------------- END NEW FUNCTIONS -------------------


def worker_menu(worker):
    while True:

        show_worker_details(worker)

        console.print()

        # ====================================================
        # Determine current state
        # ====================================================

        current = get_current_task(
            worker["worker_id"]
        )

        console.print(
            "[1] Add Task"
        )

        if current:
            console.print(
                "[2] Cancel Current Task"
            )

        console.print(
            "[3] Show Token"
        )

        console.print(
            "[4] Refresh"
        )

        console.print(
            "[5] Open Task Details"
        )

        console.print(
            "[6] Interactive Shell"
        )

        console.print(
            "[0] Back"
        )

        choice = Prompt.ask(
            "\n[cyan]Select[/cyan]",
            default="5",
        )

        # ====================================================
        # Add Task
        # ====================================================

        if choice == "1":

            add_task_cli(worker)

            Prompt.ask(
                "\nPress Enter",
                default="",
            )

        # ====================================================
        # Cancel Current Task
        # ====================================================

        elif choice == "2":

            current = get_current_task(
                worker["worker_id"]
            )

            if not current:
                console.print(
                    "[yellow]No current task.[/yellow]"
                )

                Prompt.ask(
                    "\nPress Enter",
                    default="",
                )

                continue

            console.print()

            console.print(
                Panel(
                    current["content"],
                    title=f"CURRENT TASK #{current['id']}",
                    border_style="yellow",
                )
            )

            if current["status"] == "running":

                console.print(
                    "[yellow]Worker is currently executing this task.[/yellow]"
                )

            elif current["status"] == "pending":

                console.print(
                    "[yellow]Task is waiting for the worker.[/yellow]"
                )

            if not Confirm.ask(
                "Request cancellation?"
            ):
                continue

            success, message = request_task_cancel(
                worker["worker_id"],
                current["id"],
            )

            if success:

                console.print(
                    Panel(
                        "Cancellation request sent to worker.",
                        title="Cancelled",
                        border_style="green",
                    )
                )

            else:

                console.print(
                    Panel(
                        message,
                        title="Error",
                        border_style="red",
                    )
                )

            Prompt.ask(
                "\nPress Enter",
                default="",
            )

        # ====================================================
        # Show Token
        # ====================================================

        elif choice == "3":

            show_token_cli(worker)

            Prompt.ask(
                "\nPress Enter",
                default="",
            )

        # ====================================================
        # Refresh
        # ====================================================

        elif choice == "4":
            continue

        # ====================================================
        # Open Task Details
        # ====================================================

        elif choice == "5":
            recent = get_recent_tasks(
                worker["worker_id"],
                3,
            )

            if not recent:
                console.print(
                    "[yellow]No tasks.[/yellow]"
                )

                Prompt.ask(
                    "\nPress Enter",
                    default="",
                )

                continue

            task_id = IntPrompt.ask(
                "Task ID"
            )

            show_task_full_cli(task_id)

            Prompt.ask(
                "\nPress Enter",
                default="",
            )

        # ====================================================
        # Interactive Shell
        # ====================================================

        elif choice == "6":
            interactive_shell(worker)
            # After returning from shell, refresh the view

        # ====================================================
        # Back
        # ====================================================

        elif choice == "0":
            break


def add_task_cli(worker):

    current = get_current_task(
        worker["worker_id"]
    )

    if current:

        console.print(
            Panel(
                f"Task #{current['id']}\n"
                f"Status: {current['status']}\n\n"
                f"{current['content']}\n\n"
                "[yellow]"
                "Complete or cancel the current task first."
                "[/yellow]",
                title="Cannot Add Task",
                border_style="red",
            )
        )

        return

    console.print(
        "\n[cyan]Enter task text.[/cyan]"
    )

    console.print(
        "[dim]Finish with an empty line.[/dim]\n"
    )

    lines = []

    while True:

        line = Prompt.ask(
            "",
            default="",
        )

        if not line:
            break

        lines.append(line)

    content = "\n".join(lines).strip()

    if not content:

        console.print(
            "[red]Task is empty.[/red]"
        )

        return

    try:

        task_id = create_task(
            worker["worker_id"],
            content,
        )

    except RuntimeError as exc:

        console.print(
            f"[red]{exc}[/red]"
        )

        return

    console.print(
        Panel(
            f"Task #{task_id}\n"
            "Status: [yellow]PENDING[/yellow]",
            title="Task Created",
            border_style="green",
        )
    )


def cancel_task_cli(worker):
    current = get_current_task(
        worker["worker_id"]
    )

    if not current:
        console.print(
            "[yellow]No active task.[/yellow]"
        )
        return

    console.print(
        Panel(
            current["content"],
            title=f"Task #{current['id']}",
            border_style="yellow",
        )
    )

    if not Confirm.ask(
        "Request cancellation?"
    ):
        return

    success, message = request_task_cancel(
        worker["worker_id"],
        current["id"],
    )

    if success:
        console.print(
            "[green]Cancellation requested.[/green]"
        )
    else:
        console.print(
            f"[red]{message}[/red]"
        )


def show_task_full_cli(task_id: int):
    task = get_task_by_id(task_id)

    if not task:
        console.print(
            "[red]Task not found.[/red]"
        )
        return

    console.print(
        Panel(
            f"[bold]Worker:[/bold] {task['worker_id']}\n"
            f"[bold]Status:[/bold] {task['status']}\n"
            f"[bold]Created:[/bold] {task['created_at']}\n"
            f"[bold]Started:[/bold] {task['started_at'] or '-'}\n"
            f"[bold]Finished:[/bold] {task['finished_at'] or '-'}\n\n"
            f"{task['content']}",
            title=f"Task #{task['id']}",
            border_style="cyan",
        )
    )

    reports = get_task_reports(task_id)

    if reports:
        for report in reports:
            console.print(
                Panel(
                    report["content"],
                    title=(
                        f"Report #{report['id']} "
                        f"({report['report_type']})"
                    ),
                    border_style="green",
                )
            )
    else:
        console.print(
            "[dim]No reports.[/dim]"
        )

    files = get_task_files(task_id)

    if files:
        table = Table(
            title="Files",
            box=box.SIMPLE,
        )

        table.add_column("ID")
        table.add_column("Name")
        table.add_column("Size")
        table.add_column("Created")

        for item in files:
            table.add_row(
                str(item["id"]),
                item["original_name"],
                f"{item['size']} bytes",
                item["created_at"][:19],
            )

        console.print(table)


def select_worker_cli():
    workers = get_workers()

    if not workers:
        console.print(
            "[yellow]No workers found.[/yellow]"
        )
        return

    table = Table(
        title="Workers",
        box=box.ROUNDED,
    )

    table.add_column("No", width=5)
    table.add_column("Name")
    table.add_column("Worker ID")
    table.add_column("Status")

    for index, worker in enumerate(
        workers,
        start=1,
    ):
        active = worker_is_active(
            worker["last_seen"]
        )

        status = (
            "[green]ACTIVE[/green]"
            if active
            else "[red]INACTIVE[/red]"
        )

        table.add_row(
            str(index),
            worker["name"],
            worker["worker_id"],
            status,
        )

    console.print(table)

    choice = IntPrompt.ask(
        "Worker number"
    )

    if choice < 1 or choice > len(workers):
        console.print(
            "[red]Invalid selection.[/red]"
        )
        return

    worker = workers[choice - 1]

    worker_menu(worker)


def refresh_worker_activity():
    while True:
        try:
            time.sleep(CLI_REFRESH_SECONDS)
        except KeyboardInterrupt:
            break


def cli_main():
    while True:
        clear_screen()
        header()

        workers = get_workers()

        active_count = sum(
            worker_is_active(
                worker["last_seen"]
            )
            for worker in workers
        )

        console.print(
            Panel(
                f"[bold]Workers:[/bold] "
                f"{len(workers)}    "
                f"[green]Active:[/green] "
                f"{active_count}    "
                f"[red]Inactive:[/red] "
                f"{len(workers) - active_count}\n\n"
                f"[bold]API:[/bold] "
                f"http://127.0.0.1:{API_PORT}",
                border_style="blue",
            )
        )

        console.print()

        console.print(
            "[1] Workers"
        )
        console.print(
            "[2] Add Worker"
        )
        console.print(
            "[3] Open Worker"
        )
        console.print(
            "[0] Exit"
        )

        choice = Prompt.ask(
            "\n[cyan]Select[/cyan]",
            default="1",
        )

        if choice == "1":
            clear_screen()
            header()
            show_workers()

            Prompt.ask(
                "Press Enter",
                default="",
            )

        elif choice == "2":
            clear_screen()
            header()
            create_worker_cli()

            Prompt.ask(
                "\nPress Enter",
                default="",
            )

        elif choice == "3":
            clear_screen()
            header()
            select_worker_cli()

        elif choice == "0":
            break


# ============================================================
# Main
# ============================================================

def main():
    init_database()

    api_thread = threading.Thread(
        target=start_api_server,
        daemon=True,
    )

    api_thread.start()

    time.sleep(1)

    try:
        cli_main()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()