from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import pytest

from src.keyboards.inline import FileCb, ModeCb, ProjCb, RunCb, SessCb, UpCb
from src.services.file_manager import FileManager, PathAccessError, SafePathManager, decode_text
from src.utils.registry import TokenRegistry
from src.utils.text import chunk_text, esc, esc_fit, plural, truncate
from src.utils.winpath import IS_WINDOWS, is_within, long_path, sanitize_filename, unique_path


def test_esc_fit_never_cuts_entities() -> None:
    text = "<&>" * 500
    for limit in (5, 17, 100, 999):
        out = esc_fit(text, limit)
        assert len(out) <= limit
        assert not re.search(r"&(?!(amp|lt|gt);)", out)


def test_chunk_text_respects_limit() -> None:
    text = "\n".join(f"строка {i} " + "x" * 50 for i in range(500))
    chunks = chunk_text(text, 4000)
    assert all(len(c) <= 4000 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_misc_text() -> None:
    assert esc("<b>&") == "&lt;b&gt;&amp;"
    assert truncate("abcdef", 4) == "abc…"
    assert plural(1, "файл", "файла", "файлов") == "файл"
    assert plural(3, "файл", "файла", "файлов") == "файла"
    assert plural(11, "файл", "файла", "файлов") == "файлов"


def test_sanitize_filename_windows_rules(tmp_path: Path) -> None:
    assert sanitize_filename('a<b>:"c|?*.txt') == "a_b___c___.txt"
    assert sanitize_filename("..\\..\\evil.ps1") == "evil.ps1"
    assert sanitize_filename("CON.txt") == "_CON.txt"
    assert sanitize_filename("name. . ") == "name"
    (tmp_path / "f.txt").write_text("1")
    assert unique_path(tmp_path, "f.txt").name == "f (1).txt"


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows path semantics")
def test_is_within_case_insensitive() -> None:
    assert is_within(Path(r"C:\PROJECTS\Proj\a.py"), Path(r"c:\projects"))
    assert not is_within(Path(r"C:\Projects2\a.py"), Path(r"C:\Projects"))
    assert long_path("C:\\" + "a" * 300).startswith("\\\\?\\")


def test_safe_path_manager_blocks_traversal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "proj").mkdir(parents=True)
    (root / "proj" / "a.txt").write_text("ok", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    safe = SafePathManager([root.resolve()])
    assert safe.resolve_file(root / "proj" / "a.txt").name == "a.txt"
    assert safe.resolve_file("proj/a.txt", base=root).name == "a.txt"
    with pytest.raises(PathAccessError):
        safe.resolve(root / "proj" / ".." / ".." / "secret.txt")
    with pytest.raises(PathAccessError):
        safe.resolve("..\\secret.txt", base=root)
    with pytest.raises(PathAccessError):
        safe.resolve("relative.txt")
    with pytest.raises(PathAccessError):
        safe.resolve_dir(root / "missing")
    if IS_WINDOWS:
        upper = Path(str(root / "proj" / "a.txt").upper())
        assert safe.resolve_file(upper).name.lower() == "a.txt"
    assert not safe.is_allowed(tmp_path)
    assert safe.is_allowed(root / "proj" / "new_file.txt")


@pytest.mark.skipif(not IS_WINDOWS, reason="junctions are Windows only")
def test_junction_escape_blocked(tmp_path: Path) -> None:
    import subprocess

    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "s.txt").write_text("x")
    link = root / "jump"
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
    if proc.returncode != 0:
        pytest.skip("mklink /J unavailable")
    safe = SafePathManager([root.resolve()])
    with pytest.raises(PathAccessError):
        safe.resolve_file(link / "s.txt")


