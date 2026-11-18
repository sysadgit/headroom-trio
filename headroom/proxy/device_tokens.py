"""Per-device access tokens that unlock otherwise-denied models.

The model allow/deny list (:mod:`headroom.proxy.model_allowlist`) blocks a model
for everybody. This module adds an *exception* path: a device that presents a
valid secret token in the ``x-headroom-device-token`` header may use the models
its entry lists under ``allow`` (e.g. ``claude-fable*``).

Design rules (kept deliberately conservative so a bad config can never take the
proxy down or open access wider than before):

* **Opt-in / no-op by default.** If the token file does not exist, nothing
  changes: the allow/deny list behaves exactly as it did before this module.
* **Hashes only.** The file stores SHA-256 fingerprints, never the secrets.
* **Fail closed.** A missing, empty, malformed or expired entry grants nothing.
  A bad file on *reload* keeps the last good list in memory (and logs), so a
  typo while editing cannot silently revoke or grant anyone.
* **Hot reload.** The file's mtime is checked on lookup (a cheap ``stat``), so
  ``revoke`` takes effect on the next request with no restart.
* **Constant-time comparison** of the presented token's hash.
* The secret is never logged.

File format (JSON array)::

    [
      {"name": "anand-macbook",
       "sha256": "<64 hex chars>",
       "allow": ["claude-fable*"],
       "created_at": "2026-11-18T10:00:00Z",
       "expires_at": null}
    ]
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TOKEN_HEADER = "x-headroom-device-token"
TOKEN_FILE_ENV = "HEADROOM_DEVICE_TOKENS_FILE"
TOKEN_PREFIX = "hrt_"
DEFAULT_ALLOW: tuple[str, ...] = ("claude-fable*", "fable*")
_MAX_TOKEN_LENGTH = 256  # ignore absurdly long header values without hashing them


@dataclass(frozen=True)
class Device:
    """One approved device (never contains the secret)."""

    name: str
    sha256: str
    allow: tuple[str, ...]
    created_at: str | None = None
    expires_at: str | None = None

    def is_expired(self, now: datetime | None = None) -> bool:
        if not self.expires_at:
            return False
        parsed = _parse_time(self.expires_at)
        if parsed is None:
            # Unparseable expiry: treat as expired (fail closed).
            return True
        return (now or datetime.now(timezone.utc)) >= parsed

    def permits(self, model: str) -> bool:
        lowered = model.lower()
        return any(fnmatch.fnmatch(lowered, pattern.lower()) for pattern in self.allow)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    """Return a new random secret (~256 bits of entropy)."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def default_token_file() -> Path:
    """Where the token list lives.

    ``HEADROOM_DEVICE_TOKENS_FILE`` wins; otherwise ``$HEADROOM_CONFIG_DIR`` or
    ``$HEADROOM_WORKSPACE_DIR/config`` (the persistent Docker volume), falling
    back to ``~/.headroom/config``.
    """
    explicit = os.environ.get(TOKEN_FILE_ENV, "").strip()
    if explicit:
        return Path(explicit)
    config_dir = os.environ.get("HEADROOM_CONFIG_DIR", "").strip()
    if config_dir:
        return Path(config_dir) / "device_tokens.json"
    workspace = os.environ.get("HEADROOM_WORKSPACE_DIR", "").strip()
    if workspace:
        return Path(workspace) / "config" / "device_tokens.json"
    return Path.home() / ".headroom" / "config" / "device_tokens.json"


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_entries(raw: Any) -> list[Device]:
    if not isinstance(raw, list):
        raise ValueError("device token file must contain a JSON array")
    devices: list[Device] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"entry {index} is not an object")
        name = item.get("name")
        digest = item.get("sha256")
        allow = item.get("allow")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"entry {index}: 'name' must be a non-empty string")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError(f"entry {index} ({name}): 'sha256' must be 64 hex chars")
        try:
            int(digest, 16)
        except ValueError:
            raise ValueError(f"entry {index} ({name}): 'sha256' is not hex") from None
        if not isinstance(allow, list) or not all(isinstance(p, str) for p in allow):
            raise ValueError(f"entry {index} ({name}): 'allow' must be a list of strings")
        created = item.get("created_at")
        expires = item.get("expires_at")
        devices.append(
            Device(
                name=name.strip(),
                sha256=digest.lower(),
                allow=tuple(allow),
                created_at=created if isinstance(created, str) else None,
                expires_at=expires if isinstance(expires, str) else None,
            )
        )
    return devices


