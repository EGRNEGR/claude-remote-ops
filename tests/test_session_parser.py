from __future__ import annotations

import asyncio
import re
from pathlib import Path

from conftest import SESSION_ID, sample_records, write_jsonl

from src.services.session_parser import (
    SessionInspector,
    _dfs_decode,
    clean_prompt,
    encode_project_path,
    render_report_html,
    render_report_markdown,
)


def test_encode_matches_cli_scheme() -> None:
    assert encode_project_path(r"C:\Users\alice") == "C--Users-alice"
    assert encode_project_path(r"C:\Users\alice\.claude\jobs\4c45f474\tmp") == "C--Users-alice--claude-jobs-4c45f474-tmp"
    assert encode_project_path(r"C:\Projects\Лабы\demo") == "C--Projects------demo"


def test_dfs_decode_restores_dots_and_cyrillic(tmp_path: Path) -> None:
    target = tmp_path / "Projects" / "Лабы" / "demo lab1" / ".claude"
    target.mkdir(parents=True)
    (tmp_path / "Projects" / "other").mkdir()
    rest = encode_project_path(str(target.relative_to(tmp_path)))
    assert _dfs_decode(tmp_path, rest) == target


def test_clean_prompt_classification() -> None:
    assert clean_prompt("  обычный запрос ") == ("обычный запрос", None)
    assert clean_prompt("<command-name>/model</command-name><command-args>opus</command-args>") == ("/model opus", None)
    assert clean_prompt("<bash-input>git status</bash-input>") == ("! git status", "git status")
    assert clean_prompt("<local-command-stdout>x</local-command-stdout>") == (None, None)
    assert clean_prompt("<system-reminder>x</system-reminder>") == (None, None)
    assert clean_prompt("[Request interrupted by user]") == (None, None)


def _make_project(tmp_path: Path) -> tuple[SessionInspector, Path, str]:
    projects = tmp_path / "projects"
    cwd = str(tmp_path / "work" / "proj")
    session_file = projects / encode_project_path(cwd) / f"{SESSION_ID}.jsonl"
    write_jsonl(session_file, sample_records(cwd))
    (session_file.parent / SESSION_ID / "subagents").mkdir(parents=True)
    write_jsonl(session_file.parent / SESSION_ID / "subagents" / "agent-1.jsonl", [{"type": "user"}])
    (session_file.parent / "memory").mkdir()
    return SessionInspector(projects), session_file, cwd


def test_listing_and_full_report(tmp_path: Path) -> None:
    inspector, session_file, cwd = _make_project(tmp_path)

    async def scenario() -> None:
        projects = await inspector.list_projects()
        assert len(projects) == 1
        assert projects[0].display_path == cwd and projects[0].exact and projects[0].session_count == 1
        sessions = await inspector.list_sessions(projects[0].key)
        meta = sessions[0]
        assert meta.title == "fix-tests" and meta.first_prompt == "Почини тесты в проекте"
        assert meta.last_prompt == "Теперь добавь тест" and meta.cwd == cwd
        assert await inspector.find_session(SESSION_ID) is not None
        assert await inspector.find_session("../../etc") is None

        report = await inspector.build_report(meta)
        assert report.prompts == ["Почини тесты в проекте", "/compact", "Теперь добавь тест"]
        assert list(report.files) == [cwd + "\\src\\app.py", cwd + "\\tests\\test_new.py"]
        assert report.commands == [("Bash", "pytest -q"), ("PowerShell", "Get-ChildItem -Recurse")]
        assert report.final_answer == "Все тесты проходят.\n\nИзменено 2 файла."
        assert report.assistant_messages == 3
        assert report.tool_calls == 4  # duplicate block and sidechain ignored
        assert report.tool_errors == 1 and report.interrupted == 1
        assert report.cost_usd == 1.25 and report.cost_is_final
        assert report.tokens.output == 20 and report.tokens.cache_read == 100
        assert report.git_branch == "main" and report.entrypoint == "cli" and not report.is_headless
        assert report.end_offset == session_file.stat().st_size

        html = render_report_html(report)
        assert len(html) <= 4000
        for tag in ("b", "code", "blockquote", "i"):
            assert len(re.findall(f"<{tag}[ >]", html)) == html.count(f"</{tag}>")
        assert "src\\app.py" in html and "PS&gt; Get-ChildItem" in html
        md = render_report_markdown(report)
        assert "```powershell" in md and "fix-tests" in md

    asyncio.run(scenario())


def test_delta_report_and_partial_last_line(tmp_path: Path) -> None:
    inspector, session_file, cwd = _make_project(tmp_path)

    async def scenario() -> None:
        meta = (await inspector.list_sessions(session_file.parent.name))[0]
        first = await inspector.build_report(meta)
        with open(session_file, "a", encoding="utf-8") as fh:
            fh.write('{"type":"user","cwd":"x","message":{"content":"Новый запрос с ПК"},"timestamp":"2026-10-25T11:00:00Z"}\n')
            fh.write('{"type":"assistant","message":{"id":"m9","content":[{"type":"text","text":"Ответ"}]}}\n')
            fh.write('{"type":"assistant","message":{"id":"m10","con')  # still being written
        delta = await inspector.build_report_from_path(session_file, meta.project_key, first.end_offset)
        assert delta.is_delta and delta.prompts == ["Новый запрос с ПК"]
        assert delta.final_answer == "Ответ" and delta.assistant_messages == 1
        assert delta.end_offset < session_file.stat().st_size  # partial line left for next time
        assert delta.title == "fix-tests"

    asyncio.run(scenario())


def test_huge_report_still_fits(tmp_path: Path) -> None:
    from src.services.session_parser import SessionReport

    report = SessionReport(session_id=SESSION_ID, path=tmp_path / "x.jsonl", project_key="k", cwd="C:\\p")
    report.prompts = ["<&>" * 3000, "q" * 5000]
    report.final_answer = "<b>" * 4000
    from collections import Counter

    report.files = {f"C:\\p\\dir{i}\\" + "f" * 150 + ".py": Counter({"Edit": 2}) for i in range(80)}
    report.commands = [("Bash", "echo " + "&" * 400)] * 60
    html = render_report_html(report)
    assert len(html) <= 4000
    assert not re.search(r"&(?!(amp|lt|gt);)", html)
