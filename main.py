from __future__ import annotations

import asyncio
import json
import os
import re
from io import BytesIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands


TOKEN = os.getenv("TOKEN")

# Единый канал для оставленных логов.
LOG_CHANNEL_ID = 1535408325817008238

# Приватные голосовые комнаты.
PRIVATE_ROOM_CONTROL_CHANNEL_ID = 1535168199203491860
PRIVATE_ROOM_CREATE_CHANNEL_ID = 1535168165309317192

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

# Роли с доступом к /clear. Владелец сервера также имеет доступ.
CLEAR_ROLE_IDS = {
    1527110780892483754,
    1526363607531520191,
}

NO_ACCESS_TITLES = {
    "ban": "Забанить пользователя",
    "unban": "Разбанить пользователя",
    "kick": "Исключить пользователя",
    "timeout": "Выдать тайм-аут",
    "untimeout": "Снять тайм-аут",
    "clear": "Удалить сообщения",
}

BASE_DIR = Path(__file__).resolve().parent
TEMP_BANS_FILE = BASE_DIR / "temporary_bans.json"
PRIVATE_ROOMS_FILE = BASE_DIR / "private_rooms.json"

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True
intents.moderation = True

bot = commands.Bot(command_prefix="!", intents=intents)

_commands_synced = False
_restored_temp_bans = False
_private_room_view_registered = False
_private_room_panel_ready = False

# Данные команд, чтобы события логирования получили точную причину и срок.
pending_bans: dict[tuple[int, int], dict[str, Any]] = {}
pending_unbans: dict[tuple[int, int], dict[str, Any]] = {}
pending_kicks: dict[tuple[int, int], dict[str, Any]] = {}
pending_timeouts: dict[tuple[int, int], dict[str, Any]] = {}
pending_untimeouts: dict[tuple[int, int], dict[str, Any]] = {}

# Активные задачи автоматического разбана.
temporary_ban_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}

# Задачи, которые гарантированно отправляют лог после окончания тайм-аута.
timeout_expiry_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}

# Каналы, в которых /clear сейчас удаляет сообщения. Нужен, чтобы не создавать
# отдельный лог на каждое удалённое сообщение.
clear_in_progress_channels: set[int] = set()

# Активные приватные комнаты: channel_id -> owner_id.
private_room_owners: dict[int, int] = {}
# Быстрый индекс: (guild_id, owner_id) -> channel_id.
private_room_channels: dict[tuple[int, int], int] = {}
# Защита от двойного создания/удаления при нескольких voice-state событиях подряд.
private_room_locks: dict[tuple[int, int], asyncio.Lock] = {}
private_room_delete_locks: set[int] = set()


# -----------------------------------------------------------------------------
# Общие функции
# -----------------------------------------------------------------------------

def moscow_time(value: datetime | None = None) -> datetime:
    if value is None:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(MOSCOW_TZ)


def discord_datetime(value: datetime | None = None) -> str:
    """Discord сам показывает дату и время в часовом поясе каждого пользователя."""
    if value is None:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return f"<t:{int(value.timestamp())}:f>"


def member_id_text(user: discord.abc.User) -> str:
    return f"{user.mention}\nID: `{user.id}`"


def channel_id_text(channel: discord.abc.GuildChannel | discord.Thread) -> str:
    return f"{channel.mention}\nID: `{channel.id}`"


def category_id_text(category: discord.CategoryChannel) -> str:
    # В логах создания/удаления каналов для категории показываем только название,
    # без ID категории.
    return f"> {category.name}"


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


def russian_message_count(value: int) -> str:
    last_two = value % 100
    last_one = value % 10

    if 11 <= last_two <= 14:
        word = "сообщений"
    elif last_one == 1:
        word = "сообщение"
    elif 2 <= last_one <= 4:
        word = "сообщения"
    else:
        word = "сообщений"

    return f"{value} {word}"


def build_clear_report(
    guild: discord.Guild,
    channel: discord.abc.GuildChannel | discord.Thread,
    moderator: discord.Member,
    messages: list[discord.Message],
    selected_user: discord.Member | None,
) -> discord.File:
    cleared_at = moscow_time()
    lines = [
        "ОТЧЁТ ОБ УДАЛЕНИИ СООБЩЕНИЙ",
        "",
        f"Сервер: {guild.name}",
        f"ID сервера: {guild.id}",
        f"Канал: #{getattr(channel, 'name', str(channel))}",
        f"ID канала: {channel.id}",
        f"Исполнитель: {moderator}",
        f"ID исполнителя: {moderator.id}",
        f"Время очистки (МСК): {cleared_at.strftime('%d.%m.%Y %H:%M:%S')}",
        f"Фильтр пользователя: {selected_user} ({selected_user.id})" if selected_user else "Фильтр пользователя: все пользователи",
        f"Количество удалённых сообщений: {len(messages)}",
        "",
    ]

    separator = "-" * 72
    for index, message in enumerate(sorted(messages, key=lambda item: item.created_at), start=1):
        created_at = moscow_time(message.created_at)
        content = message.content.strip() if message.content and message.content.strip() else "[Текст отсутствует]"
        lines.extend(
            [
                separator,
                f"Сообщение #{index}",
                f"Автор: {message.author}",
                f"ID автора: {message.author.id}",
                f"ID сообщения: {message.id}",
                f"Время отправки (МСК): {created_at.strftime('%d.%m.%Y %H:%M:%S')}",
                "Текст:",
                content,
            ]
        )
        if message.attachments:
            lines.append("Вложения:")
            lines.extend(f"- {item.filename}: {item.url}" for item in message.attachments)
        if message.reference and message.reference.message_id:
            lines.append(f"Ответ на сообщение ID: {message.reference.message_id}")
        lines.append("")

    if not messages:
        lines.append("Сообщения не были найдены или удалены.")

    data = "\n".join(lines).encode("utf-8")
    filename = f"clear-log-{cleared_at.strftime('%Y-%m-%d_%H-%M-%S')}.txt"
    return discord.File(BytesIO(data), filename=filename)


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


