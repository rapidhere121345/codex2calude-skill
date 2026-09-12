"""Configuration and validation for the restricted Claude MCP server."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re


DEFAULT_MODEL = "opus"
DEFAULT_PRINCIPALS = ("owner", "partner")
DEFAULT_REQUIRED_CLI_VERSION = (2, 1, 236)
NUL_BYTE = "␀"  # Logical NUL marker; validate before ever creating a real NUL.
_PRINCIPAL_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_CLI_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_FAMILY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CONVERSATION_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_CLAUDE_SESSION_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
_VERSION_RE = re.compile(r"v?([0-9]+[.][0-9]+(?:[.][0-9]+)?)")


class ConfigurationError(ValueError):
    """Invalid server configuration or untrusted request input."""


@dataclass(frozen=True)
class ModelPolicy:
    logical_name: str
    cli_value: str
    family: str
    min_cli_version: tuple[int, ...]


@dataclass(frozen=True)
class Settings:
    work_root: Path
    principal: str
    allowed_principals: tuple[str, ...]
    claude_bin: Path
    database_path: Path
    lock_dir: Path
    models: dict[str, ModelPolicy]
    prompt_limit: int
    rate_limit: int
    concurrency: int
    output_limit: int
    result_limit: int
    required_cli_version: tuple[int, ...]

    @property
    def principal_work_dir(self) -> Path:
        return self.work_root / "principals" / self.principal

    def model_policy(self, logical_name: str) -> ModelPolicy:
        try:
            return self.models[logical_name]
        except KeyError as exc:
            allowed = ", ".join(sorted(self.models)) or "(none)"
            raise ConfigurationError(
                f"model is unavailable; allowed models: {allowed}"
            ) from exc


def _bounded_int(
    raw: str | None, name: str, default: int, minimum: int, maximum: int
) -> int:
    if raw in (None, ""):
        return default
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise ConfigurationError(f"invalid {name}") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"invalid {name}")
    return value


def parse_version(value: str) -> tuple[int, ...] | None:
    match = _VERSION_RE.search(value)
    if match is None:
        return None
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except ValueError:
        return None


def _required_version(value: object) -> tuple[int, ...]:
    if value in (None, ""):
        return DEFAULT_REQUIRED_CLI_VERSION
    if not isinstance(value, str):
        raise ConfigurationError("invalid model minimum CLI version")
    parsed = parse_version(value)
    if parsed is None:
        raise ConfigurationError("invalid model minimum CLI version")
    return parsed


def _model_policy(logical_name: object, raw: object) -> ModelPolicy:
    if not isinstance(logical_name, str) or not _MODEL_RE.fullmatch(logical_name):
        raise ConfigurationError("invalid CLAUDE_MCP_MODELS")
    if isinstance(raw, str):
        raw = {"cli_value": raw}
    if not isinstance(raw, dict):
        raise ConfigurationError("invalid CLAUDE_MCP_MODELS")
    cli_value = raw.get("cli_value", logical_name)
    family = raw.get("family", logical_name)
    min_version = _required_version(raw.get("min_cli_version"))
    if (
        not isinstance(cli_value, str)
        or not isinstance(family, str)
        or not _CLI_VALUE_RE.fullmatch(cli_value)
        or not _FAMILY_RE.fullmatch(family)
    ):
        raise ConfigurationError("invalid CLAUDE_MCP_MODELS")
    return ModelPolicy(logical_name, cli_value, family, min_version)


def _models(raw: str | None) -> dict[str, ModelPolicy]:
    source: object = {"opus": {"cli_value": "opus", "family": "opus"}}
    if raw not in (None, ""):
        try:
            source = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ConfigurationError("invalid CLAUDE_MCP_MODELS") from exc
    if not isinstance(source, dict) or not source:
        raise ConfigurationError("invalid CLAUDE_MCP_MODELS")
    policies = [_model_policy(name, value) for name, value in source.items()]
    return {policy.logical_name: policy for policy in policies}


def _principals(raw: str | None) -> tuple[str, ...]:
    values = tuple(
        item.strip() for item in (raw or "owner,partner").split(",") if item.strip()
    )
    if not values or not all(_PRINCIPAL_RE.fullmatch(value) for value in values):
        raise ConfigurationError("invalid CLAUDE_MCP_PRINCIPALS")
    return values


def _executable(raw: str) -> Path:
    if not raw or "\x00" in raw:
        raise ConfigurationError("invalid CLAUDE_BIN")
    try:
        path = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ConfigurationError("invalid CLAUDE_BIN") from exc
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ConfigurationError("invalid CLAUDE_BIN")
    return path


def load_settings(env: dict[str, str] | None = None) -> Settings:
    source = os.environ if env is None else env
    raw_root = source.get("CLAUDE_MCP_WORK_ROOT", "")
    if not raw_root or "\x00" in raw_root:
        raise ConfigurationError("CLAUDE_MCP_WORK_ROOT is required")
    try:
        work_root = Path(raw_root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ConfigurationError("CLAUDE_MCP_WORK_ROOT is required") from exc
    if not work_root.is_dir() or work_root.is_symlink():
        raise ConfigurationError("CLAUDE_MCP_WORK_ROOT must be a real directory")

    principal = source.get("CLAUDE_MCP_PRINCIPAL", "")
    principals = _principals(source.get("CLAUDE_MCP_PRINCIPALS"))
    if not _PRINCIPAL_RE.fullmatch(principal) or principal not in principals:
        raise ConfigurationError("invalid CLAUDE_MCP_PRINCIPAL")
    models = _models(source.get("CLAUDE_MCP_MODELS"))
    output_limit = _bounded_int(
        source.get("CLAUDE_MCP_OUTPUT_LIMIT"),
        "CLAUDE_MCP_OUTPUT_LIMIT",
        100_000,
        1_000,
        1_000_000,
    )
    required_version = _required_version(source.get("CLAUDE_MCP_REQUIRED_CLI_VERSION"))
    return Settings(
        work_root=work_root,
        principal=principal,
        allowed_principals=principals,
        claude_bin=_executable(source.get("CLAUDE_BIN", "/usr/local/bin/claude")),
        database_path=work_root / "state" / "mcp.sqlite3",
        lock_dir=work_root / "state" / "locks",
        models=models,
        prompt_limit=_bounded_int(
            source.get("CLAUDE_MCP_PROMPT_LIMIT"),
            "CLAUDE_MCP_PROMPT_LIMIT",
            20_000,
            1,
            200_000,
        ),
        rate_limit=_bounded_int(
            source.get("CLAUDE_MCP_RATE_LIMIT_PER_HOUR"),
            "CLAUDE_MCP_RATE_LIMIT_PER_HOUR",
            20,
            1,
            1_000,
        ),
        concurrency=_bounded_int(
            source.get("CLAUDE_MCP_CONCURRENCY"),
            "CLAUDE_MCP_CONCURRENCY",
            1,
            1,
            32,
        ),
        output_limit=output_limit,
        result_limit=_bounded_int(
            source.get("CLAUDE_MCP_RESULT_LIMIT"),
            "CLAUDE_MCP_RESULT_LIMIT",
            100_000,
            1_000,
            1_000_000,
        ),
        required_cli_version=required_version,
    )


def validate_prompt(prompt: object, limit: int) -> str:
    if not isinstance(prompt, str) or not prompt:
        raise ConfigurationError("prompt must be a non-empty string")
    if len(prompt) > limit:
        raise ConfigurationError(f"prompt exceeds {limit} characters")
    if "\x00" in prompt:
        raise ConfigurationError("prompt contains an invalid character")
    return prompt


def validate_model(model: object, settings: Settings) -> str:
    logical = DEFAULT_MODEL if model in (None, "") else model
    if not isinstance(logical, str) or logical not in settings.models:
        allowed = ", ".join(sorted(settings.models)) or "(none)"
        raise ConfigurationError(f"model is unavailable; allowed models: {allowed}")
    return logical


def validate_conversation_id(value: object) -> str:
    if not isinstance(value, str) or not _CONVERSATION_RE.fullmatch(value):
        raise ConfigurationError("conversation_id must be a server-issued UUID")
    return value.lower()


def clean_claude_session_id(value: object) -> str | None:
    if not isinstance(value, str) or not _CLAUDE_SESSION_RE.fullmatch(value):
        return None
    return value
