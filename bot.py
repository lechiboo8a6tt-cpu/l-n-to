import asyncio
import discord
from discord import app_commands
import aiosqlite
import random
import os
import threading
import time
from flask import Flask, request

TOKEN = os.getenv("TOKEN")
CATEGORY_ID = None

app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    return "OK", 200

class MyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.db_path = "elo_data.db"
        self.match_queue = []
        self.active_matches = {}
        self.lock = threading.Lock()

    async def setup_hook(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS players (
                    discord_id TEXT PRIMARY KEY,
                    discord_name TEXT,
                    game_name TEXT,
                    elo INTEGER DEFAULT 1000,
                    kills INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    clan_id INTEGER
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS clans (
                    clan_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    clan_name TEXT UNIQUE,
                    owner_id TEXT
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS clan_members (
                    discord_id TEXT PRIMARY KEY,
                    clan_id INTEGER,
                    role TEXT DEFAULT 'member',
                    FOREIGN KEY(clan_id) REFERENCES clans(clan_id)
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS clan_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    clan_id INTEGER,
                    rule_text TEXT,
                    created_by TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(clan_id) REFERENCES clans(clan_id)
                )
            ''')
            await db.execute('''
                CREATE TABLE IF NOT EXISTS elo_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_id TEXT,
                    elo_change INTEGER,
                    reason TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            await db.commit()
        await self.tree.sync()

bot = MyBot()

@bot.event
async def on_ready():
    print(f"Bot online: {bot.user}")

async def init_player(db, member):
    cursor = await db.execute("SELECT elo FROM players WHERE discord_id = ?", (str(member.id),))
    if not await cursor.fetchone():
        await db.execute("INSERT INTO players (discord_id, discord_name, elo) VALUES (?, ?, 1000)", (str(member.id), member.name))
        await db.commit()

async def get_user_clan_id(db, discord_id):
    cursor = await db.execute("SELECT clan_id FROM players WHERE discord_id = ?", (str(discord_id),))
    row = await cursor.fetchone()
    return row[0] if row else None

async def get_member_role(db, discord_id):
    cursor = await db.execute("SELECT role FROM clan_members WHERE discord_id = ?", (str(discord_id),))
    row = await cursor.fetchone()
    return row[0] if row else None

async def get_clan_name(db, clan_id):
    cursor = await db.execute("SELECT clan_name FROM clans WHERE clan_id = ?", (clan_id,))
    row = await cursor.fetchone()
    return row[0] if row else None

@bot.tree.command(name="link", description="Link Discord to Blockman Go name")
async def link(interaction: discord.Interaction, game_name: str):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(bot.db_path) as db:
        await init_player(db, interaction.user)
        await db.execute("UPDATE players SET game_name = ? WHERE discord_id = ?", (game_name, str(interaction.user.id)))
        await db.commit()
    await interaction.followup.send(f"Linked to **{game_name}**")

@bot.tree.command(name="create_clan", description="Create your own clan")
async def create_clan(interaction: discord.Interaction, clan_name: str):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(bot.db_path) as db:
        if await get_user_clan_id(db, interaction.user.id):
            await interaction.followup.send("Already in a clan.")
            return
        cursor = await db.execute("SELECT clan_id FROM clans WHERE clan_name = ?", (clan_name,))
        if await cursor.fetchone():
            await interaction.followup.send("Clan name taken.")
            return
        await db.execute("INSERT INTO clans (clan_name, owner_id) VALUES (?, ?)", (clan_name, str(interaction.user.id)))
        cursor = await db.execute("SELECT clan_id FROM clans WHERE clan_name = ?", (clan_name,))
        cid = (await cursor.fetchone())[0]
        await db.execute("INSERT INTO clan_members (discord_id, clan_id, role) VALUES (?, ?, ?)", (str(interaction.user.id), cid, "owner"))
        await db.execute("UPDATE players SET clan_id = ? WHERE discord_id = ?", (cid, str(interaction.user.id)))
        await db.commit()
    await interaction.followup.send(f"Clan **{clan_name}** created. You are owner.")

@bot.tree.command(name="invite", description="Invite a member to your clan")
async def invite(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(bot.db_path) as db:
        cid = await get_user_clan_id(db, interaction.user.id)
        if not cid:
            await interaction.followup.send("You are not in a clan.")
            return
        role = await get_member_role(db, interaction.user.id)
        if role not in ["owner", "co_owner"]:
            await interaction.followup.send("Only owner/co-owner can invite.")
            return
        if await get_user_clan_id(db, member.id):
            await interaction.followup.send("User already in a clan.")
            return
        await db.execute("INSERT INTO clan_members (discord_id, clan_id, role) VALUES (?, ?, ?)", (str(member.id), cid, "member"))
        await db.execute("UPDATE players SET clan_id = ? WHERE discord_id = ?", (cid, str(member.id)))
        await db.commit()
    await interaction.followup.send(f"Invited {member.mention} as Member.")

@bot.tree.command(name="add_role", description="Assign role (Owner only)")
async def add_role(interaction: discord.Interaction, member: discord.Member, role: str):
    allowed = ["co_owner", "comander", "headcomander", "leader", "def_comander", "recruiter"]
    if role.lower() not in allowed:
        await interaction.response.send_message("Invalid role.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(bot.db_path) as db:
        cid = await get_user_clan_id(db, interaction.user.id)
        if not cid:
            await interaction.followup.send("Not in clan.")
            return
        if await get_member_role(db, interaction.user.id) != "owner":
            await interaction.followup.send("Only owner can add roles.")
            return
        if await get_user_clan_id(db, member.id) != cid:
            await interaction.followup.send("Member not in your clan.")
            return
        limits = {"comander": 10, "headcomander": 1, "leader": 10, "def_comander": 10, "recruiter": 10}
        count = (await (await db.execute("SELECT COUNT(*) FROM clan_members WHERE clan_id = ? AND role = ?", (cid, role))).fetchone())[0]
        if role in limits and count >= limits[role]:
            await interaction.followup.send(f"Role {role} at max limit.")
            return
        await db.execute("UPDATE clan_members SET role = ? WHERE discord_id = ?", (role, str(member.id)))
        await db.commit()
    await interaction.followup.send(f"Assigned {role} to {member.mention}.")

@bot.tree.command(name="kick", description="Kick member from clan")
async def kick(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(bot.db_path) as db:
        cid = await get_user_clan_id(db, interaction.user.id)
        if not cid:
            await interaction.followup.send("Not in clan.")
            return
        kicker = await get_member_role(db, interaction.user.id)
        target = await get_member_role(db, member.id)
        if not kicker or kicker == "member":
            await interaction.followup.send("You cannot kick.")
            return
        if not target or await get_user_clan_id(db, member.id) != cid:
            await interaction.followup.send("Member not in your clan.")
            return
        hierarchy = ["member", "recruiter", "def_comander", "leader", "headcomander", "comander", "co_owner", "owner"]
        if hierarchy.index(kicker) <= hierarchy.index(target):
            await interaction.followup.send("You cannot kick equal or higher role.")
            return
        await db.execute("DELETE FROM clan_members WHERE discord_id = ?", (str(member.id),))
        await db.execute("UPDATE players SET clan_id = NULL WHERE discord_id = ?", (str(member.id),))
        await db.commit()
    await interaction.followup.send(f"Kicked {member.mention}.")

@bot.tree.command(name="myrole", description="Check your role")
async def myrole(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(bot.db_path) as db:
        cid = await get_user_clan_id(db, interaction.user.id)
        if not cid:
            await interaction.followup.send("No clan.")
            return
        name = await get_clan_name(db, cid)
        role = await get_member_role(db, interaction.user.id)
    await interaction.followup.send(f"Clan: {name} | Role: {role}")

@bot.tree.command(name="clan_rank", description="Clan ELO ranking")
async def clan_rank(interaction: discord.Interaction):
    await interaction.response.defer()
    async with aiosqlite.connect(bot.db_path) as db:
        cid = await get_user_clan_id(db, interaction.user.id)
        if not cid:
            await interaction.followup.send("No clan.")
            return
        name = await get_clan_name(db, cid)
        rows = await (await db.execute('''
            SELECT p.discord_name, p.elo, p.kills, p.wins, p.losses, c.role
            FROM players p JOIN clan_members c ON p.discord_id = c.discord_id
            WHERE c.clan_id = ? ORDER BY p.elo DESC LIMIT 10
        ''', (cid,))).fetchall()
    if not rows:
        await interaction.followup.send("No data.")
        return
    msg = f"**{name}**\n```"
    for i, (n, e, k, w, l, r) in enumerate(rows, 1):
        msg += f"\n{i}. {n} [{r}]: {e} ELO (W:{w} L:{l})"
    msg += "```"
    await interaction.followup.send(msg)

@bot.tree.command(name="rules", description="Show clan rules")
async def rules(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(bot.db_path) as db:
        cid = await get_user_clan_id(db, interaction.user.id)
        if not cid:
            await interaction.followup.send("No clan.")
            return
        rows = await (await db.execute("SELECT rule_text, created_by FROM clan_rules WHERE clan_id = ? ORDER BY id DESC LIMIT 5", (cid,))).fetchall()
    if not rows:
        await interaction.followup.send("No rules.")
        return
    msg = "**Rules:**\n"
    for i, (t, a) in enumerate(rows, 1):
        msg += f"\n{i}. {t} (by {a})"
    await interaction.followup.send(msg)

@bot.tree.command(name="add_rule", description="Add clan rule (Owner/Co-owner)")
async def add_rule(interaction: discord.Interaction, rule: str):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(bot.db_path) as db:
        cid = await get_user_clan_id(db, interaction.user.id)
        if not cid:
            await interaction.followup.send("No clan.")
            return
        role = await get_member_role(db, interaction.user.id)
        if role not in ["owner", "co_owner"]:
            await interaction.followup.send("No permission.")
            return
        await db.execute("INSERT INTO clan_rules (clan_id, rule_text, created_by) VALUES (?, ?, ?)", (cid, rule, interaction.user.name))
        await db.commit()
    await interaction.followup.send("Rule added.")

@bot.tree.command(name="join", description="Join matchmaking queue")
async def joinq(interaction: discord.Interaction):
    async with aiosqlite.connect(bot.db_path) as db:
        await init_player(db, interaction.user)
    with bot.lock:
        if interaction.user in bot.match_queue:
            await interaction.response.send_message("Already in queue.", ephemeral=True)
            return
        bot.match_queue.append(interaction.user)
    await interaction.response.send_message(f"Queued. {len(bot.match_queue)} waiting.")
    if len(bot.match_queue) >= 2:
        await asyncio.sleep(0.5)
        with bot.lock:
            if len(bot.match_queue) < 2:
                return
            p1 = bot.match_queue.pop(0)
            p2 = bot.match_queue.pop(0)
        cat = None
        if CATEGORY_ID:
            cat = interaction.guild.get_channel(CATEGORY_ID)
        else:
            ov = {
                interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                bot.user: discord.PermissionOverwrite(read_messages=True)
            }
            cat = await interaction.guild.create_category("ELO Match", overwrites=ov)
        name = f"match-{random.randint(100,999)}"
        try:
            # SỬA LỖI: Cấp quyền read_messages cho cả 2 người chơi
            player_overwrites = {
                p1: discord.PermissionOverwrite(read_messages=True),
                p2: discord.PermissionOverwrite(read_messages=True)
            }
            txt = await interaction.guild.create_text_channel(name, category=cat, overwrites=player_overwrites)
            voice = await interaction.guild.create_voice_channel(f"{name}-voice", category=cat)
            await txt.send(f"⚔️ {p1.mention} vs {p2.mention}\nReport with /report win or /report lose after match.")
            bot.active_matches[txt.id] = {"text": txt, "voice": voice, "winner": None, "loser": None}
        except Exception as e:
            with bot.lock:
                bot.match_queue.insert(0, p2)
                bot.match_queue.insert(0, p1)
            await interaction.followup.send(f"Match creation failed: {e}", ephemeral=True)

@bot.tree.command(name="leave", description="Leave matchmaking queue")
async def leaveq(interaction: discord.Interaction):
    with bot.lock:
        if interaction.user in bot.match_queue:
            bot.match_queue.remove(interaction.user)
            await interaction.response.send_message("Left queue.", ephemeral=True)
        else:
            await interaction.response.send_message("Not in queue.", ephemeral=True)

@bot.tree.command(name="report", description="Report match result")
async def report(interaction: discord.Interaction, result: str):
    cid = interaction.channel.id
    if cid not in bot.active_matches:
        await interaction.response.send_message("Not a match channel.", ephemeral=True)
        return
    data = bot.active_matches[cid]
    r = result.lower()
    if r not in ["win", "lose"]:
        await interaction.response.send_message("Use win or lose.", ephemeral=True)
        return
    if r == "win":
        if data["winner"]:
            await interaction.response.send_message("Winner already reported.", ephemeral=True)
            return
        data["winner"] = interaction.user
    else:
        if data["loser"]:
            await interaction.response.send_message("Loser already reported.", ephemeral=True)
            return
        data["loser"] = interaction.user
    await interaction.response.send_message(f"Reported {r}. Waiting for opponent.")
    if data["winner"] and data["loser"]:
        w = data["winner"]
        l = data["loser"]
        async with aiosqlite.connect(bot.db_path) as db:
            await db.execute("UPDATE players SET elo = elo + 15, wins = wins + 1 WHERE discord_id = ?", (str(w.id),))
            await db.execute("UPDATE players SET elo = elo - 15, losses = losses + 1 WHERE discord_id = ?", (str(l.id),))
            await db.commit()
        await interaction.channel.send(f"🏆 {w.mention} +15 ELO\n💀 {l.mention} -15 ELO")
        await asyncio.sleep(10)
        await data["text"].delete()
        await data["voice"].delete()
        del bot.active_matches[cid]

@bot.tree.command(name="profile", description="View player profile")
async def profile(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    await interaction.response.defer()
    async with aiosqlite.connect(bot.db_path) as db:
        await init_player(db, target)
        cur = await db.execute("SELECT elo, kills, wins, losses FROM players WHERE discord_id = ?", (str(target.id),))
        row = await cur.fetchone()
    e, k, w, l = row
    embed = discord.Embed(title=target.display_name, color=0xff5555)
    embed.add_field(name="ELO", value=str(e), inline=True)
    embed.add_field(name="Kills", value=str(k), inline=True)
    embed.add_field(name="Wins", value=str(w), inline=True)
    embed.add_field(name="Losses", value=str(l), inline=True)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="rank", description="Global ELO leaderboard")
async def rank(interaction: discord.Interaction):
    await interaction.response.defer()
    async with aiosqlite.connect(bot.db_path) as db:
        cur = await db.execute("SELECT discord_name, elo FROM players ORDER BY elo DESC LIMIT 10")
        rows = await cur.fetchall()
    if not rows:
        await interaction.followup.send("No data.")
        return
    msg = "**Global Top 10**\n```"
    for i, (n, e) in enumerate(rows, 1):
        msg += f"\n{i}. {n}: {e} ELO"
    msg += "```"
    await interaction.followup.send(msg)

def run_flask():
    app.run(host="0.0.0.0", port=10000)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    bot.run(TOKEN)
