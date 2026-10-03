from pathlib import Path

from gorets.envfile import append_key, has_key


def test_append_creates_file_with_600(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    assert not has_key(env, "GORETS_TG_SESSION")
    append_key(env, "GORETS_TG_SESSION", "abc")
    assert env.read_text() == "GORETS_TG_SESSION=abc\n"
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    assert has_key(env, "GORETS_TG_SESSION")


def test_append_to_file_without_trailing_newline(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("GORETS_TG_API_ID=1")
    append_key(env, "GORETS_TG_SESSION", "abc")
    assert env.read_text() == "GORETS_TG_API_ID=1\nGORETS_TG_SESSION=abc\n"


def test_has_key_ignores_comments_and_other_keys(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# GORETS_TG_SESSION=old\nGORETS_TG_SESSION_X=1\nexport GORETS_OWNER=@me\n")
    assert not has_key(env, "GORETS_TG_SESSION")
    assert has_key(env, "GORETS_OWNER")
