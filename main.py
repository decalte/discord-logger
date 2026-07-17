import asyncio
import os
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands


TOKEN = os.getenv("TOKEN")


# —————————————————————————————————————————————
# ID КАНАЛОВ ЛОГОВ
# —————————————————————————————————————————————

# Удалённые и изменённые сообщения
MESSAGE_LOG_CHANNEL_ID = 1527284881351118960

# Тайм-ауты
TIMEOUT_LOG_CHANNEL_ID = 1527340102861197423

# Кики
KICK_LOG_CHANNEL_ID = 1527340314912886865

# Баны
BAN_LOG_CHANNEL_ID = 1527340343476093069


# Цвет полоски эмбедов — #2F2F2F
COLOR = discord.Color.from_rgb(47, 47, 47)


# —————————————————————————————————————————————
# INTENTS
# —————————————————————————————————————————————

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.guilds = True
intents.members = True
intents.presences = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)
# —————————————————————————————————————————————
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# —————————————————————————————————————————————

def moscow_time():
    return datetime.now(timezone(timedelta(hours=3)))


def get_message_log_channel(guild: discord.Guild):
    return guild.get_channel(MESSAGE_LOG_CHANNEL_ID)


def get_timeout_log_channel(guild: discord.Guild):
    return guild.get_channel(TIMEOUT_LOG_CHANNEL_ID)


def get_kick_log_channel(guild: discord.Guild):
    return guild.get_channel(KICK_LOG_CHANNEL_ID)


def get_ban_log_channel(guild: discord.Guild):
    return guild.get_channel(BAN_LOG_CHANNEL_ID)


def format_user(user: discord.User | discord.Member):
    return f"{user.mention}\n`{user.id}`"


def format_reason(reason: str | None):
    return reason if reason else "Причина не указана"


async def find_audit_entry(
    guild: discord.Guild,
    action: discord.AuditLogAction,
    target_id: int,
    limit: int = 10
):
    try:
        async for entry in guild.audit_logs(
            limit=limit,
            action=action
        ):
            if entry.target and entry.target.id == target_id:
                entry_time = entry.created_at
                current_time = datetime.now(timezone.utc)

                if (current_time - entry_time).total_seconds() <= 15:
                    return entry

    except discord.Forbidden:
        print(
            f"Нет права «Просмотр журнала аудита» "
            f"на сервере {guild.name}"
        )

    except discord.HTTPException as error:
        print(f"Ошибка при получении журнала аудита: {error}")

    return None


# —————————————————————————————————————————————
# СОБЫТИЕ ЗАПУСКА БОТА
# —————————————————————————————————————————————

@bot.event
async def on_ready():
    await bot.change_presence(status=discord.Status.idle)

    try:
        synced = await bot.tree.sync()
        print(f"Синхронизировано команд: {len(synced)}")

    except discord.HTTPException as error:
        print(f"Ошибка синхронизации команд: {error}")

    print(f"Бот запущен: {bot.user}")
    print(f"ID бота: {bot.user.id}")
    # —————————————————————————————————————————————
# ЛОГИ УДАЛЁННЫХ СООБЩЕНИЙ
# —————————————————————————————————————————————

@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot:
        return

    if not message.guild:
        return

    log_channel = get_message_log_channel(message.guild)

    if not log_channel:
        return

    content = message.content or "Текст отсутствует"

    if len(content) > 1024:
        content = content[:1021] + "..."

    embed = discord.Embed(
        title="Удалённое сообщение",
        color=COLOR,
        timestamp=moscow_time()
    )

    embed.add_field(
        name="Автор",
        value=format_user(message.author),
        inline=True
    )

    embed.add_field(
        name="Канал",
        value=message.channel.mention,
        inline=True
    )

    embed.add_field(
        name="Сообщение",
        value=content,
        inline=False
    )

    if message.attachments:
        attachments = "\n".join(
            attachment.url
            for attachment in message.attachments
        )

        if len(attachments) > 1024:
            attachments = attachments[:1021] + "..."

        embed.add_field(
            name="Вложения",
            value=attachments,
            inline=False
        )

    embed.set_author(
        name=str(message.author),
        icon_url=message.author.display_avatar.url
    )

    embed.set_footer(
        text=f"ID пользователя: {message.author.id}"
    )

    try:
        await log_channel.send(embed=embed)

    except discord.HTTPException as error:
        print(f"Ошибка отправки лога удалённого сообщения: {error}")


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

    if len(old_content) > 1024:
        old_content = old_content[:1021] + "..."

    if len(new_content) > 1024:
        new_content = new_content[:1021] + "..."

    embed = discord.Embed(
        title="Изменённое сообщение",
        color=COLOR,
        timestamp=moscow_time()
    )

    embed.add_field(
        name="Автор",
        value=format_user(before.author),
        inline=True
    )

    embed.add_field(
        name="Канал",
        value=before.channel.mention,
        inline=True
    )

    embed.add_field(
        name="До изменения",
        value=old_content,
        inline=False
    )

    embed.add_field(
        name="После изменения",
        value=new_content,
        inline=False
    )

    embed.add_field(
        name="Переход к сообщению",
        value=f"[Нажмите здесь]({after.jump_url})",
        inline=False
    )

    embed.set_author(
        name=str(before.author),
        icon_url=before.author.display_avatar.url
    )

    embed.set_footer(
        text=f"ID пользователя: {before.author.id}"
    )

    try:
        await log_channel.send(embed=embed)

    except discord.HTTPException as error:
        print(f"Ошибка отправки лога изменённого сообщения: {error}")


