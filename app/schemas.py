from datetime import datetime
from typing import Literal
import re
from pydantic import BaseModel, Field, field_validator
from .config import get_settings


class JobCreate(BaseModel):
    magnet_uri: str = Field(min_length=10)
    final_parent: str = Field(min_length=2)
    final_category: str | None = None
    staging_preference: Literal["local", "nas"] = "local"

    @field_validator("final_parent")
    @classmethod
    def validate_final_parent(cls, value: str) -> str:
        settings = get_settings()
        if not value.startswith(settings.final_parent_prefix.rstrip("/") + "/") and value != settings.final_parent_prefix.rstrip("/"):
            raise ValueError(f"final_parent must be inside {settings.final_parent_prefix}")
        return value

    @field_validator("magnet_uri")
    @classmethod
    def validate_magnet(cls, value: str) -> str:
        if not value.startswith("magnet:?"):
            raise ValueError("Only magnet links are supported in this MVP")
        # Require a plausible BTIH hash to avoid opaque downstream qBittorrent errors.
        pattern = re.compile(r"(^|[?&])xt=urn:btih:([A-Za-z0-9]{32}|[A-Fa-f0-9]{40})($|&)")
        if not pattern.search(value):
            raise ValueError("magnet_uri must include a valid xt=urn:btih hash")
        return value


class JobOut(BaseModel):
    id: str
    created_at: datetime
    updated_at: datetime
    magnet_uri: str
    final_parent: str
    final_category: str | None
    staging_preference: str
    staging_actual: str | None
    staging_root_initial: str
    staging_root_actual: str | None
    staging_overridden: bool
    override_reason: str | None
    managed_tag: str
    unique_tag: str
    qbt_hash: str | None
    torrent_name: str | None
    state: str
    is_terminal: bool
    size_bytes: int | None
    content_path: str | None
    last_seen_qbt_state: str | None
    threat_name: str | None
    last_error: str | None

    model_config = {"from_attributes": True}


class CompletionEventIn(BaseModel):
    qbt_hash: str | None = None
    torrent_name: str | None = None
    content_path: str | None = None
    unique_tag: str | None = None
