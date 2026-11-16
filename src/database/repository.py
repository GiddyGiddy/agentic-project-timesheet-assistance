import calendar
import json
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from src.database.models import ActivityAudit, Base, DailyEntry
from src.graph.state import Entry


class Repository:
    def __init__(self, database_url: str):
        url = make_url(database_url)
        if url.get_backend_name() != "sqlite":
            raise ValueError("Only SQLite is supported")
        if url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(database_url)
        Base.metadata.create_all(self.engine)

    def replace_day(
        self, day: date, entries: list[Entry], activities: dict[str, list[dict[str, Any]]]
    ) -> None:
        with Session(self.engine) as session, session.begin():
            session.execute(delete(DailyEntry).where(DailyEntry.entry_date == day))
            session.execute(delete(ActivityAudit).where(ActivityAudit.entry_date == day))
            session.add_all(
                DailyEntry(entry_date=day, **entry.model_dump()) for entry in entries
            )
            for source, records in activities.items():
                session.add_all(
                    ActivityAudit(
                        entry_date=day,
                        source=source,
                        payload=json.dumps(record, ensure_ascii=False),
                    )
                    for record in records
                )

    def monthly_entries(self, year: int, month: int) -> list[DailyEntry]:
        first = date(year, month, 1)
        last = date(year, month, calendar.monthrange(year, month)[1])
        with Session(self.engine) as session:
            return list(
                session.scalars(
                    select(DailyEntry)
                    .where(DailyEntry.entry_date >= first, DailyEntry.entry_date <= last)
                    .order_by(DailyEntry.entry_date, DailyEntry.project_code, DailyEntry.id)
                ).all()
            )
