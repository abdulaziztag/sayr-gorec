import pytest

from gorets.catalog import CatalogError, Place, catalog_text, fetch_catalog, parse_places

RU = [
    {
        "slug": "beldersay",
        "name": "Бельдерсай",
        "category": "peak",
        "region_name": "Ташкентская",
        "lat": 41.42,
        "lng": 70.02,
        "elevation_m": 2000,
    },
    {
        "slug": "chimgan",
        "name": "Большой Чимган",
        "category": "peak",
        "region_name": "Ташкентская",
        "lat": "41.5",
        "lng": "70.0",
        "elevation_m": None,
    },
    {"name": "без slug"},
]
UZ = [{"slug": "beldersay", "name": "Beldersoy"}]


def test_parse_places_merges_uz_names() -> None:
    places = parse_places(RU, UZ)
    assert [p.slug for p in places] == ["beldersay", "chimgan"]
    assert places[0].name_uz == "Beldersoy"
    assert places[1].name_uz is None
    assert places[1].lat == 41.5 and places[1].elevation_m is None
    assert places[0].url == "https://sayr.info/p/beldersay"


def test_parse_places_accepts_wrapped_payload() -> None:
    assert len(parse_places({"items": RU})) == 2
    with pytest.raises(CatalogError):
        parse_places({"nope": 1})


def test_fetch_catalog_retries_and_tolerates_missing_uz() -> None:
    calls: list[str] = []

    def fetcher(url: str):
        calls.append(url)
        if "lang=uz" in url:
            raise OSError("нет uz")
        if len(calls) == 1:
            raise OSError("сеть моргнула")
        return RU

    places = fetch_catalog(
        "https://sayr.info/api/v1/places", fetcher=fetcher, sleep=lambda _s: None
    )
    assert [p.slug for p in places] == ["beldersay", "chimgan"]
    assert calls[0] == calls[1] == "https://sayr.info/api/v1/places?limit=1000"
    assert calls[2] == "https://sayr.info/api/v1/places?limit=1000&lang=uz"


def test_fetch_catalog_gives_up() -> None:
    def fetcher(url: str):
        raise OSError("сервер лежит")

    with pytest.raises(CatalogError, match="сервер лежит"):
        fetch_catalog("https://sayr.info/api/v1/places", fetcher=fetcher, sleep=lambda _s: None)
    with pytest.raises(CatalogError, match="пуст"):
        fetch_catalog("https://x", fetcher=lambda url: [], sleep=lambda _s: None)


def test_catalog_text() -> None:
    text = catalog_text([Place("chimgan", "Чимган", "Chimyon", "peak", "Ташкентская")])
    assert text.splitlines()[1] == "chimgan | Чимган | Chimyon | peak | Ташкентская"


def test_place_linker() -> None:
    from gorets.catalog import PlaceLinker

    linker = PlaceLinker(
        [
            Place("beldersay", "Бельдерсай", "Beldersoy"),
            Place("big-chimgan", "Большой Чимган", "Katta Chimyon"),
            Place("pulatkhan", "Плато Пулатхан", "Pulatxon"),
            Place("urungach", "Озеро Урунгач", "Urungach ko'li"),
        ]
    )
    assert linker.link("Бельдерсай") == "beldersay"
    assert linker.link("гора Бельдерсай") == "beldersay"
    assert linker.link("Beldersoy") == "beldersay"
    assert linker.link("Большой Чимган") == "big-chimgan"
    assert linker.link("Пулатхан") == "pulatkhan"
    assert linker.link("озёра Урунгач") == "urungach"
    assert (
        linker.link("Урунгачские озёра") is None or linker.link("Урунгачские озёра") == "urungach"
    )
    assert linker.link("Ташкент") is None
    assert linker.link("") is None and linker.link(None) is None
