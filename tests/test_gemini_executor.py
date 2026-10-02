from __future__ import annotations

import json
import asyncio
import os
from pathlib import Path
import stat
import textwrap

import pytest

from gemini_research_mcp.config import (
    DEFAULT_REQUIRED_CLI_VERSION,
    ModelPolicy,
    Settings,
    load_settings,
)
from gemini_research_mcp.executor import (
    CommandTimeoutError,
    ExecutionError,
    OptionsValidationError,
    OutputLimitExceededError,
    build_ask_command,
    run_command,
    validate_options,
)


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _settings(binary: Path) -> Settings:
    return Settings(
        work_root=binary.parent,
        principal="owner",
        allowed_principals=("owner",),
        gemini_bin=binary,
        database_path=binary.parent / "state.sqlite3",
        lock_dir=binary.parent / "locks",
        models={
            "auto": ModelPolicy(
                logical_name="auto",
                cli_value="auto",
                family="antigravity",
                min_cli_version=DEFAULT_REQUIRED_CLI_VERSION,
            )
        },
        prompt_limit=20_000,
        continue_burst_limit=50,
        continue_burst_window_seconds=60,
        concurrency=1,
        output_limit=100_000,
        result_limit=100_000,
        required_cli_version=DEFAULT_REQUIRED_CLI_VERSION,
    )


@pytest.mark.parametrize(
    ("selected_variable", "expected"),
    [("AGY_BIN", "agy-primary"), ("GEMINI_BIN", "gemini-compat")],
)
def test_executable_environment_priority(
    tmp_path: Path, selected_variable: str, expected: str
) -> None:
    agy = _write_executable(tmp_path / "agy-primary", "#!/bin/sh\nexit 0\n")
    gemini = _write_executable(tmp_path / "gemini-compat", "#!/bin/sh\nexit 0\n")
    environment = {
        "GEMINI_MCP_WORK_ROOT": str(tmp_path),
        "GEMINI_MCP_PRINCIPAL": "owner",
        "AGY_BIN": str(agy),
        "GEMINI_BIN": str(gemini),
    }
    if selected_variable == "GEMINI_BIN":
        del environment["AGY_BIN"]

    settings = load_settings(environment)

    assert settings.gemini_bin.name == expected


def test_minimum_cli_version_defaults_to_antigravity_1_2_14() -> None:
    assert DEFAULT_REQUIRED_CLI_VERSION == (1, 2, 14)


def test_fixed_argv_web_auto_and_compatibility_max_turns() -> None:
    binary = Path("/usr/local/bin/agy")
    command = build_ask_command(
        _settings(binary),
        resume_session_id=None,
        model_cli_value="auto",
        use_web=True,
        max_turns=12,
    )

    assert command == [
        str(binary),
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--disable-slash-commands",
        "--agent",
        "research-web",
    ]
    prohibited = {
        "--skip-trust",
        "--extensions",
        "--allowed-tools",
        "--max-turns",
        "--dangerously-skip-permissions",
    }
    assert not prohibited.intersection(command)


def test_fixed_argv_resume_offline_and_explicit_model() -> None:
    binary = Path("/usr/local/bin/agy")
    command = build_ask_command(
        _settings(binary),
        resume_session_id="01234567-89ab-cdef-0123-456789abcdef",
        model_cli_value="gemini-2.5-pro",
        use_web=False,
        max_turns=20,
    )

    assert command == [
        str(binary),
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--disable-slash-commands",
        "--agent",
        "research-offline",
        "--conversation",
        "01234567-89ab-cdef-0123-456789abcdef",
        "--model",
        "gemini-2.5-pro",
    ]


def test_options_validation_retains_max_turns_compatibility() -> None:
    assert validate_options(
        model="auto", use_web=False, max_turns=7, timeout_seconds=30
    ) == ("auto", False, 7, 30)
    with pytest.raises(OptionsValidationError):
        validate_options(model="auto", use_web=False, max_turns=0, timeout_seconds=30)


