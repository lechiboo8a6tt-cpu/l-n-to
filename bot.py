import asyncio
import discord
from discord import app_commands
import aiosqlite
import matplotlib.pyplot as plt
import io
import random
import math
import os
import threading
from flask import Flask, request

TOKEN = os.getenv("TOKEN")
CLAN_NAME = "fxl"  # Sẽ xóa dòng này sau khi Deploy. Mình đã xóa bỏ xử lý Clan mặc định.

# ------------------- CẤU HÌNH CHUNG -------------------
CATEGORY_ID = None 

# ------------------- FLASK WEBHOOK (GIỮ LẠI ĐỂ DÙNG SAU) -------------------
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook_listener():
    data = request.json
    return "OK", 200

# ------------------- DISCORD BOT -------------------
class MyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.all()) 
        self.tree = app_commands.CommandTree(self)
        self.db_path = "elo_data.db"
        self.match_queue = [] # Hàng đợi chờ ghép trận
        self.active_matches = {} # Lưu thông tin trận đang đấu

    async def setup_hook(self):
        async with aiosqlite.connect(self.db_path) as db:
            # Bảng người chơi
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
            # Bảng Clan (Đã thay đổi: thêm owner_id để quản lý)
            await db.execute('''
                CREATE TABLE IF NOT EXISTS clans (
                    clan_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    clan_name TEXT UNIQUE,
                    owner_id TEXT
                )
            ''')
            # Bảng thành viên Clan (Liên kết clan_id)
            await db.execute('''
                CREATE TABLE IF NOT EXISTS clan_members (
                    discord_id TEXT PRIMARY KEY,
                    clan_id INTEGER,
                    role TEXT DEFAULT 'member',
                    FOREIGN KEY(clan_id) REFERENCES clans(clan_id)
                )
            ''')
            # Bảng nội quy (Liên kết clan_id)
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
            # Bảng lịch sử ELO
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
    print(f"Bot đã đăng nhập với tên: {bot.user}")

# ------------------- HÀM HỖ TRỢ -------------------
async def init_player_if_not_exists(db, member: discord.Member):
    cursor = await db.execute("SELECT elo FROM players WHERE discord_id = ?", (str(member.id),))
    row = await cursor.fetchone()
    if not row:
        await db.execute('''
            INSERT INTO players (discord_id, discord_name, elo, kills, wins, losses) 
            VALUES (?, ?, 1000, 0, 0, 0)
        ''', (str(member.id), member.name))
        await db.commit()

# Hàm lấy ID Clan của người dùng
async def get_user_clan_id(db, discord_id):
    cursor = await db.execute("SELECT clan_id FROM clan_members WHERE discord_id = ?", (str(discord_id),))
    row = await cursor.fetchone()
    return row[0] if row else None

# Hàm lấy Role của người dùng trong Clan
async def get_member_role(db, discord_id):
    cursor = await db.execute("SELECT role FROM clan_members WHERE discord_id = ?", (str(discord_id),))
    row = await cursor.fetchone()
    return row[0] if row else None

# Hàm kiểm tra quyền Kick
async def has_permission_to_kick(kicker_role, target_role):
    hierarchy = ["member", "recruiter", "def_comander", "leader", "headcomander", "comander", "co_owner", "owner"]
    if kicker_role not in hierarchy or target_role not in hierarchy:
        return False
    return hierarchy.index(kicker_role) > hierarchy.index(target_role)

# Tạo kênh trận đấu (Giữ nguyên)
async def create_match_channels(guild: discord.Guild, players, teams):
    category = None
    if CATEGORY_ID:
        category = guild.get_channel(CATEGORY_ID)
    else:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True)
        }
        category = await guild.create_category("Trận đấu ELO", overwrites=overwrites)
    
    channel_name = f"match-{random.randint(1000, 9999)}"
    text_channel = await guild.create_text_channel(channel_name, category=category)
    voice_channel = await guild.create_voice_channel(f"{channel_name}-voice", category=category)

    embed = discord.Embed(title="⚔️ Trận đấu sắp bắt đầu!", color=0xff5555)
    team1_mentions = " ".join([p.mention for p in teams[0]])
    team2_mentions = " ".join([p.mention for p in teams[1]])
    embed.add_field(name="Đội 1", value=team1_mentions, inline=True)
    embed.add_field(name="Đội 2", value=team2_mentions, inline=True)
    
    await text_channel.send(embed=embed)
    
    bot.active_matches[text_channel.id] = {
        'text_channel': text_channel,
        'voice_channel': voice_channel,
        'players': players,
        'teams': teams,
        'confirmed_winner': None,
        'confirmed_loser': None
    }
    return text_channel

