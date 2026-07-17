import datetime
import textwrap
import sqlite3

import asqlite
import ckdl
import humanize
import quart
from discord.utils import snowflake_time
from lookout import db

# it has no idea what's in this module
ckdl: Any  # type: ignore


app = quart.Quart(__name__)

def show_reason(r: sqlite3.Row) -> str:
    if quart.request.headers["Host"] == "tt.dpyle.xyz":
        return "You must change tt.dpyle.xyz to tt.lyricly.fans in your Blacklist config by 2027-03-23. See TT Seevee for more information."

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
    pool = await db.create_pool("the.db", size=1)
    async with pool.acquire() as conn:
        dump = ckdl.Document(
            [ckdl.Node(None, "-", r["account_name"], reason=show_reason(r)) for r in await conn.fetchall("SELECT * FROM Blacklists")],
        ).dump(ckdl.EmitterOptions(version=1))
    await pool.close()
    return quart.Response(dump, mimetype="application/vnd.kdl")

if __name__ == "__main__":
    app.run()
