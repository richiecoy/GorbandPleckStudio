import enum
from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Text, Enum, DateTime, ForeignKey, Boolean, JSON
)
from sqlalchemy.orm import relationship
from app.database import Base


class ShotType(str, enum.Enum):
    STILL = "still"
    VEO3_CLIP = "veo3_clip"
    TITLE_CARD = "title_card"
    REUSE = "reuse"
    GRAPHIC = "graphic"
    BUMPER = "bumper"


class AssetStatus(str, enum.Enum):
    PENDING = "pending"
    GENERATING = "generating"
    REVIEW = "review"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


class GenerationType(str, enum.Enum):
    CHARACTER = "character"
    START_FRAME = "start_frame"
    STILL = "still"
    VIDEO = "video"


def utcnow():
    return datetime.now(timezone.utc)


class AppSetting(Base):
    """Runtime-configurable settings stored in SQLite."""
    __tablename__ = "app_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, default="")
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class Episode(Base):
    __tablename__ = "episodes"

    id = Column(Integer, primary_key=True)
    number = Column(Integer, nullable=False, unique=True)
    slug = Column(String(100), nullable=False, unique=True)
    title = Column(String(200), nullable=False)
    location = Column(String(200), default="")
    visual_plan_raw = Column(Text, default="")
    parsed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    shots = relationship("Shot", back_populates="episode", cascade="all, delete-orphan",
                         order_by="Shot.number")
    characters = relationship("Character", back_populates="episode",
                              cascade="all, delete-orphan")

    @property
    def stats(self):
        """Count individual assets: each generatable shot = 1 image,
        each veo3_clip = +1 video asset."""
        counts = {"pending": 0, "generating": 0, "review": 0, "approved": 0, "failed": 0}
        total = 0
        for s in self.shots:
            if s.shot_type not in (ShotType.STILL, ShotType.VEO3_CLIP, ShotType.TITLE_CARD):
                continue
            img_st, vid_st = _derive_asset_statuses(s)
            # Count image asset
            total += 1
            counts[img_st] = counts.get(img_st, 0) + 1
            # Count video asset for clips
            if s.shot_type == ShotType.VEO3_CLIP:
                total += 1
                bucket = "pending" if vid_st == "locked" else vid_st
                counts[bucket] = counts.get(bucket, 0) + 1
        return {
            "total": total,
            "pending": counts.get("pending", 0),
            "generating": counts.get("generating", 0),
            "review": counts.get("review", 0),
            "approved": counts.get("approved", 0),
        }


class Character(Base):
    __tablename__ = "characters"

    id = Column(Integer, primary_key=True)
    episode_id = Column(Integer, ForeignKey("episodes.id"), nullable=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, default="")
    prompt = Column(Text, default="")
    is_main = Column(Boolean, default=False)
    reference_image_path = Column(String(500), nullable=True)
    reference_image_url = Column(String(1000), nullable=True)
    status = Column(Enum(AssetStatus), default=AssetStatus.PENDING)
    created_at = Column(DateTime, default=utcnow)

    episode = relationship("Episode", back_populates="characters")
    generations = relationship("Generation", back_populates="character",
                               cascade="all, delete-orphan")


class Shot(Base):
    __tablename__ = "shots"

    id = Column(Integer, primary_key=True)
    episode_id = Column(Integer, ForeignKey("episodes.id"), nullable=False)
    number = Column(Integer, nullable=False)
    name = Column(String(200), nullable=False)
    segment = Column(String(100), default="")
    shot_type = Column(Enum(ShotType), nullable=False)
    status = Column(Enum(AssetStatus), default=AssetStatus.PENDING)

    nano_prompt = Column(Text, default="")
    veo3_prompt = Column(Text, default="")
    dialogue = Column(Text, default="")
    direction_notes = Column(Text, default="")

    character_refs = Column(JSON, default=list)

    image_path = Column(String(500), nullable=True)
    image_url = Column(String(1000), nullable=True)
    video_path = Column(String(500), nullable=True)
    video_url = Column(String(1000), nullable=True)

    duration = Column(String(50), default="")
    camera_notes = Column(Text, default="")

    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    episode = relationship("Episode", back_populates="shots")
    generations = relationship("Generation", back_populates="shot",
                               cascade="all, delete-orphan",
                               order_by="Generation.created_at.desc()")

    @property
    def needs_image(self):
        return self.shot_type in (ShotType.STILL, ShotType.VEO3_CLIP, ShotType.TITLE_CARD)

    @property
    def needs_video(self):
        return self.shot_type == ShotType.VEO3_CLIP

    @property
    def latest_image_gen(self):
        for g in self.generations:
            if g.gen_type in (GenerationType.START_FRAME, GenerationType.STILL):
                return g
        return None

    @property
    def latest_video_gen(self):
        for g in self.generations:
            if g.gen_type == GenerationType.VIDEO:
                return g
        return None


class Generation(Base):
    __tablename__ = "generations"

    id = Column(Integer, primary_key=True)
    shot_id = Column(Integer, ForeignKey("shots.id"), nullable=True)
    character_id = Column(Integer, ForeignKey("characters.id"), nullable=True)
    gen_type = Column(Enum(GenerationType), nullable=False)
    status = Column(Enum(AssetStatus), default=AssetStatus.GENERATING)

    task_id = Column(String(200), nullable=True)
    model = Column(String(100), default="")
    prompt_used = Column(Text, default="")
    reference_urls = Column(JSON, default=list)

    result_url = Column(String(1000), nullable=True)
    local_path = Column(String(500), nullable=True)
    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime, default=utcnow)
    completed_at = Column(DateTime, nullable=True)

    shot = relationship("Shot", back_populates="generations")
    character = relationship("Character", back_populates="generations")


def _derive_asset_statuses(shot) -> tuple:
    """Derive (image_status, video_status) from a shot's state.
    Used by Episode.stats and status_routes."""
    status = shot.status.value
    has_img = bool(shot.image_path)
    has_vid = bool(shot.video_path)
    is_clip = shot.shot_type == ShotType.VEO3_CLIP

    if not is_clip:
        return (status, "n/a")

    if not has_img:
        return (status, "locked")

    if not has_vid:
        if status == "review":
            return ("review", "locked")
        elif status == "generating":
            return ("approved", "generating")
        elif status == "failed":
            return ("approved", "failed")
        else:
            return ("approved", "pending")
    else:
        if status == "review":
            return ("approved", "review")
        elif status == "approved":
            return ("approved", "approved")
        elif status == "failed":
            return ("approved", "failed")
        else:
            return ("approved", status)
