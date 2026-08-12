import asyncio
import discord
from discord import app_commands
import aiosqlite
import random
import os
from flask import Flask, request

TOKEN = os.getenv("TOKEN")
CATEGORY_ID = None 

# --- FLASK SERVER (giữ để sau này dùng webhook) ---
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook_listener():
    return "OK", 200

# --- DISCORD BOT ---
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
                    losses INTEGER DEFAULT 0
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
    print(f"✅ Bot đã đăng nhập với tên: {bot.user}")

# --- HÀM HỖ TRỢ ---
async def init_player(db, member):
    cursor = await db.execute("SELECT elo FROM players WHERE discord_id = ?", (str(member.id),))
    if not await cursor.fetchone():
        await db.execute("INSERT INTO players (discord_id, discord_name, game_name, elo, kills, wins, losses) VALUES (?, ?, ?, 1000, 0, 0, 0)", (str(member.id), member.name, None))
        await db.commit()

async def get_user_clan_id(db, discord_id):
    cursor = await db.execute("SELECT clan_id FROM clan_members WHERE discord_id = ?", (str(discord_id),))
    row = await cursor.fetchone()
    return row[0] if row else None

async def get_member_role(db, discord_id):
    cursor = await db.execute("SELECT role FROM clan_members WHERE discord_id = ?", (str(discord_id),))
    row = await cursor.fetchone()
    return row[0] if row else None

async def has_permission_to_kick(kicker_role, target_role):
    hierarchy = ["member", "recruiter", "def_comander", "leader", "headcomander", "comander", "co_owner", "owner"]
    if kicker_role not in hierarchy or target_role not in hierarchy:
        return False
    return hierarchy.index(kicker_role) > hierarchy.index(target_role)

# --- LỆNH LIÊN KẾT TÊN GAME ---
@bot.tree.command(name="link", description="Liên kết tên Discord với tên trong Blockman Go")
async def link_name(interaction: discord.Interaction, game_name: str):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(bot.db_path) as db:
        await init_player(db, interaction.user)
        await db.execute("UPDATE players SET game_name = ? WHERE discord_id = ?", (game_name, str(interaction.user.id)))
        await db.commit()
    await interaction.followup.send(f"✅ Đã liên kết tên game **{game_name}** với Discord của bạn! (ELO khởi tạo: 1000)")

# --- LỆNH TẠO CLAN ---
@bot.tree.command(name="create_clan", description="Tạo Clan riêng cho bạn (Bạn sẽ là Owner)")
async def create_clan(interaction: discord.Interaction, clan_name: str):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(bot.db_path) as db:
        if await get_user_clan_id(db, interaction.user.id):
            return await interaction.followup.send("❌ Bạn đã có clan rồi! Không thể tạo thêm.", ephemeral=True)
        
        cursor = await db.execute("SELECT clan_id FROM clans WHERE clan_name = ?", (clan_name,))
        if await cursor.fetchone():
            return await interaction.followup.send("❌ Tên Clan này đã có người đặt rồi! Hãy chọn tên khác.", ephemeral=True)
        
        await db.execute("INSERT INTO clans (clan_name, owner_id) VALUES (?, ?)", (clan_name, str(interaction.user.id)))
        cursor = await db.execute("SELECT clan_id FROM clans WHERE clan_name = ?", (clan_name,))
        new_clan_id = (await cursor.fetchone())[0]
        
        await db.execute("INSERT INTO clan_members (discord_id, clan_id, role) VALUES (?, ?, ?)", (str(interaction.user.id), new_clan_id, "owner"))
        await db.commit()
    await interaction.followup.send(f"✅ **Clan `{clan_name}` đã được tạo thành công!**\nBạn là **Owner**. Dùng `/invite` để mời bạn bè vào.")