async def get_log_channel(
    guild: discord.Guild,
    channel_id: int,
) -> discord.abc.Messageable | None:
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
            print(f"Не удалось получить канал логов {channel_id}: {error}")
            return None

    if not isinstance(channel, discord.abc.Messageable):
        print(f"Канал {channel_id} не поддерживает отправку сообщений.")
        return None
    return channel


async def send_log_to(
    guild: discord.Guild,
    embed: discord.Embed,
    channel_id: int,
    file: discord.File | None = None,
) -> discord.Message | None:
    channel = await get_log_channel(guild, channel_id)
    if channel is None:
        return None
    try:
        return await channel.send(embed=embed, file=file, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException) as error:
        print(f"Ошибка отправки лога в канал {channel_id}: {error}")
        return None


async def send_log(guild: discord.Guild, embed: discord.Embed) -> discord.Message | None:
    return await send_log_to(guild, embed, LOG_CHANNEL_ID)


async def send_server_log(guild: discord.Guild, embed: discord.Embed) -> discord.Message | None:
    return await send_log_to(guild, embed, LOG_CHANNEL_ID)


async def send_message_log(guild: discord.Guild, embed: discord.Embed) -> discord.Message | None:
    return await send_log_to(guild, embed, LOG_CHANNEL_ID)




async def find_audit_entry(
    guild: discord.Guild,
    action: discord.AuditLogAction,
    target_id: int,
    max_age: int = 30,
) -> discord.AuditLogEntry | None:
    """Ищет свежую запись аудита для конкретной цели.

    Берём больше записей, потому что на активном сервере нужная запись легко
    уезжает за первые 10-12 событий ещё до прихода gateway-события.
    """
    try:
        async for entry in guild.audit_logs(limit=50, action=action):
            if not entry.target or entry.target.id != target_id:
                continue
            age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
            if 0 <= age <= max_age:
                return entry
    except discord.Forbidden:
        print(f"Нет права на просмотр журнала аудита: {guild.name}")
    except discord.HTTPException as error:
        print(f"Ошибка получения журнала аудита: {error}")
    return None


async def find_channel_overwrite_actor(
    guild: discord.Guild,
    channel_id: int,
    *,
    max_age: int = 30,
) -> discord.abc.User | None:
    """Определяет автора изменения прав канала.

    Discord пишет изменения permission overwrites отдельными действиями
    overwrite_create/update/delete, а не channel_update. Поэтому обычный поиск
    по target_id канала для таких изменений всегда часто возвращал None.
    """
    overwrite_actions = {
        discord.AuditLogAction.overwrite_create,
        discord.AuditLogAction.overwrite_update,
        discord.AuditLogAction.overwrite_delete,
    }
    try:
        async for entry in guild.audit_logs(limit=50):
            age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
            if age < 0 or age > max_age:
                continue
            if entry.action not in overwrite_actions:
                continue

            # Для overwrite_* target — это сам канал, а extra — роль/участник,
            # чьи права были изменены.
            audit_channel_id = getattr(entry.target, "id", None)
            if audit_channel_id == channel_id:
                return entry.user
    except discord.Forbidden:
        print(f"Нет права на просмотр журнала аудита: {guild.name}")
    except discord.HTTPException as error:
        print(f"Ошибка получения журнала аудита прав канала: {error}")
    return None


async def find_voice_move_actor(
    guild: discord.Guild,
    destination_channel_id: int,
    *,
    attempts: int = 8,
    delay: float = 0.5,
    max_age: int = 6,
) -> discord.abc.User | None:
    """Ищет модератора, который принудительно переместил участника.

    У MEMBER_MOVE Discord не гарантирует target_id перемещённого пользователя.
    Поэтому сопоставляем свежую запись по каналу назначения. Если записи нет,
    значит участник, скорее всего, перешёл между каналами самостоятельно.
    """
    for attempt in range(attempts):
        try:
            async for entry in guild.audit_logs(
                limit=30,
                action=discord.AuditLogAction.member_move,
            ):
                age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
                if age < 0 or age > max_age:
                    continue

                audit_channel = getattr(entry.extra, "channel", None)
                audit_channel_id = getattr(audit_channel, "id", None)
                if audit_channel_id is None:
                    audit_channel_id = getattr(entry.extra, "channel_id", None)

                if audit_channel_id == destination_channel_id:
                    return entry.user
        except discord.Forbidden:
            print(f"Нет права на просмотр журнала аудита: {guild.name}")
            return None
        except discord.HTTPException as error:
            print(f"Ошибка получения журнала аудита перемещений: {error}")

        if attempt < attempts - 1:
            await asyncio.sleep(delay)
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
# Приватные голосовые комнаты
# -----------------------------------------------------------------------------

def default_private_room_settings() -> dict[str, Any]:
    return {
        "name": None,
        "limit": 0,
        "locked": False,
        "hidden": False,
        "allowed_users": [],
        "denied_users": [],
        "muted_users": [],
    }


