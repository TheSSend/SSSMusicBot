import discord
import aiohttp
import asyncio
import sqlite3
import os
import logging
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from runtime_paths import data_path

load_dotenv()
logger = logging.getLogger(__name__)

DB = data_path("forum_eye.db")

REQUEST_DELAY = 1
CACHE_TIME = 300
PAGE_CACHE = {}

FORUM_ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("FORUM_ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]

SERVER_SECTIONS = {
1:"https://forum.majestic-rp.ru/forums/zhaloby-na-igrokov-server-1.635/",
2:"https://forum.majestic-rp.ru/forums/zhaloby-na-igrokov-server-2.636/",
3:"https://forum.majestic-rp.ru/forums/zhaloby-na-igrokov-server-3.637/",
4:"https://forum.majestic-rp.ru/forums/zhaloby-na-igrokov-server-4.638/",
5:"https://forum.majestic-rp.ru/forums/zhaloby-na-igrokov-server-5.639/",
6:"https://forum.majestic-rp.ru/forums/zhaloby-na-igrokov-server-6.640/",
7:"https://forum.majestic-rp.ru/forums/zhaloby-na-igrokov-los-angeles.651/",
8:"https://forum.majestic-rp.ru/forums/zhaloby-na-igrokov-server-8.652/",
9:"https://forum.majestic-rp.ru/forums/zhaloby-na-igrokov-server-9.653/",
10:"https://forum.majestic-rp.ru/forums/zhaloby-na-igrokov-server-10.654/",
11:"https://forum.majestic-rp.ru/forums/zhaloby-na-igrokov-server-11.655/",
12:"https://forum.majestic-rp.ru/forums/zhaloby-na-igrokov-server-12.656/",
13:"https://forum.majestic-rp.ru/forums/zhaloby-na-igrokov-server-13.657/",
14:"https://forum.majestic-rp.ru/forums/zhaloby-na-igrokov-server-14.658/",
15:"https://forum.majestic-rp.ru/forums/zhaloby-na-igrokov-server-15.659/",
16:"https://forum.majestic-rp.ru/forums/zhaloby-na-igrokov-server-16.660/",
17:"https://forum.majestic-rp.ru/forums/zhaloby-na-igrokov-server-17.661/"
}

# ================= DATABASE =================

def init_db():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS profiles(
    user_id INTEGER PRIMARY KEY,
    static TEXT,
    server INTEGER
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS complaints(
    url TEXT PRIMARY KEY,
    static TEXT,
    server INTEGER,
    status TEXT
    )
    """)

    conn.commit()
    conn.close()


def get_all_profiles():

    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    cur.execute("SELECT user_id, static, server FROM profiles")

    rows = cur.fetchall()

    conn.close()

    return rows


def set_profile(user, static, server):

    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    cur.execute(
        "INSERT OR REPLACE INTO profiles VALUES(?,?,?)",
        (user, static, server)
    )

    conn.commit()
    conn.close()


def get_profile(user):

    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    cur.execute(
        "SELECT static,server FROM profiles WHERE user_id=?",
        (user,)
    )

    row = cur.fetchone()

    conn.close()

    return row


def save_complaint(url, static, server, status):

    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    cur.execute(
        "INSERT OR REPLACE INTO complaints VALUES(?,?,?,?)",
        (url, static, server, status)
    )

    conn.commit()
    conn.close()


def get_complaint(url):

    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    cur.execute(
        "SELECT status FROM complaints WHERE url=?",
        (url,)
    )

    row = cur.fetchone()

    conn.close()

    return row


# ================= AI ANALYSIS =================

def analyze(text):

    text = text.lower()

    if "наказан" in text:
        return "accepted"

    if "выдано наказание" in text:
        return "accepted"

    if "отказано" in text:
        return "denied"

    if "жалоба отклонена" in text:
        return "denied"

    return "pending"


def get_soup_parser():
    try:
        from bs4 import BeautifulSoup
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Модуль beautifulsoup4 не установлен. Установи зависимости из requirements.txt"
        ) from exc

    return BeautifulSoup


# ================= PARSER =================

class ForumParser:

    async def fetch(self, url):

        if url in PAGE_CACHE:

            html, ts = PAGE_CACHE[url]

            if asyncio.get_event_loop().time() - ts < CACHE_TIME:
                return html

        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as r:
                r.raise_for_status()
                html = await r.text()

        PAGE_CACHE[url] = (html, asyncio.get_event_loop().time())

        await asyncio.sleep(REQUEST_DELAY)

        return html


    async def parse_topic(self, url):

        html = await self.fetch(url)

        BeautifulSoup = get_soup_parser()
        soup = BeautifulSoup(html, "html.parser")

        posts = soup.select(".message-body")

        if not posts:
            return "pending"

        last_post = posts[-1].text

        return analyze(last_post)


    async def parse_section(self, url, static):

        results = []
        page = 1
        BeautifulSoup = get_soup_parser()

        while True:

            html = await self.fetch(url + f"page-{page}")

            soup = BeautifulSoup(html, "html.parser")

            topics = soup.select("div.structItem--thread")

            if not topics:
                break

            for topic in topics:

                title_tag = topic.select_one("a[data-tp-primary]")

                if not title_tag:
                    continue

                title = title_tag.text.strip()
                link = title_tag["href"]

                if not link.startswith("http"):
                    link = "https://forum.majestic-rp.ru" + link

                if static in title:

                    status = await self.parse_topic(link)

                    results.append({
                        "title": title,
                        "url": link,
                        "status": status
                    })

            page += 1

        return results


# ================= STATISTICS =================

def build_stats(results):

    stats = {
        "accepted": 0,
        "denied": 0,
        "pending": 0
    }

    for r in results:
        stats[r["status"]] += 1

    return stats


# ================= VIEW =================

class ForumView(discord.ui.View):

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog


    @discord.ui.button(label="Редактировать профиль",
    style=discord.ButtonStyle.secondary,
    custom_id="forum_edit_profile")
    async def edit(self, interaction, button):

        modal = ProfileModal()
        await interaction.response.send_modal(modal)


    @discord.ui.button(label="Жалобы ОТ игрока",
    style=discord.ButtonStyle.primary,
    custom_id="forum_from_player")
    async def from_player(self, interaction, button):

        profile = get_profile(interaction.user.id)

        if not profile:
            await interaction.response.send_message(
                "Сначала заполните профиль",
                ephemeral=True
            )
            return

        static, server = profile

        parser = ForumParser()

        results = await parser.parse_section(
            SERVER_SECTIONS[server],
            static
        )

        embed = discord.Embed(
            title=f"Жалобы ОТ {static}",
            color=0xff3b3b
        )

        if not results:
            embed.description = "Ничего не найдено"

        for r in results[:10]:
            embed.add_field(
                name=r["title"],
                value=r["url"],
                inline=False
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)


    @discord.ui.button(label="Жалобы НА игрока",
    style=discord.ButtonStyle.primary,
    custom_id="forum_on_player")
    async def on_player(self, interaction, button):

        profile = get_profile(interaction.user.id)

        if not profile:
            await interaction.response.send_message(
                "Сначала заполните профиль",
                ephemeral=True
            )
            return

        static, server = profile

        parser = ForumParser()

        results = await parser.parse_section(
            SERVER_SECTIONS[server],
            static
        )

        embed = discord.Embed(
            title=f"Жалобы НА {static}",
            color=0xff3b3b
        )

        if not results:
            embed.description = "Ничего не найдено"

        for r in results[:10]:
            embed.add_field(
                name=r["title"],
                value=r["url"],
                inline=False
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)


class ProfileModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Профиль форума")

    static = discord.ui.TextInput(
        label="Статик",
        placeholder="Введите ваш статик",
        required=True,
        min_length=1,
        max_length=32
    )

    server = discord.ui.TextInput(
        label="Сервер",
        placeholder="Введите номер сервера 1-17",
        required=True,
        min_length=1,
        max_length=2
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not self.server.value.isdigit():
            await interaction.response.send_message(
                "Сервер должен быть числом от 1 до 17",
                ephemeral=True
            )
            return

        server = int(self.server.value)
        if server not in SERVER_SECTIONS:
            await interaction.response.send_message(
                "Сервер должен быть от 1 до 17",
                ephemeral=True
            )
            return

        set_profile(interaction.user.id, self.static.value.strip(), server)
        await interaction.response.send_message(
            "Профиль сохранен",
            ephemeral=True
        )


# ================= MONITOR =================

class ForumSearch(commands.Cog):

    def __init__(self, bot):

        self.bot = bot

        init_db()

        self.bot.add_view(ForumView(self))

        self.bot.loop.create_task(self.monitor_forum())


    def cog_unload(self):
        logger.info("ForumSearch выгружен")


    async def monitor_forum(self):

        await self.bot.wait_until_ready()

        parser = ForumParser()

        while True:
            try:
                BeautifulSoup = get_soup_parser()
            except RuntimeError:
                logger.exception("Forum monitor отключен: отсутствует beautifulsoup4")
                await asyncio.sleep(300)
                continue

            profiles = get_all_profiles()

            try:
                for server, url in SERVER_SECTIONS.items():

                    html = await parser.fetch(url)

                    soup = BeautifulSoup(html, "html.parser")

                    topics = soup.select("div.structItem--thread")

                    for topic in topics[:5]:

                        title_tag = topic.select_one("a[data-tp-primary]")

                        if not title_tag:
                            continue

                        title = title_tag.text.strip()
                        link = title_tag["href"]

                        if not link.startswith("http"):
                            link = "https://forum.majestic-rp.ru" + link

                        for user_id, static, server_id in profiles:

                            if server_id != server:
                                continue

                            member = self.bot.get_user(user_id)

                            if not member:
                                continue

                            if static in title:

                                status = await parser.parse_topic(link)

                                old = get_complaint(link)

                                if not old:

                                    save_complaint(link, static, server, status)

                                    try:
                                        await member.send(
                                            f"🚨 Новая жалоба на вас\n{title}\n{link}"
                                        )
                                        await asyncio.sleep(1)
                                    except Exception:
                                        logger.exception("Не удалось отправить уведомление о новой жалобе %s", link)

                                else:

                                    old_status = old[0]

                                    if old_status == "pending" and status != "pending":

                                        save_complaint(link, static, server, status)

                                        try:
                                            await member.send(
                                                f"📢 Решение по жалобе\n{title}\nСтатус: {status}\n{link}"
                                            )
                                            await asyncio.sleep(1)
                                        except Exception:
                                            logger.exception("Не удалось отправить уведомление о решении жалобы %s", link)
            except Exception:
                logger.exception("Ошибка мониторинга форума")

            await asyncio.sleep(120)


    @app_commands.command(
        name="add_checkzb",
        description="Создать панель поиска жалоб"
    )
    async def add_checkzb(self, interaction: discord.Interaction):

        if interaction.user.id not in FORUM_ADMIN_IDS:

            await interaction.response.send_message(
                "❌ У вас нет доступа",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="👁 Forum Eye",
            description="Поиск жалоб",
            color=0xff3b3b
        )

        await interaction.channel.send(
            embed=embed,
            view=ForumView(self)
        )

        await interaction.response.send_message(
            "Панель создана",
            ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(ForumSearch(bot))
