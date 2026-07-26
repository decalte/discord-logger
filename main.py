from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands


TOKEN = os.getenv("TOKEN")

# Один канал для всех логов.
# Можно указать ID через переменную окружения LOG_CHANNEL_ID
# или заменить число ниже на ID нужного канала.
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "1527284881351118960"))

COLOR = discord.Color(0x303136)
MOSCOW_TZ = timezone(timedelta(hours=3))

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True
intents.moderation = True

bot = commands.Bot(command_prefix="!", intents=intents)


def moscow_time(value: datetime | None = None) -> datetime:
    if value is None:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(MOSCOW_TZ)


def log_datetime(value: datetime | None = None) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    local = moscow_time(value)
    today = moscow_time().date()
    time_text = local.strftime("%I:%M %p").lstrip("0")

    if local.date() == today:
        return f"Сегодня, в {time_text}"
    if local.date() == today - timedelta(days=1):
        return f"Вчера, в {time_text}"

    months = (
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    )
    return f"{local.day} {months[local.month - 1]} {local.year}, в {time_text}"


def russian_datetime(value: datetime) -> str:
    months = (
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    )
    local = moscow_time(value)
    today = moscow_time().date()

    if local.date() == today:
        return local.strftime("Сегодня, в %H:%M")
    if local.date() == today + timedelta(days=1):
        return local.strftime("Завтра, в %H:%M")

    return f"{local.day} {months[local.month - 1]} {local.year}, в {local:%H:%M}"


def member_id_text(user: discord.abc.User) -> str:
    return f"{user.mention}\nID: `{user.id}`"


async def get_log_channel(guild: discord.Guild) -> discord.abc.Messageable | None:
    channel = guild.get_channel(LOG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await guild.fetch_channel(LOG_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
            print(f"Не удалось получить канал логов {LOG_CHANNEL_ID}: {error}")
            return None

    if not isinstance(channel, discord.abc.Messageable):
        print(f"Канал {LOG_CHANNEL_ID} не поддерживает отправку сообщений.")
        return None
    return channel


async def send_log(guild: discord.Guild, embed: discord.Embed) -> None:
    channel = await get_log_channel(guild)
    if channel is None:
        return
    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException) as error:
        print(f"Ошибка отправки лога: {error}")


async def find_audit_entry(
    guild: discord.Guild,
    action: discord.AuditLogAction,
    target_id: int,
    max_age: int = 15,
) -> discord.AuditLogEntry | None:
    try:
        async for entry in guild.audit_logs(limit=12, action=action):
            if not entry.target or entry.target.id != target_id:
                continue
            age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
            if age <= max_age:
                return entry
    except discord.Forbidden:
        print(f"Нет права на просмотр журнала аудита: {guild.name}")
    except discord.HTTPException as error:
        print(f"Ошибка получения журнала аудита: {error}")
    return None


def limited_text(text: str | None, fallback: str = "Отсутствует") -> str:
    value = (text or "").strip() or fallback
    return value[:997] + "..." if len(value) > 1000 else value


@bot.event
async def on_ready() -> None:
    await bot.change_presence(
        status=discord.Status.idle,
        activity=discord.CustomActivity(name="🌙 Луна"),
    )
    print(f"Бот запущен: {bot.user}")
    print(f"Все логи отправляются в канал ID: {LOG_CHANNEL_ID}")


# -----------------------------------------------------------------------------
# Логи сообщений
# -----------------------------------------------------------------------------

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
    if before.author.bot or not before.guild or before.content == after.content:
        return

    embed = discord.Embed(
        title="Изменённое сообщение",
        color=COLOR,
        timestamp=moscow_time(),
    )
    embed.add_field(name="Пользователь", value=member_id_text(before.author), inline=False)
    embed.add_field(name="Канал", value=before.channel.mention, inline=False)
    embed.add_field(name="Было", value=f"> {limited_text(before.content, 'Текст отсутствует')}", inline=False)
    embed.add_field(name="Стало", value=f"> {limited_text(after.content, 'Текст отсутствует')}", inline=False)
    embed.add_field(name="Ссылка", value=f"> [Перейти к сообщению]({after.jump_url})", inline=False)
    await send_log(before.guild, embed)


async def find_message_deleter(message: discord.Message) -> discord.abc.User | None:
    await asyncio.sleep(1)
    if not message.guild:
        return None
    try:
        async for entry in message.guild.audit_logs(
            limit=8,
            action=discord.AuditLogAction.message_delete,
        ):
            if not entry.target or entry.target.id != message.author.id:
                continue
            audit_channel = getattr(entry.extra, "channel", None)
            if audit_channel and audit_channel.id != message.channel.id:
                continue
            if (datetime.now(timezone.utc) - entry.created_at).total_seconds() > 10:
                continue
            return None if entry.user.id == message.author.id else entry.user
    except (discord.Forbidden, discord.HTTPException):
        return None
    return None


@bot.event
async def on_message_delete(message: discord.Message) -> None:
    if message.author.bot or not message.guild:
        return

    deleter = await find_message_deleter(message)
    embed = discord.Embed(
        title="Удалённое сообщение",
        color=COLOR,
        timestamp=moscow_time(),
    )
    if deleter:
        embed.add_field(name="Удалил(а)", value=member_id_text(deleter), inline=False)
        embed.add_field(name="Пользователю", value=member_id_text(message.author), inline=False)
    else:
        embed.add_field(name="Пользователь", value=member_id_text(message.author), inline=False)

    embed.add_field(name="Канал", value=message.channel.mention, inline=False)
    embed.add_field(name="Сообщение", value=f"> {limited_text(message.content)}", inline=False)

    if message.attachments:
        attachments = "\n".join(f"> [{item.filename}]({item.url})" for item in message.attachments)
        embed.add_field(name="Вложения", value=attachments[:1024], inline=False)

    await send_log(message.guild, embed)


# -----------------------------------------------------------------------------
# Заходы и выходы, кики
# -----------------------------------------------------------------------------

@bot.event
async def on_member_join(member: discord.Member) -> None:
    embed = discord.Embed(title="Вход на сервер", color=COLOR)
    embed.add_field(name="Пользователь", value=member_id_text(member), inline=False)
    embed.add_field(name="Дата и время входа", value=f"> {log_datetime()}", inline=False)
    await send_log(member.guild, embed)


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    await asyncio.sleep(1)
    audit = await find_audit_entry(member.guild, discord.AuditLogAction.kick, member.id)

    if audit:
        embed = discord.Embed(
            title="Выгнан пользователь",
            color=COLOR,
            timestamp=moscow_time(),
        )
        embed.add_field(name="Выгнал(а)", value=member_id_text(audit.user), inline=False)
        embed.add_field(name="Пользователя", value=member_id_text(member), inline=False)
        embed.add_field(
            name="Причина",
            value=f"> {audit.reason or 'Причина не указана'}",
            inline=False,
        )
        await send_log(member.guild, embed)
        return

    embed = discord.Embed(title="Выход с сервера", color=COLOR)
    embed.add_field(name="Пользователь", value=member_id_text(member), inline=False)
    embed.add_field(name="Дата и время выхода", value=f"> {log_datetime()}", inline=False)
    await send_log(member.guild, embed)


# -----------------------------------------------------------------------------
# Баны и разбаны
# -----------------------------------------------------------------------------

@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User) -> None:
    await asyncio.sleep(1)
    audit = await find_audit_entry(guild, discord.AuditLogAction.ban, user.id)

    embed = discord.Embed(
        title="Выдача бана",
        color=COLOR,
        timestamp=moscow_time(),
    )
    embed.add_field(
        name="Выдал(а)",
        value=member_id_text(audit.user) if audit else "Не удалось определить",
        inline=False,
    )
    embed.add_field(name="Пользователю", value=member_id_text(user), inline=False)
    embed.add_field(
        name="Причина",
        value=f"> {audit.reason if audit and audit.reason else 'Причина не указана'}",
        inline=False,
    )
    embed.add_field(name="До", value="> Навсегда", inline=False)
    await send_log(guild, embed)