def test_prompt_travels_by_stdin_not_argv(tmp_path: Path) -> None:
    binary = _write_executable(
        tmp_path / "agy",
        """
        #!/usr/bin/env python3
        import json
        import sys
        from pathlib import Path
        print(
            json.dumps(
                {
                    "event": "init",
                    "conversation_id": "01234567-89ab-cdef-0123-456789abcdef",
                    "init": {
                        "cwd": str(Path(__file__).parent),
                        "tools": ["search_web", "read_url_content", "ask_permission"],
                        "permission_mode": "request-review",
                    },
                }
            ),
            flush=True,
        )
        first_line = sys.stdin.readline()
        if not first_line:
            raise SystemExit(2)
        Path(__file__).with_name("invocation.json").write_text(
            json.dumps({"argv": sys.argv, "stdin": first_line})
        )
        print(json.dumps({"result": "ok", "session_id": None}))
        """,
    )
    command = build_ask_command(
        _settings(binary),
        resume_session_id=None,
        model_cli_value="auto",
        use_web=True,
        max_turns=20,
    )

    result = asyncio.run(
        run_command(
            command,
            cwd=tmp_path,
            timeout_seconds=5,
            output_limit=10_000,
            prompt="untrusted prompt with spaces and $shell",
        )
    )

    assert json.loads(result.stdout.splitlines()[-1])["result"] == "ok"
    invocation = json.loads((tmp_path / "invocation.json").read_text())
    assert json.loads(invocation["stdin"]) == {
        "event": "user",
        "message": {"content": "untrusted prompt with spaces and $shell"},
    }
    assert invocation["argv"] == command


def test_full_global_tool_catalog_is_accepted_and_receives_prompt(
    tmp_path: Path,
) -> None:
    tools = [
        "search_web",
        "read_url_content",
        "run_command",
        "browser",
        "invoke_subagent",
        *(f"global_tool_{index}" for index in range(53)),
    ]
    assert len(tools) == 58
    binary = _write_executable(
        tmp_path / "agy-global-tools",
        """
        #!/usr/bin/env python3
        import json
        import sys
        from pathlib import Path
        print(
            json.dumps(
                {
                    "event": "init",
                    "conversation_id": "01234567-89ab-cdef-0123-456789abcdef",
                    "init": {"tools": __TOOLS__},
                }
            ),
            flush=True,
        )
        first_line = sys.stdin.readline()
        if not first_line:
            raise SystemExit(2)
        Path(__file__).with_name("stdin.json").write_text(first_line)
        print(json.dumps({"result": "global tools", "session_id": None}))
        """,
    )
    command = build_ask_command(
        _settings(binary),
        resume_session_id=None,
        model_cli_value="auto",
        use_web=True,
        max_turns=20,
    )
    source = binary.read_text(encoding="utf-8").replace("__TOOLS__", repr(tools))
    binary.write_text(source, encoding="utf-8")

    result = asyncio.run(
        run_command(
            command,
            cwd=tmp_path,
            timeout_seconds=5,
            output_limit=10_000,
            prompt="global catalog prompt",
        )
    )

    assert json.loads(result.stdout.splitlines()[-1])["result"] == "global tools"
    assert json.loads((tmp_path / "stdin.json").read_text()) == {
        "event": "user",
        "message": {"content": "global catalog prompt"},
    }


def test_legacy_system_init_and_web_without_ask_permission(tmp_path: Path) -> None:
    binary = _write_executable(
        tmp_path / "agy-legacy",
        """
        #!/usr/bin/env python3
        import json
        import sys
        from pathlib import Path
        print(
            json.dumps(
                {
                    "event": "system",
                    "type": "init",
                    "tools": ["search_web", "read_url_content"],
                }
            ),
            flush=True,
        )
        first_line = sys.stdin.readline()
        if not first_line:
            raise SystemExit(2)
        Path(__file__).with_name("legacy-stdin.json").write_text(first_line)
        print(json.dumps({"result": "legacy", "session_id": None}))
        """,
    )
    command = build_ask_command(
        _settings(binary),
        resume_session_id=None,
        model_cli_value="auto",
        use_web=True,
        max_turns=20,
    )

    result = asyncio.run(
        run_command(
            command,
            cwd=tmp_path,
            timeout_seconds=5,
            output_limit=10_000,
            prompt="legacy prompt",
        )
    )

    assert json.loads(result.stdout.splitlines()[-1])["result"] == "legacy"
    assert json.loads((tmp_path / "legacy-stdin.json").read_text()) == {
        "event": "user",
        "message": {"content": "legacy prompt"},
    }


