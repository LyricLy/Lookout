import datetime
import textwrap

import aiosqlite
import ckdl
import humanize
import quart
from discord.utils import snowflake_time
from lookout import db


app = quart.Quart(__name__)

async def get_db() -> aiosqlite.Connection:
    if not hasattr(quart.g, "db"):
        quart.g.db = await db.connect("the.db")
    return quart.g.db

def show_reason(r: aiosqlite.Row) -> str:
    if r["no_retrial"]:
        pill = '<style="VampireColor">[No retrial]</style>'
    else:
        time = snowflake_time(r["thread_id"])
        color = "ApocalypseColor" if datetime.datetime.now(datetime.timezone.utc) - time < datetime.timedelta(days=30) else "TownColor"
        pill = f'<style="{color}">[{humanize.naturaltime(time)}]</style>'

    if reason := r["reason"]:
        return f"{pill} {textwrap.shorten(r['reason'].splitlines()[0], 100)}"
    else:
        return f"{pill} [reason not found]"

@app.route("/")
async def root():
    db = await get_db()
    async with db.execute("SELECT * FROM Blacklists") as cur:
        dump = ckdl.Document([ckdl.Node(None, "-", r["account_name"], reason=show_reason(r)) async for r in cur]).dump(ckdl.EmitterOptions(version=1))  # type: ignore
    return quart.Response(dump, mimetype="application/vnd.kdl")

if __name__ == "__main__":
    app.run()
