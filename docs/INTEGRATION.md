# sayr-gorets: справка для соседних проектов

Документ для разработчика или агента, который подключает к своему проекту
данные форумов «ГОРЕЦ» и «Горняшка». Здесь всё, что нужно, чтобы читать
данные, и список вопросов, на которые хочется получить отзыв.

## Что это

`sayr-gorets` — служба на том же VPS, что и сервер Sayr. Раз в сутки она
читает два туристических Telegram-форума Узбекистана (`@gorets_uzb`,
`@hikinguz`), складывает сообщения в свою базу Postgres и прогоняет их через
Claude по заданным схемам. Наружу отдаёт:

- сырые сообщения по фидам (чат плюс ветки) с курсором;
- **извлечения** — структурированные данные по сообщениям: туры из афиш,
  объявления попутчиков, состояние троп, упоминания мест, вопросы участников;
- календарь выходов (туры и попутчики) в JSON и iCal;
- места каталога Sayr по числу упоминаний и всё извлечённое о месте;
- темы вопросов со счётчиками (бэклог приложения);
- недельные отчёты (Markdown и JSON);
- полнотекстовый поиск.

Авторы в данных — только хеши, личных данных нет. Исключение — ветки афиш и
поиска попутчиков: там телефоны и ники сохраняются, это контакты предложений.

## Подключение

- Адрес: `http://127.0.0.1:8790` с этого же сервера или
  `https://gorets.sayr.info` снаружи (после установки `deploy/nginx-gorets.conf`).
- Токен: `GORETS_API_TOKEN` из `/root/Projects/sayr-gorets/.env`.
  Заголовок `Authorization: Bearer <токен>` или `X-API-Token: <токен>`.
- `GET /health` без токена: `{"ok": true, "collect_fresh": true, "runs": {...}}`.
  `collect_fresh` = false, если ночной сбор не проходил больше 36 часов.

```python
import httpx

gorets = httpx.Client(base_url="http://127.0.0.1:8790",
                      headers={"Authorization": f"Bearer {GORETS_API_TOKEN}"}, timeout=30)
```

## Списки, курсоры, дедупликация

Все списки отвечают одинаково:

```json
{"items": [...], "next_cursor": 118423, "has_more": true}
```

Потребитель хранит `next_cursor` и передаёт его в `after` при следующем
запросе. Курсор у сообщений — `msg_id` внутри чата, у извлечений — `id`
записи. Так ничего не теряется и не дублируется при любых сбоях. `limit` до
500, по умолчанию 100. `since`/`until` — ISO-даты.

У сообщений есть `fingerprint`: одинаковый текст в двух чатах даёт один
отпечаток. `?dedupe=true` в фидах пропускает копии, у которых есть более
ранний оригинал где угодно; `GET /messages/{chat}/{msg_id}/duplicates`
показывает копии.

## Сообщение

```json
{
  "chat_id": 1234567890, "msg_id": 118423,
  "date": "2026-10-06T05:40:12+00:00",
  "topic_id": 4521, "topic_title": "АФИША ПОХОДОВ",
  "reply_to_msg_id": null,
  "author": "3f9c…64 hex",
  "text": "Выезд на Бельдерсай 12 октября…",
  "media_type": "photo",            // photo|video|document|gpx|kml|voice|sticker|poll|location|venue|other|null
  "file_name": null, "lat": null, "lng": null,
  "forwarded_from": null, "edited_at": null,
  "fingerprint": "a1b2…",
  "link": "https://t.me/gorets_uzb/118423"
}
```

Картинки не скачиваются: афиша, которая была только картинкой, приходит с
пустым `text` и `media_type = "photo"`.

## Фиды

`GET /feeds` — список. Сейчас описаны (см. `gorets.toml`):

