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
        total = len(self.shots)
        approved = sum(1 for s in self.shots if s.status == AssetStatus.APPROVED)
        generating = sum(1 for s in self.shots if s.status == AssetStatus.GENERATING)
        review = sum(1 for s in self.shots if s.status == AssetStatus.REVIEW)
        return {
            "total": total, "approved": approved, "generating": generating,
            "review": review, "pending": total - approved - generating - review
        }


class Character(Base):
    __tablename__ = "characters"

    id = Column(Integer, primary_key=True)
    episode_id = Column(Integer, ForeignKey("episodes.id"), nullable=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, default="")
    prompt = Column(Text, default="")
    is_main = Column(Boolean, default=False)  # True for Gorb/Pleck
    reference_image_path = Column(String(500), nullable=True)
    reference_image_url = Column(String(1000), nullable=True)  # URL for API calls
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

    # Prompts parsed from visual plan
    nano_prompt = Column(Text, default="")        # Nano Banana prompt (still or start frame)
    veo3_prompt = Column(Text, default="")         # Veo3 prompt (if video shot)
    dialogue = Column(Text, default="")
    direction_notes = Column(Text, default="")

    # Character references needed for this shot
    character_refs = Column(JSON, default=list)     # List of character names

    # Generated asset paths
    image_path = Column(String(500), nullable=True)
    image_url = Column(String(1000), nullable=True)   # Temporary kie.ai URL
    video_path = Column(String(500), nullable=True)
    video_url = Column(String(1000), nullable=True)

    # Duration / metadata from visual plan
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

    # Kie.ai task tracking
    task_id = Column(String(200), nullable=True)
    model = Column(String(100), default="")
    prompt_used = Column(Text, default="")
    reference_urls = Column(JSON, default=list)

    # Results
    result_url = Column(String(1000), nullable=True)   # Kie.ai temp URL
    local_path = Column(String(500), nullable=True)     # Downloaded local path
    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime, default=utcnow)
    completed_at = Column(DateTime, nullable=True)

    shot = relationship("Shot", back_populates="generations")
    character = relationship("Character", back_populates="generations")
