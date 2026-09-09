from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

TOKEN = os.getenv("TOKEN")

PRIVATE_ROOM_CONTROL_CHANNEL_ID = 1546849303379706016
PRIVATE_ROOM_CREATE_CHANNEL_ID = 1546849353371492413
DUEL_CATEGORY_ID = 1546866271482683494

# Каналы логов из исходного файла.
SERVER_LOG_CHANNEL_ID = int(os.getenv("SERVER_LOG_CHANNEL_ID", "1547180990814756915"))
MESSAGE_LOG_CHANNEL_ID = int(os.getenv("MESSAGE_LOG_CHANNEL_ID", "1547180990814756915"))

COLOR = discord.Color(0x303136)
MOSCOW_TZ = timezone(timedelta(hours=3))
BASE_DIR = Path(__file__).resolve().parent
PRIVATE_ROOMS_FILE = BASE_DIR / "private_rooms.json"

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

_private_room_view_registered = False
_private_room_panel_ready = False
_commands_synced = False

# Активные дуэли: channel_id -> состояние дуэли.
active_duels: dict[int, dict[str, Any]] = {}
private_room_owners: dict[int, int] = {}
private_room_channels: dict[tuple[int, int], int] = {}
private_room_locks: dict[tuple[int, int], asyncio.Lock] = {}
private_room_delete_locks: set[int] = set()

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


# -----------------------------------------------------------------------------
# Логи сообщений, входов и выходов
# -----------------------------------------------------------------------------

def moscow_time(value: datetime | None = None) -> datetime:
    if value is None:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(MOSCOW_TZ)


def discord_datetime(value: datetime | None = None) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return f"<t:{int(value.timestamp())}:f>"


def member_id_text(user: discord.abc.User) -> str:
    return f"{user.mention}\nID: `{user.id}`"


def channel_id_text(channel: discord.abc.GuildChannel | discord.Thread) -> str:
    return f"{channel.mention}\nID: `{channel.id}`"


def limited_text(text: str | None, fallback: str = "Отсутствует") -> str:
    value = (text or "").strip() or fallback
    return value[:997] + "..." if len(value) > 1000 else value


async def get_log_channel(guild: discord.Guild, channel_id: int) -> discord.abc.Messageable | None:
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
    guild: discord.Guild, view: discord.ui.LayoutView, channel_id: int,
    *, mention_users: tuple[discord.abc.User, ...] = (),
) -> discord.Message | None:
    channel = await get_log_channel(guild, channel_id)
    if channel is None:
        return None
    try:
        # Разрешаем только участников события; упоминания из цитат не включаем.
        # silent подавляет push/desktop-уведомления, но не значок упоминания.
        return await channel.send(
            view=view,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=list(mention_users), replied_user=False,
            ),
            silent=True,
        )
    except (discord.Forbidden, discord.HTTPException) as error:
        print(f"Ошибка отправки лога в канал {channel_id}: {error}")
        return None


def log_layout(section: str, title: str, body: str, *, url: str | None = None) -> discord.ui.LayoutView:
    items: list[Any] = [
        discord.ui.TextDisplay(f"-# {section}"),
        discord.ui.TextDisplay(f"## {title}"),
        discord.ui.Separator(),
        discord.ui.TextDisplay(body),
    ]
    if url:
        items.append(discord.ui.Separator())
        items.append(discord.ui.ActionRow(
            discord.ui.Button(label="Перейти к сообщению", style=discord.ButtonStyle.link, url=url)
        ))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*items, accent_color=COLOR))
    return view


async def send_server_log(
    guild: discord.Guild, view: discord.ui.LayoutView,
    *, mention_users: tuple[discord.abc.User, ...] = (),
) -> discord.Message | None:
    return await send_log_to(guild, view, SERVER_LOG_CHANNEL_ID, mention_users=mention_users)


async def send_message_log(
    guild: discord.Guild, view: discord.ui.LayoutView,
    *, mention_users: tuple[discord.abc.User, ...] = (),
) -> discord.Message | None:
    return await send_log_to(guild, view, MESSAGE_LOG_CHANNEL_ID, mention_users=mention_users)


