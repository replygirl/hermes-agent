"""Fetch the public petdex manifest.

``https://petdex.dev/api/manifest`` 307-redirects to a JSON document on R2:

    {
      "generatedAt": "...",
      "total": 2926,
      "pets": [
        {"slug": "boba", "displayName": "Boba", "kind": "creature",
         "submittedBy": "railly",
         "spritesheetUrl": "https://assets.petdex.dev/.../spritesheet.webp",
         "petJsonUrl": "https://assets.petdex.dev/.../pet.json",
         "zipUrl": "https://assets.petdex.dev/.../boba.zip"},
        ...
      ]
    }

Read-only and unauthenticated; no credentials involved.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MANIFEST_URL = "https://petdex.dev/api/manifest"

_DEFAULT_TIMEOUT = 20.0


@dataclass(frozen=True)
class ManifestEntry:
    """A single pet's row in the manifest."""

    slug: str
    display_name: str
    kind: str
    submitted_by: str
    spritesheet_url: str
    pet_json_url: str
    zip_url: str

    @classmethod
    def from_dict(cls, data: dict) -> "ManifestEntry":
        return cls(
            slug=str(data.get("slug", "")).strip(),
            display_name=str(data.get("displayName", "") or data.get("slug", "")),
            kind=str(data.get("kind", "") or "pet"),
            submitted_by=str(data.get("submittedBy", "") or ""),
            spritesheet_url=str(data.get("spritesheetUrl", "") or ""),
            pet_json_url=str(data.get("petJsonUrl", "") or ""),
            zip_url=str(data.get("zipUrl", "") or ""),
        )


class ManifestError(RuntimeError):
    """Raised when the manifest can't be fetched or parsed."""


def fetch_manifest(*, timeout: float = _DEFAULT_TIMEOUT) -> list[ManifestEntry]:
    """Return every approved pet from the public manifest.

    Follows the 307 redirect to R2.  Raises :class:`ManifestError` on any
    network/parse failure so callers can surface a clean message.
    """
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - httpx is a core dep
        raise ManifestError("httpx is required to fetch the petdex manifest") from exc

    try:
        resp = httpx.get(
            MANIFEST_URL,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "hermes-agent-petdex"},
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 - normalize to one error type
        raise ManifestError(f"could not fetch petdex manifest: {exc}") from exc

    pets = payload.get("pets") if isinstance(payload, dict) else None
    if not isinstance(pets, list):
        raise ManifestError("petdex manifest had no 'pets' array")

    entries: list[ManifestEntry] = []
    for raw in pets:
        if not isinstance(raw, dict):
            continue
        entry = ManifestEntry.from_dict(raw)
        if entry.slug and entry.spritesheet_url:
            entries.append(entry)
    return entries


def find_entry(slug: str, *, timeout: float = _DEFAULT_TIMEOUT) -> ManifestEntry | None:
    """Return the manifest entry for *slug*, or ``None`` if not listed."""
    slug = slug.strip().lower()
    for entry in fetch_manifest(timeout=timeout):
        if entry.slug.lower() == slug:
            return entry
    return None
