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
LEAVE_LOG_CHANNEL_ID = 1527694442998403172
ANTICRASH_LOG_CHANNEL_ID = 1527478400728694865
CHANNEL_LOG_CHANNEL_ID = 1527681524416123020


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
intents.moderation = True
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


def format_english_datetime(value: datetime | None = None):
    """Формат: July 17, 2026 at 7:17 PM."""
    if value is None:
        value = moscow_time()

    return value.astimezone(
        timezone(timedelta(hours=3))
    ).strftime(
        "%B %d, %Y at %I:%M %p"
    ).replace(" 0", " ")


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

    # Тайм-аут снят.
    if after.timed_out_until is None:
        embed = discord.Embed(
            title="Снятие тайм-аута",
            color=COLOR,
            timestamp=moscow_time()
        )

        embed.add_field(
            name="Снял(а)",
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

        try:
            await log_channel.send(embed=embed)
        except discord.HTTPException as error:
            print(f"Ошибка отправки лога снятия тайм-аута: {error}")
        return

    # Тайм-аут выдан или изменён. Формат этого лога оставлен прежним.
    until = after.timed_out_until.astimezone().strftime(
        "%B %d, %Y at %I:%M %p"
    ).replace(" 0", " ")

    embed = discord.Embed(
        title="Выдача тайм-аута",
        color=COLOR,
        timestamp=moscow_time()
    )

    embed.add_field(
        name="Выдал(а)",
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

    try:
        await log_channel.send(embed=embed)
    except discord.HTTPException as error:
        print(f"Ошибка отправки лога тайм-аута: {error}")

#—————————————————————————————————————————————
# ЛОГИ КИКОВ И ВЫХОДОВ
# —————————————————————————————————————————————

@bot.event
async def on_member_remove(member: discord.Member):
    # Discord использует одно событие и для кика, и для добровольного выхода.
    # Поэтому сначала проверяем свежую запись о кике в журнале аудита.
    await asyncio.sleep(1)

    audit = await find_audit_entry(
        guild=member.guild,
        action=discord.AuditLogAction.kick,
        target_id=member.id
    )

    if audit:
        log_channel = get_kick_log_channel(member.guild)
        if not log_channel:
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
            name="Выгнал(а)",
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
            print(f"Ошибка отправки лога кика: {error}")
        return

    # Если свежей записи о кике нет — пользователь вышел сам.
    log_channel = member.guild.get_channel(LEAVE_LOG_CHANNEL_ID)
    if not log_channel:
        return

    embed = discord.Embed(
        title="Выход с сервера",
        color=COLOR,
        timestamp=moscow_time()
    )
    embed.add_field(
        name="Пользователь",
        value=(
            f"{member.mention}\n"
            f"ID: `{member.id}`"
        ),
        inline=False
    )
    embed.add_field(
        name="Дата и время выхода",
        value=f"> {format_english_datetime()}",
        inline=False
    )

    try:
        await log_channel.send(embed=embed)
    except discord.HTTPException as error:
        print(f"Ошибка отправки лога выхода: {error}")

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
        name="Выдал(а)",
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
# ЛОГИ СНЯТИЯ БАНА
# —————————————————————————————————————————————

@bot.event
async def on_member_unban(
    guild: discord.Guild,
    user: discord.User
):
    log_channel = get_ban_log_channel(guild)

    if not log_channel:
        return

    await asyncio.sleep(1)

    audit = await find_audit_entry(
        guild=guild,
        action=discord.AuditLogAction.unban,
        target_id=user.id
    )

    if audit:
        moderator = (
            f"{audit.user.mention}\n"
            f"ID: `{audit.user.id}`"
        )
    else:
        moderator = "Не удалось определить"

    embed = discord.Embed(
        title="Снятие бана",
        color=COLOR,
        timestamp=moscow_time()
    )

    embed.add_field(
        name="Снял(а)",
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

    try:
        await log_channel.send(embed=embed)
    except discord.HTTPException as error:
        print(f"Ошибка отправки лога снятия бана: {error}")


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
        color=COLOR
    )


    embed.set_image(
        url=user.display_avatar.url
    )


    await interaction.response.send_message(
        embed=embed
    )

# —————————————————————————————————————————————
# ЛОГИ СОЗДАНИЯ И УДАЛЕНИЯ КАНАЛОВ
# —————————————————————————————————————————————


@bot.event
async def on_guild_channel_create(channel):

    log_channel = bot.get_channel(CHANNEL_LOG_CHANNEL_ID)

    if log_channel is None:
        return


    creator = "Неизвестно"


    async for entry in channel.guild.audit_logs(
        limit=5,
        action=discord.AuditLogAction.channel_create
    ):
        if entry.target.id == channel.id:
            creator = entry.user.mention
            break



    if isinstance(channel, discord.TextChannel):
        title = "Создан текстовый канал"
        name_title = "Название канала"

    elif isinstance(channel, discord.VoiceChannel):
        title = "Создан голосовой канал"
        name_title = "Название канала"

    elif isinstance(channel, discord.CategoryChannel):
        title = "Создана категория для каналов"
        name_title = "Название категории"

    else:
        title = "Создан канал"
        name_title = "Название канала"



    permissions = []


    for role in channel.guild.roles:

        overwrite = channel.overwrites_for(role)

        if overwrite.view_channel is True:
            permissions.append(role.mention)



    if not permissions:
        permissions.append("@everyone")



    perms_text = "\n".join(
        f"> {role}"
        for role in permissions
    )



    embed = discord.Embed(
        title=title,
        color=COLOR
    )


    embed.add_field(
        name="Создал(а)",
        value=f"{creator}\nID: `{channel.id}`",
        inline=False
    )


    embed.add_field(
        name=name_title,
        value=f"> {channel.name}",
        inline=False
    )


    if not isinstance(channel, discord.CategoryChannel):

        embed.add_field(
            name="Права канала",
            value=perms_text,
            inline=False
        )


    embed.add_field(
        name="Дата и время создания",
        value=f"> {format_english_datetime(channel.created_at)}",
        inline=False
    )


    await log_channel.send(
        embed=embed
    )





@bot.event
async def on_guild_channel_delete(channel):

    log_channel = bot.get_channel(CHANNEL_LOG_CHANNEL_ID)

    if log_channel is None:
        return



    deleter = "Неизвестно"



    async for entry in channel.guild.audit_logs(
        limit=5,
        action=discord.AuditLogAction.channel_delete
    ):
        if entry.target.id == channel.id:
            deleter = entry.user.mention
            break




    if isinstance(channel, discord.TextChannel):
        title = "Удален текстовый канал"
        name_title = "Название канала"

    elif isinstance(channel, discord.VoiceChannel):
        title = "Удален голосовой канал"
        name_title = "Название канала"

    elif isinstance(channel, discord.CategoryChannel):
        title = "Удалена категория для каналов"
        name_title = "Название категории"

    else:
        title = "Удален канал"
        name_title = "Название канала"




    permissions = []


    for role, overwrite in channel.overwrites.items():

        if overwrite.view_channel is True:
            permissions.append(role.mention)



    if not permissions:
        permissions.append("@everyone")



    perms_text = "\n".join(
        f"> {role}"
        for role in permissions
    )



    embed = discord.Embed(
        title=title,
        color=COLOR
    )



    embed.add_field(
        name="Удалил(а)",
        value=f"{deleter}\nID: `{channel.id}`",
        inline=False
    )



    embed.add_field(
        name=name_title,
        value=f"> {channel.name}",
        inline=False
    )



    if not isinstance(channel, discord.CategoryChannel):

        embed.add_field(
            name="Права канала",
            value=perms_text,
            inline=False
        )



    embed.add_field(
        name="Дата и время удаления",
        value=f"> {format_english_datetime()}",
        inline=False
    )



    await log_channel.send(
        embed=embed
    )


# —————————————————————————————————————————————
# АНТИКРАШ
# —————————————————————————————————————————————

ANTI_CRASH_ROLE_ID = 1527476785590177903


anti_crash_roles = {}

anti_crash_actions = {}



async def activate_antichrash(member, reason):

    if member.id in anti_crash_roles:
        return


    # сохраняем роли
    anti_crash_roles[member.id] = [
        role.id
        for role in member.roles
        if role != member.guild.default_role
    ]


    # снимаем все роли
    await member.edit(
        roles=[]
    )


    # выдаём роль антикраша
    anti_role = member.guild.get_role(
        ANTI_CRASH_ROLE_ID
    )

    if anti_role:
        await member.add_roles(
            anti_role
        )



    log_channel = bot.get_channel(
        ANTICRASH_LOG_CHANNEL_ID
    )


    if log_channel:

        embed = discord.Embed(
            title="Выдача антикраша",
            color=COLOR
        )


        embed.add_field(
            name="Администратору",
            value=(
                f"{member.mention}\n"
                f"ID: `{str(member.id)}`"
            ),
            inline=False
        )


        embed.add_field(
            name="Причина выдачи",
            value=f"> {reason}",
            inline=False
        )


        embed.add_field(
            name="Дата и время выдачи антикраша",
            value=f"> {format_english_datetime()}",
            inline=False
        )


        await log_channel.send(
            embed=embed,
            view=AntiCrashView(member.id)
        )





# —————————————————————————————————————————————
# КНОПКА СНЯТИЯ АНТИКРАША
# —————————————————————————————————————————————


class AntiCrashView(discord.ui.View):

    def __init__(self, member_id):

        super().__init__(
            timeout=None
        )

        self.member_id = member_id



    @discord.ui.button(
        label="Снять антикраш",
        style=discord.ButtonStyle.secondary,
        custom_id="remove_antichrash"
    )
    async def remove_antichrash(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):


        member = interaction.guild.get_member(
            self.member_id
        )


        if member is None:

            await interaction.response.send_message(
                "Пользователь не найден",
                ephemeral=True
            )

            return



        anti_role = interaction.guild.get_role(
            ANTI_CRASH_ROLE_ID
        )


        if anti_role:

            await member.remove_roles(
                anti_role
            )



        # возвращаем старые роли
        roles = anti_crash_roles.get(
            member.id,
            []
        )


        for role_id in roles:

            role = interaction.guild.get_role(
                role_id
            )

            if role:

                await member.add_roles(
                    role
                )



        restored_roles = []

        for role_id in roles:
            role = interaction.guild.get_role(role_id)
            if role:
                restored_roles.append(role.mention)

        anti_crash_roles.pop(
            member.id,
            None
        )

        roles_text = (
            "\n".join(f"> {role}" for role in restored_roles)
            if restored_roles
            else "> Роли отсутствуют"
        )

        embed = discord.Embed(
            title="Снятие антикраша",
            color=COLOR
        )

        embed.add_field(
            name="Снял(а)",
            value=(
                f"{interaction.user.mention}\n"
                f"ID: `{interaction.user.id}`"
            ),
            inline=False
        )

        embed.add_field(
            name="Администратору",
            value=(
                f"{member.mention}\n"
                f"ID: `{member.id}`"
            ),
            inline=False
        )

        embed.add_field(
            name="Возвращенные роли",
            value=roles_text,
            inline=False
        )

        embed.add_field(
            name="Дата и время снятия антикраша",
            value=f"> {format_english_datetime()}",
            inline=False
        )

        await interaction.response.send_message(
            embed=embed
        )





# —————————————————————————————————————————————
# БАНЫ ДЛЯ АНТИКРАША
# —————————————————————————————————————————————


@bot.listen("on_member_ban")
async def antichrash_ban_check(
    guild: discord.Guild,
    user: discord.User
):
    entry = await find_audit_entry(
        guild=guild,
        action=discord.AuditLogAction.ban,
        target_id=user.id
    )

    if not entry or not entry.user:
        return

    admin = entry.user


    anti_crash_actions.setdefault(
        admin.id,
        []
    )


    anti_crash_actions[admin.id].append(
        "ban"
    )


    if anti_crash_actions[admin.id].count(
        "ban"
    ) >= 2:


        await activate_antichrash(
            admin,
            "выдача 2-х банов подряд"
        )






# —————————————————————————————————————————————
# КИКИ ДЛЯ АНТИКРАША
# —————————————————————————————————————————————


@bot.listen("on_member_remove")
async def antichrash_kick_check(
    member: discord.Member
):
    await asyncio.sleep(1)

    entry = await find_audit_entry(
        guild=member.guild,
        action=discord.AuditLogAction.kick,
        target_id=member.id
    )

    if not entry or not entry.user:
        return

    admin = entry.user


    anti_crash_actions.setdefault(
        admin.id,
        []
    )


    anti_crash_actions[admin.id].append(
        "kick"
    )


    if anti_crash_actions[admin.id].count(
        "kick"
    ) >= 2:


        await activate_antichrash(
            admin,
            "выгнал 2-х пользователей с сервера подряд"
        )






# —————————————————————————————————————————————
# ТАЙМ-АУТЫ ДЛЯ АНТИКРАША
# —————————————————————————————————————————————

@bot.listen("on_member_update")
async def antichrash_timeout_check(
    before: discord.Member,
    after: discord.Member
):

    if before.timed_out_until == after.timed_out_until:
        return


    if after.timed_out_until is None:
        return



    await asyncio.sleep(1)



    audit = await find_audit_entry(
        guild=after.guild,
        action=discord.AuditLogAction.member_update,
        target_id=after.id
    )


    if audit is None:
        return



    admin = audit.user



    if admin.bot:
        return



    anti_crash_actions.setdefault(
        admin.id,
        []
    )


    anti_crash_actions[admin.id].append(
        "timeout"
    )



    if anti_crash_actions[admin.id].count(
        "timeout"
    ) >= 3:


        await activate_antichrash(
            admin,
            "выдача 3-х тайм-аутов подряд"
        )


        anti_crash_actions[admin.id] = []

@bot.event
async def on_ready():

    await bot.change_presence(
        status=discord.Status.idle
    )

    print(f"Бот запущен: {bot.user}")

# —————————————————————————————————————————————
# ЗАПУСК БОТА
# —————————————————————————————————————————————

if __name__ == "__main__":
    bot.run(TOKEN)

