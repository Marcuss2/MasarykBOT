import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta

from discord import Member, TextChannel, CategoryChannel
from discord.abc import PrivateChannel
from discord.ext import tasks, commands
from discord.ext.commands import has_permissions
from discord.errors import Forbidden, NotFound

log = logging.getLogger(__name__)

def partition(cond, lst):
    return [[i for i in lst if cond(i)], [i for i in lst if not cond(i)]]


class BackupUntilPresent:
    def __init__(self, bot):
        self.bot = bot

    async def backup(self):
        log.info("Starting backup process")
        await self.backup_guilds()

        for guild in self.bot.guilds:
            await self.backup_categories(guild)
            await self.backup_roles(guild)
            await self.backup_members(guild)
            await self.backup_channels(guild)
            await self.backup_messages(guild)

        log.info("Finished backup process")

    async def backup_guilds(self):
        log.info("backing up guilds")
        data = await self.bot.db.guilds.prepare(self.bot.guilds)
        await self.bot.db.guilds.insert(data)

    async def backup_categories(self, guild):
        log.info("backing up categories")
        data = await self.bot.db.categories.prepare(guild.categories)
        await self.bot.db.categories.insert(data)

    async def backup_roles(self, guild):
        log.info("backing up roles")
        data = await self.bot.db.roles.prepare(guild.roles)
        await self.bot.db.roles.insert(data)

    async def backup_members(self, guild):
        log.info("backing up members")
        for i in range(0, len(guild.members), 550):
            chunk = guild.members[i:i+550]
            data = await self.bot.db.members.prepare(chunk)
            await self.bot.db.members.insert(data)

    async def backup_channels(self, guild):
        log.info("backing up channels")
        data = await self.bot.db.channels.prepare(guild.text_channels)
        await self.bot.db.channels.insert(data)

    async def backup_messages(self, guild):
        log.info("backing up messages")
        await self.backup_failed_weeks(guild)
        await self.backup_new_weeks(guild)

    async def backup_failed_weeks(self, guild):
        while _still_failed := await self.backup_failed_week(guild):
            log.debug("finished running failed process, re-checking if everything is fine...")
            await asyncio.sleep(3)

    async def backup_new_weeks(self, guild):
        while _still_behind := await self.backup_new_week(guild):
            log.debug("newer week exists, re-running backup for next week")
            await asyncio.sleep(2)

    async def backup_failed_week(self, guild):
        rows = await self.bot.db.logger.select(guild.id)
        failed_rows, _success_rows = partition(lambda row: row.get("finished_at") is None, rows)

        for failed_row in failed_rows:
            await self.rebackup_failed_week(guild, failed_row)

        return len(failed_rows) != 0

    async def rebackup_failed_week(self, guild, failed_row):
        from_date = failed_row.get("from_date")
        to_date = failed_row.get("to_date")

        for channel in guild.text_channels:
            await self.try_to_backup_messages_in_nonempty_channel(channel, from_date, to_date)

        await self.bot.db.logger.mark_process_finished(guild.id, from_date, to_date, is_first_week=False)

    async def backup_new_week(self, guild):
        finished_process = await self.get_finished_process(guild)
        (from_date, to_date) = self.get_next_week(guild, finished_process)
        if from_date > datetime.now():
            from_date, to_date = datetime.now() - timedelta(weeks=1), datetime.now()
        await self.bot.db.logger.start_process(guild.id, from_date, to_date)

        for channel in guild.text_channels:
            await self.try_to_backup_messages_in_nonempty_channel(channel, from_date, to_date)

        is_first_week = finished_process is None
        await self.bot.db.logger.mark_process_finished(guild.id, from_date, to_date, is_first_week)
        return self.next_week_still_behind_today(to_date)

    async def get_finished_process(self, guild):
        finished_processes = await self.bot.db.logger.select(guild.id)
        if not finished_processes:
            return None
        return max(finished_processes, key=lambda proc: proc.get("finished_at"))

    @staticmethod
    def get_next_week(guild, process):
        if process is None:
            return guild.created_at, guild.created_at + timedelta(weeks=1)
        else:
            return process.get("to_date"), process.get("to_date") + timedelta(weeks=1)

    @staticmethod
    def next_week_still_behind_today(to_date):
        return to_date + timedelta(weeks=1) < datetime.now()

    async def try_to_backup_messages_in_nonempty_channel(self, channel, from_date, to_date):
        if channel.last_message_id is None:
            return

        try:
            await self.backup_messages_in_nonempty_channel(channel, from_date, to_date)
        except Forbidden:
            log.debug("missing permissions to backup messages in %s (%s)", channel, channel.guild)
        except NotFound:
            log.debug("channel %s was not found in (%s)", channel, channel.guild)

    async def backup_messages_in_nonempty_channel(self, channel, from_date, to_date):
        from_date_str = from_date.strftime('%d.%m.%Y')
        to_date_str = to_date.strftime('%d.%m.%Y')
        log.info("backing up messages {%s} - {%s} in %s (%s)", from_date_str, to_date_str, channel, channel.guild)

        collectables = self.get_collectables()
        async for message in channel.history(after=from_date, before=to_date, limit=1_000_000, oldest_first=True):
            for collectable in collectables:
                await collectable.add(message)

        else:
            for collectable in collectables:
                await collectable.db_insert()

    def get_collectables(self):
        return [
            Collectable(
                prepare_fn=self.bot.db.members.prepare_from_message,
                insert_fn=self.bot.db.members.insert
            ),
            Collectable(
                prepare_fn=self.bot.db.messages.prepare,
                insert_fn=self.bot.db.messages.insert
            ),
            Collectable(
                prepare_fn=self.bot.db.attachments.prepare,
                insert_fn=self.bot.db.attachments.insert
            ),
            Collectable(
                prepare_fn=self.bot.db.reactions.prepare,
                insert_fn=self.bot.db.reactions.insert
            ),
            Collectable(
                prepare_fn=self.bot.db.emojis.prepare,
                insert_fn=self.bot.db.emojis.insert
            )
        ]


