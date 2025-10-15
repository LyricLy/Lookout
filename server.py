from __future__ import annotations

import aiosqlite
import ckdl
import quart
from lookout import db


app = quart.Quart(__name__)

async def get_db() -> aiosqlite.Connection:
    if not hasattr(quart.g, "db"):
        quart.g.db = await db.connect("the.db")
    return quart.g.db

@app.route("/")
async def root():
    db = await get_db()
    async with db.execute("SELECT account_name FROM Blacklists") as cur:
        return ckdl.Document([ckdl.Node(None, "-", x) async for x, in cur]).dump(ckdl.EmitterOptions(version=1))  # type: ignore

if __name__ == "__main__":
    app.run()
