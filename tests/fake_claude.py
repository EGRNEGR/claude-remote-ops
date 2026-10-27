"""Stand-in for the Claude CLI in tests: emits stream-json events in the 2.1.x format.

Prompt comes from stdin (like `claude -p` without a positional prompt).
  * prompt containing "SLEEP": spawns a long-living child (pid written to FAKE_CLAUDE_PIDFILE) and hangs
  * otherwise: init -> tool_use Edit -> tool_result -> text -> result (echoes the prompt and argv)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time


def emit(obj: dict) -> None:
    sys.stdout.buffer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def main() -> int:
    prompt = sys.stdin.buffer.read().decode("utf-8")
    sid = "11111111-2222-4333-8444-555555555555"
    if "--resume" in sys.argv:
        sid = sys.argv[sys.argv.index("--resume") + 1]
    emit({"type": "system", "subtype": "init", "session_id": sid, "cwd": os.getcwd(), "model": "fake-model",
          "permissionMode": "default", "tools": ["Bash", "Edit"]})
    if "SLEEP" in prompt:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        pidfile = os.environ.get("FAKE_CLAUDE_PIDFILE")
        if pidfile:
            with open(pidfile, "w", encoding="utf-8") as fh:
                fh.write(str(child.pid))
        emit({"type": "assistant", "session_id": sid, "parent_tool_use_id": None,
              "message": {"id": "msg_1", "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                       "input": {"command": "Start-Sleep 120"}}]}})
        time.sleep(120)
        return 0
    emit({"type": "assistant", "session_id": sid, "parent_tool_use_id": None,
          "message": {"id": "msg_1", "content": [{"type": "tool_use", "id": "t1", "name": "Edit",
                                                   "input": {"file_path": "src\\main.py", "old_string": "a",
                                                             "new_string": "b"}}]}})
    emit({"type": "user", "session_id": sid,
          "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}})
    emit({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}, "session_id": sid})
    emit({"type": "assistant", "session_id": sid, "parent_tool_use_id": None,
          "message": {"id": "msg_2", "content": [{"type": "text", "text": "Готово ✅"}]}})
    skip = "--dangerously-skip-permissions" in sys.argv
    if "BUDGET" in prompt and "--max-budget-usd" in sys.argv:
        emit({"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "num_turns": 9,
              "total_cost_usd": 2.01, "session_id": sid, "permission_denials": [], "result": "",
              "terminal_reason": "max_budget"})
        return 0
    if "DENY" in prompt and not skip:
        emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "total_cost_usd": 0.3,
              "session_id": sid, "terminal_reason": "completed", "result": "Не хватило разрешений",
              "permission_denials": [
                  {"tool_name": "Bash", "tool_use_id": "t7", "tool_input": {"command": "npx playwright"}},
                  {"tool_name": "mcp__playwright__browser_run_code_unsafe", "tool_use_id": "t8", "tool_input": {}},
              ]})
        return 0
    emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "duration_ms": 10,
          "total_cost_usd": 0.0123, "session_id": sid, "permission_denials": [], "terminal_reason": "completed",
          "result": f"echo: {prompt} | argv: {' '.join(sys.argv[1:])}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
