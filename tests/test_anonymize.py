import pytest

from gorets.anonymize import author_hash


def test_hash_is_deterministic_and_hex() -> None:
    a = author_hash("secret", 123456)
    assert a == author_hash("secret", 123456)
    assert len(a) == 64
    assert int(a, 16) >= 0


def test_hash_depends_on_secret_and_id() -> None:
    assert author_hash("secret", 1) != author_hash("other", 1)
    assert author_hash("secret", 1) != author_hash("secret", 2)


def test_channels_do_not_collide_with_users() -> None:
    assert author_hash("s", 42, "user") != author_hash("s", 42, "channel")


def test_empty_secret_rejected() -> None:
    with pytest.raises(ValueError):
        author_hash("", 1)
