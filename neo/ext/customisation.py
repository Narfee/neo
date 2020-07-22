"""
neo Discord bot
Copyright (C) 2020 nickofolas

neo is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published
by the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

neo is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with neo.  If not, see <https://www.gnu.org/licenses/>.
"""
import re
import string
import asyncio
from random import Random
from datetime import datetime
from time import time
from typing import Union
from textwrap import indent, shorten
from contextlib import suppress

import discord
from discord.ext import commands, flags
from humanize import naturaltime, naturaldate
from yarl import URL

import neo
from neo.utils.checks import check_member_in_guild, is_owner_or_administrator
from neo.utils.formatters import prettify_text
from neo.utils.converters import BoolConverter, TimeConverter


class Reminder:
    def __init__(self, *, user, bot, content, deadline, conn_pool, rm_id, jump_origin):
        self.user = user
        self.content = content
        self.deadline = deadline
        self.conn_pool = conn_pool
        self.rm_id = rm_id
        self.jump_origin = jump_origin
        self.bot = bot
        self.task = bot.loop.create_task(self._do_wait(), name=f"REMINDER-{self.rm_id}")

    def __repr__(self):
        attrs = ' '.join(f"{k}={v!r}" for k, v in self.__dict__.items())
        return f"<{self.__class__.__name__} {attrs}>"

    async def _do_wait(self):
        await discord.utils.sleep_until(self.deadline)
        await self._do_remind()

    async def _do_remind(self):
        target = self.bot.get_channel(int(list(URL(self.jump_origin).parts)[3])) or self.user
        if self.bot.user_cache[self.user.id]['dm_reminders'] is True:
            target = self.user
        send_content = f"**{self.user.mention} - <{self.jump_origin}>**\n" + indent(self.content, '> ')
        await target.send(send_content, allowed_mentions=discord.AllowedMentions(users=[self.user]))
        await self.conn_pool.execute('DELETE FROM reminders WHERE id=$1 and user_id=$2', self.rm_id, self.user.id)