# --- LỆNH CLAN ---
@bot.tree.command(name="invite", description="Mời người chơi vào Clan của bạn")
async def invite_clan(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(bot.db_path) as db:
        user_clan_id = await get_user_clan_id(db, interaction.user.id)
        if not user_clan_id:
            return await interaction.followup.send("❌ Bạn không thuộc Clan nào.", ephemeral=True)
        
        role = await get_member_role(db, interaction.user.id)
        if role not in ["owner", "co_owner"]:
            return await interaction.followup.send("❌ Chỉ Owner và Co-owner mới được mời người!", ephemeral=True)
        
        if await get_user_clan_id(db, member.id):
            return await interaction.followup.send(f"❌ {member.mention} đã ở trong Clan khác rồi.", ephemeral=True)
        
        await db.execute("INSERT INTO clan_members (discord_id, clan_id, role) VALUES (?, ?, ?)", (str(member.id), user_clan_id, "member"))
        await db.commit()
    await interaction.followup.send(f"✅ Đã mời {member.mention} vào Clan của bạn với role **Member**!")

@bot.tree.command(name="add_role", description="Thêm Role cho thành viên (Chỉ Owner)")
async def add_role(interaction: discord.Interaction, member: discord.Member, new_role: str):
    allowed_roles = ["co_owner", "comander", "headcomander", "leader", "def_comander", "recruiter"]
    if new_role.lower() not in allowed_roles:
        return await interaction.response.send_message("❌ Role không hợp lệ!", ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(bot.db_path) as db:
        user_clan_id = await get_user_clan_id(db, interaction.user.id)
        if not user_clan_id:
            return await interaction.followup.send("❌ Bạn không có Clan.", ephemeral=True)
        
        role = await get_member_role(db, interaction.user.id)
        if role != "owner":
            return await interaction.followup.send("❌ Chỉ Owner mới được thêm Role!", ephemeral=True)
        
        if await get_user_clan_id(db, member.id) != user_clan_id:
            return await interaction.followup.send("❌ Người này không ở trong Clan của bạn.", ephemeral=True)
        
        limits = {"comander": 10, "headcomander": 1, "leader": 10, "def_comander": 10, "recruiter": 10}
        count = (await (await db.execute("SELECT COUNT(*) FROM clan_members WHERE clan_id = ? AND role = ?", (user_clan_id, new_role))).fetchone())[0]
        if new_role in limits and count >= limits[new_role]:
            return await interaction.followup.send(f"❌ Role **{new_role}** đã đạt tối đa trong Clan này!", ephemeral=True)
        
        await db.execute("UPDATE clan_members SET role = ? WHERE discord_id = ?", (new_role, str(member.id)))
        await db.commit()
    await interaction.followup.send(f"✅ Đã gán Role **{new_role.upper()}** cho {member.mention}!")

@bot.tree.command(name="kick", description="Đuổi thành viên khỏi Clan")
async def kick_clan(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(bot.db_path) as db:
        user_clan_id = await get_user_clan_id(db, interaction.user.id)
        if not user_clan_id:
            return await interaction.followup.send("❌ Bạn không có Clan.", ephemeral=True)
        
        kicker_role = await get_member_role(db, interaction.user.id)
        target_role = await get_member_role(db, member.id)
        
        if not kicker_role or kicker_role == "member":
            return await interaction.followup.send("❌ Bạn không có quyền Kick người khác!", ephemeral=True)
        if not target_role:
            return await interaction.followup.send("❌ Người này không có trong Clan.", ephemeral=True)
        if await get_user_clan_id(db, member.id) != user_clan_id:
            return await interaction.followup.send("❌ Người này không ở trong Clan của bạn.", ephemeral=True)
        if kicker_role == target_role:
            return await interaction.followup.send("❌ Bạn không thể Kick người cùng Role!", ephemeral=True)
        
        if not await has_permission_to_kick(kicker_role, target_role):
            return await interaction.followup.send("❌ Bạn chỉ có thể Kick người có Role thấp hơn bạn!", ephemeral=True)
        
        await db.execute("DELETE FROM clan_members WHERE discord_id = ?", (str(member.id),))
        await db.commit()
    await interaction.followup.send(f"🗑️ Đã Kick {member.mention} khỏi Clan của bạn!")

@bot.tree.command(name="myrole", description="Xem Role của bạn trong Clan hiện tại")
async def myrole(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(bot.db_path) as db:
        role = await get_member_role(db, interaction.user.id)
        if not role:
            return await interaction.followup.send("❌ Bạn không thuộc Clan nào.", ephemeral=True)
        clan_id = await get_user_clan_id(db, interaction.user.id)
        clan_cursor = await db.execute("SELECT clan_name FROM clans WHERE clan_id = ?", (clan_id,))
        clan_name = (await clan_cursor.fetchone())[0]
    await interaction.followup.send(f"🏅 Clan: **{clan_name}** | Role của bạn: **{role.upper()}**", ephemeral=True)

@bot.tree.command(name="clan_rank", description="Xem bảng xếp hạng ELO trong Clan của bạn")
async def clan_rank(interaction: discord.Interaction):
    await interaction.response.defer()
    async with aiosqlite.connect(bot.db_path) as db:
        user_clan_id = await get_user_clan_id(db, interaction.user.id)
        if not user_clan_id:
            return await interaction.followup.send("❌ Bạn không thuộc Clan nào.", ephemeral=True)
        
        clan_cursor = await db.execute("SELECT clan_name FROM clans WHERE clan_id = ?", (user_clan_id,))
        clan_name = (await clan_cursor.fetchone())[0]
        
        rows = await (await db.execute('''
            SELECT p.discord_name, p.elo, p.kills, p.wins, p.losses, c.role 
            FROM players p 
            JOIN clan_members c ON p.discord_id = c.discord_id 
            WHERE c.clan_id = ? 
            ORDER BY p.elo DESC LIMIT 10
        ''', (user_clan_id,))).fetchall()
        
    if not rows:
        return await interaction.followup.send(f"Clan **{clan_name}** chưa có dữ liệu ELO.")
    
    msg = f"**🏆 Bảng Xếp Hạng Clan: {clan_name}**\n```"
    for i, row in enumerate(rows, 1):
        name, elo, kills, wins, losses, role = row
        msg += f"\n{i}. {name} [{role.upper()}]: {elo} ELO (W:{wins} L:{losses})"
    msg += "```"
    await interaction.followup.send(msg)

@bot.tree.command(name="rules", description="Xem nội quy của Clan bạn")
async def rules(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(bot.db_path) as db:
        user_clan_id = await get_user_clan_id(db, interaction.user.id)
        if not user_clan_id:
            return await interaction.followup.send("❌ Bạn không thuộc Clan nào.", ephemeral=True)
        
        rows = await (await db.execute("SELECT rule_text, created_by FROM clan_rules WHERE clan_id = ? ORDER BY id DESC LIMIT 5", (user_clan_id,))).fetchall()
        if not rows:
            return await interaction.followup.send("Clan này chưa có nội quy nào.")
        
        msg = "**📜 Nội quy Clan:**\n"
        for i, (rule, author) in enumerate(rows, 1):
            msg += f"\n{i}. {rule} (Tạo bởi: {author})"
    await interaction.followup.send(msg)

@bot.tree.command(name="add_rule", description="Thêm nội quy cho Clan (Chỉ Owner/Co-owner)")
async def add_rule(interaction: discord.Interaction, rule: str):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect(bot.db_path) as db:
        user_clan_id = await get_user_clan_id(db, interaction.user.id)
        if not user_clan_id:
            return await interaction.followup.send("❌ Bạn không thuộc Clan nào.", ephemeral=True)
        
        role = await get_member_role(db, interaction.user.id)
        if role not in ["owner", "co_owner"]:
            return await interaction.followup.send("❌ Chỉ Owner và Co-owner mới được thêm nội quy!", ephemeral=True)
        
        await db.execute("INSERT INTO clan_rules (clan_id, rule_text, created_by) VALUES (?, ?, ?)", (user_clan_id, rule, interaction.user.name))
        await db.commit()
    await interaction.followup.send(f"✅ Đã thêm nội quy: **{rule}**")

# --- LỆNH JOIN (ĐÃ TỐI ƯU) ---
@bot.tree.command(name="join", description="Tham gia hàng đợi ghép trận 1v1")
async def join_queue(interaction: discord.Interaction):
    async with aiosqlite.connect(bot.db_path) as db:
        await init_player(db, interaction.user)

    if interaction.user in bot.match_queue:
        return await interaction.response.send_message("❌ Bạn đã có trong hàng đợi!", ephemeral=True)
    
    bot.match_queue.append(interaction.user)
    await interaction.response.send_message(f"✅ {interaction.user.mention} đã vào hàng đợi! ({len(bot.match_queue)} người chờ)")

    if len(bot.match_queue) >= 2:
        p1 = bot.match_queue.pop(0)
        p2 = bot.match_queue.pop(0)
        
        category = None
        if CATEGORY_ID:
            category = interaction.guild.get_channel(CATEGORY_ID)
        else:
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                bot.user: discord.PermissionOverwrite(read_messages=True)
            }
            try:
                category = await interaction.guild.create_category("Trận ELO", overwrites=overwrites)
            except:
                category = None

        name = f"match-{random.randint(100, 999)}"
        try:
            txt = await interaction.guild.create_text_channel(name, category=category)
            voice = await interaction.guild.create_voice_channel(f"{name}-voice", category=category)
            
            await txt.send(f"⚔️ Trận đấu bắt đầu! {p1.mention} vs {p2.mention}\n📌 Đánh xong dùng `/report win` hoặc `/report lose`")
            
            bot.active_matches[txt.id] = {'text': txt, 'voice': voice, 'winner': None, 'loser': None}
            await interaction.followup.send(f"🎯 Đã tạo phòng: {txt.mention}", ephemeral=True)
        except Exception as e:
            bot.match_queue.insert(0, p2)
            bot.match_queue.insert(0, p1)
            await interaction.followup.send(f"❌ Lỗi tạo phòng: {e}", ephemeral=True)

@bot.tree.command(name="leave", description="Rời khỏi hàng đợi")
async def leave_queue(interaction: discord.Interaction):
    if interaction.user in bot.match_queue:
        bot.match_queue.remove(interaction.user)
        await interaction.response.send_message("✅ Đã rời hàng đợi.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Bạn không có trong hàng đợi.", ephemeral=True)

# --- LỆNH REPORT ---
@bot.tree.command(name="report", description="Báo cáo kết quả trận đấu (Dùng trong kênh match)")
async def report(interaction: discord.Interaction, result: str):
    if interaction.channel.id not in bot.active_matches:
        return await interaction.response.send_message("❌ Không phải kênh match!", ephemeral=True)
    
    data = bot.active_matches[interaction.channel.id]
    result = result.lower()
    if result not in ["win", "lose"]:
        return await interaction.response.send_message("❌ Chỉ chấp nhận `win` hoặc `lose`", ephemeral=True)
    
    if result == "win":
        if data['winner']:
            return await interaction.response.send_message("Đã có người báo win rồi", ephemeral=True)
        data['winner'] = interaction.user
    else:
        if data['loser']:
            return await interaction.response.send_message("Đã có người báo lose rồi", ephemeral=True)
        data['loser'] = interaction.user

    await interaction.response.send_message(f"✅ Đã ghi nhận. Chờ đối thủ xác nhận...")

    if data['winner'] and data['loser']:
        w = data['winner']
        l = data['loser']
        async with aiosqlite.connect(bot.db_path) as db:
            await db.execute("UPDATE players SET elo = elo + 15, wins = wins + 1 WHERE discord_id = ?", (str(w.id),))
            await db.execute("UPDATE players SET elo = elo - 15, losses = losses + 1 WHERE discord_id = ?", (str(l.id),))
            await db.commit()
        await interaction.channel.send(f"🏆 {w.mention} +15 ELO | 💀 {l.mention} -15 ELO")
        await asyncio.sleep(10)
        await data['text'].delete()
        await data['voice'].delete()
        del bot.active_matches[interaction.channel.id]

# --- LỆNH PROFILE ---
@bot.tree.command(name="profile", description="Xem thông tin của bạn")
async def profile(interaction: discord.Interaction, member: discord.Member = None):
    target = member if member else interaction.user
    await interaction.response.defer()
    async with aiosqlite.connect(bot.db_path) as db:
        await init_player(db, target)
        cur = await db.execute("SELECT elo, kills, wins, losses FROM players WHERE discord_id = ?", (str(target.id),))
        row = await cur.fetchone()
    e, k, w, l = row
    embed = discord.Embed(title=f"{target.display_name}", color=0xff5555)
    embed.add_field(name="🏆 ELO", value=str(e), inline=True)
    embed.add_field(name="⚔️ Kills", value=str(k), inline=True)
    embed.add_field(name="✅ Thắng", value=str(w), inline=True)
    embed.add_field(name="💀 Thua", value=str(l), inline=True)
    await interaction.followup.send(embed=embed)

# --- LỆNH RANK ---
@bot.tree.command(name="rank", description="Bảng xếp hạng ELO toàn cầu")
async def rank(interaction: discord.Interaction):
    await interaction.response.defer()
    async with aiosqlite.connect(bot.db_path) as db:
        cur = await db.execute("SELECT discord_name, elo FROM players ORDER BY elo DESC LIMIT 10")
        rows = await cur.fetchall()
    if not rows: return await interaction.followup.send("Chưa có dữ liệu.")
    msg = "**🏆 Top 10 ELO toàn cầu**\n```"
    for i, (n, e) in enumerate(rows, 1):
        msg += f"\n{i}. {n}: {e} ELO"
    msg += "```"
    await interaction.followup.send(msg)

# --- CHẠY BOT ---
def run_flask():
    app.run(host='0.0.0.0', port=10000)

if __name__ == "__main__":
    import threading
    threading.Thread(target=run_flask, daemon=True).start()
    bot.run(TOKEN)
