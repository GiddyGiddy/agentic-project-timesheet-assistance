from datetime import date

from sqlalchemy import Date, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DailyEntry(Base):
    __tablename__ = "daily_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_date: Mapped[date] = mapped_column(Date, index=True)
    project_code: Mapped[str] = mapped_column(String, index=True)
    hours: Mapped[float] = mapped_column(Float)
    summary: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)


class ActivityAudit(Base):
    __tablename__ = "activity_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_date: Mapped[date] = mapped_column(Date, index=True)
    source: Mapped[str] = mapped_column(String)
    payload: Mapped[str] = mapped_column(Text)