# ------------------- LỆNH TẠO CLAN MỚI -------------------
@bot.tree.command(name="create_clan", description="Tạo Clan riêng cho bạn (Bạn sẽ là Owner)")
async def create_clan(interaction: discord.Interaction, clan_name: str):
    await interaction.response.defer()
    async with aiosqlite.connect(bot.db_path) as db:
        # Kiểm tra xem người dùng đã có clan chưa
        existing_clan = await get_user_clan_id(db, interaction.user.id)
        if existing_clan:
            return await interaction.followup.send("❌ Bạn đã có clan rồi! Không thể tạo thêm.", ephemeral=True)
        
        # Kiểm tra xem tên clan đã tồn tại chưa
        cursor = await db.execute("SELECT clan_id FROM clans WHERE clan_name = ?", (clan_name,))
        if await cursor.fetchone():
            return await interaction.followup.send("❌ Tên Clan này đã có người đặt rồi! Hãy chọn tên khác.", ephemeral=True)
        
        # Tạo Clan mới vào bảng `clans`
        await db.execute("INSERT INTO clans (clan_name, owner_id) VALUES (?, ?)", (clan_name, str(interaction.user.id)))
        # Lấy ID của clan vừa tạo
        cursor = await db.execute("SELECT clan_id FROM clans WHERE clan_name = ?", (clan_name,))
        new_clan_id = (await cursor.fetchone())[0]
        
        # Thêm người tạo vào bảng thành viên với role owner
        await db.execute("INSERT INTO clan_members (discord_id, clan_id, role) VALUES (?, ?, ?)", (str(interaction.user.id), new_clan_id, "owner"))
        await db.commit()
        
    await interaction.followup.send(f"✅ **Clan `{clan_name}` đã được tạo thành công!**\nBạn là **Owner** của clan này. Dùng `/invite` để mời bạn bè vào.")

# ------------------- LỆNH CLAN (Đã cập nhật logic lấy clan_id từ người dùng) -------------------
@bot.tree.command(name="invite", description="Mời người chơi vào Clan của bạn")
async def invite_clan(interaction: discord.Interaction, member: discord.Member):
    async with aiosqlite.connect(bot.db_path) as db:
        user_clan_id = await get_user_clan_id(db, interaction.user.id)
        if not user_clan_id:
            return await interaction.response.send_message("❌ Bạn không thuộc Clan nào.", ephemeral=True)
        
        role = await get_member_role(db, interaction.user.id)
        if role not in ["owner", "co_owner"]:
            return await interaction.response.send_message("❌ Chỉ Owner và Co-owner mới được mời người!", ephemeral=True)
        
        if await get_user_clan_id(db, member.id):
            return await interaction.response.send_message(f"❌ {member.mention} đã ở trong Clan khác rồi.", ephemeral=True)
        
        await db.execute("INSERT INTO clan_members (discord_id, clan_id, role) VALUES (?, ?, ?)", (str(member.id), user_clan_id, "member"))
        await db.commit()
        await interaction.response.send_message(f"✅ Đã mời {member.mention} vào Clan của bạn với role **Member**!")

