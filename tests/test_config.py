from datetime import datetime, timezone

from digest.config import load_settings, resolve_period


def test_taxonomy_loads_and_has_a_fallback():
    settings = load_settings()
    assert settings.categories
    assert settings.fallback_category in settings.category_names


def test_aligned_window_is_stable_across_the_week():
    """Monday's run and Thursday's catch-up must cover the same seven days."""
    settings = load_settings()
    monday = resolve_period(settings, now=datetime(2026, 9, 14, 11, 0, tzinfo=timezone.utc))
    thursday = resolve_period(settings, now=datetime(2026, 9, 17, 3, 0, tzinfo=timezone.utc))
    assert monday == thursday
    assert monday[0].weekday() == 0
    assert (monday[1] - monday[0]).days == 7


def test_unaligned_window_ends_now():
    settings = load_settings()
    now = datetime(2026, 9, 17, 3, 0, tzinfo=timezone.utc)
    start, end = resolve_period(settings, now=now, align=False)
    assert end == now
    assert (end - start).days == 7


def test_category_order_follows_the_taxonomy_file():
    settings = load_settings()
    first, second = settings.category_names[0], settings.category_names[1]
    assert settings.category_order(first) < settings.category_order(second)
    assert settings.category_order("Nonexistent") >= len(settings.categories)


def test_provider_defaults_to_synthetic():
    settings = load_settings()
    assert settings.provider == "synthetic"
    assert settings.base_url.startswith("https://api.synthetic.new")
    assert settings.concurrency >= 1


def test_unset_model_raises_an_actionable_error():
    """A blank model must point at `digest models`, not fail deep in a worker."""
    import pytest

    settings = load_settings()
    settings.model_item = ""
    with pytest.raises(RuntimeError, match="digest models"):
        settings.model_for("item")


def test_configured_model_is_returned_verbatim():
    settings = load_settings()
    settings.model_brief = "hf:moonshotai/Kimi-Example"
    assert settings.model_for("brief") == "hf:moonshotai/Kimi-Example"
