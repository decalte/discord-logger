import asyncio
import os
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands


TOKEN = os.getenv("TOKEN")

# Канал логов сообщений
MESSAGE_LOG_CHANNEL_ID = 1527284881351118960

# Укажи отдельные каналы
TIMEOUT_LOG_CHANNEL_ID = 111111111111111111
KICK_LOG_CHANNEL_ID = 222222222222222222
BAN_LOG_CHANNEL_ID = 333333333333333333

# Цвет полоски всех эмбедов: #2F2F2F
COLOR = discord.Color.from_rgb(47, 47, 47)


intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.guilds = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents)


def moscow_time() -> datetime:
    return datetime.now(timezone(timedelta(hours=3)))


def format_moscow_time() -> str:
    return moscow_time().strftime("%d.%m.%Y, %H:%M МСК")


def get_channel(guild: discord.Guild, channel_id: int):
    return guild.get_channel(channel_id)


async def find_audit_entry(
    guild: discord.Guild,
    action: discord.AuditLogAction,
    target_id: int,
    attempts: int = 4
):
    """
    Ищет свежую запись в журнале аудита.

    Повторные попытки нужны, потому что Discord иногда добавляет
    запись в журнал аудита немного позже самого события.
    """
    for attempt in range(attempts):
        try:
            async for entry in guild.audit_logs(
                limit=10,
                action=action
            ):
                if entry.target is None:
                    continue

                if entry.target.id != target_id:
                    continue

                # Не используем старые записи журнала аудита
                age = (
                    datetime.now(timezone.utc)
                    - entry.created_at
                ).total_seconds()

                if age <= 15:
                    return entry

        except discord.Forbidden:
            print(
                f"Нет права просмотра журнала аудита "
                f"на сервере {guild.name}"
            )
            return None

        except discord.HTTPException as error:
            print(f"Ошибка получения журнала аудита: {error}")

        if attempt < attempts - 1:
            await asyncio.sleep(1)

    return None


@bot.event
async def on_ready():
    await bot.change_presence(status=discord.Status.idle)

    try:
        synced = await bot.tree.sync()
        print(f"Синхронизировано команд: {len(synced)}")
    except Exception as error:
        print(f"Ошибка синхронизации: {error}")

    print(f"Бот запущен: {bot.user}")


# =========================================================
# ЛОГИ СООБЩЕНИЙ
# =========================================================

@bot.event
async def on_message_delete(message: discord.Message):
    if message.guild is None:
        return

    if message.author.bot:
        return

    log_channel = get_channel(
        message.guild,
        MESSAGE_LOG_CHANNEL_ID
    )

    if log_channel is None:
        return

    message_text = message.content or "Без текста"

    embed = discord.Embed(
        title="Удалённое сообщение",
        color=COLOR,
        timestamp=moscow_time()
    )

    embed.add_field(
        name="Пользователь",
        value=(
            f"{message.author.mention}\n"
            f"ID: `{message.author.id}`"
        ),
        inline=False
    )

    embed.add_field(
        name="Канал",
        value=message.channel.mention,
        inline=False
    )

    embed.add_field(
        name="Сообщение",
        value=f"> {message_text[:1000]}",
        inline=False
    )

    embed.add_field(
        name="Время",
        value=f"> {format_moscow_time()}",
        inline=False
    )

    await log_channel.send(embed=embed)


@bot.event
async def on_message_edit(
    before: discord.Message,
    after: discord.Message
):
    if before.guild is None:
        return

    if before.author.bot:
        return

    if before.content == after.content:
        return

    log_channel = get_channel(
        before.guild,
        MESSAGE_LOG_CHANNEL_ID
    )

    if log_channel is None:
        return

    before_text = before.content or "Без текста"
    after_text = after.content or "Без текста"

    embed = discord.Embed(
        title="Изменённое сообщение",
        color=COLOR,
        timestamp=moscow_time()
    )

    embed.add_field(
        name="Пользователь",
        value=(
            f"{before.author.mention}\n"
            f"ID: `{before.author.id}`"
        ),
        inline=False
    )

    embed.add_field(
        name="Канал",
        value=before.channel.mention,
        inline=False
    )

    embed.add_field(
        name="Было",
        value=f"> {before_text[:1000]}",
        inline=False
    )

    embed.add_field(
        name="Стало",
        value=f"> {after_text[:1000]}",
        inline=False
    )

    embed.add_field(
        name="Время",
        value=f"> {format_moscow_time()}",
        inline=False
    )

    await log_channel.send(embed=embed)


# =========================================================
# ЛОГИ ТАЙМ-АУТОВ
# Срабатывает при выдаче через ПКМ и встроенное меню Discord
# =========================================================

