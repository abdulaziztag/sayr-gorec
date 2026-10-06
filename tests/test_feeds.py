from pathlib import Path

import pytest

from gorets.feeds import FeedConfigError, load_project_config, parse_project_config

ROOT = Path(__file__).resolve().parent.parent


def test_example_config_loads() -> None:
    config = load_project_config(ROOT / "gorets.toml")
    assert set(config.feeds) == {"afisha", "conditions"}
    afisha = config.feeds["afisha"]
    assert afisha.keep_contacts is True
    assert afisha.matches(1, "gorets_uzb", 500, "афиши")
    assert not afisha.matches(1, "gorets_uzb", 1, "General")
    assert not afisha.matches(2, "other", 500, "Афиши")
    assert config.feeds["conditions"].matches(1, "gorets_uzb", 999, None)
    ext = config.extractors["afisha_tour"]
    assert ext.feed == "afisha"
    assert ext.schema["type"] == "object"
    assert "title" in ext.schema["properties"]
    assert config.keeps_contacts(1, "gorets_uzb", 500, "Афиши")
    assert not config.keeps_contacts(1, "gorets_uzb", 1, "General")


def test_missing_file_is_empty_config(tmp_path: Path) -> None:
    config = load_project_config(tmp_path / "nope.toml")
    assert config.feeds == {} and config.extractors == {}
    assert load_project_config(None).feeds == {}


def test_chat_by_id_and_topic_by_id() -> None:
    config = parse_project_config(
        {"feeds": {"x": {"chat": "-1001234567890", "topics": [500]}}}, Path(".")
    )
    assert config.feeds["x"].matches(1234567890, None, 500, None)
    assert not config.feeds["x"].matches(1234567890, None, 501, "500")


def test_validation_errors(tmp_path: Path) -> None:
    with pytest.raises(FeedConfigError, match="нужен chat"):
        parse_project_config({"feeds": {"x": {}}}, tmp_path)
    with pytest.raises(FeedConfigError, match="не описан"):
        parse_project_config(
            {"extractors": {"e": {"feed": "x", "prompt": "p", "schema": "{}"}}}, tmp_path
        )
    with pytest.raises(FeedConfigError, match="объект"):
        parse_project_config(
            {
                "feeds": {"x": {"chat": "c"}},
                "extractors": {"e": {"feed": "x", "prompt": "p", "schema": '{"type": "array"}'}},
            },
            tmp_path,
        )
    schema_file = tmp_path / "s.json"
    schema_file.write_text('{"type": "object", "properties": {}}')
    config = parse_project_config(
        {
            "feeds": {"x": {"chat": "c"}},
            "extractors": {"e": {"feed": "x", "prompt": "p", "schema_file": "s.json"}},
        },
        tmp_path,
    )
    assert config.extractors["e"].schema["properties"] == {}
    with pytest.raises(FeedConfigError, match="не читается"):
        parse_project_config(
            {
                "feeds": {"x": {"chat": "c"}},
                "extractors": {"e": {"feed": "x", "prompt": "p", "schema_file": "missing.json"}},
            },
            tmp_path,
        )
