import asyncio
from pathlib import Path

from gorets.telegram.deliver import resolve_owner, send_report, split_message
from tests.fakes import FakeTelegramClient


def test_short_text_is_one_part_without_marker() -> None:
    assert split_message("Коротко") == ["Коротко"]


def test_long_text_split_by_paragraphs_within_limit() -> None:
    paragraphs = [f"Абзац {i}. " + "слово " * 60 for i in range(40)]
    text = "\n\n".join(paragraphs)
    parts = split_message(text, limit=1000)
    assert len(parts) > 1
    assert all(len(p) <= 1000 for p in parts)
    assert parts[0].startswith("(1/")
    assert "".join(parts).count("Абзац") == 40
    # Абзацы не рвутся посередине: каждая часть начинается с «Абзац» после маркера.
    for part in parts:
        body = part.split("\n\n", 1)[1]
        assert body.startswith("Абзац")


def test_huge_paragraph_is_cut_by_lines_and_words() -> None:
    text = "\n".join("строка " * 10 for _ in range(300))
    parts = split_message(text, limit=500)
    assert all(len(p) <= 500 for p in parts)
    assert "".join(p.split("\n\n", 1)[1] for p in parts).replace("\n", " ").count("строка") == 3000


def test_resolve_owner() -> None:
    assert resolve_owner("@someone") == "@someone"
    assert resolve_owner("123456") == 123456
    assert resolve_owner(42) == 42


def test_numeric_owner_is_resolved_through_dialogs(tmp_path: Path) -> None:
    client = FakeTelegramClient([])
    assert asyncio.run(send_report(client, "123456", text="Отчёт", file_path=None)) == "parts"
    assert client.dialogs_loaded is True
    assert client.sent == [(123456, "Отчёт")]


def test_send_as_parts_or_file(tmp_path: Path) -> None:
    client = FakeTelegramClient([])
    report = tmp_path / "2026-W40.md"
    report.write_text("# Отчёт")
    short = "Короткий отчёт"
    assert asyncio.run(send_report(client, "@owner", text=short, file_path=report)) == "parts"
    assert client.sent == [("@owner", short)]

    long_text = "\n\n".join("абзац " * 100 for _ in range(30))
    how = asyncio.run(
        send_report(client, "@owner", text=long_text, file_path=report, mode="auto", max_parts=2)
    )
    assert how == "file"
    assert client.files[0][1] == str(report)
    assert client.files[0][2].startswith("абзац")

    how = asyncio.run(send_report(client, "@owner", text=long_text, file_path=report, mode="parts"))
    assert how == "parts"
    assert len(client.sent) > 2
