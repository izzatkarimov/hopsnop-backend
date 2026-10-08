from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RateLimit(Base):
    """One counter: how often something was done within the current window.

    There is one row per counter, not one per window. A request that finds
    the window over starts the next one in the same row, so the table grows
    with the number of distinct keys and not with time.
    """

    __tablename__ = "rate_limits"
    __table_args__ = (CheckConstraint("count >= 0", name="count_not_negative"),)

    # HMAC-SHA256 hex digest of what is counted (which limit, and for which
    # address, identifier or account), keyed with a server-side secret. The
    # address or identifier itself is never stored, and without the secret a
    # row cannot be tested against a guess of it either.
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    # When the window being counted began. Set by the application.
    window_start: Mapped[datetime] = mapped_column()
    count: Mapped[int] = mapped_column()
