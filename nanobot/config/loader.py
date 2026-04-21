"""Configuration loading utilities."""

import json
import os
from pathlib import Path

import pydantic
from loguru import logger

from eeebot.config.schema import Config

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None


def _apply_eeebot_env_aliases() -> None:
    """Map EEEBOT_* env vars to NANOBOT_* when canonical vars are absent."""
    for key, value in list(os.environ.items()):
        if not key.startswith('EEEBOT_'):
            continue
        canonical = 'NANOBOT_' + key[len('EEEBOT_'):]
        if canonical not in os.environ:
            os.environ[canonical] = value


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """Get the configuration file path."""
    if _current_config_path:
        return _current_config_path
    eeebot = Path.home() / '.eeebot' / 'config.json'
    nanobot = Path.home() / '.nanobot' / 'config.json'
    if eeebot.exists() and not nanobot.exists():
        return eeebot
    return nanobot


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()

    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data = _migrate_config(data)
            _apply_eeebot_env_aliases()
            return Config.model_validate(data)
        except (json.JSONDecodeError, ValueError, pydantic.ValidationError) as e:
            logger.warning(f"Failed to load config from {path}: {e}")
            logger.warning("Using default configuration.")

    _apply_eeebot_env_aliases()
    return Config()


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(mode="json", by_alias=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")
    return data
