from __future__ import annotations

import asyncio
import json
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks


TOKEN = os.getenv("TOKEN")

# Каналы и роли
MESSAGE_LOG_CHANNEL_ID = 1527284881351118960
ROLE_LOG_CHANNEL_ID = 1527838995302580315
TIMEOUT_LOG_CHANNEL_ID = 1527340102861197423
KICK_LOG_CHANNEL_ID = 1527340314912886865
BAN_LOG_CHANNEL_ID = 1527340343476093069
LEAVE_LOG_CHANNEL_ID = 1527694442998403172
ANTICRASH_LOG_CHANNEL_ID = 1527478400728694865
CHANNEL_LOG_CHANNEL_ID = 1527681524416123020
VOICE_CHANNEL_ID = 1527411803032780960
ECONOMY_LOG_CHANNEL_ID = 1529911589967368273

ANTI_CRASH_ROLE_ID = 1527476785590177903
BOOSTER_ROLE_ID = 1517190145835798639
AUTO_ROLE_ID = 1527176147777884160
OWNER_ID = 1519960093787951107

CLEAR_ALLOWED_ROLE_IDS = {
    1518684434252169247,
    1516563878669193357,
    1527110780892483754,
    1526363607531520191,
}

COLOR = discord.Color.from_rgb(47, 47, 47)
MOSCOW_TZ = timezone(timedelta(hours=3))
DATABASE_PATH = Path(os.getenv("BOT_DATABASE", "bot_data.sqlite3"))

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.moderation = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
voice_connection_lock = asyncio.Lock()
db_lock = asyncio.Lock()
anti_crash_actions: dict[int, list[str]] = {}


# -----------------------------------------------------------------------------
# База данных
# -----------------------------------------------------------------------------

