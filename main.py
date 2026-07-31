from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks


TOKEN = os.getenv("TOKEN")

# Один канал для всех логов.
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "1531038064229748888"))

COLOR = discord.Color(0x303136)
MOSCOW_TZ = timezone(timedelta(hours=3))

# Роли с доступом ко всем новым slash-командам.
FULL_MODERATION_ROLE_IDS = {
    1518684434252169247,
    1527110780892483754,
}

# Эта роль имеет доступ только к /timeout и /untimeout.
TIMEOUT_ONLY_ROLE_IDS = {
    1526363607531520191,
}

TIMEOUT_MODERATION_ROLE_IDS = FULL_MODERATION_ROLE_IDS | TIMEOUT_ONLY_ROLE_IDS

NO_ACCESS_TITLES = {
    "ban": "Забанить пользователя",
    "unban": "Разбанить пользователя",
    "kick": "Исключить пользователя",
    "timeout": "Выдача тайм-аута",
    "untimeout": "Снятие тайм-аута",
}

BASE_DIR = Path(__file__).resolve().parent
TEMP_BANS_FILE = BASE_DIR / "temporary_bans.json"
DYNAMIC_LOGS_FILE = BASE_DIR / "dynamic_activity_logs.json"

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True
intents.moderation = True

bot = commands.Bot(command_prefix="!", intents=intents)

_commands_synced = False
_restored_temp_bans = False

# Данные команд, чтобы события логирования получили точную причину и срок.
pending_bans: dict[tuple[int, int], dict[str, Any]] = {}
pending_unbans: dict[tuple[int, int], dict[str, Any]] = {}
pending_kicks: dict[tuple[int, int], dict[str, Any]] = {}
pending_timeouts: dict[tuple[int, int], dict[str, Any]] = {}
pending_untimeouts: dict[tuple[int, int], dict[str, Any]] = {}

# Активные задачи автоматического разбана.
temporary_ban_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}


# -----------------------------------------------------------------------------
# Общие функции
# -----------------------------------------------------------------------------

def moscow_time(value: datetime | None = None) -> datetime:
    if value is None:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(MOSCOW_TZ)


def log_datetime(value: datetime | None = None) -> str:
    """Формат для логов входа/выхода: Сегодня, Вчера или точная дата."""
    if value is None:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    local = moscow_time(value)
    today = moscow_time().date()
    time_text = local.strftime("%H:%M")

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


def channel_id_text(channel: discord.abc.GuildChannel | discord.Thread) -> str:
    return f"{channel.mention}\nID: `{channel.id}`"


def limited_text(text: str | None, fallback: str = "Отсутствует") -> str:
    value = (text or "").strip() or fallback
    return value[:997] + "..." if len(value) > 1000 else value


def parse_duration(value: str) -> timedelta | None:
    match = re.fullmatch(r"\s*(\d+)\s*([mhdw])\s*", value.lower())
    if not match:
        return None

    amount = int(match.group(1))
    if amount <= 0:
        return None

    unit = match.group(2)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    return timedelta(weeks=amount)


def has_allowed_role(member: discord.Member, role_ids: set[int]) -> bool:
    return any(role.id in role_ids for role in member.roles)


def load_json(path: Path, fallback: Any) -> Any:
    try:
        if not path.exists():
            return fallback
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        print(f"Не удалось прочитать {path.name}: {error}")
        return fallback


def save_json(path: Path, data: Any) -> None:
    try:
        temporary_path = path.with_suffix(path.suffix + ".tmp")
        with temporary_path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        temporary_path.replace(path)
    except OSError as error:
        print(f"Не удалось сохранить {path.name}: {error}")


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


async def send_log(guild: discord.Guild, embed: discord.Embed) -> discord.Message | None:
    channel = await get_log_channel(guild)
    if channel is None:
        return None
    try:
        return await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException) as error:
        print(f"Ошибка отправки лога: {error}")
        return None


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


