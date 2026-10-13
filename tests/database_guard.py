"""Keeps the tests away from a database that has anything in it.

The tests assume empty tables: a feed with one post in it has one post in
it. They also do things no database with real data should be near. Some
commit for real and remove what they wrote, and the tests of erasing and of
the retention purge delete on purpose.

So before the first test, every table of the application is checked, and if
any of them holds a row the run is refused. Nothing is deleted to make room:
whatever is there is someone's data until proven otherwise.

After the last test the same check is made again. Rows that are there then
were left behind by a test that commits, and the run is failed for it,
because the next run would be refused.
"""

from sqlalchemy import Connection, func, select, table
from sqlalchemy.engine import URL

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.db.base import Base

# The database the README sets up for the tests.
TEST_DATABASE = "hopsnop_test"


def tables_with_rows(connection: Connection) -> dict[str, int]:
    """Every table of the application that is not empty, with its row count.

    The tables are those of the models, whichever there are: one that is
    added later is checked without anyone having to remember it.
    """
    found = {}
    for name in sorted(Base.metadata.tables):
        count = connection.scalar(select(func.count()).select_from(table(name)))
        if count:
            found[name] = count
    return found


def _listed(found: dict[str, int]) -> str:
    return ", ".join(f"{name} ({count})" for name, count in found.items())


def refusal(found: dict[str, int], url: URL) -> str:
    """What is said when the tests are not run. No password is in it."""
    return (
        f"\nThe tests were not run: the database "
        f"{url.database!r} on {url.host}:{url.port} is not empty.\n"
        f"  Rows found in: {_listed(found)}\n"
        "The tests assume empty tables, and some of them commit and delete "
        "for real.\nNothing was changed. Point DATABASE_URL at an empty, "
        "migrated database, for example:\n"
        f"  DATABASE_URL=postgresql+psycopg://…/{TEST_DATABASE} pytest\n"
        "(see README, Tests).\n"
    )


def leak_report(found: dict[str, int]) -> str:
    """What is said when a run leaves rows behind."""
    return (
        "Rows were left in the database by this test run: "
        f"{_listed(found)}. A test that commits did not remove what it "
        "wrote. The next run will be refused until they are gone."
    )
