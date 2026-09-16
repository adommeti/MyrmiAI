import shutil

from digest.config import CONFIG_DIR, load_settings
from digest.models_cmd import choose_defaults, format_catalogue, write_models

CATALOGUE = [
    "hf:moonshotai/Kimi-K2-Pro",
    "hf:zai-org/GLM-5.3-Flash",
    "hf:deepseek-ai/DeepSeek-V4-Flash",
    "hf:Qwen/Qwen3.8-27B",
]


def test_choose_defaults_assigns_all_three_steps():
    chosen = choose_defaults(CATALOGUE)
    assert set(chosen) == {"item", "brief", "classify"}
    assert all(model in CATALOGUE for model in chosen.values())


def test_choose_defaults_prefers_a_flagship_for_the_brief():
    chosen = choose_defaults(CATALOGUE)
    assert "Pro" in chosen["brief"]
    assert "Flash" in chosen["classify"]


def test_choose_defaults_copes_with_a_single_model():
    chosen = choose_defaults(["hf:only/model"])
    assert set(chosen.values()) == {"hf:only/model"}


def test_choose_defaults_on_an_empty_catalogue():
    assert choose_defaults([]) == {}


def test_format_catalogue_handles_missing_metadata():
    assert "hf:a/b" in format_catalogue([{"id": "hf:a/b"}])
    assert "no models" in format_catalogue([])


def test_write_models_preserves_comments(tmp_path):
    """settings.yaml is mostly explanatory comments; a YAML round-trip loses them."""
    shutil.copy(CONFIG_DIR / "settings.yaml", tmp_path / "settings.yaml")
    shutil.copy(CONFIG_DIR / "taxonomy.yaml", tmp_path / "taxonomy.yaml")

    write_models(
        {"item": "hf:a/item", "brief": "hf:a/brief", "classify": "hf:a/classify"},
        config_dir=tmp_path,
    )

    text = (tmp_path / "settings.yaml").read_text()
    assert "# Deliberately blank" in text
    assert "digest models --write" in text
    assert 'item: "hf:a/item"' in text

    settings = load_settings(tmp_path)
    assert settings.model_for("item") == "hf:a/item"
    assert settings.model_for("brief") == "hf:a/brief"
    assert settings.model_for("classify") == "hf:a/classify"
    # Everything outside the models block is untouched.
    assert settings.provider == "synthetic"
    assert settings.concurrency == 6


def test_write_models_does_not_touch_other_blocks(tmp_path):
    shutil.copy(CONFIG_DIR / "settings.yaml", tmp_path / "settings.yaml")
    shutil.copy(CONFIG_DIR / "taxonomy.yaml", tmp_path / "taxonomy.yaml")
    before = (tmp_path / "settings.yaml").read_text()

    write_models({"item": "hf:x/y"}, config_dir=tmp_path)
    after = (tmp_path / "settings.yaml").read_text()

    # Only the one line changed.
    diff = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    assert len(diff) == 1
    assert "item:" in diff[0][1]
