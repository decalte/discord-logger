import asyncio
import os
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands


# —————————————————————————————————————————————
# ТОКЕН БОТА
# —————————————————————————————————————————————

TOKEN = os.getenv("TOKEN")


# —————————————————————————————————————————————
# ID КАНАЛОВ ЛОГОВ
# —————————————————————————————————————————————

MESSAGE_LOG_CHANNEL_ID = 1527284881351118960
TIMEOUT_LOG_CHANNEL_ID = 1527340102861197423
KICK_LOG_CHANNEL_ID = 1527340314912886865
BAN_LOG_CHANNEL_ID = 1527340343476093069


# —————————————————————————————————————————————
# ЦВЕТ ЭМБЕДОВ — #2F2F2F
# —————————————————————————————————————————————

COLOR = discord.Color.from_rgb(47, 47, 47)


# —————————————————————————————————————————————
# НАСТРОЙКА INTENTS
# —————————————————————————————————————————————

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True


# —————————————————————————————————————————————
# СОЗДАНИЕ БОТА
# —————————————————————————————————————————————

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)
# —————————————————————————————————————————————
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# —————————————————————————————————————————————

def moscow_time():
    return datetime.now(
        timezone(timedelta(hours=3))
    )


def get_message_log_channel(guild: discord.Guild):
    return guild.get_channel(MESSAGE_LOG_CHANNEL_ID)


def get_timeout_log_channel(guild: discord.Guild):
    return guild.get_channel(TIMEOUT_LOG_CHANNEL_ID)


def get_kick_log_channel(guild: discord.Guild):
    return guild.get_channel(KICK_LOG_CHANNEL_ID)


def get_ban_log_channel(guild: discord.Guild):
    return guild.get_channel(BAN_LOG_CHANNEL_ID)


def format_reason(reason: str | None):
    if reason:
        return reason

    return "Причина не указана"


async def find_audit_entry(
    guild: discord.Guild,
    action: discord.AuditLogAction,
    target_id: int
):
    try:
        async for entry in guild.audit_logs(
            limit=10,
            action=action
        ):
            if not entry.target:
                continue

            if entry.target.id != target_id:
                continue

            time_difference = (
                datetime.now(timezone.utc)
                - entry.created_at
            ).total_seconds()

            if time_difference <= 15:
                return entry

    except discord.Forbidden:
        print(
            f"Нет права на просмотр журнала аудита "
            f"на сервере: {guild.name}"
        )

    except discord.HTTPException as error:
        print(
            f"Ошибка получения журнала аудита: {error}"
        )

    return None


# —————————————————————————————————————————————
# ЗАПУСК И СИНХРОНИЗАЦИЯ КОМАНД
# —————————————————————————————————————————————

@bot.event
async def on_ready():
    try:
        synced_commands = await bot.tree.sync()

        print(
            f"Синхронизировано команд: "
            f"{len(synced_commands)}"
        )

    except discord.HTTPException as error:
        print(
            f"Ошибка синхронизации команд: {error}"
        )

    print(f"Бот запущен: {bot.user}")
    # —————————————————————————————————————————————
# ЛОГИ ИЗМЕНЁННЫХ СООБЩЕНИЙ
# —————————————————————————————————————————————

@bot.event
async def on_message_edit(
    before: discord.Message,
    after: discord.Message
):
    if before.author.bot:
        return

    if not before.guild:
        return

    if before.content == after.content:
        return

    log_channel = get_message_log_channel(before.guild)

    if not log_channel:
        return

    old_content = before.content or "Текст отсутствует"
    new_content = after.content or "Текст отсутствует"

    if len(old_content) > 1000:
        old_content = old_content[:997] + "..."

    if len(new_content) > 1000:
        new_content = new_content[:997] + "..."

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
        value=f"> {old_content}",
        inline=False
    )

    embed.add_field(
        name="Стало",
        value=f"> {new_content}",
        inline=False
    )

    try:
        await log_channel.send(embed=embed)

    except discord.HTTPException as error:
        print(
            f"Ошибка отправки лога "
            f"измененного сообщения: {error}"
        )


# —————————————————————————————————————————————
# ЛОГИ УДАЛЁННЫХ СООБЩЕНИЙ
# —————————————————————————————————————————————

@bot.event
async def on_message_delete(
    message: discord.Message
):
    if message.author.bot:
        return

    if not message.guild:
        return

    log_channel = get_message_log_channel(message.guild)

    if not log_channel:
        return

    content = message.content or "Текст отсутствует"

    if len(content) > 1000:
        content = content[:997] + "..."

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
        value=f"> {content}",
        inline=False
    )

    try:
        await log_channel.send(embed=embed)

    except discord.HTTPException as error:
        print(
            f"Ошибка отправки лога "
            f"удаленного сообщения: {error}"
        )


