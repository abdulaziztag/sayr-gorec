from pathlib import Path

import pytest

from gorets.feeds import FeedConfigError, load_project_config, parse_project_config

ROOT = Path(__file__).resolve().parent.parent


def test_project_config_loads() -> None:
    config = load_project_config(ROOT / "gorets.toml")
    assert {"afisha", "dispatch", "companions_hikinguz", "tracks_hikinguz"} <= set(config.feeds)
    afisha = config.feeds["afisha"]
    assert afisha.keep_contacts is True
    assert afisha.matches(1, "gorets_uzb", 500, "Афиша походов")
    assert not afisha.matches(1, "gorets_uzb", 1, "General")
    assert not afisha.matches(2, "hikinguz", 500, "АФИША ПОХОДОВ")
    # Обрезанное название ветки совпадает по началу.
    routes = config.feeds["routes"]
    assert routes.matches(1, "gorets_uzb", 7, "Описания и нюансы популярных маршрутов и не только")
    assert config.feeds["dispatch_hikinguz"].matches(
        2, "hikinguz", 9, "Диспетчерская (пишем кто куда ушёл и когда вернулся)"
    )
    ext = config.extractors["afisha_tour"]
    assert ext.feeds == ("afisha",)
    assert ext.schema["type"] == "object"
    assert "title" in ext.schema["properties"]
    assert config.extractors["trail_condition"].feeds[:2] == ("dispatch", "dispatch_hikinguz")
    assert config.keeps_contacts(1, "gorets_uzb", 500, "АФИША ПОХОДОВ")
    assert not config.keeps_contacts(1, "gorets_uzb", 1, "General")
    assert config.keeps_contacts(2, "hikinguz", 3, "Кто куда ? (поиск попутчиков)")
    assert not config.keeps_contacts(1, "gorets_uzb", 3, "ЧАТ")


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
    by_title = parse_project_config({"feeds": {"y": {"chat": "c", "topics": ["чат"]}}}, Path("."))
    assert by_title.feeds["y"].matches(1, "c", 2, "ЧАТ")
    assert by_title.feeds["y"].matches(1, "c", 2, "Чат про снаряжение")
    assert not by_title.feeds["y"].matches(1, "c", 2, "Общий чат")


def test_validation_errors(tmp_path: Path) -> None:
    with pytest.raises(FeedConfigError, match="нужен chat"):
        parse_project_config({"feeds": {"x": {}}}, tmp_path)
    with pytest.raises(FeedConfigError, match="не описан"):
        parse_project_config(
            {"extractors": {"e": {"feed": "x", "prompt": "p", "schema": "{}"}}}, tmp_path
        )
    with pytest.raises(FeedConfigError, match="нужны feed"):
        parse_project_config(
            {"feeds": {"x": {"chat": "c"}}, "extractors": {"e": {"prompt": "p", "schema": "{}"}}},
            tmp_path,
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
