"""Обезличивание авторов.

В базе нет id пользователей Telegram — только HMAC-SHA256 от id с секретом
из `.env`. Простой хеш числового id подбирается перебором за минуты, HMAC с
секретом — нет. Тот же HMAC использует `gorets forget-author`, чтобы найти
и удалить всё написанное автором по его id.
"""

from __future__ import annotations

import hashlib
import hmac


def author_hash(secret: str, peer_id: int, kind: str = "user") -> str:
    """HMAC-SHA256 от id автора.

    `kind` отделяет пользователей от каналов и чатов, пишущих от своего имени
    (анонимные админы, посты каналов): у них свои пространства id.
    """
    if not secret:
        raise ValueError("Не задан секрет GORETS_AUTHOR_HMAC_SECRET")
    subject = str(int(peer_id)) if kind == "user" else f"{kind}:{int(peer_id)}"
    return hmac.new(secret.encode("utf-8"), subject.encode("utf-8"), hashlib.sha256).hexdigest()