class DeviceTokenStore:
    """Read side: look up a presented token against the on-disk list."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path else default_token_file()
        self._lock = threading.Lock()
        self._devices: list[Device] = []
        self._stamp: tuple[int, int] | None = None  # (mtime_ns, size) of last good load
        self._warned_stamp: tuple[int, int] | None = None

    @property
    def path(self) -> Path:
        return self._path

    def _refresh(self) -> None:
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            if self._devices or self._stamp is not None:
                logger.info("event=device_tokens_file_removed path=%s", self._path)
            self._devices = []
            self._stamp = None
            return
        except OSError as exc:
            logger.warning("event=device_tokens_stat_failed path=%s error=%s", self._path, exc)
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp or stamp == self._warned_stamp:
            return
        try:
            with open(self._path, encoding="utf-8") as handle:
                devices = _parse_entries(json.load(handle))
        except (OSError, ValueError) as exc:
            # Keep the previous good list; warn once per file version.
            self._warned_stamp = stamp
            logger.warning(
                "event=device_tokens_reload_failed path=%s error=%s — keeping previous list",
                self._path,
                exc,
            )
            return
        self._devices = devices
        self._stamp = stamp
        self._warned_stamp = None
        logger.info("event=device_tokens_loaded path=%s count=%d", self._path, len(devices))

    def lookup(self, token: str | None) -> Device | None:
        """Return the active device for ``token``, or ``None``."""
        if not token or len(token) > _MAX_TOKEN_LENGTH:
            return None
        with self._lock:
            self._refresh()
            devices = list(self._devices)
        if not devices:
            return None
        presented = hash_token(token.strip())
        match: Device | None = None
        for device in devices:
            # No early exit: compare against every entry.
            if hmac.compare_digest(presented, device.sha256) and match is None:
                match = device
        if match is None or match.is_expired():
            return None
        return match


# ---------------------------------------------------------------------------
# Write side (used by the `headroom token` admin commands)
# ---------------------------------------------------------------------------


def _read_raw(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    _parse_entries(data)  # validate; raises ValueError on a malformed file
    return list(data)


def _write_raw(path: Path, entries: list[dict[str, Any]]) -> None:
    """Atomically replace the file (readers never see a half-written list)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".device_tokens.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(entries, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def add_device(
    name: str,
    *,
    path: Path | str | None = None,
    allow: tuple[str, ...] = DEFAULT_ALLOW,
    days: int | None = None,
) -> str:
    """Create a device entry and return the new secret (shown to the admin once)."""
    name = name.strip()
    if not name:
        raise ValueError("device name must not be empty")
    target = Path(path) if path else default_token_file()
    entries = _read_raw(target)
    if any(e.get("name") == name for e in entries):
        raise ValueError(f"device {name!r} already exists; revoke it first to re-issue")
    token = generate_token()
    now = datetime.now(timezone.utc)
    entries.append(
        {
            "name": name,
            "sha256": hash_token(token),
            "allow": list(allow),
            "created_at": _utc_iso(now),
            "expires_at": _utc_iso(now + timedelta(days=days)) if days else None,
        }
    )
    _write_raw(target, entries)
    return token


def revoke_device(name: str, *, path: Path | str | None = None) -> bool:
    """Remove a device. Returns ``False`` if it was not present."""
    target = Path(path) if path else default_token_file()
    entries = _read_raw(target)
    kept = [e for e in entries if e.get("name") != name.strip()]
    if len(kept) == len(entries):
        return False
    _write_raw(target, kept)
    return True


def list_devices(*, path: Path | str | None = None) -> list[Device]:
    target = Path(path) if path else default_token_file()
    return _parse_entries(_read_raw(target)) if target.exists() else []