# —————————————————————————————————————————————
# ОБРАБОТКА ОБЫЧНЫХ КОМАНД
# —————————————————————————————————————————————

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)# —————————————————————————————————————————————
# ЛОГИ УДАЛЁННЫХ СООБЩЕНИЙ
# —————————————————————————————————————————————

@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot:
        return

    if not message.guild:
        return

    log_channel = get_message_log_channel(message.guild)

    if not log_channel:
        return

    content = message.content or "Текст отсутствует"

    if len(content) > 1024:
        content = content[:1021] + "..."

    embed = discord.Embed(
        title="Удалённое сообщение",
        color=COLOR,
        timestamp=moscow_time()
    )

    embed.add_field(
        name="Автор",
        value=format_user(message.author),
        inline=True
    )

    embed.add_field(
        name="Канал",
        value=message.channel.mention,
        inline=True
    )

    embed.add_field(
        name="Сообщение",
        value=content,
        inline=False
    )

    if message.attachments:
        attachments = "\n".join(
            attachment.url
            for attachment in message.attachments
        )

        if len(attachments) > 1024:
            attachments = attachments[:1021] + "..."

        embed.add_field(
            name="Вложения",
            value=attachments,
            inline=False
        )

    embed.set_author(
        name=str(message.author),
        icon_url=message.author.display_avatar.url
    )

    embed.set_footer(
        text=f"ID пользователя: {message.author.id}"
    )

    try:
        await log_channel.send(embed=embed)

    except discord.HTTPException as error:
        print(f"Ошибка отправки лога удалённого сообщения: {error}")


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

    if len(old_content) > 1024:
        old_content = old_content[:1021] + "..."

    if len(new_content) > 1024:
        new_content = new_content[:1021] + "..."

    embed = discord.Embed(
        title="Изменённое сообщение",
        color=COLOR,
        timestamp=moscow_time()
    )

    embed.add_field(
        name="Автор",
        value=format_user(before.author),
        inline=True
    )

    embed.add_field(
        name="Канал",
        value=before.channel.mention,
        inline=True
    )

    embed.add_field(
        name="До изменения",
        value=old_content,
        inline=False
    )

    embed.add_field(
        name="После изменения",
        value=new_content,
        inline=False
    )

    embed.add_field(
        name="Переход к сообщению",
        value=f"[Нажмите здесь]({after.jump_url})",
        inline=False
    )

    embed.set_author(
        name=str(before.author),
        icon_url=before.author.display_avatar.url
    )

    embed.set_footer(
        text=f"ID пользователя: {before.author.id}"
    )

    try:
        await log_channel.send(embed=embed)

    except discord.HTTPException as error:
        print(f"Ошибка отправки лога изменённого сообщения: {error}")


# —————————————————————————————————————————————
# ОБРАБОТКА ОБЫЧНЫХ КОМАНД
# —————————————————————————————————————————————

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)
    # —————————————————————————————————————————————
# ЛОГИ ВЫДАЧИ ТАЙМ-АУТА
# —————————————————————————————————————————————