def db_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize_database() -> None:
    with db_connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS balances (
                user_id INTEGER PRIMARY KEY,
                coins INTEGER NOT NULL DEFAULT 0 CHECK(coins >= 0),
                diamonds INTEGER NOT NULL DEFAULT 0 CHECK(diamonds >= 0)
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                currency TEXT NOT NULL CHECK(currency IN ('coins', 'diamonds')),
                description TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_transactions_user
            ON transactions(user_id, id DESC);

            CREATE TABLE IF NOT EXISTS timely_cooldowns (
                user_id INTEGER PRIMARY KEY,
                last_claim_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS message_counts (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS hidden_roles (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                hidden_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id, role_id)
            );

            CREATE TABLE IF NOT EXISTS antichrash_members (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                saved_role_ids TEXT NOT NULL,
                activated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );
            """
        )


async def get_balance(user_id: int) -> tuple[int, int]:
    async with db_lock:
        with db_connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO balances(user_id) VALUES (?)", (user_id,)
            )
            row = connection.execute(
                "SELECT coins, diamonds FROM balances WHERE user_id = ?", (user_id,)
            ).fetchone()
    return int(row["coins"]), int(row["diamonds"])


async def change_balance(
    user_id: int,
    currency: str,
    delta: int,
    description: str,
    *,
    allow_clamp_to_zero: bool = False,
) -> tuple[bool, int]:
    if currency not in {"coins", "diamonds"}:
        raise ValueError("Неизвестная валюта")

    async with db_lock:
        with db_connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO balances(user_id) VALUES (?)", (user_id,)
            )
            row = connection.execute(
                f"SELECT {currency} AS value FROM balances WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            old_value = int(row["value"])
            new_value = old_value + delta

            if new_value < 0:
                if not allow_clamp_to_zero:
                    connection.rollback()
                    return False, old_value
                new_value = 0

            actual_delta = new_value - old_value
            connection.execute(
                f"UPDATE balances SET {currency} = ? WHERE user_id = ?",
                (new_value, user_id),
            )
            if actual_delta != 0:
                connection.execute(
                    """
                    INSERT INTO transactions(user_id, amount, currency, description, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        actual_delta,
                        currency,
                        description,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            connection.commit()
    return True, new_value


async def transfer_coins(
    sender_id: int,
    recipient_id: int,
    amount: int,
    sender_description: str,
    recipient_description: str,
) -> tuple[bool, int, int, int]:
    fee = max(1, math.ceil(amount * 0.05))
    total = amount + fee

    async with db_lock:
        with db_connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO balances(user_id) VALUES (?)", (sender_id,)
            )
            connection.execute(
                "INSERT OR IGNORE INTO balances(user_id) VALUES (?)", (recipient_id,)
            )
            sender_row = connection.execute(
                "SELECT coins FROM balances WHERE user_id = ?", (sender_id,)
            ).fetchone()
            sender_balance = int(sender_row["coins"])
            if sender_balance < total:
                connection.rollback()
                recipient_balance = int(
                    connection.execute(
                        "SELECT coins FROM balances WHERE user_id = ?", (recipient_id,)
                    ).fetchone()["coins"]
                )
                return False, sender_balance, recipient_balance, fee

            connection.execute(
                "UPDATE balances SET coins = coins - ? WHERE user_id = ?",
                (total, sender_id),
            )
            connection.execute(
                "UPDATE balances SET coins = coins + ? WHERE user_id = ?",
                (amount, recipient_id),
            )
            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """
                INSERT INTO transactions(user_id, amount, currency, description, created_at)
                VALUES (?, ?, 'coins', ?, ?)
                """,
                (sender_id, -total, sender_description, now),
            )
            connection.execute(
                """
                INSERT INTO transactions(user_id, amount, currency, description, created_at)
                VALUES (?, ?, 'coins', ?, ?)
                """,
                (recipient_id, amount, recipient_description, now),
            )
            sender_balance -= total
            recipient_balance = int(
                connection.execute(
                    "SELECT coins FROM balances WHERE user_id = ?", (recipient_id,)
                ).fetchone()["coins"]
            )
            connection.commit()
    return True, sender_balance, recipient_balance, fee


async def get_transactions(user_id: int) -> list[sqlite3.Row]:
    async with db_lock:
        with db_connect() as connection:
            rows = connection.execute(
                """
                SELECT amount, currency, description, created_at
                FROM transactions
                WHERE user_id = ?
                ORDER BY id DESC
                """,
                (user_id,),
            ).fetchall()
    return rows


async def increment_message_count(guild_id: int, user_id: int) -> None:
    async with db_lock:
        with db_connect() as connection:
            connection.execute(
                """
                INSERT INTO message_counts(guild_id, user_id, count)
                VALUES (?, ?, 1)
                ON CONFLICT(guild_id, user_id)
                DO UPDATE SET count = count + 1
                """,
                (guild_id, user_id),
            )


async def get_top_message_counts(guild_id: int) -> list[sqlite3.Row]:
    async with db_lock:
        with db_connect() as connection:
            return connection.execute(
                """
                SELECT user_id, count
                FROM message_counts
                WHERE guild_id = ?
                ORDER BY count DESC, user_id ASC
                LIMIT 10
                """,
                (guild_id,),
            ).fetchall()


# -----------------------------------------------------------------------------
# Вспомогательные функции
# -----------------------------------------------------------------------------

def moscow_time(value: datetime | None = None) -> datetime:
    if value is None:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(MOSCOW_TZ)


def russian_time(value: datetime | None = None) -> str:
    return moscow_time(value).strftime("Сегодня, в %H:%M")


def russian_date(value: datetime) -> str:
    months = (
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    )
    local = moscow_time(value)
    return f"{local.day} {months[local.month - 1]} {local.year}"


def avatar_url(user: discord.abc.User) -> str:
    return user.display_avatar.with_size(1024).url


def member_id_text(member: discord.abc.User) -> str:
    return f"{member.mention}\nID: `{member.id}`"


def get_channel(guild: discord.Guild, channel_id: int):
    return guild.get_channel(channel_id)


async def send_economy_log(guild: discord.Guild | None, embed: discord.Embed) -> None:
    """Отправляет лог изменения экономики в отдельный канал."""
    if guild is None:
        return

    channel = guild.get_channel(ECONOMY_LOG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await guild.fetch_channel(ECONOMY_LOG_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
            print(f"Не удалось получить канал логов экономики: {error}")
            return

    if not isinstance(channel, discord.abc.Messageable):
        print(f"Канал {ECONOMY_LOG_CHANNEL_ID} не поддерживает отправку сообщений")
        return

    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException) as error:
        print(f"Ошибка отправки лога экономики: {error}")


async def find_audit_entry(
    guild: discord.Guild,
    action: discord.AuditLogAction,
    target_id: int,
    max_age: int = 15,
):
    try:
        async for entry in guild.audit_logs(limit=12, action=action):
            if not entry.target or entry.target.id != target_id:
                continue
            age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
            if age <= max_age:
                return entry
    except discord.Forbidden:
        print(f"Нет права на просмотр журнала аудита на сервере: {guild.name}")
    except discord.HTTPException as error:
        print(f"Ошибка получения журнала аудита: {error}")
    return None


def format_roles_for_log(roles: list[discord.Role]) -> str:
    value = "> " + " ".join(role.mention for role in roles)
    return value if len(value) <= 1024 else value[:1021] + "..."


def protected_record(guild_id: int, user_id: int) -> list[int] | None:
    with db_connect() as connection:
        row = connection.execute(
            "SELECT saved_role_ids FROM antichrash_members WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
    if row is None:
        return None
    try:
        return [int(role_id) for role_id in json.loads(row["saved_role_ids"])]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def save_protected_record(guild_id: int, user_id: int, role_ids: list[int]) -> None:
    with db_connect() as connection:
        connection.execute(
            """
            INSERT INTO antichrash_members(guild_id, user_id, saved_role_ids, activated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET saved_role_ids = excluded.saved_role_ids,
                          activated_at = excluded.activated_at
            """,
            (
                guild_id,
                user_id,
                json.dumps(role_ids),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def remove_protected_record(guild_id: int, user_id: int) -> list[int] | None:
    role_ids = protected_record(guild_id, user_id)
    if role_ids is None:
        return None
    with db_connect() as connection:
        connection.execute(
            "DELETE FROM antichrash_members WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
    return role_ids


# -----------------------------------------------------------------------------
# Голосовой канал и запуск
# -----------------------------------------------------------------------------

async def connect_to_target_voice_channel():
    if not VOICE_CHANNEL_ID:
        return
    async with voice_connection_lock:
        channel = bot.get_channel(VOICE_CHANNEL_ID)
        if channel is None:
            try:
                channel = await bot.fetch_channel(VOICE_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                print(f"Не удалось получить голосовой канал: {error}")
                return
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return
        existing = discord.utils.get(bot.voice_clients, guild=channel.guild)
        try:
            if existing and existing.is_connected():
                if existing.channel is None or existing.channel.id != channel.id:
                    await existing.move_to(channel)
            else:
                if existing:
                    await existing.disconnect(force=True)
                await channel.connect(self_mute=True, self_deaf=True, reconnect=True, timeout=30)
            await channel.guild.change_voice_state(
                channel=channel, self_mute=True, self_deaf=True
            )
        except (asyncio.TimeoutError, discord.ClientException, discord.Forbidden, discord.HTTPException) as error:
            print(f"Ошибка голосового подключения: {error}")


@tasks.loop(seconds=30)
async def voice_connection_watchdog():
    if bot.is_ready():
        await connect_to_target_voice_channel()


@voice_connection_watchdog.before_loop
async def before_voice_connection_watchdog():
    await bot.wait_until_ready()


@bot.event
async def on_voice_state_update(member, before, after):
    if bot.user is None or member.id != bot.user.id or not VOICE_CHANNEL_ID:
        return
    if after.channel is None or after.channel.id != VOICE_CHANNEL_ID or not after.self_mute or not after.self_deaf:
        await asyncio.sleep(2)
        await connect_to_target_voice_channel()


@bot.event
async def on_ready():
    if not getattr(bot, "commands_synced", False):
        try:
            synced = await bot.tree.sync()
            bot.commands_synced = True
            print(f"Синхронизировано команд: {len(synced)}")
        except discord.HTTPException as error:
            print(f"Ошибка синхронизации команд: {error}")
    await bot.change_presence(status=discord.Status.idle)
    await connect_to_target_voice_channel()
    if not voice_connection_watchdog.is_running():
        voice_connection_watchdog.start()
    print(f"Бот запущен: {bot.user}")


# -----------------------------------------------------------------------------
# Сообщения: счётчик, команды, редактирование и удаление
# -----------------------------------------------------------------------------

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.guild:
        await increment_message_count(message.guild.id, message.author.id)
    await bot.process_commands(message)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot or not before.guild or before.content == after.content:
        return
    log_channel = get_channel(before.guild, MESSAGE_LOG_CHANNEL_ID)
    if not log_channel:
        return
    old_content = before.content or "Текст отсутствует"
    new_content = after.content or "Текст отсутствует"
    old_content = old_content[:997] + "..." if len(old_content) > 1000 else old_content
    new_content = new_content[:997] + "..." if len(new_content) > 1000 else new_content
    embed = discord.Embed(title="Изменённое сообщение", color=COLOR, timestamp=moscow_time())
    embed.add_field(name="Пользователь", value=member_id_text(before.author), inline=False)
    embed.add_field(name="Канал", value=before.channel.mention, inline=False)
    embed.add_field(name="Было", value=f"> {old_content}", inline=False)
    embed.add_field(name="Стало", value=f"> {new_content}", inline=False)
    await log_channel.send(embed=embed)


async def find_message_deleter(message: discord.Message):
    await asyncio.sleep(1)
    try:
        async for entry in message.guild.audit_logs(
            limit=8, action=discord.AuditLogAction.message_delete
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
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    log_channel = get_channel(message.guild, MESSAGE_LOG_CHANNEL_ID)
    if not log_channel:
        return
    content = message.content.strip() if message.content else ""
    if not content:
        content = "Отсутствует"
    content = content[:997] + "..." if len(content) > 1000 else content
    deleter = await find_message_deleter(message)
    embed = discord.Embed(title="Удалённое сообщение", color=COLOR, timestamp=moscow_time())
    if deleter:
        embed.add_field(name="Удалил(а)", value=member_id_text(deleter), inline=False)
        embed.add_field(name="Пользователю", value=member_id_text(message.author), inline=False)
    else:
        embed.add_field(name="Пользователь", value=member_id_text(message.author), inline=False)
    embed.add_field(name="Канал", value=message.channel.mention, inline=False)
    embed.add_field(name="Сообщение", value=f"> {content}", inline=False)
    await log_channel.send(embed=embed)


# -----------------------------------------------------------------------------
# Логи ролей участников и изменения самих ролей
# -----------------------------------------------------------------------------

async def find_role_update_actor(member: discord.Member):
    await asyncio.sleep(1)
    return await find_audit_entry(
        member.guild, discord.AuditLogAction.member_role_update, member.id
    )


@bot.listen("on_member_update")
async def role_change_logs(before: discord.Member, after: discord.Member):
    if before.roles == after.roles:
        return
    log_channel = get_channel(after.guild, ROLE_LOG_CHANNEL_ID)
    if not log_channel:
        return
    before_ids = {role.id for role in before.roles}
    after_ids = {role.id for role in after.roles}
    added = [r for r in after.roles if r.id not in before_ids and not r.is_default()]
    removed = [r for r in before.roles if r.id not in after_ids and not r.is_default()]
    if not added and not removed:
        return
    audit = await find_role_update_actor(after)
    actor_text = member_id_text(audit.user) if audit else "Не удалось определить"
    if added:
        embed = discord.Embed(title="Выдача ролей", color=COLOR, timestamp=moscow_time())
        embed.add_field(name="Выдал(а)", value=actor_text, inline=False)
        embed.add_field(name="Пользователю", value=member_id_text(after), inline=False)
        embed.add_field(name="Выданные роли", value=format_roles_for_log(added), inline=False)
        await log_channel.send(embed=embed)
    if removed:
        embed = discord.Embed(title="Снятие ролей", color=COLOR, timestamp=moscow_time())
        embed.add_field(name="Снял(а)", value=actor_text, inline=False)
        embed.add_field(name="Пользователю", value=member_id_text(after), inline=False)
        embed.add_field(name="Снятые роли", value=format_roles_for_log(removed), inline=False)
        await log_channel.send(embed=embed)


@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role):
    name_changed = before.name != after.name
    color_changed = before.color != after.color
    if not name_changed and not color_changed:
        return
    log_channel = get_channel(after.guild, ROLE_LOG_CHANNEL_ID)
    if not log_channel:
        return
    await asyncio.sleep(1)
    audit = await find_audit_entry(
        after.guild, discord.AuditLogAction.role_update, after.id
    )
    actor_text = member_id_text(audit.user) if audit else "Не удалось определить"
    embed = discord.Embed(title="Изменение роли", color=COLOR, timestamp=moscow_time())
    embed.add_field(name="Изменил(а)", value=actor_text, inline=False)
    if name_changed:
        embed.add_field(name="Новое название", value=f"> {after.mention}", inline=False)
    if color_changed:
        embed.add_field(name="Новый цвет", value=str(after.color).upper(), inline=False)
    await log_channel.send(embed=embed)


# -----------------------------------------------------------------------------
# Тайм-ауты, кики, выходы, баны
# -----------------------------------------------------------------------------

@bot.listen("on_member_update")
async def timeout_logs(before: discord.Member, after: discord.Member):
    if before.timed_out_until == after.timed_out_until:
        return
    log_channel = get_channel(after.guild, TIMEOUT_LOG_CHANNEL_ID)
    if not log_channel:
        return
    await asyncio.sleep(1)
    audit = await find_audit_entry(
        after.guild, discord.AuditLogAction.member_update, after.id
    )
    moderator = member_id_text(audit.user) if audit else "Не удалось определить"
    reason = audit.reason if audit and audit.reason else "Причина не указана"
    if after.timed_out_until is None:
        embed = discord.Embed(title="Снятие тайм-аута", color=COLOR, timestamp=moscow_time())
        embed.add_field(name="Снял(а)", value=moderator, inline=False)
        embed.add_field(name="Пользователю", value=member_id_text(after), inline=False)
    else:
        embed = discord.Embed(title="Выдача тайм-аута", color=COLOR)
        embed.add_field(name="Выдал(а)", value=moderator, inline=False)
        embed.add_field(name="Пользователю", value=member_id_text(after), inline=False)
        embed.add_field(name="Причина", value=f"> {reason}", inline=False)
        embed.add_field(name="До", value=f"> {russian_time(after.timed_out_until)}", inline=False)
    await log_channel.send(embed=embed)


@bot.event
async def on_member_remove(member: discord.Member):
    await asyncio.sleep(1)
    audit = await find_audit_entry(member.guild, discord.AuditLogAction.kick, member.id)
    if audit:
        log_channel = get_channel(member.guild, KICK_LOG_CHANNEL_ID)
        if not log_channel:
            return
        embed = discord.Embed(title="Выгнан пользователь", color=COLOR, timestamp=moscow_time())
        embed.add_field(name="Выгнал(а)", value=member_id_text(audit.user), inline=False)
        embed.add_field(name="Пользователя", value=member_id_text(member), inline=False)
        embed.add_field(name="Причина", value=f"> {audit.reason or 'Причина не указана'}", inline=False)
        await log_channel.send(embed=embed)
        return
    log_channel = get_channel(member.guild, LEAVE_LOG_CHANNEL_ID)
    if not log_channel:
        return
    embed = discord.Embed(title="Выход с сервера", color=COLOR)
    embed.add_field(name="Пользователь", value=member_id_text(member), inline=False)
    embed.add_field(name="Дата и время выхода", value=f"> {russian_time()}", inline=False)
    await log_channel.send(embed=embed)


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    log_channel = get_channel(guild, BAN_LOG_CHANNEL_ID)
    if not log_channel:
        return
    await asyncio.sleep(1)
    audit = await find_audit_entry(guild, discord.AuditLogAction.ban, user.id)
    embed = discord.Embed(title="Выдача бана", color=COLOR, timestamp=moscow_time())
    embed.add_field(
        name="Выдал(а)",
        value=member_id_text(audit.user) if audit else "Не удалось определить",
        inline=False,
    )
    embed.add_field(name="Пользователю", value=member_id_text(user), inline=False)
    embed.add_field(name="Причина", value=f"> {audit.reason if audit and audit.reason else 'Причина не указана'}", inline=False)
    embed.add_field(name="До", value="> Навсегда", inline=False)
    await log_channel.send(embed=embed)


@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.User):
    log_channel = get_channel(guild, BAN_LOG_CHANNEL_ID)
    if not log_channel:
        return
    await asyncio.sleep(1)
    audit = await find_audit_entry(guild, discord.AuditLogAction.unban, user.id)
    embed = discord.Embed(title="Снятие бана", color=COLOR, timestamp=moscow_time())
    embed.add_field(
        name="Снял(а)",
        value=member_id_text(audit.user) if audit else "Не удалось определить",
        inline=False,
    )
    embed.add_field(name="Пользователю", value=member_id_text(user), inline=False)
    await log_channel.send(embed=embed)


# -----------------------------------------------------------------------------
# Каналы: создание, удаление, переименование
# -----------------------------------------------------------------------------

def channel_kind(channel) -> tuple[str, str]:
    if isinstance(channel, discord.TextChannel):
        return "текстовый", "канала"
    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        return "голосовой", "канала"
    if isinstance(channel, discord.CategoryChannel):
        return "категория", "категории"
    return "канал", "канала"


@bot.event
async def on_guild_channel_create(channel):
    log_channel = get_channel(channel.guild, CHANNEL_LOG_CHANNEL_ID)
    if not log_channel:
        return
    await asyncio.sleep(1)
    audit = await find_audit_entry(channel.guild, discord.AuditLogAction.channel_create, channel.id)
    actor = audit.user if audit else None
    if isinstance(channel, discord.TextChannel):
        title, field_name = "Создан текстовый канал", "Название канала"
    elif isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        title, field_name = "Создан голосовой канал", "Название канала"
    elif isinstance(channel, discord.CategoryChannel):
        title, field_name = "Создана категория для каналов", "Название категории"
    else:
        title, field_name = "Создан канал", "Название канала"
    embed = discord.Embed(title=title, color=COLOR)
    embed.add_field(name="Создал(а)", value=member_id_text(actor) if actor else "Не удалось определить", inline=False)
    embed.add_field(name=field_name, value=f"> {channel.mention if not isinstance(channel, discord.CategoryChannel) else channel.name}", inline=False)
    if not isinstance(channel, discord.CategoryChannel):
        visible_roles = []
        for role in channel.guild.roles:
            if channel.overwrites_for(role).view_channel is True:
                visible_roles.append(role.mention)
        embed.add_field(
            name="Права канала",
            value="\n".join(f"> {role}" for role in visible_roles) or "> @everyone",
            inline=False,
        )
    embed.add_field(name="Дата и время создания", value=f"> {russian_time(channel.created_at)}", inline=False)
    await log_channel.send(embed=embed)


@bot.event
async def on_guild_channel_delete(channel):
    log_channel = get_channel(channel.guild, CHANNEL_LOG_CHANNEL_ID)
    if not log_channel:
        return
    await asyncio.sleep(1)
    audit = await find_audit_entry(channel.guild, discord.AuditLogAction.channel_delete, channel.id)
    actor = audit.user if audit else None
    if isinstance(channel, discord.TextChannel):
        title, field_name = "Удален текстовый канал", "Название канала"
    elif isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        title, field_name = "Удален голосовой канал", "Название канала"
    elif isinstance(channel, discord.CategoryChannel):
        title, field_name = "Удалена категория для каналов", "Название категории"
    else:
        title, field_name = "Удален канал", "Название канала"
    embed = discord.Embed(title=title, color=COLOR)
    embed.add_field(name="Удалил(а)", value=member_id_text(actor) if actor else "Не удалось определить", inline=False)
    embed.add_field(name=field_name, value=f"> {channel.name}", inline=False)
    if not isinstance(channel, discord.CategoryChannel):
        visible_roles = [
            role.mention
            for role, overwrite in channel.overwrites.items()
            if isinstance(role, discord.Role) and overwrite.view_channel is True
        ]
        embed.add_field(
            name="Права канала",
            value="\n".join(f"> {role}" for role in visible_roles) or "> @everyone",
            inline=False,
        )
    embed.add_field(name="Дата и время удаления", value=f"> {russian_time()}", inline=False)
    await log_channel.send(embed=embed)


@bot.event
async def on_guild_channel_update(before, after):
    if before.name == after.name:
        return
    log_channel = get_channel(after.guild, CHANNEL_LOG_CHANNEL_ID)
    if not log_channel:
        return
    await asyncio.sleep(1)
    audit = await find_audit_entry(after.guild, discord.AuditLogAction.channel_update, after.id)
    actor_text = member_id_text(audit.user) if audit else "Не удалось определить"
    if isinstance(after, discord.TextChannel):
        title, field_name, value = "Изменение текстового канала", "Новое название канала", after.mention
    elif isinstance(after, (discord.VoiceChannel, discord.StageChannel)):
        title, field_name, value = "Изменение голосового канала", "Новое название канала", after.mention
    elif isinstance(after, discord.CategoryChannel):
        title, field_name, value = "Изменение категории", "Новое название категории", after.name
    else:
        return
    embed = discord.Embed(title=title, color=COLOR)
    embed.add_field(name="Изменил(а)", value=actor_text, inline=False)
    embed.add_field(name=field_name, value=f"> {value}", inline=False)
    embed.add_field(name="Дата и время изменения", value=f"> {russian_time()}", inline=False)
    await log_channel.send(embed=embed)


# -----------------------------------------------------------------------------
# Антикраш
# -----------------------------------------------------------------------------

async def set_protected_roles(member: discord.Member):
    anti_role = member.guild.get_role(ANTI_CRASH_ROLE_ID)
    booster_role = member.guild.get_role(BOOSTER_ROLE_ID)
    if anti_role is None:
        return
    roles = [anti_role]
    if booster_role and booster_role in member.roles:
        roles.append(booster_role)
    await member.edit(roles=roles, reason="Активен антикраш")


async def activate_antichrash(member: discord.Member, reason: str):
    if protected_record(member.guild.id, member.id) is not None:
        return
    saved = [
        role.id for role in member.roles
        if not role.is_default() and role.id not in {ANTI_CRASH_ROLE_ID, BOOSTER_ROLE_ID}
    ]
    save_protected_record(member.guild.id, member.id, saved)
    try:
        await set_protected_roles(member)
    except (discord.Forbidden, discord.HTTPException) as error:
        print(f"Ошибка выдачи антикраша: {error}")
    log_channel = get_channel(member.guild, ANTICRASH_LOG_CHANNEL_ID)
    if log_channel:
        embed = discord.Embed(title="Выдача антикраша", color=COLOR)
        embed.add_field(name="Администратору", value=member_id_text(member), inline=False)
        embed.add_field(name="Причина выдачи", value=f"> {reason}", inline=False)
        embed.add_field(name="Дата и время выдачи антикраша", value=f"> {russian_time()}", inline=False)
        await log_channel.send(embed=embed, view=AntiCrashView(member.guild.id, member.id))


class AntiCrashView(discord.ui.View):
    def __init__(self, guild_id: int, member_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.member_id = member_id
        self.remove_lock = asyncio.Lock()

    @discord.ui.button(label="Снять антикраш", style=discord.ButtonStyle.secondary)
    async def remove_antichrash(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != OWNER_ID:
            embed = discord.Embed(
                title="Снятие антикраша",
                description="Снять антикраш может только владелец сервера.",
                color=COLOR,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        async with self.remove_lock:
            saved = protected_record(self.guild_id, self.member_id)
            if saved is None:
                await interaction.response.send_message("Антикраш уже снят.", ephemeral=True)
                return
            member = interaction.guild.get_member(self.member_id) if interaction.guild else None
            if member is None:
                await interaction.response.send_message("Пользователь не найден.", ephemeral=True)
                return
            button.disabled = True
            await interaction.response.edit_message(view=self)
            saved = remove_protected_record(self.guild_id, self.member_id) or []
            anti_role = interaction.guild.get_role(ANTI_CRASH_ROLE_ID)
            booster_role = interaction.guild.get_role(BOOSTER_ROLE_ID)
            roles = []
            for role_id in saved:
                role = interaction.guild.get_role(role_id)
                if role and role != anti_role and role != booster_role:
                    roles.append(role)
            if booster_role and booster_role in member.roles:
                roles.append(booster_role)
            try:
                await member.edit(roles=roles, reason="Антикраш снят владельцем")
            except (discord.Forbidden, discord.HTTPException) as error:
                print(f"Ошибка снятия антикраша: {error}")
            embed = discord.Embed(title="Снятие антикраша", color=COLOR)
            embed.add_field(name="Снял(а)", value=member_id_text(interaction.user), inline=False)
            embed.add_field(name="Администратору", value=member_id_text(member), inline=False)
            embed.add_field(
                name="Возвращённые роли",
                value="\n".join(f"> {role.mention}" for role in roles if role != booster_role) or "> Роли отсутствуют",
                inline=False,
            )
            embed.add_field(name="Дата и время снятия антикраша", value=f"> {russian_time()}", inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)


@bot.listen("on_member_update")
async def enforce_antichrash_role(before: discord.Member, after: discord.Member):
    if before.roles == after.roles or protected_record(after.guild.id, after.id) is None:
        return
    anti_role = after.guild.get_role(ANTI_CRASH_ROLE_ID)
    booster_role = after.guild.get_role(BOOSTER_ROLE_ID)
    allowed = {ANTI_CRASH_ROLE_ID}
    if booster_role and booster_role in after.roles:
        allowed.add(BOOSTER_ROLE_ID)
    current = {role.id for role in after.roles if not role.is_default()}
    if anti_role and current != allowed:
        try:
            await set_protected_roles(after)
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"Ошибка восстановления антикраша: {error}")


@bot.event
async def on_member_join(member: discord.Member):
    if protected_record(member.guild.id, member.id) is not None:
        try:
            await set_protected_roles(member)
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"Ошибка восстановления антикраша после входа: {error}")
        return
    auto_role = member.guild.get_role(AUTO_ROLE_ID)
    if auto_role and auto_role not in member.roles:
        try:
            await member.add_roles(auto_role, reason="Автоматическая роль при входе")
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"Ошибка автовыдачи роли: {error}")


@bot.listen("on_member_ban")
async def antichrash_ban_check(guild: discord.Guild, user: discord.User):
    entry = await find_audit_entry(guild, discord.AuditLogAction.ban, user.id)
    if not entry or not isinstance(entry.user, discord.Member) or entry.user.bot:
        return
    actions = anti_crash_actions.setdefault(entry.user.id, [])
    actions.append("ban")
    if actions.count("ban") >= 2:
        await activate_antichrash(entry.user, "Превышение лимита по банам")
        anti_crash_actions[entry.user.id] = []


@bot.listen("on_member_remove")
async def antichrash_kick_check(member: discord.Member):
    await asyncio.sleep(1)
    entry = await find_audit_entry(member.guild, discord.AuditLogAction.kick, member.id)
    if not entry or not isinstance(entry.user, discord.Member) or entry.user.bot:
        return
    actions = anti_crash_actions.setdefault(entry.user.id, [])
    actions.append("kick")
    if actions.count("kick") >= 2:
        await activate_antichrash(entry.user, "Превышение лимита по кикам")
        anti_crash_actions[entry.user.id] = []


@bot.listen("on_member_update")
async def antichrash_timeout_check(before: discord.Member, after: discord.Member):
    if before.timed_out_until == after.timed_out_until or after.timed_out_until is None:
        return
    await asyncio.sleep(1)
    entry = await find_audit_entry(after.guild, discord.AuditLogAction.member_update, after.id)
    if not entry or not isinstance(entry.user, discord.Member) or entry.user.bot:
        return
    actions = anti_crash_actions.setdefault(entry.user.id, [])
    actions.append("timeout")
    if actions.count("timeout") >= 3:
        await activate_antichrash(entry.user, "Превышение лимита по тайм-аутам")
        anti_crash_actions[entry.user.id] = []


# -----------------------------------------------------------------------------
# Экономика: /avatar, /banner, /balance, /timely, /top
# -----------------------------------------------------------------------------

async def send_avatar(interaction: discord.Interaction, user: discord.Member | None):
    target = user or interaction.user
    embed = discord.Embed(title=f"Аватар — {target.name}", color=COLOR)
    embed.set_image(url=avatar_url(target))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="avatar", description="Посмотреть аватарку")
@app_commands.rename(user="пользователь")
async def avatar(interaction: discord.Interaction, user: discord.Member | None = None):
    await send_avatar(interaction, user)


def russian_message_word(number: int) -> str:
    number = abs(number)
    if number % 100 in range(11, 15):
        return "сообщений"
    if number % 10 == 1:
        return "сообщение"
    if number % 10 in range(2, 5):
        return "сообщения"
    return "сообщений"


def has_clear_role(member: discord.Member) -> bool:
    return any(role.id in CLEAR_ALLOWED_ROLE_IDS for role in member.roles)


@bot.tree.command(name="clear", description="Удаление сообщений")
@app_commands.describe(
    amount="Количество сообщений для удаления",
    user="Пользователь, чьи сообщения нужно удалить",
)
@app_commands.rename(amount="количество", user="пользователь")
async def clear(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, 1, 1000],
    user: discord.Member | None = None,
):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "Эту команду можно использовать только на сервере.",
            ephemeral=True,
        )
        return

    if not has_clear_role(interaction.user):
        await interaction.response.send_message(
            "У вас нет доступа к этой команде.",
            ephemeral=True,
        )
        return

    channel = interaction.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message(
            "Команду можно использовать только в текстовом канале.",
            ephemeral=True,
        )
        return

    bot_member = interaction.guild.me
    permissions = channel.permissions_for(bot_member) if bot_member else None
    if not permissions or not permissions.manage_messages or not permissions.read_message_history:
        await interaction.response.send_message(
            "Боту нужны права «Управлять сообщениями» и «Читать историю сообщений».",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        if user is None:
            deleted = await channel.purge(limit=amount)
        else:
            matched = 0

            def check(message: discord.Message) -> bool:
                nonlocal matched
                if matched >= amount:
                    return False
                if message.author.id == user.id:
                    matched += 1
                    return True
                return False

            # Просматриваем историю с запасом, чтобы найти указанное количество
            # сообщений конкретного пользователя.
            deleted = await channel.purge(limit=10000, check=check)

    except discord.Forbidden:
        await interaction.followup.send(
            "Не удалось удалить сообщения: у бота недостаточно прав.",
            ephemeral=True,
        )
        return
    except discord.HTTPException as error:
        await interaction.followup.send(
            f"Не удалось удалить сообщения из-за ошибки Discord: `{error}`",
            ephemeral=True,
        )
        return

    deleted_count = len(deleted)
    word = russian_message_word(deleted_count)

    if user is None:
        description = (
            f"{interaction.user.mention}, Вы успешно удалили "
            f"**{deleted_count} {word}**."
        )
    else:
        description = (
            f"{interaction.user.mention}, Вы успешно удалили "
            f"**{deleted_count} {word}** от **пользователя** {user.mention}."
        )

    embed = discord.Embed(
        title="Удаление сообщений",
        description=description,
        color=COLOR,
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="banner", description="Посмотреть баннер")
@app_commands.rename(user="пользователь")
async def banner(interaction: discord.Interaction, user: discord.Member | None = None):
    target = user or interaction.user
    fetched = await bot.fetch_user(target.id)
    embed = discord.Embed(title=f"Баннер — {target.name}", color=COLOR)
    if fetched.banner:
        embed.set_image(url=fetched.banner.with_size(1024).url)
    else:
        embed.description = "У данного пользователя **отсутствует** баннер"
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="balance", description="Посмотреть баланс пользователя")
@app_commands.rename(user="пользователь")
async def balance(interaction: discord.Interaction, user: discord.Member | None = None):
    target = user or interaction.user
    coins, diamonds = await get_balance(target.id)
    embed = discord.Embed(title=f"Баланс — {target.name}", color=COLOR)
    embed.set_thumbnail(url=avatar_url(target))
    embed.add_field(name="• Монеты", value=f"```{coins}```", inline=False)
    embed.add_field(name="• Алмазы", value=f"```{diamonds}```", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="timely", description="Ежедневная награда")
async def timely(interaction: discord.Interaction):
    now = datetime.now(timezone.utc)
    async with db_lock:
        with db_connect() as connection:
            row = connection.execute(
                "SELECT last_claim_at FROM timely_cooldowns WHERE user_id = ?",
                (interaction.user.id,),
            ).fetchone()
    embed = discord.Embed(title="Временная награда", color=COLOR)
    embed.set_thumbnail(url=avatar_url(interaction.user))
    if row:
        last_claim = datetime.fromisoformat(row["last_claim_at"])
        next_claim = last_claim + timedelta(hours=12)
        if now < next_claim:
            remaining = next_claim - now
            total_minutes = max(1, math.ceil(remaining.total_seconds() / 60))
            hours, minutes = divmod(total_minutes, 60)
            parts = []
            if hours:
                parts.append(f"{hours} ч.")
            if minutes:
                parts.append(f"{minutes} мин.")
            embed.description = (
                f">>> {interaction.user.mention}, Вы уже **забрали** свою награду.\n"
                f"Возвращайтесь через **{' '.join(parts)}**"
            )
            await interaction.response.send_message(embed=embed)
            return
    await change_balance(
        interaction.user.id,
        "coins",
        50,
        "Ежедневная награда",
    )
    async with db_lock:
        with db_connect() as connection:
            connection.execute(
                """
                INSERT INTO timely_cooldowns(user_id, last_claim_at)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET last_claim_at = excluded.last_claim_at
                """,
                (interaction.user.id, now.isoformat()),
            )
    embed.description = (
        f">>> {interaction.user.mention}, Вы **забрали** свои **50** 🪙\n"
        "Возвращайтесь через 12 часов"
    )

    if bot.user is not None:
        log_embed = discord.Embed(title="Выдача временной награды", color=COLOR)
        log_embed.add_field(name="Выдал(а)", value=member_id_text(bot.user), inline=False)
        log_embed.add_field(name="Пользователю", value=member_id_text(interaction.user), inline=False)
        log_embed.add_field(name="Количество монет", value="> `50`", inline=False)
        log_embed.set_footer(text=russian_time())
        await send_economy_log(interaction.guild, log_embed)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="top", description="Топ пользователей")
async def top(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return
    rows = await get_top_message_counts(interaction.guild.id)
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for index, row in enumerate(rows, start=1):
        member = interaction.guild.get_member(int(row["user_id"]))
        if member is None or member.bot:
            continue
        prefix = medals[index - 1] if index <= 3 else f"{index}."
        lines.append(f"{prefix} {member.mention} — **{int(row['count'])} сообщений**")
    embed = discord.Embed(
        title="ТОП-10 пользователей по текстовому онлайну",
        description=">>> " + ("\n".join(lines) if lines else "Сообщений пока нет"),
        color=COLOR,
    )
    embed.set_thumbnail(url=avatar_url(interaction.user))
    await interaction.response.send_message(embed=embed)


# -----------------------------------------------------------------------------
# !addcoins и !removecoins
# -----------------------------------------------------------------------------

class CurrencySelectView(discord.ui.View):
    def __init__(self, issuer: discord.Member, target: discord.Member, amount: int, remove: bool):
        super().__init__(timeout=60)
        self.issuer = issuer
        self.target = target
        self.amount = amount
        self.remove = remove
        self.used = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.issuer.id:
            await interaction.response.send_message("Эти кнопки доступны только автору команды.", ephemeral=True)
            return False
        return True

    async def finish(self, interaction: discord.Interaction, currency: str):
        if self.used:
            await interaction.response.send_message("Операция уже выполнена.", ephemeral=True)
            return
        self.used = True
        await interaction.response.defer()
        await interaction.message.delete()
        delta = -self.amount if self.remove else self.amount
        currency_ru = "монет" if currency == "coins" else "алмазов"
        action = "Списание" if self.remove else "Выдача"
        description = f"{action} {currency_ru} владельцем {self.issuer.id}"
        await change_balance(
            self.target.id,
            currency,
            delta,
            description,
            allow_clamp_to_zero=self.remove,
        )
        embed = discord.Embed(title=f"{action} {currency_ru}", color=COLOR, timestamp=moscow_time())
        embed.add_field(name="Списал(а)" if self.remove else "Выдал(а)", value=member_id_text(self.issuer), inline=False)
        embed.add_field(name="У пользователя" if self.remove else "Пользователю", value=member_id_text(self.target), inline=False)
        embed.add_field(name=f"Количество {currency_ru}", value=f"> `{self.amount}`", inline=False)
        await interaction.channel.send(embed=embed)

        log_embed = discord.Embed(title=f"{action} {currency_ru}", color=COLOR)
        log_embed.add_field(
            name="Списал(а)" if self.remove else "Выдал(а)",
            value=member_id_text(self.issuer),
            inline=False,
        )
        log_embed.add_field(
            name="У пользователя" if self.remove else "Пользователю",
            value=member_id_text(self.target),
            inline=False,
        )
        log_embed.add_field(
            name=f"Количество {currency_ru}",
            value=f"> `{self.amount}`",
            inline=False,
        )
        log_embed.set_footer(text=russian_time())
        await send_economy_log(interaction.guild, log_embed)

    @discord.ui.button(label="🪙 Монеты", style=discord.ButtonStyle.secondary)
    async def coins(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.finish(interaction, "coins")

    @discord.ui.button(label="💎 Алмазы", style=discord.ButtonStyle.secondary)
    async def diamonds(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.finish(interaction, "diamonds")


async def resolve_member(ctx: commands.Context, user_id: int) -> discord.Member | None:
    if not ctx.guild:
        return None
    member = ctx.guild.get_member(user_id)
    if member is None:
        try:
            member = await ctx.guild.fetch_member(user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
    return member


async def start_currency_command(ctx: commands.Context, user_id: int, amount: int, remove: bool):
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass
    if ctx.author.id != OWNER_ID:
        return
    if amount <= 0:
        await ctx.send("Количество должно быть больше нуля.", delete_after=5)
        return
    target = await resolve_member(ctx, user_id)
    if target is None:
        await ctx.send("Пользователь не найден на сервере.", delete_after=5)
        return
    embed = discord.Embed(title="Выбрать валюту", color=COLOR)
    await ctx.send(embed=embed, view=CurrencySelectView(ctx.author, target, amount, remove))


@bot.command(name="addcoins")
async def addcoins(ctx: commands.Context, user_id: int, amount: int):
    await start_currency_command(ctx, user_id, amount, False)


@bot.command(name="removecoins")
async def removecoins(ctx: commands.Context, user_id: int, amount: int):
    await start_currency_command(ctx, user_id, amount, True)


# -----------------------------------------------------------------------------
# /give
# -----------------------------------------------------------------------------

class GiveView(discord.ui.View):
    def __init__(self, sender: discord.Member, recipient: discord.Member, amount: int):
        super().__init__(timeout=60)
        self.sender = sender
        self.recipient = recipient
        self.amount = amount
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.sender.id:
            await interaction.response.send_message("Эти кнопки доступны только автору команды.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Подтвердить", style=discord.ButtonStyle.secondary)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            await interaction.response.send_message("Операция уже завершена.", ephemeral=True)
            return
        self.done = True
        success, _, _, fee = await transfer_coins(
            self.sender.id,
            self.recipient.id,
            self.amount,
            f"Передано пользователю {self.recipient.id}",
            f"Получено от пользователя {self.sender.id}",
        )
        embed = discord.Embed(title="Передать монеты", color=COLOR)
        embed.set_thumbnail(url=avatar_url(self.sender))
        if success:
            embed.description = (
                f">>> {self.sender.mention}, вы передали **{self.amount}** 🪙\n"
                f"пользователю {self.recipient.mention}"
            )

            log_embed = discord.Embed(title="Передача монет", color=COLOR)
            log_embed.add_field(name="Выдал(а)", value=member_id_text(self.sender), inline=False)
            log_embed.add_field(name="Пользователю", value=member_id_text(self.recipient), inline=False)
            log_embed.add_field(name="Количество монет", value=f"> `{self.amount}`", inline=False)
            log_embed.add_field(name="Комиссия", value=f"> `{fee}`", inline=False)
            log_embed.add_field(name="Получено пользователем", value=f"> `{self.amount}`", inline=False)
            log_embed.add_field(
                name="Списано у отправителя",
                value=f"> `{self.amount + fee}`",
                inline=False,
            )
            log_embed.set_footer(text=russian_time())
            await send_economy_log(interaction.guild, log_embed)
        else:
            embed.description = f">>> {self.sender.mention}, у Вас недостаточно монет с учётом комиссии 5%"
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            await interaction.response.send_message("Операция уже завершена.", ephemeral=True)
            return
        self.done = True
        embed = discord.Embed(title="Передать монеты", color=COLOR)
        embed.set_thumbnail(url=avatar_url(self.sender))
        embed.description = (
            f">>> {self.sender.mention}, вы **отказались** передавать **{self.amount}** 🪙 "
            f"пользователю {self.recipient.mention}"
        )
        await interaction.response.edit_message(embed=embed, view=None)


@bot.tree.command(name="give", description="Передать монеты пользователю")
@app_commands.rename(user="пользователь", amount="количество")
async def give(interaction: discord.Interaction, user: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message("Количество должно быть больше нуля.", ephemeral=True)
        return
    if user.id == interaction.user.id:
        await interaction.response.send_message("Нельзя передавать монеты самому себе.", ephemeral=True)
        return
    if user.bot:
        await interaction.response.send_message("Нельзя передавать монеты боту.", ephemeral=True)
        return
    fee = max(1, math.ceil(amount * 0.05))
    coins, _ = await get_balance(interaction.user.id)
    if coins < amount + fee:
        await interaction.response.send_message("Недостаточно монет с учётом комиссии 5%.", ephemeral=True)
        return
    embed = discord.Embed(title="Передать монеты", color=COLOR)
    embed.set_thumbnail(url=avatar_url(interaction.user))
    embed.description = (
        f">>> {interaction.user.mention}, вы **уверены** что хотите\n"
        f"передать **{amount}** 🪙, включая\n"
        f"комиссию 5% пользователю {user.mention}?"
    )
    await interaction.response.send_message(embed=embed, view=GiveView(interaction.user, user, amount))


# -----------------------------------------------------------------------------
# /hide
# -----------------------------------------------------------------------------

def can_hide_role(member: discord.Member, role: discord.Role) -> bool:
    if role.is_default() or role.managed or role.id in {ANTI_CRASH_ROLE_ID, BOOSTER_ROLE_ID}:
        return False
    me = member.guild.me
    return role in member.roles and me is not None and role < me.top_role


async def hide_role_autocomplete(interaction: discord.Interaction, current: str):
    if not isinstance(interaction.user, discord.Member):
        return []
    choices = []
    for role in reversed(interaction.user.roles):
        if can_hide_role(interaction.user, role) and current.lower() in role.name.lower():
            choices.append(app_commands.Choice(name=role.name[:100], value=str(role.id)))
        if len(choices) >= 25:
            break
    return choices


@bot.tree.command(name="hide", description="Спрятать роль")
@app_commands.describe(role="Роль, которую нужно спрятать")
@app_commands.rename(role="роль")
@app_commands.autocomplete(role=hide_role_autocomplete)
async def hide(interaction: discord.Interaction, role: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
        return
    try:
        role_id = int(role)
    except ValueError:
        await interaction.response.send_message("Выберите роль из списка.", ephemeral=True)
        return
    target_role = interaction.guild.get_role(role_id)
    if target_role is None or not can_hide_role(interaction.user, target_role):
        embed = discord.Embed(title="Спрятать роль", color=COLOR)
        embed.set_thumbnail(url=avatar_url(interaction.user))
        embed.description = f">>> {interaction.user.mention}, у Вас нет выбранной роли"
        await interaction.response.send_message(embed=embed)
        return
    try:
        await interaction.user.remove_roles(target_role, reason="Пользователь спрятал роль через /hide")
    except (discord.Forbidden, discord.HTTPException):
        await interaction.response.send_message("Не удалось спрятать роль.", ephemeral=True)
        return
    async with db_lock:
        with db_connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO hidden_roles(guild_id, user_id, role_id, hidden_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    interaction.guild.id,
                    interaction.user.id,
                    target_role.id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    embed = discord.Embed(title="Спрятать роль", color=COLOR)
    embed.set_thumbnail(url=avatar_url(interaction.user))
    embed.description = f">>> {interaction.user.mention}, Вы успешно спрятали роль {target_role.mention}"
    await interaction.response.send_message(embed=embed)


# -----------------------------------------------------------------------------
# /transactions
# -----------------------------------------------------------------------------

class TransactionsView(discord.ui.View):
    def __init__(self, owner_id: int, user: discord.Member, rows: list[sqlite3.Row]):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.user = user
        self.rows = rows
        self.page = 0
        self.per_page = 6
        self.update_buttons()

    @property
    def page_count(self):
        return max(1, math.ceil(len(self.rows) / self.per_page))

    def update_buttons(self):
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.page_count - 1

    def build_embed(self):
        start = self.page * self.per_page
        page_rows = self.rows[start:start + self.per_page]
        lines = []
        for row in page_rows:
            amount = int(row["amount"])
            sign = "+" if amount > 0 else "−"
            icon = "➕" if amount > 0 else "➖"
            date = russian_date(datetime.fromisoformat(row["created_at"]))
            lines.append(f"{icon} **{abs(amount)}** [{date}]\n{row['description']}")
        if not lines:
            lines.append("История транзакций отсутствует")
        embed = discord.Embed(
            title=f"Транзакции — {self.user.name}",
            description="\n".join(lines) + f"\n\n**Страница {self.page + 1}/{self.page_count}**",
            color=COLOR,
        )
        embed.set_thumbnail(url=avatar_url(self.user))
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Кнопки доступны только автору команды.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


@bot.tree.command(name="transactions", description="История транзакций")
async def transactions(interaction: discord.Interaction):
    rows = await get_transactions(interaction.user.id)
    view = TransactionsView(interaction.user.id, interaction.user, rows)
    await interaction.response.send_message(embed=view.build_embed(), view=view)


if __name__ == "__main__":
    initialize_database()
    if not TOKEN:
        raise RuntimeError("Переменная окружения TOKEN не задана.")
    bot.run(TOKEN)