def load_private_room_data() -> dict[str, Any]:
    data = load_json(PRIVATE_ROOMS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_private_room_data(data: dict[str, Any]) -> None:
    save_json(PRIVATE_ROOMS_FILE, data)


def get_private_room_settings(guild_id: int, user_id: int) -> dict[str, Any]:
    data = load_private_room_data()
    guild_data = data.get(str(guild_id), {})
    raw = guild_data.get(str(user_id), {}) if isinstance(guild_data, dict) else {}
    settings = default_private_room_settings()
    if isinstance(raw, dict):
        settings.update({key: raw.get(key, value) for key, value in settings.items()})
    for key in ("allowed_users", "denied_users", "muted_users"):
        values = settings.get(key)
        settings[key] = [int(item) for item in values if str(item).isdigit()] if isinstance(values, list) else []
    settings["limit"] = max(0, min(int(settings.get("limit") or 0), 99))
    settings["locked"] = bool(settings.get("locked"))
    settings["hidden"] = bool(settings.get("hidden"))
    return settings


def update_private_room_settings(guild_id: int, user_id: int, **changes: Any) -> dict[str, Any]:
    data = load_private_room_data()
    guild_key = str(guild_id)
    user_key = str(user_id)
    guild_data = data.setdefault(guild_key, {})
    current = get_private_room_settings(guild_id, user_id)
    current.update(changes)
    guild_data[user_key] = current
    save_private_room_data(data)
    return current


def private_room_name(member: discord.Member, settings: dict[str, Any]) -> str:
    custom_name = (settings.get("name") or "").strip()
    if custom_name:
        return custom_name[:100]
    return f"Комната {member.name}"[:100]


def get_private_room_by_owner(guild: discord.Guild, owner_id: int) -> discord.VoiceChannel | None:
    channel_id = private_room_channels.get((guild.id, owner_id))
    if channel_id is None:
        return None
    channel = guild.get_channel(channel_id)
    if isinstance(channel, discord.VoiceChannel):
        return channel
    private_room_channels.pop((guild.id, owner_id), None)
    private_room_owners.pop(channel_id, None)
    return None


def get_owned_private_room(member: discord.Member) -> discord.VoiceChannel | None:
    return get_private_room_by_owner(member.guild, member.id)


def private_room_owner(channel: discord.VoiceChannel) -> discord.Member | None:
    owner_id = private_room_owners.get(channel.id)
    return channel.guild.get_member(owner_id) if owner_id else None


async def send_private_room_reply(
    interaction: discord.Interaction,
    title: str,
    description: str,
    *,
    view: discord.ui.View | None = None,
) -> None:
    embed = discord.Embed(title=title, description=description, color=COLOR)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def require_private_room(
    interaction: discord.Interaction,
    title: str,
) -> tuple[discord.Member, discord.VoiceChannel] | None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return None
    member = interaction.user
    channel = get_owned_private_room(member)
    if channel is None:
        await send_private_room_reply(
            interaction,
            title,
            f"{member.mention}, у Вас **нет** своей приватной комнаты.",
        )
        return None
    return member, channel


async def apply_private_room_permissions(
    channel: discord.VoiceChannel,
    owner: discord.Member,
    settings: dict[str, Any],
) -> None:
    default_role = channel.guild.default_role
    await channel.set_permissions(
        default_role,
        connect=False if settings.get("locked") else None,
        view_channel=False if settings.get("hidden") else None,
        reason="Настройки приватной комнаты",
    )
    await channel.set_permissions(
        owner,
        view_channel=True,
        connect=True,
        speak=True,
        reason="Владелец приватной комнаты",
    )
    for user_id in settings.get("allowed_users", []):
        member = channel.guild.get_member(int(user_id))
        if member is not None:
            await channel.set_permissions(member, view_channel=True, connect=True, reason="Доступ к приватной комнате")
    for user_id in settings.get("denied_users", []):
        member = channel.guild.get_member(int(user_id))
        if member is not None and member.id != owner.id:
            await channel.set_permissions(member, connect=False, reason="Запрет доступа к приватной комнате")
    for user_id in settings.get("muted_users", []):
        member = channel.guild.get_member(int(user_id))
        if member is not None and member.id != owner.id:
            overwrite = channel.overwrites_for(member)
            overwrite.speak = False
            await channel.set_permissions(member, overwrite=overwrite, reason="Запрет говорить в приватной комнате")


async def create_private_room(member: discord.Member, source: discord.VoiceChannel) -> discord.VoiceChannel | None:
    key = (member.guild.id, member.id)
    lock = private_room_locks.setdefault(key, asyncio.Lock())
    async with lock:
        existing = get_private_room_by_owner(member.guild, member.id)
        if existing is not None:
            try:
                await member.move_to(existing, reason="Возврат в существующую приватную комнату")
            except (discord.Forbidden, discord.HTTPException):
                pass
            return existing

        settings = get_private_room_settings(member.guild.id, member.id)
        overwrites = {
            member.guild.default_role: discord.PermissionOverwrite(
                connect=False if settings.get("locked") else None,
                view_channel=False if settings.get("hidden") else None,
            ),
            member: discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                speak=True,
            ),
        }
        try:
            channel = await member.guild.create_voice_channel(
                name=private_room_name(member, settings),
                category=source.category,
                overwrites=overwrites,
                user_limit=int(settings.get("limit") or 0),
                reason=f"Приватная комната пользователя {member} ({member.id})",
            )
            private_room_owners[channel.id] = member.id
            private_room_channels[key] = channel.id
            set_active_private_room(member.guild.id, member.id, channel.id)
            await apply_private_room_permissions(channel, member, settings)
            await member.move_to(channel, reason="Создание приватной комнаты")
            return channel
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"Не удалось создать приватную комнату для {member.id}: {error}")
            return None


async def delete_private_room(channel: discord.VoiceChannel) -> None:
    if channel.id in private_room_delete_locks:
        return
    owner_id = private_room_owners.get(channel.id)
    if owner_id is None:
        return
    private_room_delete_locks.add(channel.id)
    try:
        private_room_owners.pop(channel.id, None)
        private_room_channels.pop((channel.guild.id, owner_id), None)
        set_active_private_room(channel.guild.id, owner_id, None)
        try:
            await channel.delete(reason="Приватная комната опустела")
        except discord.NotFound:
            pass
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"Не удалось удалить приватную комнату {channel.id}: {error}")
            # Если удаление не удалось, возвращаем индексы, чтобы комнатой можно было управлять.
            if channel.guild.get_channel(channel.id) is not None:
                private_room_owners[channel.id] = owner_id
                private_room_channels[(channel.guild.id, owner_id)] = channel.id
    finally:
        private_room_delete_locks.discard(channel.id)