def test_missing_init_tool_list_blocks_prompt(tmp_path: Path) -> None:
    binary = _write_executable(
        tmp_path / "agy-missing-tools",
        """
        #!/usr/bin/env python3
        import json
        import sys
        import time
        from pathlib import Path
        print(
            json.dumps(
                {
                    "event": "init",
                    "conversation_id": "01234567-89ab-cdef-0123-456789abcdef",
                    "init": {},
                }
            ),
            flush=True,
        )
        value = sys.stdin.readline()
        if value:
            Path(__file__).with_name("missing-tools-leak.txt").write_text(value)
        time.sleep(2)
        """,
    )
    command = build_ask_command(
        _settings(binary),
        resume_session_id=None,
        model_cli_value="auto",
        use_web=True,
        max_turns=20,
    )

    with pytest.raises(ExecutionError, match="invalid tool initialization"):
        asyncio.run(
            run_command(
                command,
                cwd=tmp_path,
                timeout_seconds=5,
                output_limit=10_000,
                prompt="secret prompt",
            )
        )

    assert not (tmp_path / "missing-tools-leak.txt").exists()


def test_unknown_control_command_receives_no_stdin(tmp_path: Path) -> None:
    binary = _write_executable(
        tmp_path / "agy-unknown-command",
        """
        #!/usr/bin/env python3
        import sys
        from pathlib import Path
        value = sys.stdin.read()
        Path(__file__).with_name("unknown-stdin.txt").write_text(value)
        """,
    )

    asyncio.run(
        run_command(
            [str(binary), "run_command"],
            cwd=tmp_path,
            timeout_seconds=5,
            output_limit=10_000,
            prompt="untrusted stdin",
        )
    )

    assert (tmp_path / "unknown-stdin.txt").read_text() == ""


def test_process_timeout_is_enforced(tmp_path: Path) -> None:
    binary = _write_executable(
        tmp_path / "agy-timeout",
        """
        #!/usr/bin/env python3
        import time
        import json
        print(
            json.dumps(
                {
                    "event": "init",
                    "conversation_id": "01234567-89ab-cdef-0123-456789abcdef",
                    "init": {"tools": ["search_web", "read_url_content"]},
                }
            ),
            flush=True,
        )
        time.sleep(5)
        """,
    )

    with pytest.raises(CommandTimeoutError):
        asyncio.run(
            run_command(
                [str(binary)],
                cwd=tmp_path,
                timeout_seconds=1,
                output_limit=1_000,
                prompt="prompt",
            )
        )


def test_output_bound_is_enforced(tmp_path: Path) -> None:
    binary = _write_executable(
        tmp_path / "agy-output",
        """
        #!/usr/bin/env python3
        import json
        print(
            json.dumps(
                {
                    "event": "init",
                    "conversation_id": "01234567-89ab-cdef-0123-456789abcdef",
                    "init": {"tools": ["search_web", "read_url_content"]},
                }
            ),
            flush=True,
        )
        print("x" * 100)
        """,
    )

    with pytest.raises(OutputLimitExceededError):
        asyncio.run(
            run_command(
                [str(binary)],
                cwd=tmp_path,
                timeout_seconds=5,
                output_limit=10,
                prompt="prompt",
            )
        )


def test_plain_stdin_is_retained_for_controlled_commands(tmp_path: Path) -> None:
    binary = _write_executable(
        tmp_path / "agy-control",
        """
        #!/usr/bin/env python3
        import json
        import sys
        from pathlib import Path
        value = sys.stdin.read()
        Path(__file__).with_name("stdin.txt").write_text(value)
        print(json.dumps({"status": "ok"}))
        """,
    )

    result = asyncio.run(
        run_command(
            [str(binary), "auth", "status"],
            cwd=tmp_path,
            timeout_seconds=5,
            output_limit=10_000,
            prompt="controlled stdin",
        )
    )

    assert json.loads(result.stdout)["status"] == "ok"
    assert (tmp_path / "stdin.txt").read_text() == "controlled stdin"


def test_malformed_init_tool_item_blocks_prompt(tmp_path: Path) -> None:
    binary = _write_executable(
        tmp_path / "agy-invalid-init",
        """
        #!/usr/bin/env python3
        import json
        import sys
        import time
        from pathlib import Path
        print(
            json.dumps(
                {
                    "event": "init",
                    "conversation_id": "01234567-89ab-cdef-0123-456789abcdef",
                    "init": {"tools": ["search_web", "read_url_content", 7]},
                }
            ),
            flush=True,
        )
        value = sys.stdin.readline()
        if value:
            Path(__file__).with_name("prompt-leak.txt").write_text(value)
        time.sleep(2)
        """,
    )
    command = build_ask_command(
        _settings(binary),
        resume_session_id=None,
        model_cli_value="auto",
        use_web=True,
        max_turns=20,
    )

    with pytest.raises(ExecutionError, match="invalid tool initialization"):
        asyncio.run(
            run_command(
                command,
                cwd=tmp_path,
                timeout_seconds=5,
                output_limit=10_000,
                prompt="secret prompt",
            )
        )

    assert not (tmp_path / "prompt-leak.txt").exists()


