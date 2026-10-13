from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings


# The isolation level every transaction of the application runs at. It is
# PostgreSQL's own default, and it is named here because the application
# depends on it, not merely on whatever the server happens to be set to.
#
# At READ COMMITTED each statement sees what was committed when it began, and
# a statement that has waited for a row lock goes on with the row as its
# holder left it. That is what the locks in the services are built on: the
# request that comes second waits for the first to commit and then reads what
# the first has done (``app.services.auth._lock_account``, the redeeming of
# tokens, the counters of ``app.services.rate_limit``, and the erasing of
# posts and accounts). At a stricter level the second request would go on
# with the picture it had before it waited, and those checks would decide on
# stale data.
ISOLATION_LEVEL = "READ COMMITTED"


def create_application_engine(url: str, **kwargs: object) -> Engine:
    """An engine set up the way the application's is.

    There is one engine, made below. This is a function so that how it is
    made can be tried against a server that is configured differently.
    """
    return create_engine(
        url,
        isolation_level=ISOLATION_LEVEL,
        pool_pre_ping=True,
        # Keep statement parameters out of logs and exception messages. They
        # include password hashes and token hashes.
        hide_parameters=True,
        **kwargs,
    )


engine = create_application_engine(settings.database_url)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)