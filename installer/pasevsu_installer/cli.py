"""Command line entry point for the installer agent.

Verbs and flags:

``install`` (default)
    Run the migration. On a fresh checkout this is the first run and performs the
    real work: select agents, take a verified backup release, apply the declared
    changes, run every configured test suite and publish the update.
``--dry-run``
    Predict every task and every suite without mutating or committing.
``--status``
    Print the recorded state, artifacts and last run without touching anything.
``--reset``
    Remove the installer state so the next run is a first run again.
``--agents a,b``
    Restrict the run to explicitly named agents.
``gui``
    Build the agent dashboard: write ``.installer/gui`` (assets + ``snapshot.json``)
    from the live state and optionally serve it locally.
``--serve`` / ``--port`` / ``--open``
    Serve the dashboard over HTTP on ``127.0.0.1`` and optionally open a browser.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .backup import Backup, create_backup
from .config import DEFAULT_CONFIG_REL, Config, load_config
from .errors import ConfigError, InstallerError, RegistryError
from .executor import ExecutionResult, execute, merge_state_tasks
from .executor import summarise as summarise_execution
from .gitops import changed_paths, is_repository, revision
from .gui import DEFAULT_PORT, GUI_DIRNAME, asset_names, build_snapshot, serve, write_gui
from .journal import Journal
from .publisher import Publication, publish, write_publication_record, write_release_notes
from .registry import load_declarations
from .registry import summarise as summarise_registry
from .selection import SelectionReport, report, select
from .state import StateStore
from .tests_runner import TestReport, blockers, run_suites
from .tests_runner import summarise as summarise_tests
from .util import UI_ASSETS_DIR, atomic_write_text, dump_json, utc_now

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2
EXIT_LOCKED = 3


def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")


def _phase(
    store: StateStore,
    journal: Journal,
    state: dict[str, Any],
    phase: str,
    **extra: Any,
) -> dict[str, Any]:
    """Move state and journal to ``phase`` at the same moment.

    The journal records every event with the phase it belongs to; keeping both
    in one helper makes it impossible for the two to drift apart.
    """
    journal.set_phase(phase)
    return store.mark(state, phase, **extra)


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser used by :func:`run`."""
    parser = argparse.ArgumentParser(
        prog="pasevsu-installer",
        description="Инсталационен агент: миграция с бекъп, тестове и публикуване.",
    )
    parser.add_argument(
        "verb", nargs="?", default="install", choices=("install", "status", "reset", "gui")
    )
    parser.add_argument(
        "--root", default=".", help="корен на хранилището (по подразбиране текущата папка)"
    )
    parser.add_argument(
        "--config",
        default=None,
        help=f"път до конфигурацията (по подразбиране {DEFAULT_CONFIG_REL})",
    )
    parser.add_argument("--agents", default="", help="списък агенти през запетая")
    parser.add_argument("--dry-run", action="store_true", help="само прогноза, без промени")
    parser.add_argument("--no-tests", action="store_true", help="пропусни тестовите пакети")
    parser.add_argument("--no-publish", action="store_true", help="без commit/tag/push")
    parser.add_argument(
        "--force",
        action="store_true",
        help="изпълни дори при вече завършен първи пуск",
    )
    parser.add_argument("--status", action="store_true", help="съкратено за командата status")
    parser.add_argument("--reset", action="store_true", help="съкратено за командата reset")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"порт за таблото (по подразбиране {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--serve", action="store_true", help="стартирай локален сървър за таблото"
    )
    parser.add_argument(
        "--open",
        dest="open_browser",
        action="store_true",
        help="отвори таблото в браузър (изисква --serve)",
    )
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    """Execute the CLI and return a process exit code."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    root = Path(args.root).expanduser().resolve()
    if args.status:
        args.verb = "status"
    if args.reset:
        args.verb = "reset"
    try:
        config = load_config(root, Path(args.config) if args.config else None)
    except ConfigError as exc:
        _err(f"конфигурация: {exc}")
        return EXIT_CONFIG
    if args.verb == "status":
        return _status(config)
    if args.verb == "reset":
        return _reset(config, force=args.force)
    if args.verb == "gui":
        return _gui(
            config,
            port=args.port,
            do_serve=args.serve,
            open_browser=args.open_browser,
        )
    return _install(
        config,
        agents=_split_agents(args.agents),
        dry_run=args.dry_run,
        run_tests=not args.no_tests,
        do_publish=not args.no_publish,
        force=args.force,
    )


def _gui(
    config: Config,
    *,
    port: int,
    do_serve: bool,
    open_browser: bool,
) -> int:
    """Build the agent dashboard and optionally serve it locally."""
    assets = asset_names()
    if not assets:
        _err(f"табло: липсват вградени активи в {UI_ASSETS_DIR}")
        return EXIT_FAILED
    store = StateStore(config.root, config.state_dir)
    store.ensure()
    try:
        written = write_gui(config, store.artifacts(GUI_DIRNAME))
    except FileNotFoundError as exc:
        _err(f"табло: {exc}")
        return EXIT_FAILED
    snapshot = build_snapshot(config)
    _out(
        "Табло: "
        f"{len(snapshot['agents'])} агента · "
        f"{len(snapshot['tasks'])} задачи · "
        f"{len(snapshot['suites'])} тестови пакета"
    )
    _out(f"Файлове: {len(written)} · {written[0].parent}")
    _out(f"Данни: {written[-1]}")
    _out(f"Активи: {', '.join(assets)}")
    if "registry_error" in snapshot:
        _err(f"табло: декларации: {snapshot['registry_error']}")
    published = config.root / config.commit_scope / GUI_DIRNAME
    if published.is_dir():
        _out(f"Публикувано копие: {published}")
    else:
        _out("Публикувано копие: няма (ще се създаде при следващата миграция).")
    if not do_serve:
        _out("Съвет: python -m pasevsu_installer gui --serve (за табло в браузър).")
        return EXIT_OK
    try:
        serve(
            config,
            port=port,
            announce=lambda url: _out(f"Адрес: {url}"),
            open_browser=open_browser,
        )
    except OSError as exc:
        _err(f"табло: сървърът не стартира на порт {port}: {exc}")
        return EXIT_FAILED
    return EXIT_OK


def _install(
    config: Config,
    *,
    agents: tuple[str, ...],
    dry_run: bool,
    run_tests: bool,
    do_publish: bool,
    force: bool,
) -> int:
    store = StateStore(config.root, config.state_dir)
    store.ensure()
    journal = Journal(store.artifacts("journal.jsonl"))

    try:
        with store.lock("first-run-migration" if store.is_first_run() else "migration"):
            return _run_locked(
                config,
                store,
                journal,
                agents=agents,
                dry_run=dry_run,
                run_tests=run_tests,
                do_publish=do_publish,
                force=force,
            )
    except InstallerError as exc:
        journal.append("error", code=exc.code, message=str(exc))
        _err(f"грешка [{exc.code}]: {exc}")
        return EXIT_LOCKED if exc.code == "state-locked" else EXIT_FAILED


def _run_locked(
    config: Config,
    store: StateStore,
    journal: Journal,
    *,
    agents: tuple[str, ...],
    dry_run: bool,
    run_tests: bool,
    do_publish: bool,
    force: bool,
) -> int:
    version = config.read_version()
    state = store.load() if store.exists() else store.fresh(version)
    first_run = store.is_first_run()
    if not first_run and state.get("phase") == "done" and not force and not dry_run:
        _out("Инсталаторът вече е завършил успешно. Ползвайте --force за повторно изпълнение.")
        _status(config)
        return EXIT_OK

    journal.append(
        "start",
        version=version,
        first_run=first_run,
        dry_run=dry_run,
        agents=list(agents),
        revision=revision(config.root) if is_repository(config.root) else {},
    )
    state = _phase(store, journal, state, "selection", version=version)
    if first_run and not dry_run:
        state["first_run_started_at"] = state.get("first_run_started_at") or utc_now()

    try:
        declarations = load_declarations(config.registry_path)
    except RegistryError as exc:
        store.save(_phase(store, journal, state, "registry-failed", error=str(exc)))
        raise
    journal.append("registry", summary=summarise_registry(declarations))

    selection = select(declarations, state, only=agents or None)
    selection_report = report(selection)
    _write_selection(store, selection_report)
    journal.append("selection", **selection_report.to_dict())
    _print_selection(selection_report, dry_run=dry_run)
    if selection.empty:
        store.save(_phase(store, journal, state, "done-empty"))
        _out("Няма заявени промени за прилагане — нищо не е променено.")
        return EXIT_OK

    state = _phase(store, journal, state, "backup", plan_fingerprint=selection.fingerprint)
    if dry_run:
        backup = Backup(
            id="dry-run",
            created_at=utc_now(),
            kind="skipped",
            tree_sha256="",
            files=0,
            archive="",
            archive_sha256="",
            manifest="",
            version=version,
        )
        journal.append("backup", **backup.to_dict())
        _out("Пробен пуск: не се създава бекъп релиз.")
    else:
        backup = create_backup(
            config.root,
            store.artifacts("backups"),
            tag_prefix=config.backup_tag_prefix,
            version=version,
        )
        journal.append("backup", **backup.to_dict())
        _out(
            f"Бекъп релиз: {backup.id} · {backup.kind} · "
            f"{backup.files} файла · {backup.archive_sha256[:12]}"
        )
    state["backup"] = backup.to_dict()
    state = store.save(state)

    state = _phase(store, journal, state, "apply")
    execution = execute(selection, config.root, dry_run=dry_run, journal=journal)
    state["tasks"] = merge_state_tasks(state.get("tasks", {}), execution, dry_run=dry_run)
    state["plan_fingerprint"] = selection.fingerprint
    state = store.save(state)
    _print_execution(execution)

    def _record(tests: TestReport | None, publication: Publication | None) -> None:
        """Write the run report for the current state of this run."""
        _finalise(
            config,
            store,
            state,
            backup=backup,
            selection_report=selection_report,
            execution=execution,
            tests=tests,
            publication=publication,
            version=version,
        )

    if not execution.ok:
        state = store.save(
            _phase(store, journal, state, "apply-failed", stopped_at=execution.stopped_at)
        )
        _record(None, None)
        return EXIT_FAILED

    released = config.read_version()
    if released != version:
        journal.append("version-bumped", before=version, after=released)
        _out(f"Версия: {version} → {released} (обявена от задача в тази миграция)")
        version = released
        state["version"] = version
        state = store.save(state)

    tests: TestReport | None = None
    if run_tests and not dry_run:
        state = store.save(_phase(store, journal, state, "tests"))
        tests = run_suites(config, journal=journal)
        state["suites"] = [item.to_dict() for item in tests.suites]
        state = store.save(state)
        _print_tests(tests)
        if tests.blocking:
            state = store.save(
                _phase(store, journal, state, "tests-failed", blocking=list(blockers(tests)))
            )
            _record(tests, None)
            return EXIT_FAILED
        for suite in tests.advisory:
            _out(
                f"  ! {suite.id}: незадължителен пакет без успех — "
                "не блокира публикуването (виж отчета)"
            )

    publication = (
        _publish(config, journal, execution, version, dry_run=dry_run) if do_publish else None
    )
    if publication is not None:
        state["publish"] = publication.to_dict()
        write_publication_record(config, publication)
        _print_publication(publication)
        if not publication.ok:
            state["runs"] = int(state.get("runs", 0)) + 1
            state["first_run_completed_at"] = (
                state.get("first_run_completed_at") or utc_now()
            )
            state = store.save(_phase(store, journal, state, "publish-failed"))
            _record(tests, publication)
            return EXIT_FAILED

    if dry_run:
        state = store.save(_phase(store, journal, state, "planned"))
        _record(tests, publication)
        _out("Пробен пуск: нищо не е променено.")
        return EXIT_OK

    state["runs"] = int(state.get("runs", 0)) + 1
    state["first_run_completed_at"] = state.get("first_run_completed_at") or utc_now()
    state = _phase(store, journal, state, "done", plan_fingerprint=selection.fingerprint)
    _record(tests, publication)
    _out("Готово: миграцията приключи.")
    return EXIT_OK


def _publish(
    config: Config,
    journal: Journal,
    execution: ExecutionResult,
    version: str,
    *,
    dry_run: bool,
) -> Publication:
    body = _release_body(execution, config)
    paths = list(execution.changed_paths)
    note_rel = ""
    if not dry_run:
        note = write_release_notes(config, version=version, body=body)
        note_rel = note.relative_to(config.root).as_posix()
    paths = _publish_paths(config, paths, note_rel)
    return publish(
        config,
        version=version,
        paths=paths,
        summary=_commit_summary(execution),
        body=body,
        journal=journal,
        dry_run=dry_run,
    )


def _commit_summary(execution: ExecutionResult) -> str:
    """Describe the run honestly: verified tasks first, real writes second.

    A re-run of an idempotent migration changes nothing, so ``applied`` is empty;
    calling that "миграция на 0 задачи" would hide the fact that the whole
    migration was verified.
    """
    total = len(execution.outcomes)
    changed = len(execution.applied)
    if not total:
        return "миграция без задачи"
    if changed:
        return f"миграция: {changed} от {total} задачи с промени"
    return f"миграция: {total} задачи проверени, без промени"


def _publish_paths(config: Config, applied: list[str], note_rel: str) -> list[str]:
    """Return the paths that make up this migration.

    The published update is the repository *after* the run: files written now,
    files that a previous run already wrote and left uncommitted, and the release
    note. Paths outside ``note_rel`` come from git, which keeps an idempotent
    re-run (everything ``unchanged``) from publishing nothing at all. The
    installer state directory is filtered out later by the publisher.
    """
    paths = [item for item in applied if item]
    for item in changed_paths(config.root):
        if item not in paths:
            paths.append(item)
    if note_rel and note_rel not in paths:
        paths.append(note_rel)
    return paths


def _release_body(execution: ExecutionResult, config: Config) -> str:
    changed = len(execution.applied)
    total = len(execution.outcomes)
    lines = [
        (
            f"Инсталационен агент `{config.name}` провери {total} задачи "
            f"и промени {changed}."
        ),
        "",
    ]
    for outcome in execution.outcomes:
        marker = "x" if outcome.ok else " "
        lines.append(f"- [{marker}] `{outcome.id}` — {outcome.title} ({outcome.status})")
    lines.append("")
    lines.append(f"Fingerprint на плана: `{execution.fingerprint[:16]}`")
    return "\n".join(lines)


def _finalise(
    config: Config,
    store: StateStore,
    state: dict[str, Any],
    *,
    backup: Backup,
    selection_report: SelectionReport,
    execution: ExecutionResult,
    tests: TestReport | None,
    publication: Publication | None,
    version: str,
) -> None:
    report_path = store.artifacts("report.md")
    atomic_write_text(
        report_path,
        _report_markdown(
            config,
            state,
            backup=backup,
            selection_report=selection_report,
            execution=execution,
            tests=tests,
            publication=publication,
            version=version,
        ),
    )
    atomic_write_text(store.artifacts("selection.json"), dump_json(selection_report.to_dict()))
    if backup.manifest:
        atomic_write_text(store.artifacts("backup.json"), dump_json(backup.to_dict()))


def _backup_line(backup: Backup) -> str:
    """Render one report line describing the backup release."""
    if not backup.ok:
        return "не е създаден (пробен пуск)"
    return f"`{backup.id}` ({backup.kind}, {backup.files} файла, `{backup.archive_sha256[:12]}`)"


def _report_markdown(
    config: Config,
    state: dict[str, Any],
    *,
    backup: Backup,
    selection_report: SelectionReport,
    execution: ExecutionResult,
    tests: TestReport | None,
    publication: Publication | None,
    version: str,
) -> str:
    lines = [
        f"# Отчет на инсталатора · {config.name} {version}",
        "",
        f"- Фаза: **{state.get('phase', '?')}**",
        f"- Начало на първи пуск: {state.get('first_run_started_at') or '—'}",
        f"- Fingerprint на плана: `{execution.fingerprint[:16]}`",
        f"- Бекъп: {_backup_line(backup)}",
        "",
        "## Избрани агенти",
        "",
    ]
    lines.extend(f"- `{agent}`" for agent in selection_report.selection.agent_ids)
    if not selection_report.selection.agent_ids:
        lines.append("- (няма)")
    lines.extend(
        [
            "",
            "## Задачи",
            "",
            "| Задача | Агент | Статус | Пътища |",
            "| --- | --- | --- | --- |",
        ]
    )
    lines.extend(
        f"| `{outcome.id}` | `{outcome.agent}` | {outcome.status} | "
        f"{(', '.join(f'`{path}`' for path in outcome.paths)) or '—'} |"
        for outcome in execution.outcomes
    )
    exclusions = [*selection_report.excluded_agents, *selection_report.excluded_tasks]
    lines.extend(["", "## Изключени", ""])
    lines.extend(f"- `{item.id}` — {item.reason}" for item in exclusions)
    if not exclusions:
        lines.append("- (няма)")
    if tests is not None:
        lines.extend(
            [
                "",
                "## Тестове",
                "",
                "| Пакет | Задължителен | Статус | Изход | Време |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for suite in tests.suites:
            gate = "да" if suite.required else "не (не блокира)"
            lines.append(
                f"| `{suite.id}` | {gate} | {suite.status} | "
                f"{suite.exit_code if suite.exit_code is not None else '—'} | "
                f"{suite.duration_ms / 1000:.1f} s |"
            )
        if tests.advisory:
            lines.extend(["", "Незадължителни пакети без успех (не блокират публикуването):"])
            lines.extend(f"- `{item.id}` — {item.status}" for item in tests.advisory)
    if publication is not None:
        lines.extend(
            [
                "",
                "## Публикуване",
                "",
                f"- Статус: **{publication.status}** {publication.reason}".rstrip(),
                f"- Клон: `{publication.branch or '—'}`",
                f"- Commit: `{publication.commit or '—'}`",
                f"- Таг: `{publication.tag or '—'}`",
                f"- Push: {', '.join(publication.pushed) or '—'}",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def _write_selection(store: StateStore, selection_report: SelectionReport) -> None:
    path = store.artifacts("selected-agents.json")
    atomic_write_text(path, dump_json(selection_report.to_dict()))


def _print_selection(selection_report: SelectionReport, *, dry_run: bool) -> None:
    selection = selection_report.selection
    prefix = "прогноза" if dry_run else "избор"
    _out(f"Избор ({prefix}): {len(selection.agent_ids)} агента · {len(selection.tasks)} задачи")
    for agent in selection.agent_ids:
        _out(f"  + {agent}")
    for item in selection_report.excluded_agents:
        _out(f"  - {item.id}: {item.reason}")
    for item in selection_report.excluded_tasks:
        _out(f"  ~ {item.id}: {item.reason}")


def _print_execution(execution: ExecutionResult) -> None:
    _out(summarise_execution(execution))
    for outcome in execution.outcomes:
        marker = "ok" if outcome.ok else "!!"
        _out(f"  [{marker}] {outcome.id} — {outcome.title} ({outcome.status})")
        if outcome.error:
            _out(f"       {outcome.error}")


def _print_tests(tests: TestReport) -> None:
    _out(f"Тестове: {summarise_tests(tests)}")
    for suite in tests.suites:
        suffix = f" — {suite.skip_reason}" if suite.skip_reason else ""
        flag = " (незадължителен)" if not suite.required else ""
        _out(f"  [{suite.status}] {suite.id}{flag} ({suite.duration_ms / 1000:.1f} s){suffix}")


def _print_publication(publication: Publication) -> None:
    if publication.status == "published":
        commit = publication.commit or ""
        _out(f"Публикувано: {publication.branch} · {commit[:12]} · таг {publication.tag}")
        return
    _out(f"Публикуване: {publication.status} — {publication.reason}")


def _status(config: Config) -> int:
    store = StateStore(config.root, config.state_dir)
    if not store.exists():
        _out("Инсталаторът не е стартиран (няма състояние) — следващият пуск ще е първи.")
        return EXIT_OK
    state = store.load()
    _out(f"Състояние: {store.path}")
    _out(
        f"  версия: {state.get('version', '?')} · фаза: {state.get('phase', '?')} · "
        f"пускове: {state.get('runs', 0)}"
    )
    if state.get("first_run_started_at"):
        _out(
            f"  първи пуск: {state['first_run_started_at']} → "
            f"{state.get('first_run_completed_at') or 'незавършен'}"
        )
    backup = state.get("backup") or {}
    if backup:
        _out(
            f"  бекъп: {backup.get('id')} · {backup.get('files')} файла · "
            f"{backup.get('archive_sha256', '')[:12]}"
        )
    tasks = state.get("tasks") or {}
    if isinstance(tasks, dict) and tasks:
        _out("  задачи:")
        for task_id, entry in sorted(tasks.items()):
            _out(f"    {task_id}: {entry.get('status', '?')} ({entry.get('runs', 0)} пуска)")
    suites = state.get("suites") or []
    if suites:
        _out("  тестове: " + ", ".join(f"{item.get('id')}={item.get('status')}" for item in suites))
    publication = state.get("publish") or {}
    if publication:
        _out(f"  публикуване: {publication.get('status')} · {publication.get('tag') or '—'}")
    _out(f"  отчет: {store.artifacts('report.md')}")
    _out(f"  журнал: {store.artifacts('journal.jsonl')}")
    if is_repository(config.root):
        paths = changed_paths(config.root)
        _out(f"  променени файлове в работната папка: {len(paths)}")
    return EXIT_OK


def _reset(config: Config, *, force: bool) -> int:
    store = StateStore(config.root, config.state_dir)
    if not store.exists():
        _out("Няма състояние за изтриване.")
        return EXIT_OK
    if not force:
        _out("За да изтриете състоянието, добавете --force. Това не пипа бекъпите в git.")
        return EXIT_CONFIG
    if store.lock_path.exists():
        store.lock_path.unlink()
    if store.path.exists():
        store.path.unlink()
    _out(f"Състоянието е изтрито: {store.path} — следващият пуск ще е първи.")
    return EXIT_OK


def _split_agents(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI, converting unexpected errors into exit code 1."""
    try:
        return run(argv)
    except KeyboardInterrupt:
        _err("прекъснато от потребителя")
        return EXIT_FAILED


__all__ = ["EXIT_CONFIG", "EXIT_FAILED", "EXIT_LOCKED", "EXIT_OK", "build_parser", "main", "run"]