| Фид | Чат | Ветка | Контакты |
|---|---|---|---|
| `afisha` | gorets_uzb | АФИША ПОХОДОВ | сохраняются |
| `companions` | gorets_uzb | ПОИСК ПОПУТЧИКОВ | сохраняются |
| `dispatch` | gorets_uzb | ДИСПЕТЧЕРСКАЯ | нет |
| `routes` | gorets_uzb | Описания и нюансы популярных маршрутов | нет |
| `weather` | gorets_uzb | ПРОГНОЗ ПОГОДЫ | нет |
| `chat` | gorets_uzb | ЧАТ | нет |
| `companions_hikinguz` | hikinguz | Кто куда? | сохраняются |
| `dispatch_hikinguz` | hikinguz | Диспетчерская | нет |
| `reports_hikinguz` | hikinguz | Блоги и отчёты о походах | нет |
| `tracks_hikinguz` | hikinguz | Файло и трекохранилище | нет |

`GET /feeds/{name}/messages?after=&since=&until=&q=&dedupe=&limit=`.

## Извлечения

`GET /extractors` — список с JSON-схемами и счётчиками. Запись извлечения:

```json
{
  "id": 9812, "extractor": "afisha_tour",
  "chat_id": 1234567890, "msg_id": 118423,
  "message_date": "2026-10-06T05:40:12+00:00",
  "topic_id": 4521, "topic_title": "АФИША ПОХОДОВ",
  "status": "ok",                  // ok — data заполнен; skipped — не по теме; error
  "data": {...},                   // по схеме извлекателя
  "model": "claude-haiku-4-5-20251001",
  "created_at": "…",
  "link": "https://t.me/gorets_uzb/118423"
}
```

`GET /extractions/{name}?after=&since=&until=&status=&limit=`; `status` по
умолчанию `ok`, можно `skipped`, `error`, `all`.

Поля `data` по извлекателям (полные схемы — в `/extractors`):

- **afisha_tour** (фид `afisha`): `title`, `organizer`, `contact`, `place`,
  `place_slug`, `region`, `date_start`, `date_end` (ГГГГ-ММ-ДД или null),
  `days`, `price` (число), `currency`, `difficulty`, `includes[]`,
  `transport`, `summary`.
- **companion_request** (`companions`, `companions_hikinguz`): `place`,
  `place_slug`, `date_start`, `date_end`, `group_size`, `has_car`,
  `difficulty`, `contact`, `summary`.
- **trail_condition** (`dispatch`, `dispatch_hikinguz`, `chat`,
  `reports_hikinguz`): `place`, `place_slug`, `kind` (snow | water | road |
  closure | danger | weather | other), `text`, `observed_on`, `severity`
  (info | caution | danger).
- **place_mentions** (`chat`, `routes`, `reports_hikinguz`, `dispatch*`):
  `places[]` из `{name, context, slug}`.
- **user_question** (`chat`, `routes`, `companions*`): `theme` (transport |
  route | gear | weather | permits | water | stay | safety | companions |
  app | other), `question`, `place`, `place_slug`.

`place_slug` / `slug` — slug каталога Sayr (`https://sayr.info/p/<slug>`),
проставляется программой по названию без модели: точное совпадение с
русским или узбекским названием, затем «ядро» без слов «гора», «озеро»,
«перевал», затем вхождение. Сленг («Бельдер», «Чимганка») не узнаётся, slug
остаётся null.

Извлечения хранятся бессрочно, даже когда сырое сообщение удалено по сроку
(90 дней). Одно сообщение разбирается одним извлекателем один раз.

## Календарь выходов

`GET /events?from=ГГГГ-ММ-ДД&to=&kind=tour|companions&since=`:

```json
{"items": [{
  "kind": "tour", "title": "Бельдерсай за день", "place": "Бельдерсай",
  "place_slug": "beldersay", "date_start": "2026-10-12", "date_end": null,
  "price": 300000, "currency": "сум", "organizer": "…", "contact": "+998…",
  "summary": "…", "link": "https://t.me/gorets_uzb/118423",
  "chat_id": 1234567890, "msg_id": 118423, "posted_at": "…",
  "sources": ["https://t.me/gorets_uzb/118423", "https://t.me/hikinguz/5120"]
}]}
```

Одна афиша в двух чатах — одно событие с двумя `sources` (схлопывание по
месту и дате начала). `GET /events.ics` — то же в iCalendar.

