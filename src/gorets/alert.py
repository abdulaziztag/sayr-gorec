"""Оповещение владельца о сбое службы: systemd OnFailure → `gorets alert <unit>`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from gorets.storage import Repository
from gorets.telegram.deliver import resolve_target


def alert_text(unit: str, repo: Repository | None, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    lines = [f"⚠️ sayr-gorets: служба {unit} завершилась с ошибкой."]
    if repo is not None:
        try:
            for run in repo.recent_runs(5):
                if run.error and now - run.started_at < timedelta(hours=12):
                    lines.append(f"{run.kind}: {run.error[:300]}")
                    break
        except Exception as exc:
            lines.append(f"База недоступна: {exc}")
    lines.append(f"Подробности: journalctl -u {unit} -n 50")
    return "\n".join(lines)


async def send_alert(client: Any, owner: str | int, text: str) -> None:
    target = await resolve_target(client, owner)
    await client.send_message(target, text, parse_mode=None, link_preview=False)