async def find_message_deleter(message: discord.Message) -> discord.abc.User | None:
    await asyncio.sleep(1)
    if not message.guild:
        return None
    try:
        async for entry in message.guild.audit_logs(limit=8, action=discord.AuditLogAction.message_delete):
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
    # Components V2: сохраняем компактный заголовок без ## и добавляем
    # настоящий системный Separator Discord сразу после него.
    items: list[Any] = [
        discord.ui.TextDisplay(f"**{title}**"),
        discord.ui.Separator(),
        discord.ui.TextDisplay(description),
    ]
    if view is not None and view.children:
        items.append(discord.ui.ActionRow(*view.children))

    reply_view = discord.ui.LayoutView(timeout=60)
    reply_view.add_item(discord.ui.Container(*items, accent_color=COLOR))

    if interaction.response.is_done():
        await interaction.followup.send(view=reply_view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    else:
        await interaction.response.send_message(view=reply_view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


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


async def cleanup_private_room_if_empty(guild: discord.Guild, channel_id: int) -> None:
    # Даём Discord обновить voice-state/cache после выхода или перемещения участника.
    # Несколько коротких проверок закрывают гонку, из-за которой пустой канал иногда
    # оставался висеть после выхода последнего пользователя.
    for delay in (0.25, 0.75, 1.5):
        await asyncio.sleep(delay)
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            return
        if channel.id not in private_room_owners:
            return
        if channel.members:
            return
    channel = guild.get_channel(channel_id)
    if isinstance(channel, discord.VoiceChannel) and channel.id in private_room_owners and not channel.members:
        await delete_private_room(channel)


async def handle_private_room_voice_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if before.channel == after.channel:
        return

    # Проверяем освобождённую приватную комнату для ЛЮБОГО участника, включая ботов.
    # Иначе бот мог оказаться последним в канале, выйти, а комната не удалялась.
    if isinstance(before.channel, discord.VoiceChannel) and before.channel.id in private_room_owners:
        asyncio.create_task(cleanup_private_room_if_empty(member.guild, before.channel.id))

    # Боты не должны создавать себе приватные комнаты.
    if member.bot:
        return

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
# Дуэли
# -----------------------------------------------------------------------------

pending_duel_users: set[int] = set()


def duel_embed(title: str, description: str) -> discord.Embed:
    # Используется только для коротких ephemeral-ошибок, где интерактивные
    # Components V2 не нужны.
    return discord.Embed(title=title, description=description, color=COLOR)


def duel_layout(title: str, description: str, *controls: discord.ui.Item[Any]) -> discord.ui.LayoutView:
    """Сообщение дуэли с настоящим системным Separator Discord Components V2."""
    view = discord.ui.LayoutView(timeout=None)
    items: list[Any] = [
        discord.ui.TextDisplay("-# Дуэль"),
        discord.ui.TextDisplay(f"## {title}"),
        discord.ui.Separator(),
        discord.ui.TextDisplay(description),
    ]
    if controls:
        items.append(discord.ui.ActionRow(*controls))
    view.add_item(discord.ui.Container(*items, accent_color=COLOR))
    return view


def user_in_active_duel(user_id: int) -> bool:
    return any(user_id in duel["players"] for duel in active_duels.values())


def user_busy_with_duel(user_id: int) -> bool:
    return user_id in pending_duel_users or user_in_active_duel(user_id)


def duel_minutes(duel: dict[str, Any]) -> float:
    # Monkeytype-style WPM uses the actual elapsed typing time in minutes.
    started_at = duel.get("started_at")
    ended_at = duel.get("ended_at")
    if started_at is None:
        return 1 / 60
    if ended_at is None:
        ended_at = asyncio.get_running_loop().time()
    return max((float(ended_at) - float(started_at)) / 60.0, 1 / 60)


def duel_wpm(duel: dict[str, Any], member_id: int) -> int:
    stats = duel["stats"][member_id]
    # 5 typed characters (including spaces, digits and punctuation) = 1 standard word.
    standard_words = stats["characters"] / 5.0
    return round(standard_words / duel_minutes(duel))


def duel_stats_text(duel: dict[str, Any], member_id: int) -> str:
    stats = duel["stats"][member_id]
    return (
        f"Сообщений: **{stats['messages']}**\n"
        f"WPM: **{duel_wpm(duel, member_id)}**\n"
        f"Макс. сообщений подряд: **{stats['max_streak']}**"
    )


def duel_result_details_text(duel: dict[str, Any]) -> str:
    lines: list[str] = []
    if duel.get("mode") == "speed":
        seconds = int(duel.get("duration_seconds") or 60)
        lines.append(f"**Длительность:** {seconds} секунд.")
        lines.append("**Тип дуэли:** На скорость.")
    else:
        lines.append("**Тип дуэли:** На выдержку.")
    return "\n".join(lines)


def duel_finished_public_layout(
    duel: dict[str, Any], winner_id: int | None,
) -> discord.ui.LayoutView:
    """Public result: winner/loser statistics, or both participants in a draw."""
    first_id, second_id = duel["players"]
    items: list[Any] = [
        discord.ui.TextDisplay("-# Дуэль"),
        discord.ui.TextDisplay("## Дуэль окончена"),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            f"<@{first_id}> vs <@{second_id}>\n" + duel_result_details_text(duel)
        ),
        discord.ui.Separator(),
    ]
    if winner_id is not None:
        loser_id = next(member_id for member_id in duel["players"] if member_id != winner_id)
        items.append(discord.ui.TextDisplay(
            f"**Победитель:** <@{winner_id}>\n" + duel_stats_text(duel, winner_id)
        ))
        items.append(discord.ui.Separator())
        items.append(discord.ui.TextDisplay(
            f"**Проигравший:** <@{loser_id}>\n" + duel_stats_text(duel, loser_id)
        ))
    else:
        items.append(discord.ui.TextDisplay("**Ничья.**"))
        for index, member_id in enumerate(duel["players"]):
            items.append(discord.ui.TextDisplay(
                f"**<@{member_id}>**\n" + duel_stats_text(duel, member_id)
            ))
            if index == 0:
                items.append(discord.ui.Separator())
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*items, accent_color=COLOR))
    return view


