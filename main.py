import asyncio
import re

import ckdl
import discord
from aiohttp import web
from discord.ext import commands

import config


discord.utils.setup_logging()
the_list = {}


routes = web.RouteTableDef()

@routes.get("/")
async def root(request):
    return web.Response(text=ckdl.Document([ckdl.Node(None, "-", y) for x in the_list.values() for y in x]).dump(ckdl.EmitterOptions(version=1)))


intents = discord.Intents(
    guilds=True,
    messages=True,
    message_content=True,
)
bot = commands.Bot(
    command_prefix=commands.when_mentioned,
    intents=intents,
)

def apparently_has_logs(message):
    return any(attachment.filename.endswith("html") for attachment in message.attachments)

def check_thread(thread):
    concerned_people = [r for x in re.split(r",|&|\band\b", thread.name.strip("()").rsplit(":", 1)[-1], flags=re.I) if (r := x.strip()) and not r.isdigit()]

    if any(t.id in config.damning_tags for t in thread.applied_tags):
        the_list[thread.id] = concerned_people
    else:
        the_list.pop(thread.id, None)

@bot.event
async def on_ready():
    # rarely is it correct to do work in on_ready. it's correct this time, though!
    channel = bot.get_channel(config.channel_id)
    assert isinstance(channel, discord.ForumChannel)

    for thread in channel.threads:
        check_thread(thread)
    async for thread in channel.archived_threads(limit=None):
        check_thread(thread)

@bot.event
async def on_thread_create(thread):
    check_thread(thread)
    message = await thread.fetch_message(thread.id)
    if not apparently_has_logs(message):
        await thread.add_tags(discord.Object(id=config.no_logs_tag))

@bot.event
async def on_thread_update(before, after):
    check_thread(after)

@bot.event
async def on_raw_thread_delete(payload):
    the_list.pop(payload.thread_id, None)

@bot.event
async def on_message(message):
    if apparently_has_logs(message) and isinstance(message.channel, discord.Thread):
        await message.channel.remove_tags(discord.Object(id=config.no_logs_tag))


async def the_bot(_):
    await bot.load_extension("jishaku")
    task = asyncio.create_task(bot.start(config.token))
    yield
    await bot.close()

app = web.Application()
app.add_routes(routes)

app.cleanup_ctx.append(the_bot)

web.run_app(app, port=8672)
