"""Typer commands for explicit sessions and harness integrations."""

from pathlib import Path
from typing import Annotated

import click
import typer
from typer.core import TyperGroup

from agent_boundary import boundary, session
from agent_boundary.claude import statusline
from agent_boundary.claude.hooks import pre_tool_use, session_start
from agent_boundary.cli.run import CONTEXT_SETTINGS
from agent_boundary.cli.run import command as run_command
from agent_boundary.cli.session import require_dir, write
from agent_boundary.paths import profiles_dir


class _CommandGroup(TyperGroup):
    resolve_command = click.Group.resolve_command

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted(super().list_commands(ctx))


app = typer.Typer(
    cls=_CommandGroup,
    help="Create, inspect, and run OS-enforced agent boundaries.",
    invoke_without_command=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
sessions = typer.Typer(
    cls=_CommandGroup, help="Create explicit boundary sessions.", no_args_is_help=True, add_completion=False
)
claude = typer.Typer(
    cls=_CommandGroup, help="Run Claude Code integrations.", no_args_is_help=True, add_completion=False
)
hooks = typer.Typer(cls=_CommandGroup, help="Run a Claude Code hook event.", no_args_is_help=True, add_completion=False)
for group in (sessions, claude, hooks):
    group.callback()(lambda: None)
app.add_typer(sessions, name="session")
app.add_typer(claude, name="claude")
claude.add_typer(hooks, name="hook")
claude.command("statusline")(statusline.command)
hooks.command("PreToolUse")(pre_tool_use.command)
hooks.command("SessionStart")(session_start.command)
app.command("run", context_settings=CONTEXT_SETTINGS)(run_command)


@app.callback()
def main(
    ctx: typer.Context,
    session_dir: Annotated[
        Path | None,
        typer.Option(
            "--session-dir",
            envvar="AGENT_BOUNDARY_SESSION_DIR",
            help="Session directory to inspect or modify.",
            file_okay=False,
        ),
    ] = None,
) -> None:
    """Create, inspect, and run OS-enforced agent boundaries."""
    if session_dir is None:
        session_dir = session.current_dir()
    ctx.obj = session_dir.expanduser().resolve() if session_dir else None
    if ctx.invoked_subcommand is None:
        status(ctx)


@app.command("status")
def status(ctx: typer.Context) -> None:
    """Show the current session's profile and state."""
    config = session.read(require_dir(ctx))
    typer.echo(session.describe(config) if config else "no boundary for this session; run: agent-boundary reload")


@app.command("list")
def list_profiles() -> None:
    """List available boundary profiles."""
    typer.echo("\n".join(boundary.profile_names(profiles_dir())) or "(none)")


@app.command("on")
def on(ctx: typer.Context) -> None:
    """Enable boundary enforcement for this session."""
    current = session.read(require_dir(ctx))
    write(ctx, current.profile if current else "default", "on")


@app.command("off")
def off(ctx: typer.Context) -> None:
    """Return this session to its harness's normal permissions."""
    current = session.read(require_dir(ctx))
    write(ctx, current.profile if current else "default", "off")


@app.command("reload")
def reload(ctx: typer.Context) -> None:
    """Regenerate this session's pinned policy from its profile."""
    current = session.read(require_dir(ctx))
    write(ctx, current.profile if current else "default", current.state if current else "on")


@app.command("use")
def use(ctx: typer.Context, profile: Annotated[str, typer.Argument(help="Profile to activate.")]) -> None:
    """Switch profile and regenerate this session's pinned policy."""
    current = session.read(require_dir(ctx))
    write(ctx, profile, current.state if current else "on")


@sessions.command("create")
def create_session(
    directory: Annotated[Path, typer.Argument(help="Directory to create for this session.")],
    profile: Annotated[str, typer.Option(help="Profile to pin.")] = "default",
    workdir: Annotated[Path | None, typer.Option(help="Working directory protected by the session.")] = None,
) -> None:
    """Create a session with a pinned profile and policy."""
    try:
        config, warnings = session.write(directory, profile, "on", workdir=workdir or Path.cwd())
    except boundary.PROFILE_ERRORS as error:
        typer.echo(f"agent-boundary: {error}", err=True)
        raise typer.Exit(1) from error
    typer.echo(str(directory.expanduser().resolve()))
    typer.echo(session.describe(config))
    for warning in warnings:
        typer.echo(f"agent-boundary: {warning}", err=True)
