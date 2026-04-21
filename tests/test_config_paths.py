from pathlib import Path

from nanobot.config.loader import get_config_path
from nanobot.config.paths import (
    _compat_home_dir,
    get_bridge_install_dir,
    get_cli_history_path,
    get_cron_dir,
    get_data_dir,
    get_legacy_sessions_dir,
    get_logs_dir,
    get_media_dir,
    get_runtime_subdir,
    get_workspace_path,
)


def test_runtime_dirs_follow_config_path(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance-a" / "config.json"
    monkeypatch.setattr("nanobot.config.paths.get_config_path", lambda: config_file)

    assert get_data_dir() == config_file.parent
    assert get_runtime_subdir("cron") == config_file.parent / "cron"
    assert get_cron_dir() == config_file.parent / "cron"
    assert get_logs_dir() == config_file.parent / "logs"


def test_media_dir_supports_channel_namespace(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance-b" / "config.json"
    monkeypatch.setattr("nanobot.config.paths.get_config_path", lambda: config_file)

    assert get_media_dir() == config_file.parent / "media"
    assert get_media_dir("telegram") == config_file.parent / "media" / "telegram"


def test_shared_and_legacy_paths_remain_global(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setattr('pathlib.Path.home', lambda: home)
    assert _compat_home_dir() == home / '.nanobot'
    assert get_cli_history_path() == home / '.nanobot' / 'history' / 'cli_history'
    assert get_bridge_install_dir() == home / '.nanobot' / 'bridge'
    assert get_legacy_sessions_dir() == home / '.nanobot' / 'sessions'


def test_shared_paths_can_prefer_eeebot_home_when_present(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / 'home'
    (home / '.eeebot').mkdir(parents=True)
    (home / '.eeebot' / 'config.json').write_text('{}', encoding='utf-8')
    monkeypatch.setattr('pathlib.Path.home', lambda: home)
    monkeypatch.setattr('nanobot.config.loader.Path.home', lambda: home)
    monkeypatch.setattr('nanobot.config.loader._current_config_path', None)
    assert _compat_home_dir() == home / '.eeebot'
    assert get_config_path() == home / '.eeebot' / 'config.json'
    assert get_cli_history_path() == home / '.eeebot' / 'history' / 'cli_history'
    assert get_bridge_install_dir() == home / '.eeebot' / 'bridge'
    assert get_legacy_sessions_dir() == home / '.eeebot' / 'sessions'


def test_workspace_path_is_explicitly_resolved(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setattr('pathlib.Path.home', lambda: home)
    assert get_workspace_path() == home / '.nanobot' / 'workspace'
    assert get_workspace_path('~/custom-workspace') == Path('~/custom-workspace').expanduser()
