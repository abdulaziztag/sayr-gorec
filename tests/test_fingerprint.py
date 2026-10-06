from gorets.fingerprint import normalize_for_fingerprint, text_fingerprint

AFISHA = "Поход на Большой Чимган 12 октября! Сбор в 6:00 у метро Буюк Ипак Йули. Цена 150 000 сум."


def test_same_text_different_formatting_same_fingerprint() -> None:
    a = text_fingerprint(AFISHA)
    b = text_fingerprint(
        "поход на большой   чимган 12 октября,\nсбор в 6:00 у метро буюк ипак йули... "
        "цена 150 000 сум"
    )
    assert a is not None and a == b
    assert text_fingerprint(AFISHA + " https://t.me/x/1") == a  # ссылка не меняет отпечаток


def test_short_texts_have_no_fingerprint() -> None:
    assert text_fingerprint("Спасибо!") is None
    assert text_fingerprint(None) is None
    assert normalize_for_fingerprint("Ёлка, ёж!") == "елка еж"


def test_different_texts_differ() -> None:
    assert text_fingerprint(AFISHA) != text_fingerprint(AFISHA.replace("12 октября", "19 октября"))