@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.User) -> None:
    await asyncio.sleep(1)
    audit = await find_audit_entry(guild, discord.AuditLogAction.unban, user.id)

    embed = discord.Embed(
        title="Снятие бана",
        color=COLOR,
        timestamp=moscow_time(),
    )
    embed.add_field(
        name="Снял(а)",
        value=member_id_text(audit.user) if audit else "Не удалось определить",
        inline=False,
    )
    embed.add_field(name="Пользователю", value=member_id_text(user), inline=False)
    await send_log(guild, embed)


# -----------------------------------------------------------------------------
# Тайм-ауты и снятие тайм-аутов
# -----------------------------------------------------------------------------

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member) -> None:
    if before.timed_out_until == after.timed_out_until:
        return

    await asyncio.sleep(1)
    audit = await find_audit_entry(
        after.guild,
        discord.AuditLogAction.member_update,
        after.id,
    )
    moderator = member_id_text(audit.user) if audit else "Не удалось определить"
    reason = audit.reason if audit and audit.reason else "Причина не указана"

    if after.timed_out_until is None:
        embed = discord.Embed(
            title="Снятие тайм-аута",
            color=COLOR,
            timestamp=moscow_time(),
        )
        embed.add_field(name="Снял(а)", value=moderator, inline=False)
        embed.add_field(name="Пользователю", value=member_id_text(after), inline=False)
    else:
        embed = discord.Embed(title="Выдача тайм-аута", color=COLOR)
        embed.add_field(name="Выдал(а)", value=moderator, inline=False)
        embed.add_field(name="Пользователю", value=member_id_text(after), inline=False)
        embed.add_field(name="Причина", value=f"> {reason}", inline=False)
        embed.add_field(
            name="До",
            value=f"> {russian_datetime(after.timed_out_until)}",
            inline=False,
        )

    await send_log(after.guild, embed)


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Переменная окружения TOKEN не задана.")
    bot.run(TOKEN)
