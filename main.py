import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timezone, timedelta
import os

TOKEN = os.getenv("TOKEN")
LOG_CHANNEL_ID = 1527284881351118960

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.guilds = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents)


def moscow_time():
    return datetime.now(timezone(timedelta(hours=3)))


def get_log_channel(guild):
    return guild.get_channel(LOG_CHANNEL_ID)


@bot.event
async def on_ready():
    await bot.change_presence(status=discord.Status.idle)

    try:
        synced = await bot.tree.sync()
        print(f"Синхронизировано команд: {len(synced)}")
    except Exception as e:
        print(f"Ошибка синхронизации: {e}")

    print(f"Бот запущен: {bot.user}")


COLOR = discord.Color.from_rgb(47, 47, 47)   # #2F2F2F


@bot.event
async def on_message_delete(message):
    if message.author.bot or message.guild is None:
        return

    log = get_log_channel(message.guild)
    if log is None:
        return

    embed = discord.Embed(
        title="Удаленное сообщение",
        color=COLOR,
        timestamp=moscow_time()
    )

    embed.add_field(name="Пользователь", value=f"{message.author.mention}\nID: `{message.author.id}`", inline=False)
    embed.add_field(name="Канал", value=message.channel.mention, inline=False)
    embed.add_field(name="Сообщение", value=f"> {message.content}" if message.content else "> Без текста", inline=False)

    await log.send(embed=embed)


@bot.event
async def on_message_edit(before​​​​​​​​​​​​​​​​​​​​​​​​​​​​​​​​​​​​​​​​​​​​​​​​​​