async def update_duel_public_message(
    duel: dict[str, Any], winner_id: int | None,
) -> None:
    """Edit the original public challenge using the bot, not an expiring token."""
    channel_id = duel.get("announcement_channel_id")
    message_id = duel.get("announcement_message_id")
    if channel_id is None or message_id is None:
        return
    try:
        source_channel = bot.get_partial_messageable(int(channel_id))
        message = source_channel.get_partial_message(int(message_id))
        await message.edit(
            content=None,
            embeds=[],
            attachments=[],
            view=duel_finished_public_layout(duel, winner_id),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
        # A deleted/inaccessible public message must not stop channel cleanup.
        print(f"Could not update public duel message {message_id}: {error}")


async def delete_duel_channel_later(channel: discord.TextChannel) -> None:
    await asyncio.sleep(120)
    try:
        await channel.delete(reason="Дуэль завершена")
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


async def finish_duel(channel: discord.TextChannel, *, loser_id: int | None = None, reason: str = "") -> None:
    duel = active_duels.pop(channel.id, None)
    if duel is None or duel.get("finished"):
        return

    duel["finished"] = True
    duel["ended_at"] = asyncio.get_running_loop().time()

    task = duel.get("task")
    if task and task is not asyncio.current_task() and not task.done():
        task.cancel()

    first_id, second_id = duel["players"]
    first = channel.guild.get_member(first_id)
    second = channel.guild.get_member(second_id)
    first_name = first.mention if first else f"<@{first_id}>"
    second_name = second.mention if second else f"<@{second_id}>"

    winner_id: int | None = None
    if loser_id is not None:
        winner_id = second_id if loser_id == first_id else first_id
    elif duel.get("mode") == "speed":
        a = duel["stats"][first_id]
        b = duel["stats"][second_id]
        score_a = (
            duel_wpm(duel, first_id),
            a["characters"],
            a["messages"],
            a["max_streak"],
            a["max_message_chars"],
        )
        score_b = (
            duel_wpm(duel, second_id),
            b["characters"],
            b["messages"],
            b["max_streak"],
            b["max_message_chars"],
        )
        if score_a > score_b:
            winner_id = first_id
        elif score_b > score_a:
            winner_id = second_id

    result_items: list[Any] = [
        discord.ui.TextDisplay('-# Дуэль'),
        discord.ui.TextDisplay('## Результаты дуэли'),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            f"{first_name} vs {second_name}\n" + duel_result_details_text(duel)
        ),
        discord.ui.Separator(),
    ]
    if winner_id is not None:
        actual_loser_id = second_id if winner_id == first_id else first_id
        result_items.append(discord.ui.TextDisplay(
            f"**Победитель:** <@{winner_id}>\n" + duel_stats_text(duel, winner_id)
        ))
        result_items.append(discord.ui.Separator())
        result_items.append(discord.ui.TextDisplay(
            f"**Проигравший:** <@{actual_loser_id}>\n" + duel_stats_text(duel, actual_loser_id)
        ))
    else:
        result_items.append(discord.ui.TextDisplay("**Ничья.**"))
        for index, (member_id, name) in enumerate(((first_id, first_name), (second_id, second_name))):
            result_items.append(discord.ui.TextDisplay(
                f"**{name}**\n{duel_stats_text(duel, member_id)}"
            ))
            if index == 0:
                result_items.append(discord.ui.Separator())
    if reason and duel.get("mode") == "endurance":
        # Divider after the second participant's final statistic, before the endurance reason.
        result_items.append(discord.ui.Separator())
        result_items.append(discord.ui.TextDisplay('**Причина завершения:** ' + reason))

    result_view = discord.ui.LayoutView(timeout=None)
    result_view.add_item(discord.ui.Container(*result_items, accent_color=COLOR))

    try:
        await channel.send(view=result_view, allowed_mentions=discord.AllowedMentions.none())
        for player_id in duel["players"]:
            member = channel.guild.get_member(player_id)
            if member:
                overwrite = channel.overwrites_for(member)
                overwrite.send_messages = False
                await channel.set_permissions(member, overwrite=overwrite, reason="Дуэль завершена")
    except (discord.Forbidden, discord.HTTPException):
        pass

    asyncio.create_task(delete_duel_channel_later(channel))
    await update_duel_public_message(duel, winner_id)