@bot.event
async def on_member_update(
    before: discord.Member,
    after: discord.Member
):
    before_timeout = before.timed_out_until
    after_timeout = after.timed_out_until

    # Состояние тайм-аута не изменилось
    if before_timeout == after_timeout:
        return

    # Тайм-аут был снят, а не выдан
    if after_timeout is None:
        return

    # Защита от уже истёкшего тайм-аута
    if after_timeout <= datetime.now(timezone.utc):
        return

    entry = await find_audit_entry(
        guild=after.guild,
        action=discord.AuditLogAction.member_update,
        target_id=after.id
    )

    if entry is None or entry.user is None:
        return

    moderator = entry.user
    reason = entry.reason or "Причина не указана"

    log_channel = get_channel(
        after.guild,
        TIMEOUT_LOG_CHANNEL_ID
    )

    if log_channel is None:
        return

    timeout_until_msk = after_timeout.astimezone(
        timezone(timedelta(hours=3))
    )

    embed = discord.Embed(
        title="Выдача тайм-аута",
        color=COLOR,
        timestamp=moscow_time()
    )

    embed.add_field(
        name="Выдал",
        value=(
            f"{moderator.mention}\n"
            f"ID: `{moderator.id}`"
        ),
        inline=False
    )

    embed.add_field(
        name="Пользователю",
        value=(
            f"{after.mention}\n"
            f"ID: `{after.id}`"
        ),
        inline=False
    )

    embed.add_field(
        name="Причина",
        value=f"> {reason}",
        inline=False
    )

    embed.add_field(
        name="До",
        value=(
            f"> {timeout_until_msk.strftime('%d.%m.%Y, %H:%M МСК')}"
        ),
        inline=False
    )

    embed.add_field(
        name="Время",
        value=f"> {format_moscow_time()}",
        inline=False
    )

    await log_channel.send(embed=embed)


# =========================================================
# ЛОГИ КИКОВ
# Срабатывает при исключении через ПКМ
# =========================================================

@bot.event
async def on_member_remove(member: discord.Member):
    entry = await find_audit_entry(
        guild=member.guild,
        action=discord.AuditLogAction.kick,
        target_id=member.id
    )

    # Если записи кика нет, пользователь вышел сам
    # либо был забанен
    if entry is None or entry.user is None:
        return

    moderator = entry.user
    reason = entry.reason or "Причина не указана"

    log_channel = get_channel(
        member.guild,
        KICK_LOG_CHANNEL_ID
    )

    if log_channel is None:
        return

    embed = discord.Embed(
        title="Пользователь выгнан с сервера",
        color=COLOR,
        timestamp=moscow_time()
    )

    embed.add_field(
        name="Выгнал",
        value=(
            f"{moderator.mention}\n"
            f"ID: `{moderator.id}`"
        ),
        inline=False
    )

    embed.add_field(
    name="Пользователя",
        value=(
            f"{member.mention}\n"
            f"ID: `{member.id}`"
        ),
        inline=False
    )

    embed.add_field(
        name="Причина",
        value=f"> {reason}",
        inline=False
    )

    embed.add_field(
        name="Время",
        value=f"> {format_moscow_time()}",
        inline=False
    )

    await log_channel.send(embed=embed)


# =========================================================
# ЛОГИ БАНОВ
# Срабатывает при бане через ПКМ
# =========================================================

@bot.event
async def on_member_ban(
    guild: discord.Guild,
    user: discord.User
):
    entry = await find_audit_entry(
        guild=guild,
        action=discord.AuditLogAction.ban,
        target_id=user.id
    )

    if entry is None or entry.user is None:
        return

    moderator = entry.user
    reason = entry.reason or "Причина не указана"

    log_channel = get_channel(
        guild,
        BAN_LOG_CHANNEL_ID
    )

    if log_channel is None:
        return

    embed = discord.Embed(
        title="Выдан бан",
        color=COLOR,
        timestamp=moscow_time()
    )

    embed.add_field(
        name="Выдал",
        value=(
            f"{moderator.mention}\n"
            f"ID: `{moderator.id}`"
        ),
        inline=False
    )

    embed.add_field(
        name="Пользователю",
        value=(
            f"{user.mention}\n"
            f"ID: `{user.id}`"
        ),
        inline=False
    )

    embed.add_field(
        name="Причина",
        value=f"> {reason}",
        inline=False
    )

    embed.add_field(
        name="Время",
        value=f"> {format_moscow_time()}",
        inline=False
    )

    await log_channel.send(embed=embed)


# =========================================================
# КОМАНДА /AVATAR
# =========================================================

@bot.tree.command(
    name="avatar",
    description="Посмотреть аватарку"
)
@app_commands.describe(user="Пользователь")
async def avatar(
    interaction: discord.Interaction,
    user: discord.Member = None
):
    if user is None:
        user = interaction.user

    embed = discord.Embed(
        title=f"Аватар — {user.name}",
        color=COLOR
    )

    embed.set_image(url=user.display_avatar.url)

    await interaction.response.send_message(embed=embed)


if not TOKEN:
    raise RuntimeError(
        "Токен не найден. Добавь переменную окружения TOKEN."
    )

bot.run(TOKEN)