def test_file_manager_listing_search_preview_zip(tmp_path: Path) -> None:
    root = tmp_path / "root"
    for d in ("src", ".git", "node_modules", "__pycache__", "docs"):
        (root / d).mkdir(parents=True)
    (root / "src" / "main.py").write_text("print('привет')\n" * 80, encoding="utf-8")
    (root / "src" / "util_main.py").write_text("x = 1\n", encoding="utf-8")
    (root / "node_modules" / "main.py").write_text("ignored", encoding="utf-8")
    (root / "docs" / "legacy.txt").write_bytes("Отчет в cp1251".encode("cp1251"))
    (root / "docs" / "bin.dat").write_bytes(b"\x00\x01\x02" * 100)
    safe = SafePathManager([root.resolve()])
    fm = FileManager(safe, frozenset({".git", "node_modules", "__pycache__"}), show_hidden=False)

    async def scenario() -> None:
        listing = await fm.list_dir(root)
        names = [e.name for e in listing.entries]
        assert names == ["docs", "src"]
        assert listing.skipped == 3
        found = await fm.search([root], "main.py", 10)
        assert [p.name for p in found.matches] == ["main.py", "util_main.py"]
        wild = await fm.search([root], "*.TXT", 10)
        assert [p.name for p in wild.matches] == ["legacy.txt"]
        prev = await fm.preview(root / "src" / "main.py", 50)
        assert prev.lines_shown == 50 and prev.truncated and "привет" in prev.text
        legacy = await fm.preview(root / "docs" / "legacy.txt", 50)
        assert legacy.text == "Отчет в cp1251" and legacy.encoding == "cp1251"
        assert (await fm.preview(root / "docs" / "bin.dat", 50)).is_binary
        data, included = await fm.zip_files(
            [root / "src" / "main.py", root / "src" / "util_main.py", tmp_path / "outside.txt"], root, 10**7
        )
        assert len(included) == 2 and data[:2] == b"PK"

    asyncio.run(scenario())


def test_decode_text_utf8_cut_at_window_end() -> None:
    data = "абв".encode("utf-8")[:-1]
    text, enc = decode_text(data)
    assert enc == "utf-8" and text == "аб"


def test_callback_data_fits_64_bytes() -> None:
    sid = "6a17f423-802d-4103-b021-8562aecdb7d7"
    token = TokenRegistry.make_token("C:\\" + "x" * 500)
    packed = [
        SessCb(action="resume", sid=sid).pack(),
        SessCb(action="files", sid=sid).pack(),
        FileCb(action="roots").pack(),
        FileCb(action="cwd", token=token, page=9999).pack(),
        ProjCb(action="open", token=token, page=9999).pack(),
        RunCb(action="files", run_id="deadbeef").pack(),
        ModeCb(action="continue").pack(),
        UpCb(action="save_run").pack(),
    ]
    for item in packed:
        assert len(item.encode("utf-8")) <= 64, item
    assert SessCb.unpack(packed[0]).sid == sid
    assert len(token) == 12


