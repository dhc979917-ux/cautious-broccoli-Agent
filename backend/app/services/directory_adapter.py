"""Optional organization-directory integration boundary; no identity source is enabled."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DirectoryIdentity:
    source: str
    external_id: str
    account: str
    display_name: str
    department_code: str | None = None
    departed: bool = False


@dataclass(frozen=True)
class DirectoryPage:
    identities: tuple[DirectoryIdentity, ...]
    next_cursor: str | None


class DirectoryAdapter(Protocol):
    async def fetch_page(self, cursor: str | None = None) -> DirectoryPage:
        ...
