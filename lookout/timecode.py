import datetime
from dataclasses import dataclass, replace
from typing import Self

import discord


@dataclass(order=True)
class Timecode:
    message_id: int
    filename_time: datetime.datetime

    def pred(self) -> Self:
        return replace(self, filename_time=self.filename_time - datetime.timedelta(microseconds=1))

    def next(self) -> Self:
        return replace(self, filename_time=self.filename_time + datetime.timedelta(microseconds=1))

    def to_datetime(self) -> datetime.datetime:
        return discord.utils.snowflake_time(self.message_id)

    @classmethod
    def from_datetime(cls, dt: datetime.datetime) -> Self:
        return cls(discord.utils.time_snowflake(dt), datetime.datetime(1970, 1, 1))

    @classmethod
    def from_str(cls, s: str) -> Self:
        return cls(int(s[:16], 16), datetime.datetime.fromisoformat(s[16:]))

    def to_str(self) -> str:
        return f"{self.message_id:016x}{self.filename_time.isoformat()}"

    def __str__(self) -> str:
        return f"{self.to_datetime()}>{self.filename_time}"
