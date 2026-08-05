"""Model allow/deny list for the Anthropic ``/v1/messages`` endpoint.

Sibling mechanism to :mod:`headroom.proxy.model_router`: where that module
*rewrites* the outgoing model per rule, this one *rejects* a request outright
when its model doesn't clear an allow/deny glob check. Same env-var-driven,
fail-open, unit-testable shape.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os

from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelAllowlistConfig:
    """Configuration for :class:`ModelAllowlist`. Disabled by default."""

    enabled: bool = False
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()

    @classmethod
    def from_env(
        cls, enabled_raw: str | None, allow_raw: str | None, deny_raw: str | None
    ) -> ModelAllowlistConfig:
        """Build config from env-style strings, failing open to disabled.

        ``allow_raw``/``deny_raw`` are each either a JSON array of glob
        patterns (e.g. ``["claude-*"]``) or a path to a file containing one.
        A malformed value logs a warning and drops just that list rather than
        raising or disabling the whole proxy.
        """
        enabled = _truthy(enabled_raw)
        allow = _parse_patterns(allow_raw, "allow")
        deny = _parse_patterns(deny_raw, "deny")
        if enabled and not allow and not deny:
            logger.warning("model allowlist enabled but no allow/deny patterns configured; disabling")
            return cls(enabled=False)
        return cls(enabled=enabled and bool(allow or deny), allow=allow, deny=deny)


class ModelAllowlist:
    """Checks whether a model name may pass, per configured glob patterns."""

    def __init__(self, config: ModelAllowlistConfig | None) -> None:
        self._config = config or ModelAllowlistConfig()

    @property
    def enabled(self) -> bool:
        return self._config.enabled and bool(self._config.allow or self._config.deny)

    def check(self, model: str) -> str | None:
        """Return ``None`` if ``model`` is allowed, else a rejection reason."""
        if not self.enabled:
            return None
        if not isinstance(model, str) or not model:
            return None

        for pattern in self._config.deny:
            if fnmatch.fnmatch(model.lower(), pattern.lower()):
                return (
                    f"The model {model!r} is not supported by your organization. "
                    "Please contact your administrator for queries."
                )

        if self._config.allow and not any(
            fnmatch.fnmatch(model.lower(), pattern.lower()) for pattern in self._config.allow
        ):
            return (
                f"The model {model!r} is not supported by your organization. "
                "Please contact your administrator for queries."
            )

        return None


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on", "enable", "enabled"}


def _parse_patterns(raw: str | None, label: str) -> tuple[str, ...]:
    if not raw or not raw.strip():
        return ()
    try:
        if os.path.isfile(raw):
            with open(raw, encoding="utf-8") as f:
                parsed = json.load(f)
        else:
            parsed = json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("invalid model allowlist %s patterns; ignoring: %s", label, exc)
        return ()
    if not isinstance(parsed, list) or not all(isinstance(p, str) for p in parsed):
        logger.warning("model allowlist %s patterns must be a JSON array of strings; ignoring", label)
        return ()
    return tuple(parsed)