class BackupOnEvents:
    def __init__(self, bot):
        self.bot = bot

        self.insert_queues = {}
        self.update_queues = {}
        self.delete_queues = {}

        self.task_put_queues_to_database.start()

    def cog_unload(self):
        self.task_put_queues_to_database.cancel()

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        log.info("joined guild %s", guild)
        data = await self.bot.db.guilds.prepare_one(guild)
        await self.bot.db.guilds.insert([data])

    @commands.Cog.listener()
    async def on_guild_update(self, before, after):
        log.info("updated guild from %s to %s", before, after)
        data = await self.bot.db.guilds.prepare_one(after)
        await self.bot.db.guilds.insert([data])

    @commands.Cog.listener()
    async def on_guild_remove(self, guild):
        log.info("left guild %s", guild)
        await self.bot.db.guilds.soft_delete([(guild.id,)])

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        log.info("created channel %s (%s)", channel, channel.guild)

        if isinstance(channel, TextChannel):
            await self.on_textchannel_create(channel)

        elif isinstance(channel, CategoryChannel):
            await self.on_category_create(channel)

    async def on_textchannel_create(self, channel):
        data = await self.bot.db.channels.prepare_one(channel)
        await self.bot.db.channels.insert([data])

    async def on_category_create(self, channel):
        data = await self.bot.db.categories.prepare_one(channel)
        await self.bot.db.categories.insert([data])

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        log.info("updated channel %s (%s)", before, before.guild)

        if isinstance(after, TextChannel):
            await self.on_textchannel_update(before, after)

        elif isinstance(after, CategoryChannel):
            await self.on_category_update(before, after)

    async def on_textchannel_update(self, _before, after):
        data = await self.bot.db.channels.prepare_one(after)
        await self.bot.db.channels.update([data])

    async def on_category_update(self, _before, after):
        data = await self.bot.db.categories.prepare_one(after)
        await self.bot.db.categories.update([data])

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        log.info("deleted channel %s (%s)", channel, channel.guild)

        if isinstance(channel, TextChannel):
            await self.bot.db.channels.soft_delete([(channel.id,)])

        elif isinstance(channel, CategoryChannel):
            await self.bot.db.categories.soft_delete([(channel.id,)])

    @commands.Cog.listener()
    async def on_message(self, message):
        if isinstance(message.channel, PrivateChannel):
            return

        if not isinstance(message.author, Member):
            return

        data = await self.bot.db.messages.prepare_one(message)
        self.insert_queues.setdefault(self.bot.db.messages.insert, deque())
        self.insert_queues[self.bot.db.messages.insert].append(data)

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if isinstance(before.channel, PrivateChannel):
            return

        data = await self.bot.db.messages.prepare_one(after)
        self.update_queues.setdefault(self.bot.db.messages.insert, deque())
        self.update_queues[self.bot.db.messages.insert].append(data)

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if isinstance(message.channel, PrivateChannel):
            return

        self.delete_queues.setdefault(self.bot.db.messages.soft_delete, deque())
        self.delete_queues[self.bot.db.messages.soft_delete].append((message.id,))

    @commands.Cog.listener()
    async def on_member_join(self, member):
        log.info("member %s joined (%s)", member, member.guild)

        data = await self.bot.db.members.prepare_one(member)
        await self.bot.db.members.insert([data])

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if before.avatar_url != after.avatar_url:
            log.info("member %s updated his avatar_url (%s)", before, before.guild)
        elif before.name != after.name:
            log.info("member %s (%s) updated his name to %s (%s)", before, before.nick, after, before.guild)
        elif before.nick != after.nick:
            log.info("member %s (%s) updated his nickname to %s (%s)", before, before.nick, after.nick, before.guild)
        else:
            return

        data = await self.bot.db.members.prepare_one(after)
        self.update_queues.setdefault(self.bot.db.members.insert, deque())
        self.update_queues[self.bot.db.members.insert].append(data)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        log.info("member %s left (%s)", member, member.guild)

        await self.bot.db.members.soft_delete([(member.id,)])

    @commands.Cog.listener()
    async def on_guild_role_create(self, role):
        log.info("added role %s (%s)", role, role.guild)

        data = await self.bot.db.roles.prepare_one(role)
        await self.bot.db.roles.insert([data])

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        log.info("updated role from %s to %s (%s)", before, after, before.guild)

        data = await self.bot.db.roles.prepare_one(after)
        await self.bot.db.roles.insert([data])

    @commands.Cog.listener()
    async def on_guild_role_remove(self, role):
        log.info("removed role %s (%s)", role, role.guild)

        await self.bot.db.roles.soft_delete([(role.id,)])

    @tasks.loop(minutes=5)
    async def task_put_queues_to_database(self):
        try:
            await self.put_queues_to_database(self.insert_queues, limit=1000)
            await self.put_queues_to_database(self.update_queues, limit=2000)
            await self.put_queues_to_database(self.delete_queues, limit=1000)
        except Exception:
            pass

    async def put_queues_to_database(self, queues, *, limit=1000):
        counter = 0

        for (process_fn, queue) in queues.items():
            take_elements = min(limit - counter, len(queue))
            if take_elements == 0:
                return
            log.info("Putting %s from queue to database", take_elements)
            elements = [queue.popleft() for _ in range(take_elements)]
            await process_fn(elements)
            counter += take_elements


class Logger(commands.Cog, BackupUntilPresent, BackupOnEvents):
    def __init__(self, bot):
        self.bot = bot

        BackupUntilPresent.__init__(self, bot)
        BackupOnEvents.__init__(self, bot)

    @commands.Cog.listener()
    async def on_ready(self):
        await self.backup()

    @tasks.loop(hours=168)  # 168 hours == 1 week
    async def _repeat_backup(self):
        await self.backup()

    @commands.command(name="backup")
    @has_permissions(administrator=True)
    async def _backup(self, _ctx):
        await self.backup()


class Collectable:
    def __init__(self, prepare_fn=None, insert_fn=None):
        self.content = []
        self.prepare_fn = prepare_fn
        self.insert_fn = insert_fn

    async def add(self, item):
        self.content.extend(await self.prepare_fn(item))

    async def db_insert(self):
        for i in range(0, len(self.content), 550):
            batch = self.content[i:i+550]
            await self.insert_fn(batch)


def setup(bot):
    bot.add_cog(Logger(bot))