async def send_no_access(interaction: discord.Interaction, command_name: str) -> None:
    embed = discord.Embed(
        title=NO_ACCESS_TITLES.get(command_name, "Команда недоступна"),
        description=(
            f"{interaction.user.mention}, Вам недоступна **данная** команда."
        ),
        color=COLOR,
    )

    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def send_private_error(
    interaction: discord.Interaction,
    title: str,
    text: str,
) -> None:
    embed = discord.Embed(title=title, description=text, color=COLOR)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


def can_moderate_target(
    moderator: discord.Member,
    target: discord.Member,
    guild: discord.Guild,
) -> tuple[bool, str | None]:
    if moderator.id == target.id:
        return False, "Вы не можете применить эту команду к себе."
    if target.id == guild.owner_id:
        return False, "Нельзя применить эту команду к владельцу сервера."
    if moderator.id != guild.owner_id and target.top_role >= moderator.top_role:
        return False, "У пользователя равная или более высокая роль."

    bot_member = guild.me
    if bot_member is None or target.top_role >= bot_member.top_role:
        return False, "Роль бота должна находиться выше роли пользователя."
    return True, None


# -----------------------------------------------------------------------------
# Динамическое обновление «Сегодня» / «Вчера» в логах входа и выхода
# -----------------------------------------------------------------------------

def register_dynamic_activity_log(
    message: discord.Message,
    event_time: datetime,
    field_name: str,
) -> None:
    records = load_json(DYNAMIC_LOGS_FILE, [])
    records.append(
        {
            "guild_id": message.guild.id if message.guild else 0,
            "channel_id": message.channel.id,
            "message_id": message.id,
            "event_timestamp": event_time.timestamp(),
            "field_name": field_name,
        }
    )
    save_json(DYNAMIC_LOGS_FILE, records)


