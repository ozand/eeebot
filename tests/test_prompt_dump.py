from pathlib import Path

from nanobot.runtime import bridge


def test_prompt_dump_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("SELFEVO_DUMP_PROMPTS", raising=False)
    bridge.dump_spawn_prompts(tmp_path, "c1", "system", "task")
    assert not (tmp_path / "prompts").exists()


def test_prompt_dump_writes_exact_content_and_retains_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("SELFEVO_DUMP_PROMPTS", "1")
    for i in range(bridge._DUMP_PROMPTS_RETENTION + 3):
        bridge.dump_spawn_prompts(tmp_path, f"cycle-{i}", f"system-{i}", f"task-{i}")
    system = sorted((tmp_path / "prompts").glob("*.system.txt"))
    task = sorted((tmp_path / "prompts").glob("*.task.txt"))
    assert len(system) == bridge._DUMP_PROMPTS_RETENTION
    assert len(task) == bridge._DUMP_PROMPTS_RETENTION
    assert (tmp_path / "prompts" / "cycle-22.system.txt").read_text() == "system-22"
    assert (tmp_path / "prompts" / "cycle-22.task.txt").read_text() == "task-22"


def test_prompt_dump_failure_is_fail_open(tmp_path, monkeypatch):
    monkeypatch.setenv("SELFEVO_DUMP_PROMPTS", "1")
    monkeypatch.setattr(Path, "write_text", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no")))
    bridge.dump_spawn_prompts(tmp_path, "c1", "system", "task")