async def speed_duel_timer(channel_id: int, seconds: int) -> None:
    try:
        await asyncio.sleep(seconds)
        duel = active_duels.get(channel_id)
        if duel is None:
            return
        channel = bot.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            await finish_duel(channel, reason="Время дуэли истекло.")
    except asyncio.CancelledError:
        pass


async def endurance_duel_timer(channel_id: int) -> None:
    try:
        while True:
            await asyncio.sleep(1)
            duel = active_duels.get(channel_id)
            if duel is None:
                return
            now = asyncio.get_running_loop().time()
            expired = [pid for pid in duel["players"] if now - duel["last_message_at"][pid] >= 120]
            if not expired:
                continue

            channel = bot.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                active_duels.pop(channel_id, None)
                return

            if len(expired) == 2:
                await finish_duel(channel, reason="Оба участника не писали 2 минуты.")
            else:
                await finish_duel(channel, loser_id=expired[0], reason=f"<@{expired[0]}> не писал 2 минуты.")
            return
    except asyncio.CancelledError:
        pass


async def start_duel(channel: discord.TextChannel, mode: str, duration_seconds: int | None = None) -> None:
    duel = active_duels.get(channel.id)
    if duel is None or duel.get("started"):
        return

    duel["started"] = True
    duel["mode"] = mode
    duel["duration_seconds"] = duration_seconds
    now = asyncio.get_running_loop().time()
    duel["started_at"] = now
    duel["last_message_at"] = {pid: now for pid in duel["players"]}

    if mode == "speed":
        seconds = int(duration_seconds or 60)
        duel["task"] = asyncio.create_task(speed_duel_timer(channel.id, seconds))
        await channel.send(view=duel_layout(
            "Дуэль на скорость началась",
            f"Длительность: **{seconds} секунд**\n"
            "Побеждает участник с более высоким **WPM**.",
        ), allowed_mentions=discord.AllowedMentions.none())
    else:
        duel["task"] = asyncio.create_task(endurance_duel_timer(channel.id))
        await channel.send(view=duel_layout(
            "Дуэль на выдержку началась",
            "Если один из участников не отправит ни одного сообщения в течение **2 минут**, он проиграет.",
        ), allowed_mentions=discord.AllowedMentions.none())


class DuelDurationSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Выберите длительность дуэли",
            options=[
                discord.SelectOption(label="30 секунд", value="30"),
                discord.SelectOption(label="60 секунд", value="60"),
                discord.SelectOption(label="120 секунд", value="120"),
                discord.SelectOption(label="180 секунд", value="180"),
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            return
        duel = active_duels.get(interaction.channel.id)
        if duel is None or interaction.user.id not in duel["players"]:
            await interaction.response.send_message("Только участники дуэли могут выбрать время.", ephemeral=True)
            return
        if duel.get("started"):
            await interaction.response.send_message("Дуэль уже началась.", ephemeral=True)
            return

        await interaction.response.defer()
        try:
            await interaction.message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        await start_duel(interaction.channel, "speed", int(self.values[0]))


class DuelDurationView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("-# Дуэль"),
            discord.ui.TextDisplay("## Длительность дуэли"),
            discord.ui.Separator(),
            discord.ui.ActionRow(DuelDurationSelect()),
            accent_color=COLOR,
        ))


class DuelModeSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Выберите режим дуэли",
            options=[
                discord.SelectOption(label="На скорость", description="Побеждает участник с более высоким WPM", value="speed"),
                discord.SelectOption(label="На выдержку", description="Проигрывает тот, кто молчит 2 минуты", value="endurance"),
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            return
        duel = active_duels.get(interaction.channel.id)
        if duel is None or interaction.user.id not in duel["players"]:
            await interaction.response.send_message("Только участники дуэли могут выбрать режим.", ephemeral=True)
            return
        if duel.get("mode_selected") or duel.get("started"):
            await interaction.response.send_message("Режим дуэли уже выбран.", ephemeral=True)
            return

        duel["mode_selected"] = True
        selected = self.values[0]
        await interaction.response.defer()
        try:
            await interaction.message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

        if selected == "speed":
            await interaction.channel.send(view=DuelDurationView())
        else:
            await start_duel(interaction.channel, "endurance")


class DuelModeView(discord.ui.LayoutView):
    def __init__(self, challenger: discord.Member, opponent: discord.Member):
        super().__init__(timeout=120)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("-# Дуэль"),
            discord.ui.TextDisplay("## Настройка дуэли"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"**Участники:** {challenger.mention} vs {opponent.mention}"),
            discord.ui.ActionRow(DuelModeSelect()),
            accent_color=COLOR,
        ))


