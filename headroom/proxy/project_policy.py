"""Pure project attribution policy helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from headroom.proxy.savings_tracker import sanitize_project_name

PROJECT_HEADER = "x-headroom-project"
PROJECT_PATH_PREFIX = "/p/"


def classify_project(headers: Mapping[str, Any] | Any) -> str | None:
    """Extract a sanitized project name from request headers, if present."""
    get = getattr(headers, "get", None)
    if get is None:
        return None
    value = get(PROJECT_HEADER) or get("X-Headroom-Project")
    return sanitize_project_name(value)


def is_team_tagged_project(project: str | None) -> bool:
    """True when ``project`` follows the required ``team:name`` shape.

    Examples: ``developer:Bharathi_Ms``, ``ceo:vignesh``, ``pm:gopi``. Both
    sides of the colon must be non-blank so a bare ``"team:"`` or ``":name"``
    doesn't count as attributed.
    """
    if not project:
        return False
    team, sep, name = project.partition(":")
    return bool(sep) and bool(team.strip()) and bool(name.strip())


def split_project_path(path: str) -> tuple[str | None, str]:
    """Split ``/p/<name>/rest`` into ``(name, /rest)``."""
    if not path.startswith(PROJECT_PATH_PREFIX):
        return None, path
    remainder = path[len(PROJECT_PATH_PREFIX) :]
    segment, sep, rest = remainder.partition("/")
    project = sanitize_project_name(unquote(segment)) if segment else None
    if project is None:
        return None, path
    return project, ("/" + rest) if sep else "/"


def with_project_prefix(base_url: str, project: str | None) -> str:
    """Insert ``/p/<name>`` ahead of the path of a local proxy base URL."""
    name = sanitize_project_name(project)
    if name is None:
        return base_url
    parts = urlsplit(base_url)
    prefixed = f"{PROJECT_PATH_PREFIX}{quote(name, safe='')}{parts.path}"
    return urlunsplit(parts._replace(path=prefixed.rstrip("/")))
