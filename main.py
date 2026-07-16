import discord
from discord.ext import commands
from datetime import datetime, timezone, timedelta
import os

TOKEN = os.getenv("TOKEN")
LOG_CHANNEL_ID = 1527284881351118960
VOICE_CHANNEL_ID = 123456789012345678  # <-- ВСТАВЬ СЮДА ID ГОЛОСОВОГО КАНАЛА

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.guilds = True
intents.members = True
intents.presences = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

def moscow_time():
    return datetime.now(timezone(timedelta(hours=3)))

def get_log_channel(guild):
    return guild.get_channel(LOG_CHANNEL_ID)

@bot.event
async def on_ready():
    await bot.change_presence(status=discord.Status.idle)
    print(f"Бот запущен: {bot.user}")

    channel = bot.get_channel(VOICE_CHANNEL_ID)
    if channel and channel.guild.voice_client is None:
        try:
            await channel.connect()

            try:
                await channel.guild.change_voice_state(
                    channel=channel,
                    self_mute=True,
                    self_deaf=True
                )
            except:
                pass

        except Exception as e:
            print(f"Ошибка подключения к голосовому каналу: {e}")

@bot.event
async def on_message_delete(message):
    if message.author.bot or message.guild is None:
        return
    log = get_log_channel(message.guild)
    if log is None:
        return

    embed = discord.Embed(
        title="Удаленное сообщение",
        color=discord.Color.from_rgb(128, 128, 128),
        timestamp=moscow_time()
    )
    embed.add_field(name="Пользователь", value=f"{message.author.mention}\nID: `{message.author.id}`", inline=False)
    embed.add_field(name="Канал", value=message.channel.mention, inline=False)
    embed.add_field(name="Сообщение", value=f"> {message.content}" if message.content else "> Без текста", inline=False)

    await log.send(embed=embed)

@bot.event
async def on_message_edit(before, after):
    if before.author.bot or before.guild is None or before.content == after.content:
        return
    log = get_log_channel(before.guild)
    if log is None:
        return

    embed = discord.Embed(
        title="Измененное сообщение",
        color=discord.Color.from_rgb(128, 128, 128),
        timestamp=moscow_time()
    )
    embed.add_field(name="Пользователь", value=f"{before.author.mention}\nID: `{before.author.id}`", inline=False)
    embed.add_field(name="Канал", value=before.channel.mention, inline=False)
    embed.add_field(name="Было", value=f"> {before.content}", inline=False)
    embed.add_field(name="Стало", value=f"> {after.content}", inline=False)

    await log.send(embed=embed)

bot.run(TOKEN)
