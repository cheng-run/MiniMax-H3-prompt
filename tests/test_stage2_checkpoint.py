from minimax_h3_prompt.stage2_checkpoint import (
    load_checkpoint,
    save_checkpoint,
    clear_checkpoint,
    checkpoint_path,
)


def test_save_load_roundtrip(tmp_path):
    save_checkpoint(tmp_path, prompt_draft="draft prompt")
    data = load_checkpoint(tmp_path)
    assert data is not None
    assert data["prompt_draft"] == "draft prompt"
    assert data["status"] == "assemble_done"


def test_load_missing_returns_none(tmp_path):
    assert load_checkpoint(tmp_path) is None


def test_load_empty_draft_returns_none(tmp_path):
    save_checkpoint(tmp_path, prompt_draft="x")
    checkpoint_path(tmp_path).write_text(
        '{"schema_version": "stage2_checkpoint.v1", "status": "assemble_done", "prompt_draft": ""}',
        encoding="utf-8",
    )
    assert load_checkpoint(tmp_path) is None


def test_load_wrong_version_returns_none(tmp_path):
    save_checkpoint(tmp_path, prompt_draft="x")
    checkpoint_path(tmp_path).write_text('{"schema_version": "v0"}', encoding="utf-8")
    assert load_checkpoint(tmp_path) is None


def test_clear(tmp_path):
    save_checkpoint(tmp_path, prompt_draft="x")
    clear_checkpoint(tmp_path)
    assert load_checkpoint(tmp_path) is None
    clear_checkpoint(tmp_path)  # 幂等，不存在的文件再删也不报错
