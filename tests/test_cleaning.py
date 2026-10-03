"""Очистка текста: телефоны во всех встречающихся записях, почта, упоминания, ссылки."""

import pytest

from gorets.cleaning import EMAIL, PHONE, USER, TextCleaner, clean_text, mask_phones

PHONES = [
    "+998901234567",
    "+998 90 123 45 67",
    "+998 90 123-45-67",
    "+998-90-123-45-67",
    "+998(90)123-45-67",
    "+998 (90) 123 45 67",
    "+998 (90) 123-45-67",
    "+99890 123 45 67",
    "+99890-123-45-67",
    "+998 901234567",
    "+998 90 1234567",
    "+998 90 123 4567",
    "+998.90.123.45.67",
    "998901234567",
    "998 90 123 45 67",
    "998-90-123-45-67",
    "998 (90) 123 45 67",
    "998 (90)123-45-67",
    "(998) 90 123 45 67",
    "+998 33 123 45 67",
    "+998 55 123 45 67",
    "+998 77 123 45 67",
    "+998 88 123 45 67",
    "+998 71 123 45 67",
    "+998 20 123 45 67",
    "+998 50 123 45 67",
    "90 123 45 67",
    "90-123-45-67",
    "90.123.45.67",
    "(90) 123-45-67",
    "(90)123-45-67",
    "(90) 123 45 67",
    "90 1234567",
    "90 123 4567",
    "901234567",
    "931234567",
    "991234567",
    "331234567",
    "551234567",
    "771234567",
    "881234567",
    "71 123 45 67",
    "(71) 123-45-67",
    "+7 999 123-45-67",
    "+7 (999) 123-45-67",
    "+79991234567",
    "8 (999) 123-45-67",
    "8-999-123-45-67",
    "89991234567",
    "+7 701 123 45 67",
    "+1 650 555 1234",
    "+1 (650) 555-1234",
    "+49 30 1234567",
    "+971 50 123 4567",
    "+996 555 123 456",
    "+992 93 123 4567",
    "+90 532 123 45 67",
]


@pytest.mark.parametrize("phone", PHONES)
def test_phone_forms_are_masked(phone: str) -> None:
    assert mask_phones(phone) == PHONE, phone


@pytest.mark.parametrize("phone", PHONES)
def test_phone_inside_sentence(phone: str) -> None:
    text = f"Звоните {phone}, выходим в субботу."
    cleaned = mask_phones(text)
    assert cleaned == f"Звоните {PHONE}, выходим в субботу.", phone


@pytest.mark.parametrize(
    "phrase",
    [
        "тел: +998901234567",
        "тел.+998901234567",
        "Телефон:+998 90 123 45 67.",
        "WhatsApp +998 90 123 45 67!",
        "(+998 90 123 45 67)",
        "«+998 90 123 45 67»",
    ],
)
def test_phone_with_punctuation_around(phrase: str) -> None:
    assert "998" not in mask_phones(phrase)
    assert PHONE in mask_phones(phrase)


def test_two_phones_in_one_line() -> None:
    text = "Организатор: +998 90 123 45 67 или 93 765 43 21."
    assert mask_phones(text) == f"Организатор: {PHONE} или {PHONE}."


def test_phone_followed_by_number_keeps_the_number() -> None:
    text = "+998 90 123 45 67 2 места осталось"
    assert mask_phones(text) == f"{PHONE} 2 места осталось"


@pytest.mark.parametrize(
    "text",
    [
        "Выход 20.09.2025 в 06:30",
        "2025-09-20 сбор в 6:00",
        "Координаты 41.311081, 69.279737",
        "41.311 69.279",
        "Высота 4200 м, набор 1500 м",
        "Цена 150 000 сум с человека",
        "Такси 90 000 сум туда и обратно",
        "Бюджет 1 500 000 сум",
        "Группа из 15 человек, 2 машины",
        "Температура -15 градусов",
        "Трек 12.5 км, 7 часов",
        "Перевал 3 200 м",
        "Сообщение #119000 в ветке",
        "Через 30-40 минут будем",
        "Маршрут 2025 года, 3 дня",
        "100 000 000 сум — дорого",
    ],
)
def test_no_false_positives(text: str) -> None:
    assert mask_phones(text) == text


def test_emails_masked() -> None:
    assert clean_text("пишите на ivan.petrov+trek@example.com или ivan@mail.ru") == (
        f"пишите на {EMAIL} или {EMAIL}"
    )


def test_mentions_masked_but_chat_and_bots_kept() -> None:
    text = "@ivan_petrov и @MariaK, см. @gorets_uzb и @SayrBot"
    assert clean_text(text, keep={"gorets_uzb"}) == f"{USER} и {USER}, см. @gorets_uzb и @SayrBot"


def test_short_mentions_are_not_usernames() -> None:
    # В Telegram имя пользователя не короче пяти символов; «@ab» и почта трогаться не должны.
    assert clean_text("@ab и @abc") == "@ab и @abc"


def test_tg_links_to_people_masked() -> None:
    text = "пишите мне t.me/ivan_petrov или https://t.me/MariaK."
    assert clean_text(text) == f"пишите мне {USER} или {USER}."


def test_tg_links_to_messages_and_invites_kept() -> None:
    text = (
        "см. https://t.me/gorets_uzb/119000 и t.me/c/123456/789, "
        "вступайте t.me/+AbCdEf123, стикеры t.me/addstickers/pack"
    )
    assert clean_text(text) == text


def test_tg_link_with_phone_masked() -> None:
    assert clean_text("мой контакт t.me/+998901234567") == f"мой контакт {PHONE}"


def test_tg_uri_masked() -> None:
    text = "ссылка tg://user?id=123456 и tg://resolve?domain=ivan_petrov"
    assert clean_text(text) == f"ссылка {USER} и {USER}"


def test_tg_link_to_kept_chat_untouched() -> None:
    text = "наш форум https://t.me/gorets_uzb"
    assert clean_text(text, keep={"@gorets_uzb"}) == text


def test_cleaner_object_and_empty_input() -> None:
    cleaner = TextCleaner(keep=["gorets_uzb"])
    assert cleaner(None) == ""
    assert cleaner("  текст без личного  ") == "текст без личного"
    assert cleaner("@gorets_uzb @someone_else") == f"@gorets_uzb {USER}"


def test_everything_together() -> None:
    text = (
        "Идём на Бельдерсай 20.09 в 6:00, сбор у метро. Вопросы: @ivan_petrov, "
        "+998 90 123 45 67, ivan@mail.ru, t.me/ivan_petrov. Трек: https://t.me/gorets_uzb/118000"
    )
    assert clean_text(text, keep={"gorets_uzb"}) == (
        f"Идём на Бельдерсай 20.09 в 6:00, сбор у метро. Вопросы: {USER}, "
        f"{PHONE}, {EMAIL}, {USER}. Трек: https://t.me/gorets_uzb/118000"
    )