@bot.tree.command(name="add_role", description="Thêm Role cho thành viên (Chỉ Owner)")
async def add_role(interaction: discord.Interaction, member: discord.Member, new_role: str):
    allowed_roles = ["co_owner", "comander", "headcomander", "leader", "def_comander", "recruiter"]
    if new_role.lower() not in allowed_roles:
        return await interaction.response.send_message(f"❌ Role không hợp lệ!", ephemeral=True)
    
    async with aiosqlite.connect(bot.db_path) as db:
        user_clan_id = await get_user_clan_id(db, interaction.user.id)
        if not user_clan_id:
            return await interaction.response.send_message("❌ Bạn không có Clan.", ephemeral=True)
        
        role = await get_member_role(db, interaction.user.id)
        if role != "owner":
            return await interaction.response.send_message("❌ Chỉ Owner mới được thêm Role!", ephemeral=True)
        
        if await get_user_clan_id(db, member.id) != user_clan_id:
            return await interaction.response.send_message("❌ Người này không ở trong Clan của bạn.", ephemeral=True)
        
        limits = {"comander": 10, "headcomander": 1, "leader": 10, "def_comander": 10, "recruiter": 10}
        count = (await (await db.execute("SELECT COUNT(*) FROM clan_members WHERE clan_id = ? AND role = ?", (user_clan_id, new_role))).fetchone())[0]
        if new_role in limits and count >= limits[new_role]:
            return await interaction.response.send_message(f"❌ Role **{new_role}** đã đạt tối đa trong Clan này!", ephemeral=True)
        
        await db.execute("UPDATE clan_members SET role = ? WHERE discord_id = ?", (new_role, str(member.id)))
        await db.commit()
        await interaction.response.send_message(f"✅ Đã gán Role **{new_role.upper()}** cho {member.mention}!")

@bot.tree.command(name="kick", description="Đuổi thành viên khỏi Clan")
async def kick_clan(interaction: discord.Interaction, member: discord.Member):
    async with aiosqlite.connect(bot.db_path) as db:
        user_clan_id = await get_user_clan_id(db, interaction.user.id)
        if not user_clan_id:
            return await interaction.response.send_message("❌ Bạn không có Clan.", ephemeral=True)
        
        kicker_role = await get_member_role(db, interaction.user.id)
        target_role = await get_member_role(db, member.id)
        
        if not kicker_role or kicker_role == "member":
            return await interaction.response.send_message("❌ Bạn không có quyền Kick người khác!", ephemeral=True)
        if not target_role:
            return await interaction.response.send_message("❌ Người này không có trong Clan.", ephemeral=True)
        if await get_user_clan_id(db, member.id) != user_clan_id:
            return await interaction.response.send_message("❌ Người này không ở trong Clan của bạn.", ephemeral=True)
        if kicker_role == target_role:
            return await interaction.response.send_message("❌ Bạn không thể Kick người cùng Role!", ephemeral=True)
        
        if not await has_permission_to_kick(kicker_role, target_role):
            return await interaction.response.send_message("❌ Bạn chỉ có thể Kick người có Role thấp hơn bạn!", ephemeral=True)
        
        await db.execute("DELETE FROM clan_members WHERE discord_id = ?", (str(member.id),))
        await db.commit()
        await interaction.response.send_message(f"🗑️ Đã Kick {member.mention} khỏi Clan của bạn!")

@bot.tree.command(name="myrole", description="Xem Role của bạn trong Clan hiện tại")
async def myrole(interaction: discord.Interaction):
    async with aiosqlite.connect(bot.db_path) as db:
        role = await get_member_role(db, interaction.user.id)
        if not role:
            return await interaction.response.send_message("❌ Bạn không thuộc Clan nào.", ephemeral=True)
        clan_id = await get_user_clan_id(db, interaction.user.id)
        clan_cursor = await db.execute("SELECT clan_name FROM clans WHERE clan_id = ?", (clan_id,))
        clan_name = (await clan_cursor.fetchone())[0]
        await interaction.response.send_message(f"🏅 Clan: **{clan_name}** | Role của bạn: **{role.upper()}**", ephemeral=True)

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
    async with aiosqlite.connect(bot.db_path) as db:
        user_clan_id = await get_user_clan_id(db, interaction.user.id)
        if not user_clan_id:
            return await interaction.response.send_message("❌ Bạn không thuộc Clan nào.", ephemeral=True)
        
        rows = await (await db.execute("SELECT rule_text, created_by FROM clan_rules WHERE clan_id = ? ORDER BY id DESC LIMIT 5", (user_clan_id,))).fetchall()
        if not rows:
            return await interaction.response.send_message("Clan này chưa có nội quy nào.")
        
        msg = "**📜 Nội quy Clan:**\n"
        for i, (rule, author) in enumerate(rows, 1):
            msg += f"\n{i}. {rule} (Tạo bởi: {author})"
        await interaction.response.send_message(msg)