# —————————————————————————————————————————————
# ОБРАБОТКА ТЕКСТОВЫХ КОМАНД
# —————————————————————————————————————————————

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)    
# —————————————————————————————————————————————
# ЛОГИ ТАЙМ-АУТОВ
# —————————————————————————————————————————————

@bot.event
async def on_member_update(
    before: discord.Member,
    after: discord.Member
):
    if before.timed_out_until == after.timed_out_until:
        return

    if after.timed_out_until is None:
        return

    log_channel = get_timeout_log_channel(after.guild)

    if not log_channel:
        return

    await asyncio.sleep(1)

    audit = await find_audit_entry(
        guild=after.guild,
        action=discord.AuditLogAction.member_update,
        target_id=after.id
    )

    if audit:
        moderator = (
            f"{audit.user.mention}\n"
            f"ID: `{audit.user.id}`"
        )
        reason = audit.reason or "Причина не указана"
    else:
        moderator = "Не удалось определить"
        reason = "Причина не указана"

    until = discord.utils.format_dt(
        after.timed_out_until,
        style="F"
    )

    embed = discord.Embed(
        title="Выдача тайм-аута",
        color=COLOR,
        timestamp=moscow_time()
    )

    embed.add_field(
        name="Выдал",
        value=moderator,
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
        value=f"> {until}",
        inline=False
    )

    await log_channel.send(embed=embed)
    # —————————————————————————————————————————————
# ЛОГИ КИКОВ
# —————————————————————————————————————————————

@bot.event
async def on_member_remove(member: discord.Member):
    log_channel = get_kick_log_channel(member.guild)

    if not log_channel:
        return

    await asyncio.sleep(1)

    audit = await find_audit_entry(
        guild=member.guild,
        action=discord.AuditLogAction.kick,
        target_id=member.id
    )

    # Если записи о кике нет, значит пользователь
    # скорее всего вышел сам
    if not audit:
        return

    moderator = (
        f"{audit.user.mention}\n"
        f"ID: `{audit.user.id}`"
    )

    reason = audit.reason or "Причина не указана"

    embed = discord.Embed(
        title="Выгнан пользователь",
        color=COLOR,
        timestamp=moscow_time()
    )

    embed.add_field(
        name="Выгнал",
        value=moderator,
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

    try:
        await log_channel.send(embed=embed)

    except discord.HTTPException as error:
        print(
            f"Ошибка отправки лога кика: {error}"
        )
        # —————————————————————————————————————————————
# ЛОГИ БАНОВ
# —————————————————————————————————————————————

@bot.event
async def on_member_ban(
    guild: discord.Guild,
    user: discord.User
):
    log_channel = get_ban_log_channel(guild)

    if not log_channel:
        return

    await asyncio.sleep(1)

    audit = await find_audit_entry(
        guild=guild,
        action=discord.AuditLogAction.ban,
        target_id=user.id
    )

    if audit:
        moderator = (
            f"{audit.user.mention}\n"
            f"ID: `{audit.user.id}`"
        )
        reason = audit.reason or "Причина не указана"
    else:
        moderator = "Не удалось определить"
        reason = "Причина не указана"

    embed = discord.Embed(
        title="Выдача бана",
        color=COLOR,
        timestamp=moscow_time()
    )

    embed.add_field(
        name="Выдал",
        value=moderator,
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
        name="До",
        value="> Навсегда",
        inline=False
    )

    try:
        await log_channel.send(embed=embed)

    except discord.HTTPException as error:
        print(
            f"Ошибка отправки лога бана: {error}"
        )
        # —————————————————————————————————————————————
# КОМАНДА /AVATAR
# —————————————————————————————————————————————

@bot.tree.command(
    name="avatar",
    description="Показать аватар пользователя"
)
@app_commands.rename(user="пользователь")
async def avatar(
    interaction: discord.Interaction,
    user: discord.Member | None = None
):
    if user is None:
        user = interaction.user

    embed = discord.Embed(
       title=f"Аватар — {user.name}",
        color=COLOR,
        timestamp=moscow_time()
    )

    embed.set_image(
        url=user.display_avatar.url
    )

    await interaction.response.send_message(
        embed=embed
    )

@bot.event
async def on_ready():
    await bot.change_presence(
        status=discord.Status.idle
    )

    print(f"Бот запущен: {bot.user}")

    try:
        synced = await bot.tree.sync()
        print(f"Синхронизировано команд: {len(synced)}")

    except Exception as error:
        print(f"Ошибка синхронизации: {error}")
        
# —————————————————————————————————————————————
# ЗАПУСК БОТА
# —————————————————————————————————————————————

if __name__ == "__main__":
    bot.run(TOKEN)
