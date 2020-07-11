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
import contextlib
import re
from contextlib import suppress
import asyncio

from discord.ext import commands
import discord

import neo.utils.paginator as pages
from .config import CONFIG

_EMOJIS = CONFIG.emoji_suite


class Codeblock:
    def __init__(self, *, content, lang=None, cb_safe=True):
        self.lang = lang or ''
        self.cb_safe = cb_safe
        self.content = content.replace('``', '`\N{ZWSP}`') if cb_safe else content

    def __str__(self):
        return f"```{self.lang}\n{self.content}\n```"

    def __repr__(self):
        return f"<Codeblock content={self.content!r} lang={self.lang!r} cb_safe={self.cb_safe}>"


class Context(commands.Context):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def prompt(self, message):
        emojis = {
            _EMOJIS.check_button: True,
            _EMOJIS.x_button: False}
        msg = await self.send(message)
        for e in emojis.keys():
            await msg.add_reaction(e)
        payload = await self.bot.wait_for(
            'raw_reaction_add',
            check=lambda p: str(p.emoji) in emojis.keys()
            and p.user_id == self.author.id and p.message_id == msg.id)
        if emojis[str(payload.emoji)] is True:
            await msg.edit(content='Confirmed!')
            return True
        else:
            await msg.edit(content='Cancelled!')
            return False

    async def safe_send(self, content=None, **kwargs):
        if content:
            if match := re.search(re.compile(r'([a-zA-Z0-9]{24}\.[a-zA-Z0-9]{6}\.[a-zA-Z0-9_\-]{27}|mfa\.['
                                             r'a-zA-Z0-9_\-]{84})'), content):
                content = content.replace(match.group(0), '[token omitted]')
            if len(content) > 2000:
                async with self.bot.session.post(
                        "https://mystb.in/documents",
                        data=content.encode('utf-8')) as post:
                    post = await post.json()
                    url = f"https://mystb.in/{post['key']}"
                    await self.send(f'Output: <{url}>')
            else:
                return await self.send(content, **kwargs)
        else:
            await self.send(**kwargs)

    def tick(self, opt, label=None):
        lookup = {
            True: _EMOJIS.check_button,
            False: _EMOJIS.x_button,
            None: _EMOJIS.neutral_button,
        }
        emoji = lookup.get(opt, _EMOJIS.x_button)
        if label is not None:
            return f'{emoji}: {label}'
        return emoji

    def toggle(self, opt):
        options = {
            True: _EMOJIS.toggleon,
            False: _EMOJIS.toggleoff,
            None: _EMOJIS.toggleoff
        }
        emoji = options.get(opt, _EMOJIS.toggleoff)
        return emoji

    @staticmethod
    def codeblock(**kwargs):
        return Codeblock(**kwargs)

    def tab(self, repeat=1):
        tabs = []
        for i in range(repeat):
            tabs.append(' \u200b')
        return ''.join(tabs)

    async def quick_menu(self, entries, per_page, *, template: discord.Embed = None, **kwargs):
        source = pages.BareBonesMenu(entries, per_page=per_page, embed=template)
        menu = pages.CSMenu(source, **kwargs)
        await menu.start(self)

    @contextlib.asynccontextmanager
    async def loading(self, *, prop=True, tick=True, exc_ignore=None):
        clear_reacts = self.message.remove_reaction(_EMOJIS.loading, self.me)
        tasks = [clear_reacts]
        try:
            yield await self.message.add_reaction(_EMOJIS.loading)
        except Exception as e:
            if exc_ignore and isinstance(e, exc_ignore):
                pass
            elif isinstance(e, (discord.Forbidden, discord.HTTPException)):
                yield False
                return
            else:
                if prop:
                    self.bot.dispatch('command_error', self, e)
        else:
            tasks.append(self.message.add_reaction(self.tick(True))) if tick else None
        finally:
            with suppress(discord.NotFound):
                await asyncio.gather(*tasks)

    @staticmethod
    async def propagate_to_eh(bot, ctx, error, do_emojis=True):
        if do_emojis is False:
            return await ctx.send(error)
        with suppress(Exception):
            await ctx.message.add_reaction(_EMOJIS.warning_button)
            try:
                reaction, user = await bot.wait_for(
                    'reaction_add',
                    check=lambda r, u: r.message.id == ctx.message.id
                    and u.id in [ctx.author.id, 680835476034551925], timeout=30.0
                )
            except asyncio.TimeoutError:
                await ctx.message.remove_reaction(_EMOJIS.warning_button, ctx.me)
                return
            if str(reaction.emoji) == _EMOJIS.warning_button:
                return await ctx.send(error)