def test_settings_env_file_windows_paths(tmp_path: Path) -> None:
    from src.config import Settings

    a = tmp_path / "Проекты A"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    env = tmp_path / ".env"
    env.write_text(
        "BOT_TOKEN=1:abc\nADMIN_USER_ID=777\n"
        f"ALLOWED_DIRS={a};{b}\n"
        "CLAUDE_ALLOWED_TOOLS=Read,Bash(git *),Bash(pwsh *)\n"
        "IGNORED_DIRS=\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env)  # type: ignore[call-arg]
    assert settings.ADMIN_USER_ID == 777
    assert [p.name for p in settings.ALLOWED_DIRS] == ["Проекты A", "b"]
    assert settings.CLAUDE_ALLOWED_TOOLS == "Read,Bash(git *),Bash(pwsh *)"
    assert ".git" in settings.ignored_dirs_lower
    with pytest.raises(ValueError):
        Settings(_env_file=None, BOT_TOKEN="1:a", ADMIN_USER_ID=1, ALLOWED_DIRS=str(tmp_path / "nope"))  # type: ignore[call-arg]
    # New defaults: full trust, no limits, 1 hour timeout, wide allowedTools incl. all MCP servers.
    defaults = Settings(_env_file=None, BOT_TOKEN="1:a", ADMIN_USER_ID=1, ALLOWED_DIRS=str(b))  # type: ignore[call-arg]
    assert defaults.SKIP_PERMISSIONS_DEFAULT is True and defaults.CLAUDE_TIMEOUT_SEC == 3600
    assert defaults.CLAUDE_MAX_TURNS is None and defaults.CLAUDE_MAX_BUDGET_USD is None
    assert defaults.CLAUDE_ALLOWED_TOOLS.endswith(",mcp__*") and ",Bash," in defaults.CLAUDE_ALLOWED_TOOLS


@pytest.mark.parametrize(
    ("turns", "budget", "expected"),
    [("0", "0", (None, None)), ("", "", (None, None)), ("unlimited", "none", (None, None)),
     ("100", "15.5", (100, 15.5)), (0, 0.0, (None, None)), (80, 15, (80, 15.0))],
)
def test_optional_limits(tmp_path: Path, turns: object, budget: object, expected: tuple) -> None:
    from src.config import Settings

    env = tmp_path / ".env"
    env.write_text(
        f"BOT_TOKEN=1:abc\nADMIN_USER_ID=1\nALLOWED_DIRS={tmp_path}\n"
        f"CLAUDE_MAX_TURNS={turns}\nCLAUDE_MAX_BUDGET_USD={budget}\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env)  # type: ignore[call-arg]
    assert (settings.CLAUDE_MAX_TURNS, settings.CLAUDE_MAX_BUDGET_USD) == expected


def test_negative_limit_rejected(tmp_path: Path) -> None:
    from src.config import Settings

    with pytest.raises(ValueError):
        Settings(_env_file=None, BOT_TOKEN="1:a", ADMIN_USER_ID=1, ALLOWED_DIRS=str(tmp_path),  # type: ignore[call-arg]
                 CLAUDE_MAX_TURNS="-5")


def test_state_store_roundtrip(tmp_path: Path) -> None:
    from src.services.state import BotState, RunMode, StateStore

    async def scenario() -> None:
        store = StateStore(tmp_path / "data" / "state.json", BotState())
        await store.load()
        await store.update(active_cwd="C:\\Проект", run_mode=RunMode.RESUME, skip_permissions=True)
        again = StateStore(tmp_path / "data" / "state.json", BotState())
        loaded = await again.load()
        assert loaded.active_cwd == "C:\\Проект"
        assert loaded.run_mode is RunMode.RESUME and loaded.skip_permissions

    asyncio.run(scenario())


def test_state_migration_v1_applies_full_trust_default(tmp_path: Path) -> None:
    import json

    from src.services.state import (
        PROFILE_LIMITS,
        STATE_SCHEMA_VERSION,
        BgSessionPolicy,
        BotState,
        LimitProfile,
        RunLimits,
        StateStore,
        limit_profile_title,
        limits_for,
    )

    path = tmp_path / "state.json"
    path.write_text(json.dumps({"active_session_id": "abc", "run_mode": "resume", "skip_permissions": False}),
                    encoding="utf-8")

    async def scenario() -> None:
        migrated = await StateStore(path, BotState(skip_permissions=True)).load()
        assert migrated.skip_permissions and migrated.limit_profile is LimitProfile.AUTO
        assert migrated.schema_version == STATE_SCHEMA_VERSION == 4 and migrated.active_session_id == "abc"
        assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 4
        assert migrated.bg_session_policy is BgSessionPolicy.ASK
        # v2 -> v3: an explicit v2 choice is kept, the new policy comes from BG_SESSION_POLICY.
        path.write_text(json.dumps({"skip_permissions": False, "schema_version": 2}), encoding="utf-8")
        v2 = await StateStore(path, BotState(bg_session_policy=BgSessionPolicy.FORK)).load()
        assert not v2.skip_permissions and v2.bg_session_policy is BgSessionPolicy.FORK
        assert json.loads(path.read_text(encoding="utf-8"))["bg_session_policy"] == "fork"
        path.write_text(json.dumps({"active_session_id": "abc", "run_mode": "resume", "skip_permissions": False}),
                        encoding="utf-8")
        await StateStore(path, BotState(skip_permissions=True)).load()
        # Once on v2, an explicit "restricted" choice made in /mode is respected.
        store = StateStore(path, BotState(skip_permissions=True))
        await store.load()
        await store.update(skip_permissions=False)
        assert not (await StateStore(path, BotState(skip_permissions=True)).load()).skip_permissions

    asyncio.run(scenario())
    env = RunLimits(None, None)
    assert limits_for(LimitProfile.AUTO, env).unlimited
    assert limits_for(LimitProfile.ECONOMY, env) == RunLimits(15, 2.0) == PROFILE_LIMITS[LimitProfile.ECONOMY]
    assert limits_for(LimitProfile.PRO, env) == RunLimits(80, 15.0)
    assert limit_profile_title(LimitProfile.AUTO, env) == "♾ Без лимитов (Auto)"
    assert limit_profile_title(LimitProfile.ECONOMY, env) == "💵 Эконом ($2 / 15 ходов)"
    assert limit_profile_title(LimitProfile.PRO, env) == "🚀 Pro ($15 / 80 ходов)"
    assert limit_profile_title(LimitProfile.AUTO, RunLimits(30, None)) == "♾ Auto (.env: $∞ / 30 ходов)"
    assert not os.path.exists(tmp_path / "data" / "state.json.tmp")
