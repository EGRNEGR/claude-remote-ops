from __future__ import annotations

import asyncio
import json
from pathlib import Path

from conftest import SESSION_ID, sample_records, write_jsonl

from src.services.session_parser import SessionInspector, SessionReport, encode_project_path
from src.services.watcher import SessionWatcher

HEADLESS_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _append(path: Path, records: list[dict]) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def test_watcher_pushes_delta_after_pause(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    cwd = str(tmp_path / "proj")
    project_dir = projects / encode_project_path(cwd)
    session = project_dir / f"{SESSION_ID}.jsonl"
    headless = project_dir / f"{HEADLESS_ID}.jsonl"
    write_jsonl(session, sample_records(cwd))
    write_jsonl(headless, [{"type": "user", "entrypoint": "sdk-cli", "cwd": cwd, "message": {"content": "x"}}])
    busy: set[str] = set()
    pushed: list[SessionReport] = []

    async def notify(report: SessionReport) -> None:
        pushed.append(report)

    async def scenario() -> None:
        watcher = SessionWatcher(SessionInspector(projects), 0.7, notify, lambda: busy, ignore_headless=True)
        await watcher.start()
        try:
            assert watcher.running
            # Several quick writes -> one push after the pause, only with the new part.
            for i in range(3):
                _append(session, [{"type": "user", "cwd": cwd, "entrypoint": "cli",
                                   "message": {"content": f"запрос с ПК {i}"}}])
                await asyncio.sleep(0.2)
            _append(session, [{"type": "assistant", "cwd": cwd,
                               "message": {"id": "mX", "content": [{"type": "text", "text": "сделано"}]}}])
            # Headless (claude -p) sessions are ignored.
            _append(headless, [{"type": "assistant", "entrypoint": "sdk-cli",
                                "message": {"id": "h1", "content": [{"type": "text", "text": "bot run"}]}}])
            # Sidechain transcripts deeper in the tree are ignored.
            write_jsonl(project_dir / SESSION_ID / "subagents" / "agent-1.jsonl", [{"type": "user"}])
            for _ in range(60):
                if pushed:
                    break
                await asyncio.sleep(0.1)
            await asyncio.sleep(1.2)
            assert len(pushed) == 1, [p.session_id for p in pushed]
            report = pushed[0]
            assert report.session_id == SESSION_ID and report.is_delta
            assert report.prompts == ["запрос с ПК 0", "запрос с ПК 1", "запрос с ПК 2"]
            assert report.final_answer == "сделано" and report.title == "fix-tests"

            # Sessions driven by the bot itself are skipped but their offset advances.
            busy.add(SESSION_ID)
            _append(session, [{"type": "user", "cwd": cwd, "message": {"content": "от бота"}}])
            await asyncio.sleep(1.8)
            assert len(pushed) == 1
            busy.clear()
            _append(session, [{"type": "user", "cwd": cwd, "message": {"content": "снова с ПК"}}])
            for _ in range(40):
                if len(pushed) == 2:
                    break
                await asyncio.sleep(0.1)
            assert len(pushed) == 2 and pushed[1].prompts == ["снова с ПК"]
        finally:
            await watcher.stop()
        assert not watcher.running

    asyncio.run(scenario())
