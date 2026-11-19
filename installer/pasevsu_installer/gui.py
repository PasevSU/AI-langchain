"""Табло за управление на инсталационния агент.

Таблото е статична страница (``pasevsu_installer/assets/gui``), която се
публикува в ``installer/gui`` от декларацията ``installer-gui``. Този модул
описва единствения вход, който страницата консумира — JSON документ
``snapshot`` — и умее да го запише на диск или да го сервира локално с
``python -m pasevsu_installer gui --serve``.

Модулът е само за четене спрямо състоянието на инсталатора: единствените файлове,
които записва, са ``snapshot.json`` и копиета на вградените активи.
"""

from __future__ import annotations

import json
import webbrowser
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import Config, SuiteSpec
from .errors import RegistryError
from .journal import Journal
from .model import Task
from .registry import load_declarations
from .state import StateStore
from .util import UI_ASSETS_DIR, atomic_write_text, dump_json, read_text, utc_now

DASHBOARD_SCHEMA = "pasevsu.installer.dashboard.v1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
JOURNAL_TAIL = 40
#: Името трябва да съвпада с това, което ``app.js`` търси сам (``snapshot.json``).
SNAPSHOT_NAME = "snapshot.json"
#: Име на директорията, в която се публикува таблото (``installer/gui``).
GUI_DIRNAME = "gui"

#: Артефактите на инсталатора, които таблото показва в секция „Артефакти“.
ARTEFACTS = (
    "state.json",
    "selection.json",
    "selected-agents.json",
    "backup.json",
    "publish.json",
    "report.md",
    "journal.jsonl",
)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".svg": "image/svg+xml",
}


def build_snapshot(config: Config) -> dict[str, Any]:
    """Сглобява JSON документа, който таблото рендира."""
    store = StateStore(config.root, config.state_dir)
    state = store.load() if store.exists() else {}
    raw_tasks = state.get("tasks")
    tasks_state: dict[str, Any] = raw_tasks if isinstance(raw_tasks, dict) else {}
    agents, tasks, registry_error = _registry_rows(config, tasks_state)
    snapshot: dict[str, Any] = {
        "schema": DASHBOARD_SCHEMA,
        "generated_at": utc_now(),
        "installer": {
            "name": config.name,
            "version": config.read_version(),
            "version_file": config.version_file,
            "registry_dir": config.registry_dir,
            "state_dir": config.state_dir,
        },
        "run": _run_row(state),
        "backup": state.get("backup"),
        "artefacts": _artefacts(store),
        "agents": agents,
        "tasks": tasks,
        "suites": _suites(config, state),
        "publish": _publish(store, state),
        "journal": _journal_tail(store),
        "report_markdown": _report(store),
    }
    if registry_error:
        snapshot["registry_error"] = registry_error
    return snapshot


def write_snapshot(config: Config, path: Path | None = None) -> Path:
    """Записва snapshot-а и връща пътя до него (по подразбиране в state директорията)."""
    store = StateStore(config.root, config.state_dir)
    target = path or store.artifacts(SNAPSHOT_NAME)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, dump_json(build_snapshot(config)))
    return target


def write_gui(config: Config, target: Path | None = None) -> tuple[Path, ...]:
    """Копира вградените активи на таблото и записва snapshot до тях.

    Връща пътищата на всички записани файлове. По подразбиране таблото се
    генерира в ``<root>/<commit_scope>/gui``, тоест ``installer/gui``.
    """
    assets = asset_names()
    if not assets:
        raise FileNotFoundError(f"липсват вградени активи: {UI_ASSETS_DIR}")
    destination = target or (config.root / config.commit_scope / GUI_DIRNAME)
    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in assets:
        source = UI_ASSETS_DIR / name
        path = destination / name
        atomic_write_text(path, read_text(source))
        written.append(path)
    written.append(write_snapshot(config, destination / SNAPSHOT_NAME))
    return tuple(written)


def asset_names() -> tuple[str, ...]:
    """Връща имената на вградените активи на таблото (сортирани)."""
    if not UI_ASSETS_DIR.is_dir():
        return ()
    return tuple(sorted(item.name for item in UI_ASSETS_DIR.iterdir() if item.is_file()))


