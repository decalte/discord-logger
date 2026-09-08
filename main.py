from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

TOKEN = os.getenv("TOKEN")

PRIVATE_ROOM_CONTROL_CHANNEL_ID = 1546849303379706016
PRIVATE_ROOM_CREATE_CHANNEL_ID = 1546849353371492413

COLOR = discord.Color(0x303136)
BASE_DIR = Path(__file__).resolve().parent
PRIVATE_ROOMS_FILE = BASE_DIR / "private_rooms.json"

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

_private_room_view_registered = False
_private_room_panel_ready = False
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

@bot.event
async def on_ready() -> None:
    global _private_room_view_registered, _private_room_panel_ready
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
