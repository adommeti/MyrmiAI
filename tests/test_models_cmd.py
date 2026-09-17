import shutil

import pytest

from digest.config import CONFIG_DIR, load_settings
from digest.models_cmd import (
    ALIAS_DEFAULTS,
    SYN_ALIASES,
    choose_defaults,
    format_aliases,
    format_catalogue,
    write_models,
)

CATALOGUE = [
    "hf:zai-org/GLM-5.3-Flash",
    "hf:zai-org/GLM-4.7-Flash",
    "hf:moonshotai/Kimi-K3",
    "hf:deepseek-ai/DeepSeek-V4.1-Flash",
    "hf:Qwen/Qwen3.8-27B",
    "hf:openai/gpt-oss-120b",
]


def test_defaults_are_aliases_not_pinned_ids():
    """Synthetic's docs: pinning an hf: id 404s when that model is rotated out."""
    chosen = choose_defaults(CATALOGUE)
    assert set(chosen) == {"item", "brief", "classify"}
    assert all(model.startswith("syn:") for model in chosen.values())
    assert all(model in SYN_ALIASES for model in chosen.values())


def test_aliases_do_not_depend_on_the_catalogue():
    """An alias is a routing label; /models never lists it, so it must not be
    filtered out by comparing against the catalogue."""
    assert choose_defaults([]) == ALIAS_DEFAULTS


def test_the_high_volume_step_and_the_read_step_get_the_large_model():
    chosen = choose_defaults(CATALOGUE)
    assert chosen["brief"] == "syn:large:text"
    assert chosen["item"] == "syn:large:text"
    assert chosen["classify"] == "syn:small:text"


def test_pin_resolves_concrete_ids_from_the_catalogue():
    chosen = choose_defaults(CATALOGUE, pin=True)
    assert all(model in CATALOGUE for model in chosen.values())
    assert not any(model.startswith("syn:") for model in chosen.values())


def test_pin_copes_with_a_single_model():
    assert set(choose_defaults(["hf:only/model"], pin=True).values()) == {"hf:only/model"}


def test_pin_on_an_empty_catalogue_writes_nothing():
    assert choose_defaults([], pin=True) == {}


def test_format_aliases_shows_what_each_resolves_to():
    text = format_aliases()
    assert "syn:large:text" in text
    assert "hf:zai-org/GLM-5.3-Flash" in text


def test_format_catalogue_handles_missing_metadata():
    assert "hf:a/b" in format_catalogue([{"id": "hf:a/b"}])
    assert "no models" in format_catalogue([])


def test_format_catalogue_shows_context_length():
    text = format_catalogue([{"id": "hf:a/b", "context_length": 524288}])
    assert "524,288" in text


@pytest.fixture
def config_dir(tmp_path):
    shutil.copy(CONFIG_DIR / "settings.yaml", tmp_path / "settings.yaml")
    shutil.copy(CONFIG_DIR / "taxonomy.yaml", tmp_path / "taxonomy.yaml")
    return tmp_path


def test_shipped_defaults_are_aliases(config_dir):
    """The repo must work on a first run without a discovery step."""
    settings = load_settings(config_dir)
    for step in ("item", "brief", "classify"):
        assert settings.model_for(step) in SYN_ALIASES


def test_write_models_preserves_comments(config_dir):
    """settings.yaml is mostly explanatory comments; a YAML round-trip loses them."""
    write_models(
        {"item": "hf:a/item", "brief": "hf:a/brief", "classify": "hf:a/classify"},
        config_dir=config_dir,
    )

    text = (config_dir / "settings.yaml").read_text()
    assert "404 errors when we rotate" in text      # the vendor warning survives
    assert "digest models" in text
    assert 'item: "hf:a/item"' in text

    settings = load_settings(config_dir)
    assert settings.model_for("item") == "hf:a/item"
    assert settings.model_for("brief") == "hf:a/brief"
    assert settings.model_for("classify") == "hf:a/classify"
    assert settings.provider == "synthetic"
    assert settings.concurrency == 6


def test_write_models_does_not_touch_other_blocks(config_dir):
    before = (config_dir / "settings.yaml").read_text()
    write_models({"item": "hf:x/y"}, config_dir=config_dir)
    after = (config_dir / "settings.yaml").read_text()

    diff = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    assert len(diff) == 1
    assert "item:" in diff[0][1]


def test_round_trip_back_to_aliases(config_dir):
    """Pinning then un-pinning must restore working aliases."""
    write_models(choose_defaults(CATALOGUE, pin=True), config_dir=config_dir)
    assert load_settings(config_dir).model_for("brief").startswith("hf:")

    write_models(choose_defaults(CATALOGUE), config_dir=config_dir)
    assert load_settings(config_dir).model_for("brief") == "syn:large:text"
