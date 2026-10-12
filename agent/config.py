"""Lab-wide config loading.

config.yaml has two axes: a `models` registry (with `active_model` as the
default) and `mitigations` toggles. resolve_config() picks one model profile
and one optional mitigation override and returns a single effective config,
with the chosen profile materialized under `config["model"]` so the rest of
the lab (agent loop, tools, harness, smoke test) never sees the registry.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
CONFIGS_DIR = ROOT / "configs"

KNOWN_APIS = ("responses", "chat_completions")


class ConfigError(ValueError):
    """Raised when config.yaml or an override file is malformed."""


def _read_yaml(path: Path) -> Dict[str, Any]:
    with path.open() as f:
        data = yaml.safe_load(f)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    return data


def load_config() -> dict:
    """Return config.yaml exactly as written: the raw `models` registry,
    `active_model`, `mitigations`, `budgets` and `harden_all`. No model is
    selected and harden_all is not resolved. Use resolve_config() for a usable
    config with a model materialized under `model`."""
    return _read_yaml(CONFIG_PATH)


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any], _path: str = "") -> dict:
    """Return a new dict with override merged onto base; neither input is
    mutated. Mappings merge recursively, while any other value (lists
    included) replaces the base value outright.

    Keys that don't exist in base are rejected."""
    merged = copy.deepcopy(dict(base))
    for key, value in override.items():
        where = f"{_path}.{key}" if _path else str(key)
        if key not in merged:
            raise ConfigError(f"unknown config key '{where}' (not defined in config.yaml)")
        base_is_map = isinstance(merged[key], Mapping)
        override_is_map = isinstance(value, Mapping)
        if base_is_map and override_is_map:
            merged[key] = deep_merge(merged[key], value, where)
        elif base_is_map or override_is_map:
            raise ConfigError(f"'{where}' must be a mapping in both config.yaml and the override, or in neither")
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _override_path(override: Union[str, Path]) -> Path:
    """A bare name like "credit_controls" means configs/credit_controls.yaml;
    anything ending in .yaml/.yml is treated as a path (relative paths are
    tried from the current directory, then from the repo root)."""
    path = Path(override)
    if path.suffix not in (".yaml", ".yml"):
        path = CONFIGS_DIR / f"{override}.yaml"
    elif not path.is_absolute() and not path.exists():
        path = ROOT / path
    if not path.exists():
        raise ConfigError(f"override config not found: {path}")
    return path


def _validate_model_profile(name: str, profile: Any) -> None:
    if not isinstance(profile, dict):
        raise ConfigError(f"models.{name} must be a mapping")
    api = profile.get("api")
    if api not in KNOWN_APIS:
        raise ConfigError(f"models.{name}.api must be one of {KNOWN_APIS}, got {api!r}")
    for key in ("name", "api_key_env"):
        if not isinstance(profile.get(key), str) or not profile[key]:
            raise ConfigError(f"models.{name}.{key} must be a non-empty string")


def _validate(config: Dict[str, Any]) -> None:
    models = config.get("models")
    if not isinstance(models, dict) or not models:
        raise ConfigError("config.yaml must define a non-empty 'models' mapping")
    for name, profile in models.items():
        _validate_model_profile(name, profile)
    if config.get("active_model") not in models:
        raise ConfigError(f"active_model must name a model in 'models' ({sorted(models)})")

    mitigations = config.get("mitigations")
    if not isinstance(mitigations, dict) or not mitigations:
        raise ConfigError("config.yaml must define a non-empty 'mitigations' mapping")
    for name, settings in mitigations.items():
        if not isinstance(settings, dict) or not isinstance(settings.get("enabled"), bool):
            raise ConfigError(f"mitigations.{name} must be a mapping with a boolean 'enabled'")

    budgets = config.get("budgets")
    if not isinstance(budgets, dict):
        raise ConfigError("config.yaml must define a 'budgets' mapping")
    for key in ("max_steps_per_ticket", "max_tokens_per_ticket"):
        value = budgets.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError(f"budgets.{key} must be a positive integer")

    if not isinstance(config.get("harden_all"), bool):
        raise ConfigError("harden_all must be true or false")


def resolve_config(override: Optional[Union[str, Path]] = None, *, model: Optional[str] = None) -> dict:
    """Build the effective config for one run.

    Picks a model profile (the `model` argument, else active_model) and
    materializes it under `config["model"]`; deep-merges the mitigation
    `override` (a configs/ name or path) if given; validates; then applies
    harden_all. Adds `model_name` (the chosen profile) and `config_name` (the
    override stem, or "default") for logging and CSV rows. Returns a fresh
    dict every call, so callers may keep or mutate it per run."""
    config = load_config()
    _validate(config)  # validate the registry before selecting from it

    model_name = model if model is not None else config["active_model"]
    if model_name not in config["models"]:
        raise ConfigError(f"unknown model {model_name!r}; choose from {sorted(config['models'])}")
    config["model"] = copy.deepcopy(config["models"][model_name])
    config["model_name"] = model_name

    config_name = "default"
    if override is not None:
        path = _override_path(override)
        config = deep_merge(config, _read_yaml(path))
        config_name = path.stem

    _validate(config)

    if config["harden_all"]:
        for settings in config["mitigations"].values():
            settings["enabled"] = True

    config["config_name"] = config_name
    return config


def mitigation(config: Mapping[str, Any], name: str) -> Dict[str, Any]:
    """Settings for one mitigation from a resolved config. Raises
    ConfigError on an unknown name, so a misspelled lookup in tool code
    fails instead of reading as "disabled"."""
    try:
        return config["mitigations"][name]
    except KeyError:
        raise ConfigError(f"unknown mitigation '{name}'") from None


def is_enabled(config: Mapping[str, Any], name: str) -> bool:
    return bool(mitigation(config, name)["enabled"])