class Customisation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.max_highlights = 10
        self.pending_reminders = list()
        bot.loop.create_task(self._create_first_reminders())

    @commands.command(name='settings')
    async def user_settings(self, ctx, setting_name=None, *, new_setting: Union[BoolConverter, str] = None):
        """View and edit boolean user settings"""
        if setting_name is not None and new_setting is not None:
            keys = self.bot.user_cache.get(ctx.author.id).keys()
            if setting_name not in keys:
                raise commands.CommandError(f"New setting must be one of {', '.join(keys)}")
            async with ctx.loading():
               await self.bot.conn.execute(f"UPDATE user_data SET {setting_name}=$1 WHERE user_id=$2", new_setting,
                                           ctx.author.id)
            await self.bot.user_cache.refresh()
            return
        embed = neo.Embed(title=f"""{ctx.author}'s Settings""")
        readable_settings = list()
        for k, v in self.bot.user_cache[ctx.author.id].items():
            if isinstance(v, bool):
                readable_settings.append(f'{ctx.toggle(v)} **{discord.utils.escape_markdown(k)}**')
            elif isinstance(v, (list, str)) or v is None:
                continue
            else:
                readable_settings.append(f'**{discord.utils.escape_markdown(k)}** `{v}`')
        embed.description = '\n'.join(readable_settings)
        await ctx.send(embed=embed.set_thumbnail(url=ctx.author.avatar_url_as(static_format='png')))

    @commands.command(name='config', aliases=['cfg'])
    @commands.guild_only()
    @is_owner_or_administrator()
    async def _guild_config(self, ctx, setting_name=None, *, new_setting: Union[BoolConverter, str] = None):
        """View and edit boolean guild configuration options"""
        if setting_name is not None and new_setting is not None:
            keys = self.bot.guild_cache.get(ctx.guild.id).keys()
            if setting_name not in keys:
                raise commands.CommandError(f"New setting must be one of {', '.join(keys)}")
            async with ctx.loading():
               await self.bot.conn.execute(f"UPDATE guild_prefs SET {setting_name}=$1 WHERE guild_id=$2", new_setting,
                                           ctx.guild.id)
            await self.bot.guild_cache.refresh()
            return
        embed = neo.Embed(title=f"""{ctx.guild}'s Settings""")
        readable_settings = list()
        for k, v in self.bot.guild_cache[ctx.guild.id].items():
            if isinstance(v, bool):
                readable_settings.append(f'{ctx.toggle(v)} **{discord.utils.escape_markdown(k)}**')
            elif isinstance(v, (list, str)) or v is None:
                continue
            else:
                readable_settings.append(f'**{discord.utils.escape_markdown(k)}** `{v}`')
        embed.description = '\n'.join(readable_settings)
        await ctx.send(embed=embed.set_thumbnail(url=ctx.guild.icon_url_as(static_format='png')))

    @commands.group(name='prefix', invoke_without_command=True)
    async def _prefix(self, ctx):
        """Invoked by itself, it will show base prefixes, and custom prefixes for the current guild
        Its subcommands delve into customisation of said prefixes"""
        embed = neo.Embed()
        guild_data = None
        always_active = [ctx.me.mention]
        if ctx.guild:
            guild_data = self.bot.guild_cache[ctx.guild.id]
        else: # TODO: make default prefix a config value or something
            always_active.append('`n/`')
        if guild_data:
            embed.add_field(name=f'{ctx.guild}\'s prefixes',
                            value=' | '.join(map(lambda pfx: f'`{pfx}`', guild_data['prefixes'])),
                            inline=False)
        embed.description = f"**Base prefixes**\n{' | '.join(always_active)}"
        await ctx.send(embed=embed)

    @_prefix.command(name='add', aliases=['remove'])
    @is_owner_or_administrator()
    async def _modify_guild_prefixes(self, ctx, *, prefix):
        """Use `add`|`remove` aliases respectively to edit the list of guild prefixes."""
        current_prefixes = set(self.bot.guild_cache[ctx.guild.id]['prefixes'])
        strategy_map = {'add': current_prefixes.add, 'remove': current_prefixes.discard}
        async with ctx.loading():
            strat = strategy_map[ctx.subcommand_passed]
            if strat == current_prefixes.discard and len(current_prefixes) == 1:
                raise commands.CommandError('A guild must always have at least one prefix')
            strat(prefix)
            await self.bot.conn.execute(
                'UPDATE guild_prefs SET prefixes=$1 WHERE guild_id=$2',
                current_prefixes, ctx.guild.id)
            await self.bot.guild_cache.refresh()

    @commands.group(aliases=['hl'], invoke_without_command=True, ignore_extra=False)
    async def highlight(self, ctx):
        """
        Base command for keyword highlights. Run with no arguments to list your active highlights.
        """
        def format_hl(valtup):
            index, hl = valtup
            kw_full = hl.kw[:175] + ' ...' if len(hl.kw) > 175 else hl.kw
            if hl.is_regex:
                return f"`{index}` <:regex:735370786294202480> `{kw_full}`"
            return f"`{index}` `{kw_full}`"
        my_hl = list(filter(lambda hl: hl.user_id == ctx.author.id, self.bot.get_cog('HlMon').cache))
        await ctx.send(embed=neo.Embed(
            description='\n'.join(map(format_hl, enumerate(my_hl, 1)))).set_footer(
            text=f'{len(my_hl)}/10 slots used').set_author(
            name=f"{ctx.author}'s highlights", icon_url=ctx.author.avatar_url_as(static_format='png')),
                       delete_after=15.0)

    # BEGIN TODOS GROUP ~

    @commands.group(name='todo', invoke_without_command=True)
    async def todo_rw(self, ctx):
        """
        Base todo command, run with no arguments to see a list of all your active todos
        """
        todo_list = []
        fetched = [(rec['content'], rec['jump_url']) for rec in
                   await self.bot.conn.fetch("SELECT content, jump_url from todo WHERE user_id=$1 ORDER BY created_at ASC",
                                             ctx.author.id)]
        for count, value in enumerate(fetched, 1):
            todo_list.append(f'[`{count}`]({value[1]}) {value[0]}')
        if not todo_list:
            todo_list = 'No todos'
        await ctx.quick_menu(
            todo_list, 10,
            template=neo.Embed().set_author(
                    name=f"{ctx.author}'s todos ({len(todo_list) if isinstance(todo_list, list) else 0:,} items)",
                    icon_url=ctx.author.avatar_url_as(static_format='png')),
            delete_message_after=True)

    @todo_rw.command(name='add')
    async def create_todo(self, ctx, *, content: str):
        """
        Add an item to your todo list
        """
        await self.bot.conn.execute('INSERT INTO todo (user_id, content, jump_url, created_at) VALUES ($1, $2, $3, $4)',
            ctx.author.id, content, ctx.message.jump_url, datetime.utcnow())
        await ctx.message.add_reaction(ctx.tick(True))

    @todo_rw.command(name='remove', aliases=['rm', 'delete', 'del', 'yeet'])
    async def remove_todo(self, ctx, todo_index: commands.Greedy[int]):
        """
        Remove one, or multiple todos by index
        """
        if not todo_index:
            raise commands.CommandError('Use the index of a todo (found in your list of todos) to remove it')
        fetched = [rec['content'] for rec in
                   await self.bot.conn.fetch("SELECT content from todo WHERE user_id=$1 ORDER BY created_at ASC", ctx.author.id)]
        for num in todo_index:
            await self.bot.conn.execute('DELETE FROM todo WHERE user_id=$1 AND content=$2',
                                        ctx.author.id, fetched[num - 1])
        await ctx.message.add_reaction(ctx.tick(True))

    @todo_rw.command(name='clear')
    async def clear_todos(self, ctx):
        """
        Completely wipe your list of todos
        """
        conf = await ctx.prompt('Are you sure you want to clear all todos?')
        if conf:
            await self.bot.conn.execute('DELETE FROM todo WHERE user_id=$1', ctx.author.id)

    # END TODOS GROUP ~

    @commands.group(name='remind', invoke_without_command=True)
    @commands.max_concurrency(1, commands.BucketType.default)
    async def _remind(self, ctx, *, reminder: TimeConverter):
        """Add a new reminder. The first time/date found will be the one used."""
        reminder_id = int(str(int(time()))[4:])
        await self.bot.conn.execute(
            "INSERT INTO reminders (user_id, content, deadline, id, origin_jump) VALUES ($1, $2, $3, $4, $5)",
            ctx.author.id, reminder.string, reminder.time, reminder_id, ctx.message.jump_url)
        with suppress(UnboundLocalError):
            Reminder(user=ctx.author, content=reminder.string, deadline=reminder.time, 
                     bot=self.bot, conn_pool=self.bot.conn, rm_id=reminder_id, jump_origin=ctx.message.jump_url)
        pretty_time = reminder.time.strftime('%a, %b %d, %Y at %H:%M:%S')
        await ctx.send(f"{ctx.tick(True)} Reminder set for {pretty_time}")

    @_remind.command(name='list')
    async def _remind_list(self, ctx):
        """View your list of pending reminders"""
        def format_reminder(reminder):
            time = reminder['deadline'].strftime(f"%a, %b %d, %Y at %H:%M:%S %Z ({naturaltime(reminder['deadline'])})")
            return f"**{reminder['id']}: **{reminder['content']}\n{time}\n"
        reminders = [*map(
            format_reminder,
            await self.bot.conn.fetch(
                "SELECT * FROM reminders WHERE user_id=$1 ORDER BY id",
                ctx.author.id))]
        await ctx.quick_menu(reminders or ['No reminders'], 5,
                             template=neo.Embed().set_author(
                                 name=ctx.author,
                                 icon_url=ctx.author.avatar_url_as(static_format='png')),
                             delete_on_button=True, clear_reactions_after=True)

    @_remind.command(name='remove', aliases=['del', 'rm'])
    async def _remind_remove(self, ctx, items: commands.Greedy[int]):
        """Remove one, or many reminders by their unique ID"""
        running = [*filter(lambda task: task.get_name().startswith("REMINDER"), asyncio.all_tasks(self.bot.loop))]
        async with ctx.loading():
            for reminder_id in items:
                [task.cancel() for task in running if task.get_name().endswith(str(reminder_id))]
                await self.bot.conn.execute("DELETE FROM reminders WHERE id=$1 and user_id=$2", reminder_id, ctx.author.id)

    async def _create_first_reminders(self):
        await self.bot.wait_until_ready()
        for record in await self.bot.conn.fetch("SELECT * FROM reminders"):
            Reminder(user=self.bot.get_user(record['user_id']), content=record['content'],
                deadline=record['deadline'], bot=self.bot, conn_pool=self.bot.conn,
                rm_id=record['id'], jump_origin=record['origin_jump'])

    def cog_unload(self):
        running = [*filter(lambda task: task.get_name().startswith("REMINDER"), asyncio.all_tasks(self.bot.loop))]
        [task.cancel() for task in running]


def setup(bot):
    bot.add_cog(Customisation(bot))
