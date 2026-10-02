"""Configuration and validation for the restricted Gemini MCP server."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re


DEFAULT_MODEL = "auto"
REASONING_EFFORTS = frozenset(("low", "medium", "high", "max"))
DEFAULT_PRINCIPALS = ("owner", "partner")
DEFAULT_REQUIRED_CLI_VERSION = (1, 2, 14)
NUL_BYTE = "␀"  # Logical NUL marker; validate before ever creating a real NUL.
_PRINCIPAL_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_CLI_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_FAMILY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CONVERSATION_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_GEMINI_SESSION_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
_VERSION_RE = re.compile(r"v?([0-9]+[.][0-9]+(?:[.][0-9]+)?)")


class ConfigurationError(ValueError):
    """Invalid server configuration or untrusted request input."""


@dataclass(frozen=True)
class ModelPolicy:
    logical_name: str
    cli_value: str
    family: str
    min_cli_version: tuple[int, ...]
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class Settings:
    work_root: Path
    principal: str
    allowed_principals: tuple[str, ...]
    gemini_bin: Path
    database_path: Path
    lock_dir: Path
    models: dict[str, ModelPolicy]
    prompt_limit: int
    continue_burst_limit: int
    continue_burst_window_seconds: int
    concurrency: int
    output_limit: int
    result_limit: int
    required_cli_version: tuple[int, ...]
    default_model: str = DEFAULT_MODEL
    require_explicit_model: bool = False

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
        raise ConfigurationError("invalid GEMINI_MCP_MODELS")
    if isinstance(raw, str):
        raw = {"cli_value": raw}
    if not isinstance(raw, dict):
        raise ConfigurationError("invalid GEMINI_MCP_MODELS")
    cli_value = raw.get("cli_value", logical_name)
    family = raw.get("family", logical_name)
    min_version = _required_version(raw.get("min_cli_version"))
    reasoning_effort = raw.get("reasoning_effort")
    if (
        not isinstance(cli_value, str)
        or not isinstance(family, str)
        or not _CLI_VALUE_RE.fullmatch(cli_value)
        or not _FAMILY_RE.fullmatch(family)
    ):
        raise ConfigurationError("invalid GEMINI_MCP_MODELS")
    if reasoning_effort is not None:
        if not isinstance(reasoning_effort, str):
            raise ConfigurationError("invalid GEMINI_MCP_MODELS")
        reasoning_effort = reasoning_effort.casefold()
        if reasoning_effort not in REASONING_EFFORTS:
            raise ConfigurationError("invalid GEMINI_MCP_MODELS")
    return ModelPolicy(logical_name, cli_value, family, min_version, reasoning_effort)


def _models(raw: str | None) -> dict[str, ModelPolicy]:
    source: object = {"auto": {"cli_value": "auto", "family": "gemini"}}
    if raw not in (None, ""):
        try:
            source = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ConfigurationError("invalid GEMINI_MCP_MODELS") from exc
    if not isinstance(source, dict) or not source:
        raise ConfigurationError("invalid GEMINI_MCP_MODELS")
    policies = [_model_policy(name, value) for name, value in source.items()]
    return {policy.logical_name: policy for policy in policies}


def _principals(raw: str | None) -> tuple[str, ...]:
    values = tuple(
        item.strip() for item in (raw or "owner,partner").split(",") if item.strip()
    )
    if not values or not all(_PRINCIPAL_RE.fullmatch(value) for value in values):
        raise ConfigurationError("invalid GEMINI_MCP_PRINCIPALS")
    return values


def _require_explicit_model(raw: str | None) -> bool:
    if raw in (None, ""):
        return False
    normalized = raw.strip().casefold()
    if normalized in ("1", "true"):
        return True
    if normalized in ("0", "false"):
        return False
    raise ConfigurationError("invalid GEMINI_MCP_REQUIRE_EXPLICIT_MODEL")


def _executable(raw: str, variable: str = "AGY_BIN") -> Path:
    if not raw or "\x00" in raw:
        raise ConfigurationError(f"invalid {variable}")
    try:
        path = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ConfigurationError(f"invalid {variable}") from exc
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ConfigurationError(f"invalid {variable}")
    return path


def load_settings(env: dict[str, str] | None = None) -> Settings:
    source = os.environ if env is None else env
    raw_root = source.get("GEMINI_MCP_WORK_ROOT", "")
    if not raw_root or "\x00" in raw_root:
        raise ConfigurationError("GEMINI_MCP_WORK_ROOT is required")
    try:
        work_root = Path(raw_root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ConfigurationError("GEMINI_MCP_WORK_ROOT is required") from exc
    if not work_root.is_dir() or work_root.is_symlink():
        raise ConfigurationError("GEMINI_MCP_WORK_ROOT must be a real directory")

    principal = source.get("GEMINI_MCP_PRINCIPAL", "")
    principals = _principals(source.get("GEMINI_MCP_PRINCIPALS"))
    if not _PRINCIPAL_RE.fullmatch(principal) or principal not in principals:
        raise ConfigurationError("invalid GEMINI_MCP_PRINCIPAL")
    models = _models(source.get("GEMINI_MCP_MODELS"))
    default_model = source.get("GEMINI_MCP_DEFAULT_MODEL") or DEFAULT_MODEL
    if not isinstance(default_model, str) or default_model not in models:
        raise ConfigurationError("invalid GEMINI_MCP_DEFAULT_MODEL")
    require_explicit_model = _require_explicit_model(
        source.get("GEMINI_MCP_REQUIRE_EXPLICIT_MODEL")
    )
    output_limit = _bounded_int(
        source.get("GEMINI_MCP_OUTPUT_LIMIT"),
        "GEMINI_MCP_OUTPUT_LIMIT",
        4_194_304,
        1_000,
        16_777_216,
    )
    required_version = _required_version(source.get("GEMINI_MCP_REQUIRED_CLI_VERSION"))
    agy_bin = source.get("AGY_BIN")
    gemini_bin = source.get("GEMINI_BIN")
    # AGY_BIN is the current setting; GEMINI_BIN remains a deployment alias.
    if agy_bin:
        executable, executable_variable = agy_bin, "AGY_BIN"
    elif gemini_bin:
        executable, executable_variable = gemini_bin, "GEMINI_BIN"
    else:
        executable, executable_variable = "/usr/local/bin/agy", "AGY_BIN"
    return Settings(
        work_root=work_root,
        principal=principal,
        allowed_principals=principals,
        gemini_bin=_executable(executable, executable_variable),
        database_path=work_root / "state" / "mcp.sqlite3",
        lock_dir=work_root / "state" / "locks",
        models=models,
        prompt_limit=_bounded_int(
            source.get("GEMINI_MCP_PROMPT_LIMIT"),
            "GEMINI_MCP_PROMPT_LIMIT",
            20_000,
            1,
            200_000,
        ),
        continue_burst_limit=_bounded_int(
            source.get("GEMINI_MCP_CONTINUE_BURST_LIMIT"),
            "GEMINI_MCP_CONTINUE_BURST_LIMIT",
            50,
            1,
            100,
        ),
        continue_burst_window_seconds=_bounded_int(
            source.get("GEMINI_MCP_CONTINUE_BURST_WINDOW_SECONDS"),
            "GEMINI_MCP_CONTINUE_BURST_WINDOW_SECONDS",
            60,
            1,
            3_600,
        ),
        concurrency=_bounded_int(
            source.get("GEMINI_MCP_CONCURRENCY"),
            "GEMINI_MCP_CONCURRENCY",
            1,
            1,
            32,
        ),
        output_limit=output_limit,
        result_limit=_bounded_int(
            source.get("GEMINI_MCP_RESULT_LIMIT"),
            "GEMINI_MCP_RESULT_LIMIT",
            4_194_304,
            1_000,
            16_777_216,
        ),
        required_cli_version=required_version,
        default_model=default_model,
        require_explicit_model=require_explicit_model,
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
    logical = settings.default_model if model in (None, "") else model
    if not isinstance(logical, str) or logical not in settings.models:
        allowed = ", ".join(sorted(settings.models)) or "(none)"
        raise ConfigurationError(f"model is unavailable; allowed models: {allowed}")
    return logical


def validate_conversation_id(value: object) -> str:
    if not isinstance(value, str) or not _CONVERSATION_RE.fullmatch(value):
        raise ConfigurationError("conversation_id must be a server-issued UUID")
    return value.lower()


def clean_gemini_session_id(value: object) -> str | None:
    if not isinstance(value, str) or not _GEMINI_SESSION_RE.fullmatch(value):
        return None
    return value