def make_server(
    config: Config,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> ThreadingHTTPServer:
    """Създава (но не стартира) HTTP сървъра на таблото."""
    published = config.root / config.commit_scope / GUI_DIRNAME

    class DashboardHandler(BaseHTTPRequestHandler):
        """Сервира таблото и ``/api/snapshot`` от живите артефакти."""

        server_version = "pasevsu-installer-gui/1.0"

        def do_GET(self) -> None:
            """Отговаря на GET заявка."""
            raw = self.path.split("?", 1)[0]
            name = raw.lstrip("/") or "index.html"
            for prefix in ("gui/", "installer/gui/"):
                if name.startswith(prefix):
                    name = name[len(prefix) :]
                    break
            if name in ("", "index.html", "api/snapshot"):
                if name == "api/snapshot":
                    body = dump_json(build_snapshot(config)).encode("utf-8")
                    self._send(body, CONTENT_TYPES[".json"])
                    return
                name = "index.html"
            for base in (published, UI_ASSETS_DIR):
                resolved = _safe_file(base, name)
                if resolved is not None:
                    self._send(
                        resolved.read_bytes(),
                        CONTENT_TYPES.get(resolved.suffix.lower(), "application/octet-stream"),
                    )
                    return
            self._not_found()

        def _send(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            """Изпраща тяло с правилните хедъри."""
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _not_found(self) -> None:
            """Отговаря с 404 — съобщението е в тялото, статус-редът остава ASCII."""
            body = "Файлът не е част от таблото.\n".encode()
            self._send(body, "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)

    return ThreadingHTTPServer((host, port), DashboardHandler)


def serve(
    config: Config,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    announce: Callable[[str], None] | None = None,
    open_browser: bool = False,
) -> None:
    """Стартира таблото и блокира, докато сървърът бъде спрян (Ctrl+C)."""
    server = make_server(config, host=host, port=port)
    bound = int(server.server_address[1])
    url = f"http://{host}:{bound}/"
    if announce is not None:
        announce(url)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _safe_file(base: Path, name: str) -> Path | None:
    """Връща файл в ``base``, ако ``name`` не излиза извън нея."""
    if not name or name.startswith("/"):
        return None
    try:
        candidate = (base / name).resolve()
        candidate.relative_to(base.resolve())
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def _run_row(state: dict[str, Any]) -> dict[str, Any]:
    """Редът „състояние на пуска“ за таблото."""
    return {
        "phase": str(state.get("phase", "new")),
        "runs": _as_int(state.get("runs")),
        "updated_at": state.get("updated_at"),
        "first_run_started_at": state.get("first_run_started_at"),
        "first_run_completed_at": state.get("first_run_completed_at"),
        "plan_fingerprint": state.get("plan_fingerprint"),
    }


def _artefacts(store: StateStore) -> list[dict[str, Any]]:
    """Списък на артефактите в state директорията с размер и наличност."""
    rows: list[dict[str, Any]] = []
    for name in ARTEFACTS:
        path = store.artifacts(name)
        exists = path.is_file()
        rows.append({"name": name, "exists": exists, "size": path.stat().st_size if exists else 0})
    return rows


def _registry_rows(
    config: Config, tasks_state: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Агенти и задачи от декларациите, обогатени със записаното състояние."""
    try:
        declarations = load_declarations(config.registry_path)
    except RegistryError as exc:
        return [], [], str(exc)
    agents: list[dict[str, Any]] = []
    flat: list[dict[str, Any]] = []
    known: set[str] = set()
    for declaration in declarations:
        rows = [_agent_task_row(task, declaration.id, tasks_state) for task in declaration.tasks]
        known.update(row["id"] for row in rows)
        flat.extend(_flat_row(row) for row in rows)
        agents.append(
            {
                "id": declaration.id,
                "title": declaration.title,
                "status": declaration.status,
                "purpose": declaration.purpose,
                "source": declaration.source,
                "scope": list(declaration.scope),
                "declared": bool(declaration.declared_tasks),
                "tasks": rows,
            }
        )
    flat.extend(_flat_row(row) for row in _orphan_rows(tasks_state, known))
    return agents, flat, ""


def _flat_row(row: dict[str, Any]) -> dict[str, Any]:
    """Плосък ред за таблицата със задачи (полетата, които тя чете)."""
    state = row.get("state")
    record = state if isinstance(state, dict) else {}
    return {
        "id": row["id"],
        "agent": row["agent"],
        "title": row.get("title", row["id"]),
        "declared": row.get("declared", False),
        "risk": row.get("risk", "unknown"),
        "fingerprint": row.get("fingerprint", ""),
        "status": str(record.get("status", "not-run")),
        "runs": _as_int(record.get("runs")),
        "at": record.get("at"),
    }


def _agent_task_row(task: Task, agent_id: str, tasks_state: dict[str, Any]) -> dict[str, Any]:
    """Една задача във вида, който таблото очаква."""
    entry = tasks_state.get(task.id)
    record = entry if isinstance(entry, dict) else {}
    return {
        "id": task.id,
        "agent": agent_id,
        "title": task.title,
        "declared": task.declared,
        "risk": task.risk,
        "fingerprint": task.fingerprint(),
        "state": {
            "status": str(record.get("status", "not-run")),
            "runs": _as_int(record.get("runs")),
            "at": _last_at(record),
        },
    }


def _orphan_rows(tasks_state: dict[str, Any], known: set[str]) -> list[dict[str, Any]]:
    """Задачи, останали в състоянието, но вече без декларация."""
    rows: list[dict[str, Any]] = []
    for task_id in sorted(tasks_state):
        if task_id in known:
            continue
        record = tasks_state[task_id]
        entry = record if isinstance(record, dict) else {}
        rows.append(
            {
                "id": task_id,
                "agent": str(entry.get("agent", "?")),
                "title": str(entry.get("title", task_id)),
                "declared": False,
                "risk": "unknown",
                "fingerprint": str(entry.get("fingerprint", "")),
                "state": {
                    "status": str(entry.get("status", "not-run")),
                    "runs": _as_int(entry.get("runs")),
                    "at": _last_at(entry),
                },
            }
        )
    return rows


def _last_at(record: dict[str, Any]) -> Any:
    """Връща времето на последното изпълнение от историята на записа."""
    history = record.get("history")
    if isinstance(history, list) and history:
        last = history[-1]
        if isinstance(last, dict):
            return last.get("at")
    return None


def _suites(config: Config, state: dict[str, Any]) -> list[dict[str, Any]]:
    """Тестовите пакети от конфигурацията плюс резултата от последния пуск."""
    recorded = state.get("suites")
    if isinstance(recorded, list):
        entries = [item for item in recorded if isinstance(item, dict)]
    else:
        entries = []
    by_id = {str(item.get("id")): item for item in entries}
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for suite in config.suites:
        seen.add(suite.id)
        rows.append(_suite_row(suite, by_id.get(suite.id)))
    for suite_id, item in by_id.items():
        if suite_id not in seen:
            rows.append(
                {
                    "id": suite_id,
                    "description": "",
                    "required": bool(item.get("required", False)),
                    "command": list(item.get("command", []) or []),
                    "cwd": str(item.get("cwd", ".")),
                    "status": str(item.get("status", "unknown")),
                    "duration_ms": item.get("duration_ms") or 0,
                    "skip_reason": str(item.get("skip_reason", "")),
                }
            )
    return rows


def _suite_row(suite: SuiteSpec, result: dict[str, Any] | None) -> dict[str, Any]:
    """Един тестов пакет: декларация от конфигурацията + последен резултат."""
    record = result or {}
    return {
        "id": suite.id,
        "description": suite.description,
        "required": suite.required,
        "command": list(suite.command),
        "cwd": suite.cwd,
        "status": str(record.get("status", "unverified")),
        "duration_ms": record.get("duration_ms") or 0,
        "skip_reason": str(record.get("skip_reason", "")),
    }


def _publish(store: StateStore, state: dict[str, Any]) -> dict[str, Any]:
    """Последното публикуване — от ``publish.json``, с резерва в състоянието."""
    record = _read_json(store.artifacts("publish.json"))
    if not record:
        fallback = state.get("publish")
        record = fallback if isinstance(fallback, dict) else {}
    if not record:
        return {}
    paths = record.get("paths")
    pushed = record.get("pushed")
    return {
        "status": str(record.get("status", "")),
        "branch": record.get("branch"),
        "commit": record.get("commit"),
        "tag": record.get("tag"),
        "reason": record.get("reason"),
        "paths": list(paths) if isinstance(paths, list) else [],
        "pushed": list(pushed) if isinstance(pushed, list) else [],
    }


def _journal_tail(store: StateStore) -> list[dict[str, Any]]:
    """Последните събития от журнала (най-старото първо)."""
    path = store.artifacts("journal.jsonl")
    if not path.is_file():
        return []
    return Journal(path).events(limit=JOURNAL_TAIL)


def _report(store: StateStore) -> str:
    """Markdown отчетът от последния пуск (празен низ, ако липсва)."""
    path = store.artifacts("report.md")
    return read_text(path) if path.is_file() else ""


def _read_json(path: Path) -> dict[str, Any]:
    """Чете JSON обект; при липса или повреда връща празен речник."""
    if not path.is_file():
        return {}
    try:
        payload = json.loads(read_text(path))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _as_int(value: Any) -> int:
    """Привежда стойност към ``int`` без изключение."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "ARTEFACTS",
    "DASHBOARD_SCHEMA",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "GUI_DIRNAME",
    "SNAPSHOT_NAME",
    "asset_names",
    "build_snapshot",
    "make_server",
    "serve",
    "write_gui",
    "write_snapshot",
]