@bot.tree.command(name="add_rule", description="Thêm nội quy cho Clan (Chỉ Owner/Co-owner)")
async def add_rule(interaction: discord.Interaction, rule: str):
    async with aiosqlite.connect(bot.db_path) as db:
        user_clan_id = await get_user_clan_id(db, interaction.user.id)
        if not user_clan_id:
            return await interaction.response.send_message("❌ Bạn không thuộc Clan nào.", ephemeral=True)
        
        role = await get_member_role(db, interaction.user.id)
        if role not in ["owner", "co_owner"]:
            return await interaction.response.send_message("❌ Chỉ Owner và Co-owner mới được thêm nội quy!", ephemeral=True)
        
        await db.execute("INSERT INTO clan_rules (clan_id, rule_text, created_by) VALUES (?, ?, ?)", (user_clan_id, rule, interaction.user.name))
        await db.commit()
        await interaction.response.send_message(f"✅ Đã thêm nội quy: **{rule}**")

# ------------------- LỆNH QUEUE & MATCH -------------------
@bot.tree.command(name="join", description="Tham gia hàng đợi ghép trận 1v1")
async def join_queue(interaction: discord.Interaction):
    await init_player_if_not_exists(None, interaction.user)
    if interaction.user in bot.match_queue:
        return await interaction.response.send_message("❌ Bạn đã có trong hàng đợi!", ephemeral=True)
    
    bot.match_queue.append(interaction.user)
    await interaction.response.send_message(f"✅ {interaction.user.mention} đã vào hàng đợi! ({len(bot.match_queue)} người chờ)")

    if len(bot.match_queue) >= 2:
        player1 = bot.match_queue.pop(0)
        player2 = bot.match_queue.pop(0)
        teams = [[player1], [player2]]
        channel = await create_match_channels(interaction.guild, [player1, player2], teams)
        await channel.send(f"Trận đấu bắt đầu! {player1.mention} vs {player2.mention}")

@bot.tree.command(name="leave", description="Rời khỏi hàng đợi")
async def leave_queue(interaction: discord.Interaction):
    if interaction.user in bot.match_queue:
        bot.match_queue.remove(interaction.user)
        await interaction.response.send_message("✅ Đã rời hàng đợi.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Bạn không có trong hàng đợi.", ephemeral=True)

# ------------------- LỆNH BÁO CÁO KẾT QUẢ TRẬN ĐẤU -------------------
@bot.tree.command(name="profile", description="Xem thông tin của bạn")
async def profile(interaction: discord.Interaction, member: discord.Member = None):
    target = member if member else interaction.user
    await interaction.response.defer()
    async with aiosqlite.connect(bot.db_path) as db:
        await init_player_if_not_exists(db, target)
        cursor = await db.execute("SELECT elo, kills, wins, losses FROM players WHERE discord_id = ?", (str(target.id),))
        row = await cursor.fetchone()
        elo, kills, wins, losses = row
        embed = discord.Embed(title=f"{target.display_name}'s Stats", color=0xff5555)
        embed.add_field(name="🏆 ELO", value=f"**{elo}**", inline=True)
        embed.add_field(name="⚔️ Kills", value=str(kills), inline=True)
        embed.add_field(name="✅ Thắng", value=str(wins), inline=True)
        embed.add_field(name="💀 Thua", value=str(losses), inline=True)
        await interaction.followup.send(embed=embed)

@bot.tree.command(name="rank", description="Bảng xếp hạng ELO toàn cầu")
async def rank(interaction: discord.Interaction):
    await interaction.response.defer()
    async with aiosqlite.connect(bot.db_path) as db:
        cursor = await db.execute("SELECT discord_name, elo, wins, losses FROM players ORDER BY elo DESC LIMIT 10")
        rows = await cursor.fetchall()
    if not rows: return await interaction.followup.send("Chưa có dữ liệu.")
    msg = "**🏆 Top 10 ELO toàn cầu**\n```"
    for i, (name, elo, w, l) in enumerate(rows, 1):
        msg += f"\n{i}. {name}: {elo} ELO (W:{w} L:{l})"
    msg += "```"
    await interaction.followup.send(msg)

# ------------------- CHẠY BOT -------------------
def run_flask():
    app.run(host='0.0.0.0', port=10000)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    bot.run(TOKEN)