## Места и вопросы

- `GET /places/top?since=&limit=` → `[{"slug": "beldersay", "mentions": 41}]`.
- `GET /places/{slug}/mentions?since=&limit=` → извлечения, где место
  привязано к slug: состояние троп, туры, вопросы, упоминания.
- `GET /questions/themes?since=` → `[{"theme": "transport", "count": 38,
  "examples": [{"question": "…", "link": "…"}], "places": [{"slug", "count"}]}]`.
- `GET /stats/authors?feed=chat&since=&limit=` → самые активные и самые
  «отвечаемые» хеши авторов.

## Поиск и отчёты

- `GET /search?q=чимган -поход&feed=&since=&until=&limit=` — полнотекстовый
  поиск с русской морфологией, синтаксис websearch.
- `GET /digests` и `GET /digests/2026-W40?format=json|md` — недельные отчёты.
  JSON отчёта: `headline`, `places[]`, `new_places[]`, `trips[]`,
  `questions[]`, `sayr`, `stats`, `usage`.

## Вебхуки (по запросу)

Служба `gorets watch` может слать новые сообщения фида сразу: в `gorets.toml`
у фида задаются `webhook_url` и `webhook_secret`. Приходит `POST` с JSON
`{"event": "message" | "message_edited", "feed": "afisha", "message": {…}}`
и заголовками `X-Gorets-Event`, `X-Gorets-Feed`,
`X-Gorets-Signature: sha256=<HMAC-SHA256 тела по секрету>`. Проверка:

```python
import hmac, hashlib
def verify(secret: str, body: bytes, header: str) -> bool:
    return hmac.compare_digest("sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(), header)
```

Сейчас слушатель выключен; включается, когда потребитель готов принимать.

## MCP для агентов

`/root/Projects/sayr-gorets/.venv/bin/gorets mcp` — MCP-сервер (stdio) с
инструментами `list_feeds`, `feed_messages`, `search_messages`,
`list_extractors`, `extractions`, `events`, `places_top`, `place_mentions`,
`question_themes`, `digest`. Для Claude Code:

```json
{"mcpServers": {"gorets": {"command": "/root/Projects/sayr-gorets/.venv/bin/gorets", "args": ["mcp"]}}}
```

## Ограничения, о которых стоит знать

- Данные обновляются раз в сутки (04:00 по Ташкенту); извлечения — тем же
  прогоном. Реальное время — только через вебхуки.
- Сырые сообщения хранятся 90 дней, извлечения и отчёты — бессрочно.
- Сейчас в базе сообщения с 8 июля 2026, извлечения по афишам и попутчикам
  — полностью за этот период, по остальным — по 3000 последних.
- Извлечения делает модель: даты и цены иногда не распознаны (null), место
  может быть названо не так, как в каталоге. Поле `status = skipped` значит
  «модель сочла сообщение не по теме».
- Одно сообщение может быть и туром в афише, и вопросом: извлекатели
  независимы.

## На что хочется получить отзыв

1. Хватает ли полей в `afisha_tour` и `companion_request` для агрегатора?
   Чего не хватает: длительность в часах, точка сбора, время старта, тип
   транспорта, возрастные ограничения, язык группы?
2. Нужен ли отдельный признак «актуально» (дата старта в будущем) и
   «отменено/перенесено» по последующим сообщениям в той же ветке?
3. Удобен ли формат `events` или лучше читать `extractions` напрямую?
4. Нужны ли вебхуки сразу или суточной задержки достаточно?
5. Нужна ли картинка афиши (сейчас не скачивается) и в каком виде: ссылка на
   файл на этом сервере, base64, разбор картинки моделью в текст?
6. Какие места из каталога не узнаются (`place_slug = null` при очевидном
   названии)? Список синонимов добавляется легко.
7. Формат ошибок, лимиты, пагинация — что мешает?

Отзыв можно оставить issue в репозитории `abdulaziztag/sayr-gorec` или
просто списком: новые поля добавляются правкой `gorets.toml`, без кода.
