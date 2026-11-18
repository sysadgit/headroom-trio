"""Admin commands for per-device access tokens (``headroom token ...``).

Run these where the proxy's persistent volume is mounted, e.g. on the server::

    docker exec <container> headroom token add anand-macbook
"""

from __future__ import annotations

import click

from .main import main


def _path_option(func):
    return click.option(
        "--file",
        "file_path",
        type=click.Path(dir_okay=False),
        default=None,
        help="Token file (default: $HEADROOM_DEVICE_TOKENS_FILE or the workspace config dir).",
    )(func)


@main.group()
def token() -> None:
    """Manage per-device tokens that unlock denied models (e.g. Fable)."""


@token.command("add")
@click.argument("name")
@click.option(
    "--allow",
    "allow",
    multiple=True,
    help="Model glob this device may use (repeatable). Default: claude-fable*, fable*.",
)
@click.option("--days", type=click.IntRange(min=1), default=None, help="Expire after N days.")
@_path_option
def token_add(name: str, allow: tuple[str, ...], days: int | None, file_path: str | None) -> None:
    """Create a token for device NAME. The secret is shown ONCE."""
    from headroom.proxy import device_tokens

    try:
        secret = device_tokens.add_device(
            name,
            path=file_path,
            allow=allow or device_tokens.DEFAULT_ALLOW,
            days=days,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Device : {name}")
    click.echo(f"Token  : {secret}")
    click.echo("")
    click.echo("Store this in 1Password now. It cannot be shown again;")
    click.echo("only its SHA-256 fingerprint is kept on the server.")


@token.command("list")
@_path_option
def token_list(file_path: str | None) -> None:
    """List approved devices (names and permissions only, never secrets)."""
    from headroom.proxy import device_tokens

    try:
        devices = device_tokens.list_devices(path=file_path)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if not devices:
        click.echo("No devices.")
        return
    for device in devices:
        state = "EXPIRED" if device.is_expired() else "active"
        click.echo(
            f"{device.name:<28} {state:<8} allow={','.join(device.allow)} "
            f"created={device.created_at or '-'} expires={device.expires_at or 'never'}"
        )


@token.command("revoke")
@click.argument("name")
@_path_option
def token_revoke(name: str, file_path: str | None) -> None:
    """Remove device NAME. Takes effect on the next request, no restart."""
    from headroom.proxy import device_tokens

    try:
        removed = device_tokens.revoke_device(name, path=file_path)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if not removed:
        raise click.ClickException(f"No device named {name!r}.")
    click.echo(f"Revoked {name}.")