async def create_duel_channel(
    guild: discord.Guild,
    challenger: discord.Member,
    opponent: discord.Member,
) -> discord.TextChannel | None:
    category = guild.get_channel(DUEL_CATEGORY_ID)
    if category is None:
        try:
            category = await guild.fetch_channel(DUEL_CATEGORY_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
    if not isinstance(category, discord.CategoryChannel):
        return None

    me = guild.me
    overwrites: dict[Any, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False, send_messages=False),
        challenger: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        opponent: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    if me:
        overwrites[me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            read_message_history=True,
        )

    try:
        return await guild.create_text_channel(
            name=f"duel-{challenger.name}-{opponent.name}"[:100],
            category=category,
            overwrites=overwrites,
            reason=f"Дуэль {challenger} vs {opponent}",
        )
    except (discord.Forbidden, discord.HTTPException):
        return None


class DuelChallengeView(discord.ui.View):
    def __init__(self, challenger_id: int, opponent_id: int | None):
        # Кнопка фактически помещается внутрь Components V2 LayoutView, поэтому
        # стандартный timeout этого View не запускается. Таймер вызова ведём сами.
        super().__init__(timeout=None)
        self.challenger_id = challenger_id
        self.opponent_id = opponent_id
        self.message: discord.Message | None = None
        self.accepted = False
        self.timeout_task: asyncio.Task[None] | None = None

    def release_pending(self) -> None:
        pending_duel_users.discard(self.challenger_id)
        if self.opponent_id is not None:
            pending_duel_users.discard(self.opponent_id)

    @discord.ui.button(label="Принять", style=discord.ButtonStyle.secondary)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return

        challenger = interaction.guild.get_member(self.challenger_id)
        if challenger is None:
            await interaction.response.send_message("Участник больше недоступен.", ephemeral=True)
            return

        if interaction.user.id == self.challenger_id:
            await interaction.response.send_message(
                view=duel_layout("Принять дуэль", "Вы не можете принять собственный вызов."),
                ephemeral=True,
            )
            return

        if self.opponent_id is not None and interaction.user.id != self.opponent_id:
            await interaction.response.send_message(
                view=duel_layout("Начать дуэль", "Этот вызов предназначен другому участнику."),
                ephemeral=True,
            )
            return

        opponent = interaction.user
        if user_in_active_duel(challenger.id) or user_in_active_duel(opponent.id):
            await interaction.response.send_message(
                view=duel_layout("Начать дуэль", "Один из участников уже находится в активной дуэли."),
                ephemeral=True,
            )
            return

        if self.accepted:
            await interaction.response.send_message("Вызов уже принят.", ephemeral=True)
            return

        self.accepted = True
        self.stop()
        if self.timeout_task and not self.timeout_task.done():
            self.timeout_task.cancel()
        pending_duel_users.add(opponent.id)

        await interaction.response.edit_message(
            embed=None,
            view=duel_layout("Вызов принят", f"{opponent.mention} принял вызов {challenger.mention}."),
            allowed_mentions=discord.AllowedMentions.none(),
        )

        channel = await create_duel_channel(interaction.guild, challenger, opponent)
        if channel is None:
            self.release_pending()
            pending_duel_users.discard(opponent.id)
            try:
                await interaction.message.edit(
                    embed=duel_embed("Дуэль", "Не удалось создать канал дуэли."),
                    view=None,
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            return

        active_duels[channel.id] = {
            # The public /duel message survives deletion of the private channel.
            "announcement_channel_id": interaction.channel_id,
            "announcement_message_id": interaction.message.id,
            "creator_id": challenger.id,
            "players": (challenger.id, opponent.id),
            "started": False,
            "finished": False,
            "mode": None,
            "mode_selected": False,
            "duration_seconds": None,
            "started_at": None,
            "ended_at": None,
            "task": None,
            "last_author_id": None,
            "current_streak": 0,
            "stats": {
                challenger.id: {"messages": 0, "characters": 0, "max_streak": 0, "max_message_chars": 0},
                opponent.id: {"messages": 0, "characters": 0, "max_streak": 0, "max_message_chars": 0},
            },
        }
        self.release_pending()
        pending_duel_users.discard(opponent.id)

        try:
            await interaction.message.edit(
                embed=None,
                view=discord.ui.LayoutView(timeout=None).add_item(discord.ui.Container(
                    discord.ui.TextDisplay("-# Дуэль"),
                    discord.ui.TextDisplay("## Канал дуэли создан"),
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(f"Канал: {channel.mention}"),
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(f"{challenger.mention} vs {opponent.mention}"),
                    accent_color=COLOR,
                )),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

        await channel.send(
            view=DuelModeView(challenger, opponent),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def expire_after(self, seconds: int = 360) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return

        if self.accepted:
            return

        self.release_pending()
        if self.message is None:
            return

        if self.opponent_id is None:
            description = "Вызов никто не принял."
        else:
            description = f"<@{self.opponent_id}>, не принял вызов."

        try:
            await self.message.edit(
                embed=None,
                view=duel_layout("Вызов не принят", description),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass




@bot.tree.command(name="duel", description="Бросить вызов на дуэль")
@discord.app_commands.rename(opponent="пользователь")
@discord.app_commands.describe(opponent="Пользователь, с которым будет дуэль.")
async def duel_command(interaction: discord.Interaction, opponent: discord.Member | None = None) -> None:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return

    challenger = interaction.user

    if opponent is not None and opponent.id == challenger.id:
        await interaction.response.send_message(
            view=duel_layout("Не удалось начать дуэль", "Вы не можете начать дуэль с самим собой."),
            ephemeral=True,
        )
        return

    if opponent is not None and opponent.bot:
        await interaction.response.send_message(
            view=duel_layout("Не удалось начать дуэль", "Вы не можете начать дуэль с ботом."),
            ephemeral=True,
        )
        return

    if user_busy_with_duel(challenger.id) or (opponent is not None and user_busy_with_duel(opponent.id)):
        await interaction.response.send_message(
            view=duel_layout("Не удалось начать дуэль", "Один из участников уже находится в активной дуэли."),
            ephemeral=True,
        )
        return

    pending_duel_users.add(challenger.id)
    if opponent is not None:
        pending_duel_users.add(opponent.id)

    view = DuelChallengeView(challenger.id, opponent.id if opponent is not None else None)
    if opponent is None:
        description = f"{challenger.mention}, бросил вызов."
    else:
        description = f"{opponent.mention}, вам бросил вызов {challenger.mention}."

    challenge_layout = duel_layout("Вызов на дуэль", description, *view.children)
    await interaction.response.send_message(
        view=challenge_layout,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    try:
        view.message = await interaction.original_response()
        # Вызов действует 6 минут. После этого он автоматически отменяется.
        view.timeout_task = asyncio.create_task(view.expire_after(360))
    except discord.HTTPException:
        view.release_pending()


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or not message.guild:
        return
    duel = active_duels.get(message.channel.id)
    if duel is None or not duel.get("started") or message.author.id not in duel["players"]:
        return

    stats = duel["stats"][message.author.id]
    char_count = len(message.content or "")
    stats["messages"] += 1
    stats["characters"] += char_count
    stats["max_message_chars"] = max(stats["max_message_chars"], char_count)

    if duel["last_author_id"] == message.author.id:
        duel["current_streak"] += 1
    else:
        duel["last_author_id"] = message.author.id
        duel["current_streak"] = 1
    stats["max_streak"] = max(stats["max_streak"], duel["current_streak"])

    if duel["mode"] == "endurance":
        duel["last_message_at"][message.author.id] = asyncio.get_running_loop().time()


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
    if before.author.bot or not before.guild or before.content == after.content:
        return

    info = (
        f"Пользователь: {member_id_text(before.author)}\n"
        f"Канал: {channel_id_text(before.channel)}"
    )
    before_text = f"Было:\n> {limited_text(before.content, 'Текст отсутствует')}"
    after_text = f"Стало:\n> {limited_text(after.content, 'Текст отсутствует')}"
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay("-# Логи сообщений"),
        discord.ui.TextDisplay("## Сообщение изменено"),
        discord.ui.Separator(),
        discord.ui.TextDisplay(info),
        discord.ui.Separator(),
        discord.ui.TextDisplay(before_text),
        discord.ui.Separator(),
        discord.ui.TextDisplay(after_text),
        discord.ui.Separator(),
        discord.ui.ActionRow(discord.ui.Button(
            label="Перейти к сообщению", style=discord.ButtonStyle.link, url=after.jump_url
        )),
        accent_color=COLOR,
    ))
    await send_message_log(before.guild, view, mention_users=(before.author,))


@bot.event
async def on_message_delete(message: discord.Message) -> None:
    if message.author.bot or not message.guild:
        return

    deleter = await find_message_deleter(message)
    info_lines = []
    if deleter:
        info_lines.append(f"Исполнитель: {member_id_text(deleter)}")
    info_lines.extend([
        f"Пользователь: {member_id_text(message.author)}",
        f"Канал: {channel_id_text(message.channel)}",
    ])

    items: list[Any] = [
        discord.ui.TextDisplay("-# Логи сообщений"),
        discord.ui.TextDisplay("## Сообщение удалено"),
        discord.ui.Separator(),
        discord.ui.TextDisplay("\n".join(info_lines)),
        discord.ui.Separator(),
    ]
    if message.content and message.content.strip():
        items.append(discord.ui.TextDisplay(f"Сообщение:\n> {limited_text(message.content)}"))
        items.append(discord.ui.Separator())
    if message.attachments:
        attachment_title = "Вложение:" if len(message.attachments) == 1 else "Вложения:"
        attachment_lines = [attachment_title]
        attachment_lines.extend(f"> [{item.filename}]({item.url})" for item in message.attachments)
        items.append(discord.ui.TextDisplay("\n".join(attachment_lines)))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*items, accent_color=COLOR))
    await send_message_log(
        message.guild, view,
        mention_users=(message.author, deleter) if deleter else (message.author,),
    )


def server_member_log_layout(member: discord.Member, *, joined: bool) -> discord.ui.LayoutView:
    if joined:
        title = "Участник присоединился"
        action = "присоединился к серверу."
        details = (
            f"Аккаунт создан: {discord_datetime(member.created_at)}\n"
            f"На сервере: {member.guild.member_count or 0} участников."
        )
    else:
        title = "Участник покинул сервер"
        action = "покинул сервер."
        joined_at = member.joined_at
        joined_text = discord_datetime(joined_at) if joined_at else "Неизвестно"
        if joined_at is not None:
            now = datetime.now(timezone.utc)
            joined_utc = joined_at if joined_at.tzinfo else joined_at.replace(tzinfo=timezone.utc)
            days = max(0, (now - joined_utc.astimezone(timezone.utc)).days)
            stayed_text = f"{days} дн."
        else:
            stayed_text = "Неизвестно"
        details = (
            f"Присоединился: {joined_text}\n"
            f"Пробыл на сервере: {stayed_text}\n"
            f"На сервере: {member.guild.member_count or 0} участников."
        )

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay("-# Логи сервера"),
        discord.ui.TextDisplay(f"## {title}"),
        discord.ui.Separator(),
        discord.ui.TextDisplay(
            f"{member.mention}, {action}\n\n"
            f"Пользователь: {member}\n"
            f"ID: `{member.id}`"
        ),
        discord.ui.Separator(),
        discord.ui.TextDisplay(details),
        accent_color=COLOR,
    ))
    return view


@bot.event
async def on_member_join(member: discord.Member) -> None:
    await send_server_log(
        member.guild, server_member_log_layout(member, joined=True),
        mention_users=(member,),
    )


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    await send_server_log(
        member.guild, server_member_log_layout(member, joined=False),
        mention_users=(member,),
    )


@bot.event
async def on_ready() -> None:
    global _private_room_view_registered, _private_room_panel_ready, _commands_synced
    if not _commands_synced:
        try:
            await bot.tree.sync()
            _commands_synced = True
        except discord.HTTPException as error:
            print(f"Не удалось синхронизировать slash-команды: {error}")
    if not _private_room_view_registered:
        bot.add_view(PrivateRoomPanelView())
        _private_room_view_registered = True
    if not _private_room_panel_ready:
        await restore_private_room_indexes()
        await ensure_private_room_panel()
        _private_room_panel_ready = True
    print(f"Бот приватных комнат запущен: {bot.user}")

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
    await handle_private_room_voice_update(member, before, after)

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Переменная окружения TOKEN не задана.")
    bot.run(TOKEN)