async def handle_private_room_voice_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if member.bot or before.channel == after.channel:
        return

    # Приватная комната удаляется только тогда, когда в ней больше никого нет.
    # Если владелец вышел, но внутри остались участники, комната продолжает
    # существовать и остаётся закреплённой за тем же владельцем.
    if isinstance(before.channel, discord.VoiceChannel) and before.channel.id in private_room_owners:
        await asyncio.sleep(0.3)
        channel = member.guild.get_channel(before.channel.id)
        if isinstance(channel, discord.VoiceChannel) and not channel.members:
            await delete_private_room(channel)

    # Пользователь зашёл в канал создания. Если его прежняя комната ещё существует
    # (например, внутри остались люди), create_private_room вернёт его туда. Если
    # старая комната уже опустела и была удалена, создастся новая с сохранёнными
    # настройками пользователя.
    if isinstance(after.channel, discord.VoiceChannel) and after.channel.id == PRIVATE_ROOM_CREATE_CHANNEL_ID:
        await create_private_room(member, after.channel)


async def restore_private_room_indexes() -> None:
    """После перезапуска восстанавливает владельцев по сохранённым настройкам и имени канала.

    Надёжно восстановить старый channel_id без отдельной записи нельзя, поэтому активные
    комнаты дополнительно сохраняются в JSON в поле active_channel_id.
    """
    data = load_private_room_data()
    for guild in bot.guilds:
        guild_data = data.get(str(guild.id), {})
        if not isinstance(guild_data, dict):
            continue
        for owner_key, raw in guild_data.items():
            if not isinstance(raw, dict):
                continue
            channel_id = raw.get("active_channel_id")
            if not channel_id:
                continue
            try:
                owner_id = int(owner_key)
                channel_id = int(channel_id)
            except (TypeError, ValueError):
                continue
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.VoiceChannel):
                # Комната считается активной, пока в ней есть хотя бы один человек.
                # Владелец не обязан находиться внутри комнаты в момент перезапуска.
                if channel.members:
                    private_room_owners[channel.id] = owner_id
                    private_room_channels[(guild.id, owner_id)] = channel.id
                else:
                    raw.pop("active_channel_id", None)
                    try:
                        await channel.delete(reason="Очистка пустой приватной комнаты после перезапуска")
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
            else:
                raw.pop("active_channel_id", None)
    save_private_room_data(data)


def set_active_private_room(guild_id: int, owner_id: int, channel_id: int | None) -> None:
    data = load_private_room_data()
    guild_data = data.setdefault(str(guild_id), {})
    settings = guild_data.setdefault(str(owner_id), default_private_room_settings())
    if channel_id is None:
        settings.pop("active_channel_id", None)
    else:
        settings["active_channel_id"] = channel_id
    save_private_room_data(data)


class PrivateRoomNameModal(discord.ui.Modal, title="Изменить название комнаты"):
    name = discord.ui.TextInput(label="Название комнаты", min_length=1, max_length=100)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        result = await require_private_room(interaction, "Изменить название комнаты")
        if result is None:
            return
        member, channel = result
        value = str(self.name.value).strip()
        try:
            await channel.edit(name=value, reason=f"Владелец комнаты: {member}")
            update_private_room_settings(member.guild.id, member.id, name=value)
        except (discord.Forbidden, discord.HTTPException):
            await send_private_room_reply(interaction, "Изменить название комнаты", f"{member.mention}, не удалось **изменить** название комнаты.")
            return
        await send_private_room_reply(interaction, "Изменить название комнаты", f"{member.mention}, Вы успешно **изменили** название комнаты.")


class PrivateRoomLimitModal(discord.ui.Modal, title="Изменить лимит участников"):
    limit = discord.ui.TextInput(label="Лимит участников", placeholder="0-99", min_length=1, max_length=2)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        result = await require_private_room(interaction, "Изменить лимит участников")
        if result is None:
            return
        member, channel = result
        raw = str(self.limit.value).strip()
        if not raw.isdigit() or not 0 <= int(raw) <= 99:
            await send_private_room_reply(interaction, "Изменить лимит участников", f"{member.mention}, укажите **корректный** лимит от 0 до 99.")
            return
        value = int(raw)
        try:
            await channel.edit(user_limit=value, reason=f"Владелец комнаты: {member}")
            update_private_room_settings(member.guild.id, member.id, limit=value)
        except (discord.Forbidden, discord.HTTPException):
            await send_private_room_reply(interaction, "Изменить лимит участников", f"{member.mention}, не удалось **изменить** лимит комнаты.")
            return
        await send_private_room_reply(interaction, "Изменить лимит участников", f"{member.mention}, Вы успешно **изменили** лимит участников комнаты.")


