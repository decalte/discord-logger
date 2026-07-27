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
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "1531038064229748888"))

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


async def send_log(
    guild: discord.Guild,
    embed: discord.Embed,
    files: list[discord.File] | None = None,
) -> None:
    channel = await get_log_channel(guild)
    if channel is None:
        return
    try:
        await channel.send(embed=embed, files=files or [])
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
    await bot.change_presence(status=discord.Status.idle)
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
    embed.add_field(name="Было", value=f">>> {limited_text(before.content, 'Текст отсутствует')}", inline=False)
    embed.add_field(name="Стало", value=f">>> {limited_text(after.content, 'Текст отсутствует')}", inline=False)
    embed.add_field(name="Ссылка", value=f"> [Перейти к сообщению]({after.jump_url})", inline=False)
    await send_log(before.guild, embed)


async def find_message_deleter(message: discord.Message) -> discord.abc.User | None:
    """Определяет модератора для одиночного удаления сообщения."""
    await asyncio.sleep(1.5)
    if not message.guild:
        return None

    try:
        async for entry in message.guild.audit_logs(
            limit=20,
            action=discord.AuditLogAction.message_delete,
        ):
            if not entry.target or entry.target.id != message.author.id:
                continue

            audit_channel = getattr(entry.extra, "channel", None)
            if audit_channel and audit_channel.id != message.channel.id:
                continue

            age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
            if age <= 15:
                return entry.user
    except (discord.Forbidden, discord.HTTPException):
        return None

    return None


async def find_bulk_message_deleter(
    guild: discord.Guild,
    channel_id: int,
) -> discord.abc.User | None:
    """Определяет модератора, который массово удалил сообщения."""
    await asyncio.sleep(1.5)

    try:
        async for entry in guild.audit_logs(
            limit=20,
            action=discord.AuditLogAction.message_bulk_delete,
        ):
            target_id = getattr(entry.target, "id", None)
            extra_channel = getattr(entry.extra, "channel", None)
            extra_channel_id = getattr(extra_channel, "id", None)

            if channel_id not in (target_id, extra_channel_id):
                continue

            age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
            if age <= 15:
                return entry.user
    except (discord.Forbidden, discord.HTTPException):
        return None

    return None


async def save_message_attachments(message: discord.Message) -> list[discord.File]:
    saved_files: list[discord.File] = []
    for attachment in message.attachments:
        try:
            saved_files.append(await attachment.to_file(use_cached=True))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
            print(f"Не удалось сохранить вложение {attachment.filename}: {error}")
    return saved_files


async def send_deleted_message_log(
    message: discord.Message,
    deleter: discord.abc.User | None,
    saved_files: list[discord.File],
) -> None:
    if not message.guild:
        return

    embed = discord.Embed(
        title="Удалённое сообщение",
        color=COLOR,
        timestamp=moscow_time(),
    )

    embed.add_field(
        name="Удалил(а)",
        value=member_id_text(deleter) if deleter else "Не удалось определить",
        inline=False,
    )
    embed.add_field(
        name="Пользователю",
        value=member_id_text(message.author),
        inline=False,
    )
    embed.add_field(name="Канал", value=message.channel.mention, inline=False)

    if message.content and message.content.strip():
        embed.add_field(
            name="Сообщение",
            value=f">>> {limited_text(message.content)}",
            inline=False,
        )

    if message.attachments:
        attachment_field_name = "Вложение" if len(message.attachments) == 1 else "Вложения"
        saved_names = {file.filename for file in saved_files}
        attachment_lines = []
        for item in message.attachments:
            status = "сохранено ниже" if item.filename in saved_names else "не удалось сохранить"
            attachment_lines.append(f"> `{item.filename}` — {status}")

        embed.add_field(
            name=attachment_field_name,
            value="\n".join(attachment_lines)[:1024],
            inline=False,
        )

    await send_log(message.guild, embed, files=saved_files)


@bot.event
async def on_message_delete(message: discord.Message) -> None:
    if message.author.bot or not message.guild:
        return

    saved_files = await save_message_attachments(message)
    deleter = await find_message_deleter(message)
    await send_deleted_message_log(message, deleter, saved_files)


@bot.event
async def on_bulk_message_delete(messages: list[discord.Message]) -> None:
    valid_messages = [
        message
        for message in messages
        if message.guild and not message.author.bot
    ]
    if not valid_messages:
        return

    # Вложения сохраняются до ожидания журнала аудита, пока они ещё доступны.
    saved_by_message: dict[int, list[discord.File]] = {}
    for message in valid_messages:
        saved_by_message[message.id] = await save_message_attachments(message)

    first = valid_messages[0]
    deleter = await find_bulk_message_deleter(first.guild, first.channel.id)

    for message in valid_messages:
        await send_deleted_message_log(
            message,
            deleter,
            saved_by_message.get(message.id, []),
        )


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
