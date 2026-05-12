import asqlite
import re

async def migrate(db: asqlite.Connection):
    for digest, clean_content in await db.fetchall("SELECT hash, clean_content FROM Gamelogs WHERE clean_content LIKE '%gradient%'"):
        await db.execute("UPDATE Gamelogs SET clean_content = ? WHERE hash = ?", (re.sub(r"</?gradient[^>]*>", "", clean_content), digest))
