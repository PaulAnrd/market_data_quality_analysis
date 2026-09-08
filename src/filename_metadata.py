from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_PATTERN = re.compile(r"^(\d{8})_([^_]+)(?:_([^_]+))?(?:_([^_]+))?(?:_([^_]+))?$")


@dataclass(frozen=True)
class FeedMetadata:
    date: str
    product: str
    market: str
    server: str
    source: str
    path: Path

    @property
    def instrument_key(self) -> tuple[str, str]:
        return (self.date, self.product)

    @property
    def label(self) -> str:
        parts = [p for p in (self.market, self.server, self.source) if p]
        return "_".join(parts) if parts else self.path.stem


def parse(path: Path) -> FeedMetadata | None:
    m = _PATTERN.match(path.stem)
    if not m:
        return None
    date, product, market, server, source = m.groups()
    try:
        datetime.strptime(date, "%Y%m%d")
    except ValueError:
        return None
    return FeedMetadata(
        date=date, product=product, market=market or "", server=server or "", source=source or "", path=path,
    )


def group_by_instrument(metas: list[FeedMetadata]) -> dict[tuple[str, str], list[FeedMetadata]]:
    groups: dict[tuple[str, str], list[FeedMetadata]] = {}
    for m in metas:
        groups.setdefault(m.instrument_key, []).append(m)
    return groups