@bot.event
async def on_member_update(
    before: discord.Member,
    after: discord.Member
):
    before_timeout = before.timed_out_until
    after_timeout = after.timed_out_until

    if before_timeout == after_timeout:
        return

    # Логируем только выдачу тайм-аута
    if after_timeout is None:
        return

    log_channel = get_timeout_log_channel(after.guild)

    if not log_channel:
        return

    await asyncio.sleep(1)

    audit_entry = await find_audit_entry(
        guild=after.guild,
        action=discord.AuditLogAction.member_update,
        target_id=after.id
    )

    moderator = None
    reason = None

    if audit_entry:
        moderator = audit_entry.user
        reason = audit_entry.reason

    moderator_value = (
        format_user(moderator)
        if moderator
        else "Не удалось определить"
    )

    timeout_until = after_timeout.astimezone(
        timezone(timedelta(hours=3))
    )

    timeout_until_text = discord.utils.format_dt(
        timeout_until,
        style="F"
    )

    embed = discord.Embed(
        title="Выдача тайм-аута",
        color=COLOR,
        timestamp=moscow_time()
    )

    embed.add_field(
        name="Выдал",
        value=moderator_value,
        inline=True
    )

    embed.add_field(
        name="Пользователю",
        value=format_user(after),
        inline=True
    )

    embed.add_field(
        name="Причина",
        value=format_reason(reason),
        inline=False
    )

    embed.add_field(
        name="До",
        value=timeout_until_text,
        inline=False
    )

    embed.set_thumbnail(
        url=after.display_avatar.url
    )

    embed.set_footer(
        text=f"ID пользователя: {after.id}"
    )

    try:
        await log_channel.send(embed=embed)

    except discord.HTTPException as error:
        print(f"Ошибка отправки лога тайм-аута: {error}")
        # —————————————————————————————————————————————
# ЛОГИ КИКОВ
# —————————————————————————————————————————————

@bot.event
async def on_member_remove(member: discord.Member):
    log_channel = get_kick_log_channel(member.guild)

    if not log_channel:
        return

    await asyncio.sleep(1)

    audit_entry = await find_audit_entry(
        guild=member.guild,
        action=discord.AuditLogAction.kick,
        target_id=member.id
    )

    # Если записи о кике нет, значит пользователь, скорее всего,
    # вышел с сервера самостоятельно.
    if not audit_entry:
        return

    moderator = audit_entry.user
    reason = audit_entry.reason

    embed = discord.Embed(
        title="Выгнан пользователь",
        color=COLOR,
        timestamp=moscow_time()
    )

    embed.add_field(
        name="Выгнал",
        value=format_user(moderator),
        inline=True
    )

    embed.add_field(
        name="Пользователя",
        value=format_user(member),
        inline=True
    )

    embed.add_field(
        name="Причина",
        value=format_reason(reason),
        inline=False
    )

    embed.set_thumbnail(
        url=member.display_avatar.url
    )

    embed.set_footer(
        text=f"ID пользователя: {member.id}"
    )

    try:
        await log_channel.send(embed=embed)

    except discord.HTTPException as error:
        print(f"Ошибка отправки лога кика: {error}")
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

    audit_entry = await find_audit_entry(
        guild=guild,
        action=discord.AuditLogAction.ban,
        target_id=user.id
    )

    moderator = None
    reason = None

    if audit_entry:
        moderator = audit_entry.user
        reason = audit_entry.reason

    moderator_value = (
        format_user(moderator)
        if moderator
        else "Не удалось определить"
    )

    embed = discord.Embed(
        title="Выдача бана",
        color=COLOR,
        timestamp=moscow_time()
    )

    embed.add_field(
        name="Выдал",
        value=moderator_value,
        inline=True
    )

    embed.add_field(
        name="Пользователю",
        value=format_user(user),
        inline=True
    )

    embed.add_field(
        name="Причина",
        value=format_reason(reason),
        inline=False
    )

    embed.add_field(
        name="До",
        value="Навсегда",
        inline=False
    )

    embed.set_thumbnail(
        url=user.display_avatar.url
    )

    embed.set_footer(
        text=f"ID пользователя: {user.id}"
    )

    try:
        await log_channel.send(embed=embed)

    except discord.HTTPException as error:
        print(f"Ошибка отправки лога бана: {error}")
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
        title=f"Аватар {user}",
        color=COLOR,
        timestamp=moscow_time()
    )

    embed.set_image(url=user.display_avatar.url)

    embed.set_footer(
        text=f"ID пользователя: {user.id}"
    )

    await interaction.response.send_message(
        embed=embed
    )


# —————————————————————————————————————————————
# ЗАПУСК БОТА
# —————————————————————————————————————————————

if __name__ == "__main__":
    bot.run(TOKEN)