class PrivateRoomUserSelect(discord.ui.UserSelect):
    def __init__(self, action: str, title: str):
        super().__init__(placeholder="Выберите пользователя", min_values=1, max_values=1)
        self.action = action
        self.action_title = title

    async def callback(self, interaction: discord.Interaction) -> None:
        result = await require_private_room(interaction, self.action_title)
        if result is None:
            return
        owner, channel = result
        target = self.values[0]
        if not isinstance(target, discord.Member):
            target = owner.guild.get_member(target.id)
        if target is None:
            await send_private_room_reply(interaction, self.action_title, f"{owner.mention}, пользователь **недоступен** на сервере.")
            return
        if target.bot or target.id == owner.id:
            await send_private_room_reply(interaction, self.action_title, f"{owner.mention}, Вы не можете **выбрать** этого пользователя.")
            return

        settings = get_private_room_settings(owner.guild.id, owner.id)
        allowed = set(settings.get("allowed_users", []))
        denied = set(settings.get("denied_users", []))
        muted = set(settings.get("muted_users", []))

        try:
            if self.action == "allow":
                allowed.add(target.id); denied.discard(target.id)
                overwrite = channel.overwrites_for(target)
                overwrite.view_channel = True; overwrite.connect = True
                await channel.set_permissions(target, overwrite=overwrite, reason=f"Доступ выдан владельцем {owner}")
                update_private_room_settings(owner.guild.id, owner.id, allowed_users=sorted(allowed), denied_users=sorted(denied))
                text = f"{owner.mention}, Вы успешно **выдали** доступ к комнате пользователю {target.mention}."
            elif self.action == "deny":
                denied.add(target.id); allowed.discard(target.id)
                overwrite = channel.overwrites_for(target)
                overwrite.connect = False
                await channel.set_permissions(target, overwrite=overwrite, reason=f"Доступ забран владельцем {owner}")
                if target.voice and target.voice.channel and target.voice.channel.id == channel.id:
                    await target.move_to(None, reason="Доступ к приватной комнате забран")
                update_private_room_settings(owner.guild.id, owner.id, allowed_users=sorted(allowed), denied_users=sorted(denied))
                text = f"{owner.mention}, Вы успешно **забрали** доступ к комнате у {target.mention}."
            elif self.action == "kick":
                if not target.voice or not target.voice.channel or target.voice.channel.id != channel.id:
                    await send_private_room_reply(interaction, self.action_title, f"{owner.mention}, пользователь **не находится** в Вашей комнате.")
                    return
                await target.move_to(None, reason=f"Выгнан владельцем комнаты {owner}")
                text = f"{owner.mention}, Вы успешно **выгнали** пользователя {target.mention} из комнаты."
            elif self.action == "mute":
                muted.add(target.id)
                overwrite = channel.overwrites_for(target); overwrite.speak = False
                await channel.set_permissions(target, overwrite=overwrite, reason=f"Запрет говорить владельцем {owner}")
                update_private_room_settings(owner.guild.id, owner.id, muted_users=sorted(muted))
                text = f"{owner.mention}, Вы успешно **запретили** пользователю {target.mention} говорить."
            elif self.action == "unmute":
                muted.discard(target.id)
                overwrite = channel.overwrites_for(target); overwrite.speak = None
                await channel.set_permissions(target, overwrite=overwrite, reason=f"Разрешено говорить владельцем {owner}")
                update_private_room_settings(owner.guild.id, owner.id, muted_users=sorted(muted))
                text = f"{owner.mention}, Вы успешно **разрешили** пользователю {target.mention} говорить."
            elif self.action == "transfer":
                existing_target_room = get_private_room_by_owner(owner.guild, target.id)
                if existing_target_room is not None and existing_target_room.id != channel.id:
                    await send_private_room_reply(interaction, self.action_title, f"{owner.mention}, пользователь уже **владеет** своей приватной комнатой.")
                    return
                old_settings = settings
                # Комната продолжает жить, а текущие настройки переходят новому владельцу.
                update_private_room_settings(target.guild.id, target.id, **{k: old_settings[k] for k in default_private_room_settings()})
                private_room_owners[channel.id] = target.id
                private_room_channels.pop((owner.guild.id, owner.id), None)
                private_room_channels[(owner.guild.id, target.id)] = channel.id
                set_active_private_room(owner.guild.id, owner.id, None)
                set_active_private_room(owner.guild.id, target.id, channel.id)
                old_overwrite = channel.overwrites_for(owner)
                await channel.set_permissions(owner, overwrite=old_overwrite, reason="Передача владельца приватной комнаты")
                new_overwrite = channel.overwrites_for(target)
                new_overwrite.view_channel = True; new_overwrite.connect = True; new_overwrite.speak = True
                await channel.set_permissions(target, overwrite=new_overwrite, reason="Новый владелец приватной комнаты")
                text = f"{owner.mention}, Вы успешно **передали** владение комнатой пользователю {target.mention}."
            else:
                return
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"Ошибка управления приватной комнатой: {error}")
            await send_private_room_reply(interaction, self.action_title, f"{owner.mention}, Discord не смог **выполнить** это действие.")
            return
        await send_private_room_reply(interaction, self.action_title, text)


class PrivateRoomUserActionView(discord.ui.View):
    def __init__(self, action: str, title: str):
        super().__init__(timeout=60)
        self.add_item(PrivateRoomUserSelect(action, title))


class PrivateRoomSettingsSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Изменить название", value="rename"),
            discord.SelectOption(label="Лимит участников", value="limit"),
            discord.SelectOption(label="Открыть/закрыть", value="lock"),
            discord.SelectOption(label="Скрыть/показать", value="visibility"),
            discord.SelectOption(label="Передать владение", value="transfer"),
        ]
        super().__init__(
            placeholder="Настройки комнаты",
            options=options,
            custom_id="private_room:settings_select",
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "rename":
            if await require_private_room(interaction, "Изменить название комнаты") is not None:
                await interaction.response.send_modal(PrivateRoomNameModal())
        elif value == "limit":
            if await require_private_room(interaction, "Изменить лимит участников") is not None:
                await interaction.response.send_modal(PrivateRoomLimitModal())
        elif value == "lock":
            result = await require_private_room(interaction, "Открыть/Закрыть комнату")
            if result is None:
                return
            member, channel = result
            settings = get_private_room_settings(member.guild.id, member.id)
            locked = not bool(settings.get("locked"))
            try:
                overwrite = channel.overwrites_for(channel.guild.default_role)
                overwrite.connect = False if locked else None
                await channel.set_permissions(
                    channel.guild.default_role,
                    overwrite=overwrite,
                    reason=f"Владелец комнаты: {member}",
                )
                update_private_room_settings(member.guild.id, member.id, locked=locked)
            except (discord.Forbidden, discord.HTTPException):
                await send_private_room_reply(
                    interaction,
                    "Открыть/Закрыть комнату",
                    f"{member.mention}, не удалось **изменить** доступ к комнате.",
                )
                return
            verb = "закрыли" if locked else "открыли"
            await send_private_room_reply(
                interaction,
                "Открыть/Закрыть комнату",
                f"{member.mention}, Вы успешно **{verb}** свою комнату.",
            )
        elif value == "visibility":
            result = await require_private_room(interaction, "Скрыть/Показать комнату")
            if result is None:
                return
            member, channel = result
            settings = get_private_room_settings(member.guild.id, member.id)
            hidden = not bool(settings.get("hidden"))
            try:
                overwrite = channel.overwrites_for(channel.guild.default_role)
                overwrite.view_channel = False if hidden else None
                await channel.set_permissions(
                    channel.guild.default_role,
                    overwrite=overwrite,
                    reason=f"Владелец комнаты: {member}",
                )
                update_private_room_settings(member.guild.id, member.id, hidden=hidden)
            except (discord.Forbidden, discord.HTTPException):
                await send_private_room_reply(
                    interaction,
                    "Скрыть/Показать комнату",
                    f"{member.mention}, не удалось **изменить** видимость комнаты.",
                )
                return
            verb = "скрыли" if hidden else "показали"
            await send_private_room_reply(
                interaction,
                "Скрыть/Показать комнату",
                f"{member.mention}, Вы успешно **{verb}** свою комнату.",
            )
        elif value == "transfer":
            result = await require_private_room(interaction, "Передать владение")
            if result is not None:
                await send_private_room_reply(interaction, "Передать владение", "Выберите пользователя, которому хотите передать комнату.", view=PrivateRoomUserActionView("transfer", "Передать владение"))


class PrivateRoomSettingsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(PrivateRoomSettingsSelect())


class PrivateRoomMemberActionsSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Выгнать из комнаты", value="kick"),
            discord.SelectOption(label="Дать доступ", value="allow"),
            discord.SelectOption(label="Забрать доступ", value="deny"),
            discord.SelectOption(label="Разрешить говорить", value="unmute"),
            discord.SelectOption(label="Запретить говорить", value="mute"),
        ]
        super().__init__(
            placeholder="Действия с участниками",
            options=options,
            custom_id="private_room:members_select",
            row=3,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        mapping = {
            "kick": "Выгнать из комнаты",
            "allow": "Дать доступ",
            "deny": "Забрать доступ",
            "unmute": "Разрешить говорить",
            "mute": "Запретить говорить",
        }
        action = self.values[0]
        title = mapping[action]

        descriptions = {
            "allow": "Выберите пользователя, которому хотите дать доступ к комнате.",
            "deny": "Выберите пользователя, у которого хотите забрать доступ к комнате.",
            "kick": "Выберите пользователя, которого хотите выгнать из комнаты.",
            "mute": "Выберите пользователя, которому хотите запретить говорить.",
            "unmute": "Выберите пользователя, которому хотите разрешить говорить.",
        }

        if await require_private_room(interaction, title) is None:
            return

        await send_private_room_reply(
            interaction,
            title,
            descriptions.get(action, "Выберите пользователя."),
            view=PrivateRoomUserActionView(action, title)
        )


class PrivateRoomMemberActionsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(PrivateRoomMemberActionsSelect())


class PrivateRoomToggleLockButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Открыть/закрыть",
            style=discord.ButtonStyle.secondary,
            custom_id="private_room:toggle_lock",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        result = await require_private_room(interaction, "Открыть/Закрыть комнату")
        if result is None:
            return
        member, channel = result
        settings = get_private_room_settings(member.guild.id, member.id)
        locked = not bool(settings.get("locked"))
        try:
            overwrite = channel.overwrites_for(channel.guild.default_role)
            overwrite.connect = False if locked else None
            await channel.set_permissions(
                channel.guild.default_role,
                overwrite=overwrite,
                reason=f"Владелец комнаты: {member}",
            )
            update_private_room_settings(member.guild.id, member.id, locked=locked)
        except (discord.Forbidden, discord.HTTPException):
            await send_private_room_reply(
                interaction,
                "Открыть/Закрыть комнату",
                f"{member.mention}, не удалось **изменить** доступ к комнате.",
            )
            return
        verb = "закрыли" if locked else "открыли"
        await send_private_room_reply(
            interaction,
            "Открыть/Закрыть комнату",
            f"{member.mention}, Вы успешно **{verb}** свою комнату.",
        )


class PrivateRoomToggleVisibilityButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Скрыть/показать",
            style=discord.ButtonStyle.secondary,
            custom_id="private_room:toggle_visibility",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        result = await require_private_room(interaction, "Скрыть/Показать комнату")
        if result is None:
            return
        member, channel = result
        settings = get_private_room_settings(member.guild.id, member.id)
        hidden = not bool(settings.get("hidden"))
        try:
            overwrite = channel.overwrites_for(channel.guild.default_role)
            overwrite.view_channel = False if hidden else None
            await channel.set_permissions(
                channel.guild.default_role,
                overwrite=overwrite,
                reason=f"Владелец комнаты: {member}",
            )
            update_private_room_settings(member.guild.id, member.id, hidden=hidden)
        except (discord.Forbidden, discord.HTTPException):
            await send_private_room_reply(
                interaction,
                "Скрыть/Показать комнату",
                f"{member.mention}, не удалось **изменить** видимость комнаты.",
            )
            return
        verb = "скрыли" if hidden else "показали"
        await send_private_room_reply(
            interaction,
            "Скрыть/Показать комнату",
            f"{member.mention}, Вы успешно **{verb}** свою комнату.",
        )


class PrivateRoomAllowButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Дать доступ",
            style=discord.ButtonStyle.success,
            custom_id="private_room:allow",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if await require_private_room(interaction, "Дать доступ") is not None:
            await send_private_room_reply(
                interaction,
                "Дать доступ",
                "Выберите пользователя.",
                view=PrivateRoomUserActionView("allow", "Дать доступ"),
            )


class PrivateRoomDenyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Забрать доступ",
            style=discord.ButtonStyle.danger,
            custom_id="private_room:deny",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if await require_private_room(interaction, "Забрать доступ") is not None:
            await send_private_room_reply(
                interaction,
                "Забрать доступ",
                "Выберите пользователя.",
                view=PrivateRoomUserActionView("deny", "Забрать доступ"),
            )


class PrivateRoomPanelView(discord.ui.LayoutView):
    """Постоянная панель приватных комнат на Discord Components V2."""

    def __init__(self):
        super().__init__(timeout=None)

        container = discord.ui.Container(
            discord.ui.TextDisplay(
                "## Управление приватной комнатой\n\n"
                "Здесь Вы можете управлять своей приватной комнатой.\n"
                "Используйте разделы ниже, чтобы изменить её настройки и управлять участниками."
            ),
            discord.ui.Separator(),
            discord.ui.ActionRow(
                PrivateRoomSettingsSelect(),
            ),
            discord.ui.ActionRow(PrivateRoomMemberActionsSelect()),
            accent_color=COLOR,
        )
        self.add_item(container)


def message_has_private_room_panel(message: discord.Message) -> bool:
    """Находит как старую embed-панель, так и новую панель Components V2."""
    if message.embeds and message.embeds[0].title == "Управление приватной комнатой":
        return True

    def has_private_id(component: Any) -> bool:
        custom_id = getattr(component, "custom_id", None)
        if isinstance(custom_id, str) and custom_id.startswith("private_room:"):
            return True
        for child in getattr(component, "children", []) or []:
            if has_private_id(child):
                return True
        return False

    return any(has_private_id(component) for component in message.components)


async def ensure_private_room_panel() -> None:
    channel = bot.get_channel(PRIVATE_ROOM_CONTROL_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(PRIVATE_ROOM_CONTROL_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
            print(f"Не удалось получить канал управления приватными комнатами: {error}")
            return
    if not isinstance(channel, discord.TextChannel):
        print("Канал управления приватными комнатами не является текстовым каналом.")
        return

    panel_message: discord.Message | None = None
    try:
        async for message in channel.history(limit=50):
            if bot.user is None or message.author.id != bot.user.id:
                continue
            if message_has_private_room_panel(message):
                panel_message = message
                break

        panel_view = PrivateRoomPanelView()
        if panel_message is None:
            await channel.send(view=panel_view)
        else:
            try:
                await panel_message.edit(
                    content=None,
                    embed=None,
                    attachments=[],
                    view=panel_view,
                )
            except (discord.HTTPException, ValueError):
                await channel.send(view=panel_view)
    except (discord.Forbidden, discord.HTTPException, ValueError) as error:
        print(f"Не удалось создать/обновить панель приватных комнат: {error}")

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
# Контроль окончания тайм-аутов
# -----------------------------------------------------------------------------

async def timeout_expiry_worker(
    guild_id: int,
    user_id: int,
    expires_at: datetime,
) -> None:
    key = (guild_id, user_id)
    try:
        delay = max(0.0, (expires_at - datetime.now(timezone.utc)).total_seconds())
        await asyncio.sleep(delay + 2)

        guild = bot.get_guild(guild_id)
        if guild is None:
            return

        try:
            member = await guild.fetch_member(user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

        current_until = member.timed_out_until
        now = datetime.now(timezone.utc)

        # Тайм-аут продлили — переносим проверку на новый срок.
        if current_until is not None and current_until > now:
            schedule_timeout_expiry(guild_id, user_id, current_until)
            return

        embed = discord.Embed(
            title="Снятие тайм-аута",
            color=COLOR,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Пользователь",
            value=member_id_text(member),
            inline=False,
        )
        embed.add_field(
            name="Причина",
            value="> Время действия тайм-аута закончилось.",
            inline=False,
        )
        await send_log(guild, embed)
    finally:
        if timeout_expiry_tasks.get(key) is asyncio.current_task():
            timeout_expiry_tasks.pop(key, None)


def schedule_timeout_expiry(
    guild_id: int,
    user_id: int,
    expires_at: datetime,
) -> None:
    key = (guild_id, user_id)
    old_task = timeout_expiry_tasks.get(key)
    if old_task and not old_task.done() and old_task is not asyncio.current_task():
        old_task.cancel()

    timeout_expiry_tasks[key] = asyncio.create_task(
        timeout_expiry_worker(guild_id, user_id, expires_at)
    )


def cancel_timeout_expiry(guild_id: int, user_id: int) -> None:
    key = (guild_id, user_id)
    task = timeout_expiry_tasks.pop(key, None)
    if task and not task.done() and task is not asyncio.current_task():
        task.cancel()


async def restore_timeout_expiry_tasks() -> None:
    now = datetime.now(timezone.utc)
    for guild in bot.guilds:
        for member in guild.members:
            until = member.timed_out_until
            if until is not None and until > now:
                schedule_timeout_expiry(guild.id, member.id, until)


# -----------------------------------------------------------------------------
# Запуск
# -----------------------------------------------------------------------------

@bot.event
async def on_ready() -> None:
    global _commands_synced, _restored_temp_bans, _private_room_view_registered, _private_room_panel_ready

    if not _commands_synced:
        try:
            await bot.tree.sync()
            _commands_synced = True
        except discord.HTTPException as error:
            print(f"Не удалось синхронизировать slash-команды: {error}")

    if not _restored_temp_bans:
        await restore_temporary_bans()
        _restored_temp_bans = True

    await restore_timeout_expiry_tasks()

    if not _private_room_view_registered:
        bot.add_view(PrivateRoomPanelView())
        _private_room_view_registered = True

    if not _private_room_panel_ready:
        await restore_private_room_indexes()
        await ensure_private_room_panel()
        _private_room_panel_ready = True

    await bot.change_presence(status=discord.Status.idle)
    print(f"Бот запущен: {bot.user}")
    print(f"Канал логов: {LOG_CHANNEL_ID}")


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
    await send_message_log(before.guild, embed)


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
    if message.channel.id in clear_in_progress_channels:
        return

    deleter = await find_message_deleter(message)
    embed = discord.Embed(
        title="Удалённое сообщение",
        color=COLOR,
        timestamp=moscow_time(),
    )
    if deleter:
        embed.add_field(name="Исполнитель", value=member_id_text(deleter), inline=False)
        embed.add_field(name="Пользователь", value=member_id_text(message.author), inline=False)
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

    await send_message_log(message.guild, embed)


# -----------------------------------------------------------------------------
# Заходы, выходы и кики
# -----------------------------------------------------------------------------

@bot.event
async def on_member_join(member: discord.Member) -> None:
    event_time = datetime.now(timezone.utc)
    embed = discord.Embed(title="Вход на сервер", color=COLOR)
    embed.add_field(name="Пользователь", value=member_id_text(member), inline=False)
    embed.add_field(name="Дата и время входа", value=f"> {discord_datetime(event_time)}", inline=False)
    await send_server_log(member.guild, embed)


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
            title="Исключение пользователя",
            color=COLOR,
            timestamp=moscow_time(),
        )
        embed.add_field(name="Исполнитель", value=member_id_text(moderator), inline=False)
        embed.add_field(name="Пользователь", value=member_id_text(member), inline=False)
        embed.add_field(name="Причина", value=f"> {reason}", inline=False)
        await send_log(member.guild, embed)
        return

    embed = discord.Embed(title="Выход с сервера", color=COLOR)
    embed.add_field(name="Пользователь", value=member_id_text(member), inline=False)
    embed.add_field(name="Дата и время выхода", value=f"> {discord_datetime(event_time)}", inline=False)
    await send_server_log(member.guild, embed)


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
        audit.reason if audit and audit.reason else "Не указана"
    )
    unban_at = pending.get("unban_at") if pending else None

    embed = discord.Embed(
        title="Выдача бана",
        color=COLOR,
        timestamp=moscow_time(),
    )
    embed.add_field(
        name="Исполнитель",
        value=member_id_text(moderator) if moderator else "Не удалось определить",
        inline=False,
    )
    embed.add_field(name="Пользователь", value=member_id_text(user), inline=False)
    embed.add_field(name="Причина", value=f"> {reason}", inline=False)
    embed.add_field(
        name="До",
        value="> Навсегда" if unban_at is None else f"> {discord_datetime(unban_at)}",
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
        audit.reason if audit and audit.reason else "Не указана"
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
        name="Исполнитель",
        value=member_id_text(moderator) if moderator else "Не удалось определить",
        inline=False,
    )
    embed.add_field(name="Пользователь", value=member_id_text(user), inline=False)
    embed.add_field(name="Причина", value=f"> {reason}", inline=False)
    await send_log(guild, embed)


# -----------------------------------------------------------------------------
# Тайм-ауты и снятие тайм-аутов
# -----------------------------------------------------------------------------

async def handle_timeout_update(before: discord.Member, after: discord.Member) -> None:
    if before.timed_out_until == after.timed_out_until:
        return

    key = (after.guild.id, after.id)
    await asyncio.sleep(1)

    is_removal = after.timed_out_until is None

    # При выдаче или продлении тайм-аута создаём отдельную задачу.
    # Она отправит лог даже если Discord не пришлёт on_member_update в момент окончания.
    if not is_removal:
        schedule_timeout_expiry(after.guild.id, after.id, after.timed_out_until)

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

    # Естественное окончание срока логирует timeout_expiry_worker.
    # Здесь ничего не отправляем, чтобы не было двух одинаковых логов.
    if is_removal and pending is None and audit is None:
        return

    moderator = pending.get("moderator") if pending else (audit.user if audit else None)
    reason = pending.get("reason") if pending else (
        audit.reason if audit and audit.reason else None
    )

    if is_removal:
        cancel_timeout_expiry(after.guild.id, after.id)
        embed = discord.Embed(
            title="Снятие тайм-аута",
            color=COLOR,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Исполнитель",
            value=member_id_text(moderator) if moderator else "Не удалось определить",
            inline=False,
        )
        embed.add_field(name="Пользователь", value=member_id_text(after), inline=False)
        if reason:
            embed.add_field(name="Причина", value=f"> {reason}", inline=False)
    else:
        until = pending.get("until") if pending else after.timed_out_until
        reason = reason or "Не указана"
        embed = discord.Embed(
            title="Выдача тайм-аута",
            color=COLOR,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Исполнитель",
            value=member_id_text(moderator) if moderator else "Не удалось определить",
            inline=False,
        )
        embed.add_field(name="Пользователь", value=member_id_text(after), inline=False)
        embed.add_field(name="Причина", value=f"> {reason}", inline=False)
        embed.add_field(
            name="До",
            value=f"> {discord_datetime(until)}",
            inline=False,
        )

    await send_log(after.guild, embed)


# -----------------------------------------------------------------------------
# Служебные события без дополнительных логов
# -----------------------------------------------------------------------------

@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    # Голосовое событие оставлено только для работы приватных комнат.
    await handle_private_room_voice_update(member, before, after)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member) -> None:
    # Из изменений участника логируются только выдача/снятие тайм-аута.
    if before.timed_out_until != after.timed_out_until:
        await handle_timeout_update(before, after)


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Переменная окружения TOKEN не задана.")
    bot.run(TOKEN)
