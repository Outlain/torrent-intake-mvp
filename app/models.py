from datetime import datetime
from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    magnet_uri: Mapped[str] = mapped_column(Text, nullable=False)
    final_parent: Mapped[str] = mapped_column(Text, nullable=False)
    final_category: Mapped[str | None] = mapped_column(String(255), nullable=True)

    staging_preference: Mapped[str] = mapped_column(String(16), nullable=False)  # local | nas
    staging_actual: Mapped[str | None] = mapped_column(String(16), nullable=True)
    staging_root_initial: Mapped[str] = mapped_column(Text, nullable=False)
    staging_root_actual: Mapped[str | None] = mapped_column(Text, nullable=True)
    staging_overridden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    override_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    managed_tag: Mapped[str] = mapped_column(String(255), nullable=False)
    unique_tag: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    qbt_hash: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    torrent_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    state: Mapped[str] = mapped_column(String(64), default="submitted", nullable=False)
    is_terminal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen_qbt_state: Mapped[str | None] = mapped_column(String(128), nullable=True)

    completion_event_received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    download_complete_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scan_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    threat_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
