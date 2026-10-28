from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Settings(Model):
    schema_version: Literal[1] = 1
    drive_folder_id: str = "1RUg3Mlt9JKHBGEdjE-VMJfB2nmNnq8SE"
    # Empty disables the Drive upload log. Story folders are never written.
    upload_log_folder_id: str = ""
    youtube_channel_id: str
    upload_lead_minutes: int = Field(default=180, gt=0)
    poll_seconds: int = Field(default=30, ge=5)
    schedule_margin_seconds: int = Field(default=60, ge=30)
    state_path: str = "data/state.json"
    cache_dir: str = "data/cache"


class Video(Model):
    filename: str
    publish_at: datetime
    title: str = Field(min_length=1, max_length=100)
    description: str
    category_id: str
    made_for_kids: bool = Field(strict=True)
    contains_synthetic_media: bool = Field(strict=True)
    tags: list[str] = Field(default_factory=list)
    default_language: str | None = None
    default_audio_language: str | None = None
    license: Literal["youtube", "creativeCommon"] = "youtube"
    embeddable: bool = Field(default=True, strict=True)
    notify_subscribers: bool = Field(default=True, strict=True)

    @field_validator("filename")
    @classmethod
    def filename_only(cls, value):
        if not value or value in {".", ".."} or any(c in value for c in "/\\:\x00"):
            raise ValueError("filename must be a direct child filename")
        return value

    @field_validator("publish_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("publish_at requires an explicit timezone offset")
        return value

    @model_validator(mode="after")
    def metadata(self):
        if not self.title.strip() or any(
            c in self.title + self.description for c in "<>"
        ):
            raise ValueError("invalid title or description")
        if len(self.description.encode("utf-8")) > 5000:
            raise ValueError("description exceeds 5000 bytes")
        if (
            sum(len(t) + (2 if " " in t else 0) for t in self.tags)
            + max(0, len(self.tags) - 1)
            > 500
        ):
            raise ValueError("tags exceed 500 characters")
        return self


class Story(Model):
    schema_version: Literal[1]
    enabled: bool = Field(strict=True)
    videos: dict[str, Video]

    @field_validator("videos")
    @classmethod
    def pair(cls, value):
        if set(value) != {"long", "short"}:
            raise ValueError("videos must contain exactly long and short")
        if value["long"].filename == value["short"].filename:
            raise ValueError("long and short need different files")
        return value
