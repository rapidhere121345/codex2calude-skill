from pathlib import Path
import sqlite3

import pytest

from claude_research_mcp.persistence import (
    finish_call,
    initialize,
    recorded_token_usage,
    reserve_call,
)
from gemini_research_mcp.persistence import (
    finish_call as gemini_finish_call,
    initialize as gemini_initialize,
    recorded_token_usage as gemini_recorded_token_usage,
    reserve_call as gemini_reserve_call,
)


TOKEN_COLUMNS = {
    "input_tokens",
    "output_tokens",
    "thinking_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "total_tokens",
    "token_usage_source",
    "reported_models_json",
}


def audit_columns(database: Path) -> set[str]:
    with sqlite3.connect(database) as connection:
        return {
            row[1]
            for row in connection.execute("PRAGMA table_info(audit_log)")
        }


@pytest.mark.parametrize(
    ("initialize", "reserve", "finish", "source"),
    (
        (initialize, reserve_call, finish_call, "claude_cli_result"),
        (
            gemini_initialize,
            gemini_reserve_call,
            gemini_finish_call,
            "gemini_cli_result",
        ),
    ),
)
def test_token_columns_are_nullable_and_record_real_cli_counts(
    tmp_path,
    initialize,
    reserve,
    finish,
    source,
):
    database = tmp_path / "mcp.sqlite3"
    initialize(database)
    initialize(database)
    assert TOKEN_COLUMNS <= audit_columns(database)

    audit_id = reserve_call(
        database,
        principal="owner",
        tool="claude_once" if source.startswith("claude") else "gemini_once",
        conversation_id=None,
        model="opus",
        request_id=None,
    )
    finish(
        database,
        audit_id,
        status="success",
        duration_ms=12,
        token_usage=(
            {
                "input_tokens": 11,
                "output_tokens": 5,
                "cache_creation_input_tokens": 3,
                "cache_read_input_tokens": 2,
            },
            source,
        ),
        reported_models=["model-a", "x" * 201, None, "model-b"],
    )
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT input_tokens, output_tokens, cache_creation_tokens, "
            "cache_read_tokens, token_usage_source, reported_models_json "
            "FROM audit_log"
        ).fetchone()
    assert dict(row) == {
        "input_tokens": 11,
        "output_tokens": 5,
        "cache_creation_tokens": 3,
        "cache_read_tokens": 2,
        "token_usage_source": source,
        "reported_models_json": '["model-a","model-b"]',
    }


def test_absent_token_usage_is_not_fabricated(tmp_path):
    database = tmp_path / "mcp.sqlite3"
    initialize(database)
    assert recorded_token_usage([{"model": "opus"}], "claude_cli_result") is None
    assert recorded_token_usage([{"input_tokens": 7}], "invented_source") is None
    assert gemini_recorded_token_usage([], "gemini_cli_result") is None

    audit_id = reserve_call(
        database,
        principal="owner",
        tool="claude_once",
        conversation_id=None,
        model="opus",
        request_id=None,
    )
    finish_call(database, audit_id, status="success", duration_ms=1)
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT input_tokens, output_tokens, token_usage_source "
            "FROM audit_log"
        ).fetchone()
    assert row == (None, None, None)


@pytest.mark.parametrize('normalize,source', [(recorded_token_usage,'claude_cli_result'),(gemini_recorded_token_usage,'gemini_cli_result')])
def test_partial_token_fields_stay_unknown_and_aliases_are_not_added(normalize,source):
    result=normalize({'input_tokens':7,'cache_read_tokens':2,'cache_read_input_tokens':2},source)
    assert result==({'input_tokens':7,'cache_read_tokens':2},source)
    assert normalize([{'input_tokens':7},{'output_tokens':3}],source) is None
    assert normalize({'input_tokens':2**64},source) is None
