import re
import logging
from typing import Union

from discord import Emoji, PartialEmoji, PermissionOverwrite, Member
from discord.ext import commands
from discord.utils import get, find
from discord.errors import HTTPException

from .utils import constants


log = logging.getLogger(__name__)


class UnicodeEmoji(commands.Converter):
    """
    discord.py's way of handling emojis is [Emoji, PartialEmoji, str]
    UnicodeEmoji accepts str only if it is present
    in UNICODE_EMOJI list
    """
    async def convert(self, _ctx, argument):
        from emoji import UNICODE_EMOJI

        if isinstance(argument, list):
            for arg in argument:
                if arg not in UNICODE_EMOJI:
                    raise commands.BadArgument('Emoji "{}" not found'.format(arg))
        else:
            if argument not in UNICODE_EMOJI:
                raise commands.BadArgument('Emoji "{}" not found'.format(argument))

        return argument


Emote = Union[Emoji, PartialEmoji, UnicodeEmoji]


class Rolemenu(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        await self.on_raw_reaction_update(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        await self.on_raw_reaction_update(payload)

    async def on_raw_reaction_update(self, payload):
        if payload.channel_id not in constants.about_you_channels:
            return

        guild = self.bot.get_guild(payload.guild_id)
        channel = get(guild.text_channels, id=payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        author = guild.get_member(payload.user_id)

        if author == self.bot.user:
            return

        row = find(lambda row: row.startswith(str(payload.emoji)), message.content.split('\n'))
        try:
            desc = row.split(" ", 1)[1]
        except ValueError:
            return

        if payload.event_type == "REACTION_ADD":
            await self.reaction_add(guild, author, desc)
        else:
            await self.reaction_remove(guild, author, desc)

    async def reaction_add(self, guild, author, desc):
        if role := self.is_role(guild, desc):
            await author.add_roles(role)
            log.info("added role %s to %s", str(role), author)
            return

        if channel := self.is_channel(guild, desc):
            await channel.set_permissions(author,
                                          overwrite=PermissionOverwrite(read_messages=True))
            log.info("shown channel %s to %s", str(channel), author)
            return

    async def reaction_remove(self, guild, author, desc):
        if role := self.is_role(guild, desc):
            await author.remove_roles(role)
            log.info("removed role %s to %s", str(role), author)
            return

        if channel := self.is_channel(guild, desc):
            await channel.set_permissions(author, overwrite=None)
            log.info("hidden channel %s to %s", str(channel), author)
            return

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.channel.id not in constants.about_you_channels:
            return

        for row in message.content.split("\n"):
            emoji = row.strip().split(" ", 1)[0]
            try:
                await message.add_reaction(emoji)
            except HTTPException:
                continue

    @staticmethod
    def is_role(guild, string):
        if not (match := re.match(r"<@&(\d+)>", string)):
            return None
        role_id = int(match.group(1))
        return get(guild.roles, id=role_id)

    @staticmethod
    def is_channel(guild, string):
        if not (match := re.match(r"<#(\d+)>", string)):
            return None
        channel_id = int(match.group(1))
        return get(guild.channels, id=channel_id)

    def get_emoji(self, string):
        emoji_id = re.match(r"<:.*:(\d+)>", string).group(1)
        return get(self.bot.emojis, id=int(emoji_id))

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        if payload.channel_id not in constants.about_you_channels:
            return

        channel = self.bot.get_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)

        seen_emojis = set()
        for row in payload.data['content'].split("\n"):
            emoji = row.strip().split(" ", 1)[0]
            seen_emojis.add(emoji)
            try:
                await message.add_reaction(emoji)
            except HTTPException:
                continue

        for reaction in message.reactions:
            if str(reaction.emoji) not in seen_emojis:
                await message.clear_reaction(reaction.emoji)


    @commands.Cog.listener()
    async def on_ready(self):
        for channel_id in constants.about_you_channels:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                continue

            async for message in channel.history():
                if not message.reactions:
                    continue

                for row in message.content.split("\n"):
                    try:
                        desc = row.split(" ", 1)[1]
                    except ValueError:
                        continue

                    if role := self.is_role(message.guild, desc):
                        await self.balance_role(message, role)

                    if channel := self.is_channel(message.guild, desc):
                        await self.balance_channel(message, channel)

    async def balance_role(self, message, role):
        async for user in message.reactions[0].users():
            has_role = get(user.roles, id=role.id)
            if not has_role:
                log.info("added role %s to %s", str(role), user)
                await user.add_roles(role)

        for user in role.members:
            has_reacted = await message.reactions[0].users().get(id=user.id)
            if not has_reacted:
                log.info("removed role %s to %s", str(role), user)
                await user.remove_roles(role)

    async def balance_channel(self, message, channel):
        async for user in message.reactions[0].users():
            if not channel.permissions_for(user).read_messages:
                log.info("showing channel %s to %s", str(channel), user)
                await channel.set_permissions(user,
                                              overwrite=PermissionOverwrite(read_messages=True))

        visible_to = {user: overwrite
                      for (user, overwrite) in channel.overwrites.items()
                      if overwrite.read_messages and isinstance(user, Member) and not user.bot}
        for user in visible_to:
            has_reacted = await message.reactions[0].users().get(id=user.id)
            if not has_reacted:
                log.info("hide channel %s to %s", str(channel), user)
                await channel.set_permissions(user,
                                              overwrite=None)

def setup(bot):
    bot.add_cog(Rolemenu(bot))
