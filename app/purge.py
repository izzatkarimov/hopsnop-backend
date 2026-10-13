"""The retention purge as a command: ``python -m app.purge``.

Removes what ``app.services.retention`` says is of no use any more. What that
is, is decided by the settings, the same ones the application runs with; the
command prints the ones it found, so that a run under settings nobody meant
does not go unnoticed.

Run as it is, it removes nothing: it counts what it would remove and says
so. To remove, it has to be told to, and told which database it is meant
for and on which server::

    python -m app.purge --execute --database hopsnop --host db.internal:5432

The name is compared with the database the connection has actually reached,
and the host and port with those the connection was made to. If either
differs the command refuses and changes nothing: a ``DATABASE_URL`` that
points somewhere else than its user believes is found out before anything is
deleted there. The name alone would not do: a development database and the
production one are easily both called ``hopsnop``.

It ends with 0 when it has finished, also when there was nothing to remove;
with 1 when it failed part of the way, in which case what it removed before
that stays removed and is printed; and with 2 when it refused to begin.
"""

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.core.config import settings
from app.db.session import SessionLocal, engine
from app.services import retention

_WIDTH = 30
# The port PostgreSQL is reached on when the URL names none.
_DEFAULT_PORT = 5432


def _server() -> str:
    """Where the connection is made to, as ``host:port``.

    Taken from the URL the engine was built from, which is what a wrong
    ``DATABASE_URL`` gets wrong. Nothing else of the URL is used: not the
    user, not the password. A URL without a host connects through the local
    socket, and is called ``local`` here.
    """
    url = engine.url
    return f"{(url.host or 'local').lower()}:{url.port or _DEFAULT_PORT}"


def _line(name: str, value: object) -> None:
    print(f"  {name:<{_WIDTH}}{value}")


def _print_settings(database: str) -> None:
    """What this run goes by. No secret and no password is among it."""
    _line("database", f"{database} on {_server()}")
    _line("environment", settings.environment)
    _line("rate-limit window", f"{settings.rate_limit_window_minutes} minutes")
    _line("expired stories kept", f"{settings.expired_story_retention_hours} hours")
    _line("ended sessions kept", f"{settings.ended_session_retention_days} days")
    _line("spent tokens kept", f"{settings.spent_token_retention_days} days")
    _line("finished counters kept", f"{settings.rate_limit_retention_hours} hours")
    _line(
        "unverified accounts",
        f"removed after {settings.unverified_account_retention_days} days"
        if settings.purge_unverified_accounts
        else "not removed (PURGE_UNVERIFIED_ACCOUNTS is off)",
    )
    _line("batch size", settings.purge_batch_size)


def _print_counts(report: retention.Report, heading: str) -> None:
    print(heading)
    for category, removed in report.as_dict().items():
        print(f"  {category.replace('_', ' '):<{_WIDTH}}{removed:>8}")
    print(f"  {'total':<{_WIDTH}}{report.total:>8}")


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.purge",
        description="Remove rows that are of no use any more (see README, "
        "Retention). Without --execute nothing is removed.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="remove the rows; without this they are only counted",
    )
    parser.add_argument(
        "--database",
        metavar="NAME",
        help="the name of the database that is meant; the command refuses "
        "if it is connected to another. Required with --execute",
    )
    parser.add_argument(
        "--host",
        metavar="HOST:PORT",
        help="the server that database is meant to be on, as the connection "
        "is made to it; the command refuses if it is connected to another. "
        "Required with --execute",
    )
    arguments = parser.parse_args(argv)
    if arguments.execute and not (arguments.database and arguments.host):
        parser.error(
            "--execute needs --database NAME and --host HOST:PORT, the "
            "database that is meant and the server it is on"
        )
    return arguments


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    now = datetime.now(timezone.utc)
    mode = "" if arguments.execute else " (dry run: nothing is removed)"
    print(f"Retention purge at {now.isoformat(timespec='seconds')}{mode}")
    try:
        with SessionLocal() as db:
            # Asked of the server, not read from the URL: it is where the
            # connection has arrived that matters.
            database = db.scalar(select(func.current_database()))
            db.commit()
            _print_settings(database)
            server = _server()
            meant = (arguments.database, arguments.host and arguments.host.lower())
            if meant[0] not in (None, database) or meant[1] not in (None, server):
                print(
                    f"Refused: this is the database {database!r} on {server}, "
                    f"and {meant[0] or database!r} on {meant[1] or server} "
                    "was asked for. Nothing was changed.",
                    file=sys.stderr,
                )
                return 2
            if not arguments.execute:
                _print_counts(retention.pending(db, now), "Would remove:")
                print(
                    "Nothing was removed. To remove, run with "
                    f"--execute --database {database} --host {server}"
                )
                return 0
            report = retention.purge(db, now)
    except retention.PurgeError as error:
        _print_counts(error.report, "Removed:")
        # Only what kind of error it was, here and below: its text can hold
        # the connection string, and with it the password.
        print(
            f"Failed while removing {error.category.replace('_', ' ')} "
            f"({type(error.__cause__).__name__}). What is listed above "
            "was removed; the rest is left for the next run.",
            file=sys.stderr,
        )
        return 1
    except Exception as error:
        print(
            f"Failed before anything was removed ({type(error).__name__}).",
            file=sys.stderr,
        )
        return 1
    _print_counts(report, "Removed:")
    return 0


if __name__ == "__main__":
    sys.exit(main())
