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
import argparse
import asyncio
import copy
import io
import os
import re
import shlex
import textwrap
import time
import traceback
from contextlib import redirect_stdout, suppress
from typing import Union

import discord
import import_expression
from discord.ext import commands
from tabulate import tabulate

import utils
from utils.config import conf
from utils.formatters import return_lang_hl, pluralize
from utils.paginator import ShellMenu, CSMenu


class Arguments(argparse.ArgumentParser):
    def error(self, message):
        raise RuntimeError(message)


async def do_shell(args):
    shell = os.getenv("SHELL") or "/bin/bash"
    process = await asyncio.create_subprocess_shell(
        f'{shell} -c "{args}"',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await process.communicate()
    return stdout, stderr


async def copy_ctx(
        self, ctx, command_string, *,
        channel: discord.TextChannel = None,
        author: Union[discord.Member, discord.User] = None):
    msg = copy.copy(ctx.message)
    msg.channel = channel or ctx.channel
    msg.author = author or ctx.author
    msg.content = ctx.prefix + command_string
    new_ctx = await self.bot.get_context(msg, cls=utils.context.Context)
    return new_ctx


def clean_bytes(line):
    """
    Cleans a byte sequence of shell directives and decodes it.
    """
    text = line.decode('utf-8').replace('\r', '').strip('\n')
    return re.sub(r'\x1b[^m]*m', '', text).replace("``", "`\u200b`").strip('\n')


def cleanup_code(content):
    """Automatically removes code blocks from the code."""
    # remove ```py\n```
    if content.startswith('```') and content.endswith('```'):
        return '\n'.join(content.split('\n')[1:-1])

    # remove `foo`
    return content.strip('` \n')


# noinspection PyBroadException
class Dev(commands.Cog):
    """Commands made to assist with bot development"""

    def __init__(self, bot):
        self.bot = bot
        self._last_result = None

    async def cog_check(self, ctx):
        return await self.bot.is_owner(ctx.author)

    @commands.command(aliases=['sh'])
    async def shell(self, ctx, *, args):
        """Invokes the system shell,
        attempting to run the inputted command"""
        hl_lang = 'sh'
        if 'cat' in args:
            hl_lang = return_lang_hl(args)
        if 'git diff' in args:
            hl_lang = 'diff'
        await ctx.trigger_typing()
        stdout, stderr = await do_shell(args)
        output = stdout + stderr
        entries = list(clean_bytes(output))
        source = ShellMenu(entries, code_lang=hl_lang, per_page=1925)
        menu = CSMenu(source, delete_message_after=True)
        await menu.start(ctx)

    @commands.command(name='eval')
    async def eval_(self, ctx, *, body: str):
        """Runs code that you input to the command"""

        env = {
            'bot': self.bot,
            'ctx': ctx,
            'channel': ctx.channel,
            'author': ctx.author,
            'guild': ctx.guild,
            'message': ctx.message,
            '_': self._last_result
        }
        env.update(globals())
        body = cleanup_code(body)
        stdout = io.StringIO()
        sent = None
        to_compile = f'async def func():\n{textwrap.indent(body, "  ")}'
        try:
            import_expression.exec(to_compile, env)
        except Exception as e:
            return await ctx.safe_send(f'```py\n{e.__class__.__name__}: {e}\n```')
        evaluated_func = env['func']
        try:
            with redirect_stdout(stdout):
                result = await evaluated_func()
        except Exception:
            value = stdout.getvalue()
            sent = await ctx.safe_send(f'```py\n{value}{traceback.format_exc()}\n```')
        else:
            value = stdout.getvalue()
            with suppress(Exception):
                await ctx.message.add_reaction(ctx.tick(True))
            if result is None:
                if value:
                    sent = await ctx.safe_send(f'{value}')
            else:
                self._last_result = result
                if isinstance(result, discord.Embed):
                    sent = await ctx.send(embed=result)
                elif isinstance(result, discord.File):
                    sent = await ctx.send(file=result)
                else:
                    sent = await ctx.safe_send(f'{value}{result}')
        if sent:
            await sent.add_reaction(ctx.tick(False))
            try:
                reaction, user = await self.bot.wait_for(
                    'reaction_add',
                    check=lambda r, u: r.message.id == sent.id and u.id == ctx.author.id,
                    timeout=30)
            except asyncio.TimeoutError:
                await sent.remove_reaction(ctx.tick(False), ctx.me)
            else:
                if str(reaction.emoji) == str(ctx.tick(False)):
                    await reaction.message.delete()

    @commands.command()
    async def debug(self, ctx, *, command_string):
        """Runs a command, checking for errors and returning exec time"""
        start = time.perf_counter()
        new_ctx = await copy_ctx(self, ctx, command_string)
        stdout = io.StringIO()
        try:
            with redirect_stdout(stdout):
                await new_ctx.reinvoke()
        except Exception:
            await ctx.message.add_reaction('❗')
            value = stdout.getvalue()
            paginator = commands.Paginator(prefix='```py')
            for line in (value + traceback.format_exc()).split('\n'):
                paginator.add_line(line)
            for page in paginator.pages:
                await ctx.author.send(page)
            return
        end = time.perf_counter()
        await ctx.send(f'Cmd `{command_string}` executed in {end - start:.3f}s')

    @commands.command()
    async def sql(self, ctx, *, query: str):
        """Run SQL statements"""
        is_multistatement = query.count(';') > 1
        if is_multistatement:
            strategy = self.bot.conn.execute
        else:
            strategy = self.bot.conn.fetch

        start = time.perf_counter()
        results = await strategy(query)
        dt = (time.perf_counter() - start) * 1000.0

        rows = len(results)
        if is_multistatement or rows == 0:
            return await ctx.send(f'`{dt:.2f}ms: {results}`')
        headers = list(results[0].keys())
        table = tabulate(list(list(r.values()) for r in results), headers=headers, tablefmt='pretty')
        await ctx.safe_send(f'```\n{table}```\nReturned {rows} {pluralize("row", rows)} in {dt:.2f}ms')

    @commands.group(name='dev', invoke_without_command=True)
    async def dev_command_group(self, ctx):
        """Some dev commands"""
        await ctx.send("We get it buddy, you're super cool because you can use the dev commands")

    @dev_command_group.command(name='logs')
    async def view_journal_ctl(self, ctx):
        stdout, stderr = await do_shell('journalctl -u neo -n 300 --no-pager -o cat')
        output = stdout + stderr
        entries = list(clean_bytes(output))
        source = ShellMenu(entries, code_lang='sh', per_page=1925)
        menu = CSMenu(source, delete_message_after=True)
        await menu.start(ctx)

    @dev_command_group.command(name='delete', aliases=['del'])
    async def delete_bot_msg(self, ctx, message_ids: commands.Greedy[int]):
        for m_id in message_ids:
            converter = commands.MessageConverter()
            m = await converter.convert(ctx, str(m_id))
            if not m.author.bot:
                raise commands.CommandError('I can only delete my own messages')
            await m.delete()
        await ctx.message.add_reaction(ctx.tick(True))

    @commands.command(name='edit')
    async def args_edit(self, ctx, *, args: str):
        """
        Edit the bot's aspects using a command-line syntax.
        Available arguments:
            -p --presence: edits the bot's presence (playing, listening, streaming, watching, none)
            -n --nick: edits the bot's nickname for the current guild
            -s --status: edits the bot's status (dnd, idle, online, offline)
        """
        status_dict = {
            'online': discord.Status.online,
            'offline': discord.Status.offline,
            'dnd': discord.Status.dnd,
            'idle': discord.Status.idle
        }
        type_dict = {
            'playing': 0,
            'streaming': 'streaming',
            'listening': 2,
            'watching': 3,
            'none': None
        }
        updated_list = []
        parser = Arguments(add_help=False, allow_abbrev=False)
        parser.add_argument('-s', '--status', nargs='?', const='online', dest='status')
        parser.add_argument('-p', '--presence', nargs='+', dest='presence')
        parser.add_argument('-n', '--nick', nargs='?', const='None', dest='nick')
        args = parser.parse_args(shlex.split(args))
        if args.presence:
            if type_dict.get(args.presence[0]) is None:
                await self.bot.change_presence(status=ctx.me.status)
            elif type_dict.get(args.presence[0]) == 'streaming':
                args.presence.pop(0)
                await self.bot.change_presence(activity=discord.Streaming(
                    name=' '.join(args.presence), url='https://www.twitch.tv/#'))
            else:
                await self.bot.change_presence(
                    status=ctx.me.status,
                    activity=discord.Activity(
                        type=type_dict[args.presence.pop(0)], name=' '.join(args.presence)))
            updated_list.append(
                f'Changed presence to {ctx.me.activity.name if ctx.me.activity is not None else "None"}')
        if args.nick:
            await ctx.me.edit(nick=args.nick if args.nick != 'None' else None)
            updated_list.append(f'Changed nickname to {args.nick}')
        if args.status:
            await self.bot.change_presence(status=status_dict[args.status.lower()], activity=ctx.me.activity)
            updated_list.append(f'Changed status to {conf["emoji_dict"][args.status.lower()]}')
        await ctx.send(
            embed=discord.Embed(
                title='Edited bot', description='\n'.join(updated_list), color=discord.Color.main),
            delete_after=7.5
        )

    @commands.group(invoke_without_command=True)
    async def sudo(self, ctx, target: Union[discord.Member, discord.User, None], *, command):
        """Run command as another user"""
        if not isinstance(target, (discord.Member, discord.User)):
            new_ctx = await copy_ctx(self, ctx, command, author=ctx.author)
            await new_ctx.reinvoke()
            return
        new_ctx = await copy_ctx(self, ctx, command, author=target)
        await self.bot.invoke(new_ctx)

    @sudo.command(name='in')
    async def _in(
            self, ctx,
            channel: discord.TextChannel,
            *, command):
        new_ctx = await copy_ctx(
            self, ctx, command, channel=channel)
        await self.bot.invoke(new_ctx)

    @commands.command(aliases=['die', 'kys'])
    async def reboot(self, ctx):
        """Kills all of the bot's processes"""
        response = await ctx.prompt('Are you sure you want to reboot?')
        if response:
            await self.bot.close()

    @commands.command(name='screenshot', aliases=['ss'])
    async def _website_screenshot(self, ctx, *, site):
        """Take a screenshot of a site"""
        async with ctx.typing():
            response = await self.bot.session.get('https://magmachain.herokuapp.com/api/v1', headers={'website': site})
            url = (await response.json())['snapshot']
            await ctx.send(embed=discord.Embed(colour=discord.Color.main).set_image(url=url))

    @commands.command(name='extensions', aliases=['ext'])
    async def _dev_extensions(self, ctx, *, args=None):
        """
        View or manage extensions
        r, l, u are valid flag options
        """
        mode_mapping = {'r': self.bot.reload_extension, 'l': self.bot.load_extension, 'u': self.bot.unload_extension}
        if args is None:
            return await ctx.send(embed=discord.Embed(
                description='\n'.join([*self.bot.extensions.keys()]), color=discord.Color.main))
        parser = Arguments(add_help=False, allow_abbrev=False)
        parser.add_argument('-m', '--mode', choices=['r', 'l', 'u'])
        parser.add_argument('-p', '--pull', action='store_true')
        parser.add_argument('extension', nargs='*', default='~')
        args = parser.parse_args(shlex.split(args))
        if args.pull:
            await do_shell('git pull')
        mode = mode_mapping.get(args.mode) if args.mode else self.bot.reload_extension
        extensions = [*self.bot.extensions.keys()] if args.extension[0] == '~' else args.extension
        for ext in extensions:
            mode(ext)
        await ctx.message.add_reaction(ctx.tick(True))
        # TODO: Write a context manager for ^ this so it doesnt always react true


def setup(bot):
    bot.add_cog(Dev(bot))