@tasks.loop(minutes=5)
async def refresh_dynamic_activity_logs() -> None:
    records = load_json(DYNAMIC_LOGS_FILE, [])
    if not records:
        return

    remaining: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    for record in records:
        event_time = datetime.fromtimestamp(record["event_timestamp"], timezone.utc)
        age = now - event_time

        # После двух суток текст уже превращается в точную дату и больше не меняется.
        keep_tracking = age < timedelta(days=3)

        channel = bot.get_channel(int(record["channel_id"]))
        if channel is None or not isinstance(channel, discord.abc.Messageable):
            if keep_tracking:
                remaining.append(record)
            continue

        try:
            message = await channel.fetch_message(int(record["message_id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            continue

        if not message.embeds:
            continue

        embed = message.embeds[0].copy()
        changed = False
        for index, field in enumerate(embed.fields):
            if field.name == record["field_name"]:
                new_value = f"> {log_datetime(event_time)}"
                if field.value != new_value:
                    embed.set_field_at(
                        index,
                        name=field.name,
                        value=new_value,
                        inline=field.inline,
                    )
                    changed = True
                break

        if changed:
            try:
                await message.edit(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                pass

        if keep_tracking:
            remaining.append(record)

    save_json(DYNAMIC_LOGS_FILE, remaining)


@refresh_dynamic_activity_logs.before_loop
async def before_refresh_dynamic_activity_logs() -> None:
    await bot.wait_until_ready()


# -----------------------------------------------------------------------------
# Временные баны с восстановлением после перезапуска
# -----------------------------------------------------------------------------

def load_temporary_bans() -> list[dict[str, Any]]:
    return load_json(TEMP_BANS_FILE, [])


def save_temporary_bans(records: list[dict[str, Any]]) -> None:
    save_json(TEMP_BANS_FILE, records)


def upsert_temporary_ban(guild_id: int, user_id: int, unban_at: datetime) -> None:
    records = load_temporary_bans()
    records = [
        item
        for item in records
        if not (item["guild_id"] == guild_id and item["user_id"] == user_id)
    ]
    records.append(
        {
            "guild_id": guild_id,
            "user_id": user_id,
            "unban_timestamp": unban_at.timestamp(),
        }
    )
    save_temporary_bans(records)


def remove_temporary_ban(guild_id: int, user_id: int) -> None:
    records = load_temporary_bans()
    records = [
        item
        for item in records
        if not (item["guild_id"] == guild_id and item["user_id"] == user_id)
    ]
    save_temporary_bans(records)


async def temporary_unban_worker(
    guild_id: int,
    user_id: int,
    unban_at: datetime,
) -> None:
    key = (guild_id, user_id)
    try:
        delay = max(0.0, (unban_at - datetime.now(timezone.utc)).total_seconds())
        await asyncio.sleep(delay)

        guild = bot.get_guild(guild_id)
        if guild is None:
            remove_temporary_ban(guild_id, user_id)
            return

        try:
            user = await bot.fetch_user(user_id)
            pending_unbans[key] = {
                "reason": "Истёк срок временного бана",
                "moderator": bot.user,
            }
            await guild.unban(user, reason="Истёк срок временного бана")
        except discord.NotFound:
            pass
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"Не удалось автоматически разбанить {user_id}: {error}")
            return

        remove_temporary_ban(guild_id, user_id)
    finally:
        temporary_ban_tasks.pop(key, None)


def schedule_temporary_unban(guild_id: int, user_id: int, unban_at: datetime) -> None:
    key = (guild_id, user_id)
    old_task = temporary_ban_tasks.get(key)
    if old_task and not old_task.done():
        old_task.cancel()

    temporary_ban_tasks[key] = asyncio.create_task(
        temporary_unban_worker(guild_id, user_id, unban_at)
    )


async def restore_temporary_bans() -> None:
    for item in load_temporary_bans():
        try:
            unban_at = datetime.fromtimestamp(item["unban_timestamp"], timezone.utc)
            schedule_temporary_unban(
                int(item["guild_id"]),
                int(item["user_id"]),
                unban_at,
            )
        except (KeyError, TypeError, ValueError):
            continue


# -----------------------------------------------------------------------------
# Запуск
# -----------------------------------------------------------------------------

@bot.event
async def on_ready() -> None:
    global _commands_synced, _restored_temp_bans

    if not _commands_synced:
        try:
            await bot.tree.sync()
            _commands_synced = True
        except discord.HTTPException as error:
            print(f"Не удалось синхронизировать slash-команды: {error}")

    if not _restored_temp_bans:
        await restore_temporary_bans()
        _restored_temp_bans = True

    if not refresh_dynamic_activity_logs.is_running():
        refresh_dynamic_activity_logs.start()

    await bot.change_presence(status=discord.Status.idle)
    print(f"Бот запущен: {bot.user}")
    print(f"Все логи отправляются в канал ID: {LOG_CHANNEL_ID}")


# -----------------------------------------------------------------------------
# Slash-команды модерации
# -----------------------------------------------------------------------------

@bot.tree.command(name="ban", description="Забанить пользователя")
@app_commands.describe(
    пользователь="Пользователь, которого нужно забанить",
    причина="Причина бана",
    время="Формат: m (минуты), h (часы), d (дни)",
)
@app_commands.guild_only()
async def ban_command(
    interaction: discord.Interaction,
    пользователь: discord.Member,
    причина: str = "Не указана",
    время: str | None = None,
) -> None:
    guild = interaction.guild
    moderator = interaction.user
    if guild is None or not isinstance(moderator, discord.Member):
        return

    if not has_allowed_role(moderator, FULL_MODERATION_ROLE_IDS):
        await send_no_access(interaction, "ban")
        return

    allowed, error = can_moderate_target(moderator, пользователь, guild)
    if not allowed:
        await send_private_error(interaction, "Забанить пользователя", error or "Команда недоступна.")
        return

    duration: timedelta | None = None
    unban_at: datetime | None = None
    if время:
        duration = parse_duration(время)
        if duration is None:
            await send_private_error(
                interaction,
                "Забанить пользователя",
                "Неверный формат времени. Используйте `30m`, `12h`, `7d`",
            )
            return
        unban_at = datetime.now(timezone.utc) + duration

    reason = limited_text(причина, "Не указана")
    key = (guild.id, пользователь.id)
    pending_bans[key] = {
        "moderator": moderator,
        "reason": reason,
        "unban_at": unban_at,
    }

    try:
        await пользователь.ban(
            reason=f"{reason} | Модератор: {moderator} ({moderator.id})"
        )
    except discord.Forbidden:
        pending_bans.pop(key, None)
        await send_private_error(interaction, "Забанить пользователя", "У бота недостаточно прав для выдачи бана.")
        return
    except discord.HTTPException:
        pending_bans.pop(key, None)
        await send_private_error(interaction, "Забанить пользователя", "Discord не смог выполнить выдачу бана.")
        return

    if unban_at is not None:
        upsert_temporary_ban(guild.id, пользователь.id, unban_at)
        schedule_temporary_unban(guild.id, пользователь.id, unban_at)
    else:
        remove_temporary_ban(guild.id, пользователь.id)

    embed = discord.Embed(
        title="Выдача бана",
        description=(
            f"{moderator.mention}, Вы успешно **забанили** {пользователь.mention}!"
        ),
        color=COLOR,
    )
    embed.add_field(name="Причина", value=f"> {reason}", inline=False)
    embed.add_field(
        name="До",
        value="> Навсегда" if unban_at is None else f"> {russian_datetime(unban_at)}",
        inline=False,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="unban", description="Разбанить пользователя")
@app_commands.describe(
    пользователь="ID или упоминание забаненного пользователя",
    причина="Причина снятия бана",
)
@app_commands.guild_only()
async def unban_command(
    interaction: discord.Interaction,
    пользователь: str,
    причина: str = "Не указана",
) -> None:
    guild = interaction.guild
    moderator = interaction.user
    if guild is None or not isinstance(moderator, discord.Member):
        return

    if not has_allowed_role(moderator, FULL_MODERATION_ROLE_IDS):
        await send_no_access(interaction, "unban")
        return

    match = re.search(r"\d{15,22}", пользователь)
    if not match:
        await send_private_error(interaction, "Разбанить пользователя", "Укажите корректный ID пользователя.")
        return

    user_id = int(match.group(0))
    try:
        user = await bot.fetch_user(user_id)
        await guild.fetch_ban(user)
    except discord.NotFound:
        await send_private_error(interaction, "Разбанить пользователя", "Этот пользователь не находится в бане.")
        return
    except discord.HTTPException:
        await send_private_error(interaction, "Разбанить пользователя", "Не удалось получить данные пользователя.")
        return

    reason = limited_text(причина, "Не указана")
    key = (guild.id, user.id)
    pending_unbans[key] = {"moderator": moderator, "reason": reason}

    try:
        await guild.unban(
            user,
            reason=f"{reason} | Модератор: {moderator} ({moderator.id})",
        )
    except discord.Forbidden:
        pending_unbans.pop(key, None)
        await send_private_error(interaction, "Разбанить пользователя", "У бота недостаточно прав для снятия бана.")
        return
    except discord.HTTPException:
        pending_unbans.pop(key, None)
        await send_private_error(interaction, "Разбанить пользователя", "Discord не смог выполнить снятие бана.")
        return

    remove_temporary_ban(guild.id, user.id)
    task = temporary_ban_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()

    embed = discord.Embed(
        title="Снятие бана",
        description=(
            f"{moderator.mention}, Вы успешно **разбанили** {user.mention}!"
        ),
        color=COLOR,
    )
    embed.add_field(name="Причина", value=f"> {reason}", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="kick", description="Исключить пользователя")
@app_commands.describe(
    пользователь="Пользователь, которого нужно исключить",
    причина="Причина исключения",
)
@app_commands.guild_only()
async def kick_command(
    interaction: discord.Interaction,
    пользователь: discord.Member,
    причина: str = "Не указана",
) -> None:
    guild = interaction.guild
    moderator = interaction.user
    if guild is None or not isinstance(moderator, discord.Member):
        return

    if not has_allowed_role(moderator, FULL_MODERATION_ROLE_IDS):
        await send_no_access(interaction, "kick")
        return

    allowed, error = can_moderate_target(moderator, пользователь, guild)
    if not allowed:
        await send_private_error(interaction, "Исключить пользователя", error or "Команда недоступна.")
        return

    reason = limited_text(причина, "Не указана")
    key = (guild.id, пользователь.id)
    pending_kicks[key] = {"moderator": moderator, "reason": reason}

    try:
        await пользователь.kick(
            reason=f"{reason} | Модератор: {moderator} ({moderator.id})"
        )
    except discord.Forbidden:
        pending_kicks.pop(key, None)
        await send_private_error(interaction, "Исключить пользователя", "У бота недостаточно прав для исключения пользователя.")
        return
    except discord.HTTPException:
        pending_kicks.pop(key, None)
        await send_private_error(interaction, "Исключить пользователя", "Discord не смог выполнить исключение.")
        return

    embed = discord.Embed(
        title="Исключение пользователя",
        description=(
            f"{moderator.mention}, Вы успешно **выгнали** {пользователь.mention}!"
        ),
        color=COLOR,
    )
    embed.add_field(name="Причина", value=f"> {reason}", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="timeout", description="Выдать пользователю тайм-аут")
@app_commands.describe(
    пользователь="Пользователь, которому нужно выдать тайм-аут",
    время="Формат: m (минуты), h (часы), d (дни)",
    причина="Причина тайм-аута",
)
@app_commands.guild_only()
async def timeout_command(
    interaction: discord.Interaction,
    пользователь: discord.Member,
    время: str,
    причина: str = "Не указана",
) -> None:
    guild = interaction.guild
    moderator = interaction.user
    if guild is None or not isinstance(moderator, discord.Member):
        return

    if not has_allowed_role(moderator, TIMEOUT_MODERATION_ROLE_IDS):
        await send_no_access(interaction, "timeout")
        return

    allowed, error = can_moderate_target(moderator, пользователь, guild)
    if not allowed:
        await send_private_error(interaction, "Выдача тайм-аута", error or "Команда недоступна.")
        return

    duration = parse_duration(время)
    if duration is None:
        await send_private_error(
            interaction,
            "Выдача тайм-аута",
            "Неверный формат времени. Используйте `30m`, `12h`, `7d` или `2w`.",
        )
        return

    # Ограничение Discord — не более 28 дней.
    if duration > timedelta(days=28):
        await send_private_error(interaction, "Выдача тайм-аута", "Тайм-аут не может быть дольше 28 дней.")
        return

    until = datetime.now(timezone.utc) + duration
    reason = limited_text(причина, "Не указана")
    key = (guild.id, пользователь.id)
    pending_timeouts[key] = {
        "moderator": moderator,
        "reason": reason,
        "until": until,
    }

    try:
        await пользователь.timeout(
            until,
            reason=f"{reason} | Модератор: {moderator} ({moderator.id})",
        )
    except discord.Forbidden:
        pending_timeouts.pop(key, None)
        await send_private_error(interaction, "Выдача тайм-аута", "У бота недостаточно прав для выдачи тайм-аута.")
        return
    except discord.HTTPException:
        pending_timeouts.pop(key, None)
        await send_private_error(interaction, "Выдача тайм-аута", "Discord не смог выполнить выдачу тайм-аута.")
        return

    embed = discord.Embed(
        title="Выдача тайм-аута",
        description=(
            f"{moderator.mention}, Вы успешно **выдали** тайм-аут {пользователь.mention}!"
        ),
        color=COLOR,
    )
    embed.add_field(name="Причина", value=f"> {reason}", inline=False)
    embed.add_field(name="До", value=f"> {russian_datetime(until)}", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="untimeout", description="Снять тайм-аут с пользователя")
@app_commands.describe(
    пользователь="Пользователь, у которого нужно снять тайм-аут",
    причина="Причина снятия тайм-аута",
)
@app_commands.guild_only()
async def untimeout_command(
    interaction: discord.Interaction,
    пользователь: discord.Member,
    причина: str = "Не указана",
) -> None:
    guild = interaction.guild
    moderator = interaction.user
    if guild is None or not isinstance(moderator, discord.Member):
        return

    if not has_allowed_role(moderator, TIMEOUT_MODERATION_ROLE_IDS):
        await send_no_access(interaction, "untimeout")
        return

    allowed, error = can_moderate_target(moderator, пользователь, guild)
    if not allowed:
        await send_private_error(interaction, "Снятие тайм-аута", error or "Команда недоступна.")
        return

    if пользователь.timed_out_until is None:
        await send_private_error(interaction, "Снятие тайм-аута", "У пользователя нет активного тайм-аута.")
        return

    reason = limited_text(причина, "Не указана")
    key = (guild.id, пользователь.id)
    pending_untimeouts[key] = {"moderator": moderator, "reason": reason}

    try:
        await пользователь.timeout(
            None,
            reason=f"{reason} | Модератор: {moderator} ({moderator.id})",
        )
    except discord.Forbidden:
        pending_untimeouts.pop(key, None)
        await send_private_error(interaction, "Снятие тайм-аута", "У бота недостаточно прав для снятия тайм-аута.")
        return
    except discord.HTTPException:
        pending_untimeouts.pop(key, None)
        await send_private_error(interaction, "Снятие тайм-аута", "Discord не смог выполнить снятие тайм-аута.")
        return

    embed = discord.Embed(
        title="Снятие тайм-аута",
        description=(
            f"{moderator.mention}, Вы успешно **сняли** тайм-аут с {пользователь.mention}!"
        ),
        color=COLOR,
    )
    embed.add_field(name="Причина", value=f"> {reason}", inline=False)
    await interaction.response.send_message(embed=embed)


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
    embed.add_field(name="Канал", value=channel_id_text(before.channel), inline=False)
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

    embed.add_field(name="Канал", value=channel_id_text(message.channel), inline=False)

    if message.content and message.content.strip():
        embed.add_field(
            name="Сообщение",
            value=f"> {limited_text(message.content)}",
            inline=False,
        )

    if message.attachments:
        attachments = "\n".join(
            f"> [{item.filename}]({item.url})" for item in message.attachments
        )
        attachment_field_name = "Вложение" if len(message.attachments) == 1 else "Вложения"
        embed.add_field(
            name=attachment_field_name,
            value=attachments[:1024],
            inline=False,
        )

    await send_log(message.guild, embed)


# -----------------------------------------------------------------------------
# Заходы, выходы и кики
# -----------------------------------------------------------------------------

@bot.event
async def on_member_join(member: discord.Member) -> None:
    event_time = datetime.now(timezone.utc)
    embed = discord.Embed(title="Вход на сервер", color=COLOR)
    embed.add_field(name="Пользователь", value=member_id_text(member), inline=False)
    embed.add_field(name="Дата и время входа", value=f"> {log_datetime(event_time)}", inline=False)
    message = await send_log(member.guild, embed)
    if message:
        register_dynamic_activity_log(message, event_time, "Дата и время входа")


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    event_time = datetime.now(timezone.utc)
    key = (member.guild.id, member.id)

    await asyncio.sleep(1)
    pending = pending_kicks.pop(key, None)
    audit = None if pending else await find_audit_entry(
        member.guild,
        discord.AuditLogAction.kick,
        member.id,
    )

    if pending or audit:
        moderator = pending.get("moderator") if pending else audit.user
        reason = pending.get("reason") if pending else (audit.reason or "Причина не указана")

        embed = discord.Embed(
            title="Выгнан пользователь",
            color=COLOR,
            timestamp=moscow_time(),
        )
        embed.add_field(name="Выгнал(а)", value=member_id_text(moderator), inline=False)
        embed.add_field(name="Пользователя", value=member_id_text(member), inline=False)
        embed.add_field(name="Причина", value=f"> {reason}", inline=False)
        await send_log(member.guild, embed)
        return

    embed = discord.Embed(title="Выход с сервера", color=COLOR)
    embed.add_field(name="Пользователь", value=member_id_text(member), inline=False)
    embed.add_field(name="Дата и время выхода", value=f"> {log_datetime(event_time)}", inline=False)
    message = await send_log(member.guild, embed)
    if message:
        register_dynamic_activity_log(message, event_time, "Дата и время выхода")


# -----------------------------------------------------------------------------
# Баны и разбаны
# -----------------------------------------------------------------------------

@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User) -> None:
    key = (guild.id, user.id)
    await asyncio.sleep(1)
    pending = pending_bans.pop(key, None)
    audit = None if pending else await find_audit_entry(guild, discord.AuditLogAction.ban, user.id)

    moderator = pending.get("moderator") if pending else (audit.user if audit else None)
    reason = pending.get("reason") if pending else (
        audit.reason if audit and audit.reason else "Причина не указана"
    )
    unban_at = pending.get("unban_at") if pending else None

    embed = discord.Embed(
        title="Выдача бана",
        color=COLOR,
        timestamp=moscow_time(),
    )
    embed.add_field(
        name="Выдал(а)",
        value=member_id_text(moderator) if moderator else "Не удалось определить",
        inline=False,
    )
    embed.add_field(name="Пользователю", value=member_id_text(user), inline=False)
    embed.add_field(name="Причина", value=f"> {reason}", inline=False)
    embed.add_field(
        name="До",
        value="> Навсегда" if unban_at is None else f"> {russian_datetime(unban_at)}",
        inline=False,
    )
    await send_log(guild, embed)


@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.User) -> None:
    key = (guild.id, user.id)
    await asyncio.sleep(1)
    pending = pending_unbans.pop(key, None)
    audit = None if pending else await find_audit_entry(guild, discord.AuditLogAction.unban, user.id)

    moderator = pending.get("moderator") if pending else (audit.user if audit else None)
    reason = pending.get("reason") if pending else (
        audit.reason if audit and audit.reason else "Причина не указана"
    )

    remove_temporary_ban(guild.id, user.id)
    task = temporary_ban_tasks.pop(key, None)
    if task and not task.done() and task is not asyncio.current_task():
        task.cancel()

    embed = discord.Embed(
        title="Снятие бана",
        color=COLOR,
        timestamp=moscow_time(),
    )
    embed.add_field(
        name="Снял(а)",
        value=member_id_text(moderator) if moderator else "Не удалось определить",
        inline=False,
    )
    embed.add_field(name="Пользователю", value=member_id_text(user), inline=False)
    embed.add_field(name="Причина", value=f"> {reason}", inline=False)
    await send_log(guild, embed)


# -----------------------------------------------------------------------------
# Тайм-ауты и снятие тайм-аутов
# -----------------------------------------------------------------------------

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member) -> None:
    if before.timed_out_until == after.timed_out_until:
        return

    key = (after.guild.id, after.id)
    await asyncio.sleep(1)

    is_removal = after.timed_out_until is None
    pending = (
        pending_untimeouts.pop(key, None)
        if is_removal
        else pending_timeouts.pop(key, None)
    )

    audit = None if pending else await find_audit_entry(
        after.guild,
        discord.AuditLogAction.member_update,
        after.id,
    )

    moderator = pending.get("moderator") if pending else (audit.user if audit else None)
    reason = pending.get("reason") if pending else (
        audit.reason if audit and audit.reason else "Причина не указана"
    )

    if is_removal:
        embed = discord.Embed(
            title="Снятие тайм-аута",
            color=COLOR,
            timestamp=moscow_time(),
        )
        embed.add_field(
            name="Снял(а)",
            value=member_id_text(moderator) if moderator else "Не удалось определить",
            inline=False,
        )
        embed.add_field(name="Пользователю", value=member_id_text(after), inline=False)
        embed.add_field(name="Причина", value=f"> {reason}", inline=False)
    else:
        until = pending.get("until") if pending else after.timed_out_until
        embed = discord.Embed(title="Выдача тайм-аута", color=COLOR)
        embed.add_field(
            name="Выдал(а)",
            value=member_id_text(moderator) if moderator else "Не удалось определить",
            inline=False,
        )
        embed.add_field(name="Пользователю", value=member_id_text(after), inline=False)
        embed.add_field(name="Причина", value=f"> {reason}", inline=False)
        embed.add_field(
            name="До",
            value=f"> {russian_datetime(until)}",
            inline=False,
        )

    await send_log(after.guild, embed)


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Переменная окружения TOKEN не задана.")
    bot.run(TOKEN)
