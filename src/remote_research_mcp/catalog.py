"""Lead-owned model catalog loading and public-model resolution."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CATALOG_PATH = Path("/etc/remote-research/catalog.json")
PROVIDERS = ("claude", "gemini")
ALLOWED_PRINCIPALS = ("owner", "partner", "partner2", "partner3", "partner4")


class CatalogError(ValueError):
    """Invalid catalog or model selection."""


@dataclass(frozen=True)
class ModelSpec:
    public_id: str
    provider: str
    backend_model: str
    cli_value: str
    family: str
    reasoning_effort: str | None
    min_cli_version: str
    enabled: bool
    verified: bool
    label: str


@dataclass(frozen=True)
class ProviderCatalog:
    default_model: str
    models: dict[str, ModelSpec]


@dataclass(frozen=True)
class Catalog:
    providers: dict[str, ProviderCatalog]

    def provider(self, name: str) -> ProviderCatalog:
        try:
            return self.providers[name]
        except KeyError as exc:
            raise CatalogError("unknown provider") from exc

    def resolve(self, provider: str, public_id: str | None) -> ModelSpec:
        cat = self.provider(provider)
        model_id = cat.default_model if public_id is None else public_id
        spec = cat.models.get(model_id)
        if spec is None or spec.provider != provider:
            raise CatalogError("unknown or unavailable model")
        if not spec.enabled:
            raise CatalogError("model is disabled")
        return spec

    def backend_for_public(self, public_id: str) -> ModelSpec | None:
        for cat in self.providers.values():
            spec = cat.models.get(public_id)
            if spec is not None:
                return spec
        return None

    def public_for_backend(self, provider: str, backend_model: str) -> str | None:
        cat = self.providers.get(provider)
        if cat is None:
            return None
        for spec in cat.models.values():
            if spec.backend_model == backend_model:
                return spec.public_id
        return None


def _model_spec(provider: str, public_id: str, raw: object) -> ModelSpec:
    if not isinstance(raw, dict):
        raise CatalogError("invalid catalog model entry")
    backend = raw.get("backend_model")
    cli = raw.get("cli_value")
    family = raw.get("family")
    effort = raw.get("reasoning_effort")
    min_ver = raw.get("min_cli_version")
    if not all(isinstance(v, str) and v for v in
               (backend, cli, family, min_ver)):
        raise CatalogError("invalid catalog model entry")
    if effort is not None and not isinstance(effort, str):
        raise CatalogError("invalid catalog model entry")
    return ModelSpec(
        public_id=public_id, provider=provider, backend_model=backend,
        cli_value=cli, family=family, reasoning_effort=effort,
        min_cli_version=min_ver,
        enabled=raw.get("enabled") is True,
        verified=raw.get("verified") is True,
        label=str(raw.get("label") or public_id),
    )


def load_catalog(path: Path = DEFAULT_CATALOG_PATH) -> Catalog:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogError("catalog unreadable") from exc
    providers_raw = raw.get("providers") if isinstance(raw, dict) else None
    if not isinstance(providers_raw, dict):
        raise CatalogError("invalid catalog")
    providers: dict[str, ProviderCatalog] = {}
    for name in PROVIDERS:
        entry = providers_raw.get(name)
        if not isinstance(entry, dict):
            raise CatalogError("invalid catalog provider")
        default = entry.get("default_model")
        models_raw = entry.get("models")
        if not isinstance(default, str) or not isinstance(models_raw, dict):
            raise CatalogError("invalid catalog provider")
        models = {
            mid: _model_spec(name, mid, mraw)
            for mid, mraw in models_raw.items()
            if isinstance(mid, str)
        }
        if default not in models:
            raise CatalogError("catalog default model missing")
        providers[name] = ProviderCatalog(default_model=default, models=models)
    return Catalog(providers=providers)