def test_web_init_missing_required_tool_blocks_prompt(tmp_path: Path) -> None:
    binary = _write_executable(
        tmp_path / "agy-missing-required-tool",
        """
        #!/usr/bin/env python3
        import json
        import sys
        import time
        from pathlib import Path
        print(
            json.dumps(
                {
                    "event": "init",
                    "conversation_id": "01234567-89ab-cdef-0123-456789abcdef",
                    "init": {"tools": ["search_web", "run_command"]},
                }
            ),
            flush=True,
        )
        value = sys.stdin.readline()
        if value:
            Path(__file__).with_name("required-tool-leak.txt").write_text(value)
        time.sleep(2)
        """,
    )
    command = build_ask_command(
        _settings(binary),
        resume_session_id=None,
        model_cli_value="auto",
        use_web=True,
        max_turns=20,
    )

    with pytest.raises(ExecutionError, match="invalid tool initialization"):
        asyncio.run(
            run_command(
                command,
                cwd=tmp_path,
                timeout_seconds=5,
                output_limit=10_000,
                prompt="secret prompt",
            )
        )

    assert not (tmp_path / "required-tool-leak.txt").exists()


def test_invalid_init_event_blocks_prompt(tmp_path: Path) -> None:
    binary = _write_executable(
        tmp_path / "agy-invalid-init-event",
        """
        #!/usr/bin/env python3
        import sys
        import time
        from pathlib import Path
        print("{broken", flush=True)
        value = sys.stdin.readline()
        if value:
            Path(__file__).with_name("invalid-init-leak.txt").write_text(value)
        time.sleep(2)
        """,
    )
    command = build_ask_command(
        _settings(binary),
        resume_session_id=None,
        model_cli_value="auto",
        use_web=True,
        max_turns=20,
    )

    with pytest.raises(ExecutionError, match="invalid tool initialization"):
        asyncio.run(
            run_command(
                command,
                cwd=tmp_path,
                timeout_seconds=5,
                output_limit=10_000,
                prompt="secret prompt",
            )
        )

    assert not (tmp_path / "invalid-init-leak.txt").exists()


def test_offline_init_accepts_empty_tool_catalog_and_sends_prompt(
    tmp_path: Path,
) -> None:
    binary = _write_executable(
        tmp_path / "agy-offline",
        """
        #!/usr/bin/env python3
        import json
        import sys
        from pathlib import Path
        print(
            json.dumps(
                {
                    "event": "init",
                    "conversation_id": "01234567-89ab-cdef-0123-456789abcdef",
                    "init": {"tools": []},
                }
            ),
            flush=True,
        )
        value = sys.stdin.readline()
        Path(__file__).with_name("stdin.json").write_text(value)
        print(json.dumps({"result": "offline", "session_id": None}))
        _ = sys.stdin.read()
        """,
    )
    command = build_ask_command(
        _settings(binary),
        resume_session_id=None,
        model_cli_value="auto",
        use_web=False,
        max_turns=20,
    )

    result = asyncio.run(
        run_command(
            command,
            cwd=tmp_path,
            timeout_seconds=5,
            output_limit=10_000,
            prompt="offline question",
        )
    )

    assert json.loads(result.stdout.splitlines()[-1])["result"] == "offline"
    assert json.loads((tmp_path / "stdin.json").read_text()) == {
        "event": "user",
        "message": {"content": "offline question"},
    }


def test_cancellation_cleans_stream_process_group(tmp_path: Path) -> None:
    binary = _write_executable(
        tmp_path / "agy-cancel",
        """
        #!/usr/bin/env python3
        import json
        import os
        import sys
        import time
        from pathlib import Path
        Path(__file__).with_name("pid.txt").write_text(str(os.getpid()))
        print(
            json.dumps(
                {
                    "event": "init",
                    "conversation_id": "01234567-89ab-cdef-0123-456789abcdef",
                    "init": {"tools": ["search_web", "read_url_content"]},
                }
            ),
            flush=True,
        )
        time.sleep(5)
        """,
    )
    command = build_ask_command(
        _settings(binary),
        resume_session_id=None,
        model_cli_value="auto",
        use_web=True,
        max_turns=20,
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            run_command(
                command,
                cwd=tmp_path,
                timeout_seconds=5,
                output_limit=10_000,
                prompt="prompt",
            )
        )
        await asyncio.sleep(0.1)
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scenario())

    pid = int((tmp_path / "pid.txt").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
