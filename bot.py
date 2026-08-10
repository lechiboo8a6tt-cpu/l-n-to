import asyncio
import discord
from discord import app_commands
import aiosqlite
import matplotlib.pyplot as plt
import io
import random
import math
import threading
from flask import Flask, request

TOKEN = "PUT_ON"
CLAN_NAME = "fxl"

match_queue = []

# ---------------- FLASK WEBHOOK (NHẬN DỮ LIỆU TỪ NEATQUEUE) ----------------
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook_listener():
    data = request.json
    if not data:
        return "No data", 400

    winner = data.get('winner')
    loser = data.get('loser')
    kills_winner = data.get('winner_kills', 0)
    kills_loser = data.get('loser_kills', 0)

    if winner and loser:
        asyncio.run(process_auto_match(winner, loser, kills_winner, kills_loser))
    return "OK", 200

async def process_auto_match(winner_game, loser_game, winner_kills, loser_kills):
    async with aiosqlite.connect("elo_data.db") as db:
        w_cursor = await db.execute("SELECT discord_id FROM players WHERE game_name = ?", (winner_game,))
        w_row = await w_cursor.fetchone()
        l_cursor = await db.execute("SELECT discord_id FROM players WHERE game_name = ?", (loser_game,))
        l_row = await l_cursor.fetchone()

        if not w_row or not l_row:
            print(f"⚠️ Không tìm thấy tên game: {winner_game} hoặc {loser_game}")
            return

        winner_id, loser_id = w_row[0], l_row[0]

        diff = winner_kills - loser_kills
        elo_gain = 10 + diff
        elo_loss = 10 + diff

        await db.execute('''
            UPDATE players 
            SET elo = elo + ?, kills = kills + ?, wins = wins + 1 
            WHERE discord_id = ?
        ''', (elo_gain, winner_kills, winner_id))
        await db.execute('''
            UPDATE players 
            SET elo = elo - ?, losses = losses + 1 
            WHERE discord_id = ?
        ''', (elo_loss, loser_id))

        await db.execute("INSERT INTO elo_history (discord_id, elo_change, reason) VALUES (?, ?, ?)", 
                         (winner_id, elo_gain, f"Auto Win ({winner_kills}-{loser_kills})"))
        await db.execute("INSERT INTO elo_history (discord_id, elo_change, reason) VALUES (?, ?, ?)", 
                         (loser_id, -elo_loss, f"Auto Lose ({winner_kills}-{loser_kills})"))
        
        await db.commit()
        print(f"✅ Tự động: {winner_game} +{elo_gain}, {loser_game} -{elo_loss}")

# ---------------- DISCORD BOT ----------------
class MyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.db_path = "elo_data.db"

    async def setup_hook(self):
        async with aiosqlite.connect(self.db_path) as db:
            # Bảng players (thêm game_name cho NeatQueue)
            await db.execute('''
                CREATE TABLE IF NOT EXISTS players (
                    discord_id TEXT PRIMARY KEY,
                    discord_name TEXT,
                    game_name TEXT,
                    elo INTEGER DEFAULT 1000,
                    kills INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    clan_role TEXT DEFAULT 'member'
                )
            ''')
            # Bảng ELO history
            await db.execute('''
                CREATE TABLE IF NOT EXISTS elo_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_id TEXT,
                    elo_change INTEGER,
                    reason TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            # Bảng Clan stats
            await db.execute('''
                CREATE TABLE IF NOT EXISTS clan_stats (
                    clan_name TEXT PRIMARY KEY,
                    wars_won INTEGER DEFAULT 0,
                    total_clan_elo INTEGER DEFAULT 0
                )
            ''')
            # Bảng Clan members
            await db.execute('''
                CREATE TABLE IF NOT EXISTS clan_members (
                    discord_id TEXT PRIMARY KEY,
                    name TEXT,
                    role TEXT DEFAULT 'member'
                )
            ''')
            # Bảng Clan rules
            await db.execute('''
                CREATE TABLE IF NOT EXISTS clan_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_text TEXT,
                    created_by TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            await db.execute("INSERT OR IGNORE INTO clan_stats (clan_name, wars_won, total_clan_elo) VALUES (?, 0, 0)", (CLAN_NAME,))
            await db.commit()
        await self.tree.sync()

bot = MyBot()

@bot.event
async def on_ready():
    print(f"Bot đã đăng nhập với tên: {bot.user}")

# ---------------- HÀM HỖ TRỢ ----------------
async def get_member_role(db, discord_id):
    cursor = await db.execute("SELECT role FROM clan_members WHERE discord_id = ?", (str(discord_id),))
    row = await cursor.fetchone()
    return row[0] if row else None

async def has_permission_to_kick(kicker_role, target_role):
    hierarchy = ["member", "recruiter", "def_comander", "leader", "headcomander", "comander", "co_owner", "owner"]
    if kicker_role not in hierarchy or target_role not in hierarchy:
        return False
    return hierarchy.index(kicker_role) > hierarchy.index(target_role)

async def create_graph_image(discord_id):
    async with aiosqlite.connect(bot.db_path) as db:
        cursor = await db.execute('''
            SELECT elo_change, timestamp FROM elo_history 
            WHERE discord_id = ? ORDER BY timestamp ASC LIMIT 10
        ''', (discord_id,))
        rows = await cursor.fetchall()
        
    if not rows:
        return None
    
    elo_values = [0]
    times = [0]
    current_elo = 0
    for change, ts in rows:
        current_elo += change
        elo_values.append(current_elo)
        times.append(len(times))

    plt.figure(figsize=(8, 3), facecolor='#2f3136')
    plt.plot(times, elo_values, color='#ff5555', linewidth=2)
    plt.fill_between(times, elo_values, color='#ff5555', alpha=0.1)
    plt.xlabel('Số trận gần nhất', color='#b9bbbe')
    plt.ylabel('ELO', color='#b9bbbe')
    plt.tick_params(axis='x', colors='#b9bbbe')
    plt.tick_params(axis='y', colors='#b9bbbe')
    plt.grid(color='#40444b', linestyle='--', linewidth=0.5)
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', transparent=True, bbox_inches='tight')
    buf.seek(0)
    plt.close()
    return discord.File(buf, filename="graph.png")

# ---------------- LỆNH ELO & QUEUE ----------------
@bot.tree.command(name="profile", description="Xem thông tin chi tiết của người chơi")
async def profile(interaction: discord.Interaction, member: discord.Member = None):
    target = member if member else interaction.user
    await interaction.response.defer()
    async with aiosqlite.connect(bot.db_path) as db:
        cursor = await db.execute("SELECT elo, kills, wins, losses, game_name FROM players WHERE discord_id = ?", (str(target.id),))
        row = await cursor.fetchone()
        if not row:
            return await interaction.followup.send("❌ Người này chưa có dữ liệu.")
        elo, kills, wins, losses, game_name = row
        embed = discord.Embed(title=f"{target.display_name}'s Stats", color=0xff5555)
        embed.add_field(name="🏆 ELO", value=f"**{elo}**", inline=True)
        embed.add_field(name="⚔️ Kills", value=str(kills), inline=True)
        embed.add_field(name="✅ Thắng", value=str(wins), inline=True)
        embed.add_field(name="💀 Thua", value=str(losses), inline=True)
        embed.add_field(name="🎮 Game", value=game_name if game_name else "Chưa link", inline=True)
        
        graph_file = await create_graph_image(str(target.id))
        if graph_file:
            embed.set_image(url="attachment://graph.png")
            await interaction.followup.send(embed=embed, file=graph_file)
        else:
            await interaction.followup.send(embed=embed)

@bot.tree.command(name="rank", description="Xem bảng xếp hạng ELO")
async def rank(interaction: discord.Interaction):
    await interaction.response.defer()
    async with aiosqlite.connect(bot.db_path) as db:
        cursor = await db.execute("SELECT discord_name, elo, wins, losses, kills FROM players ORDER BY elo DESC LIMIT 10")
        rows = await cursor.fetchall()
    if not rows:
        return await interaction.followup.send("Chưa có dữ liệu.")
    msg = "**🏆 Bảng Xếp Hạng ELO**\n```"
    for i, (name, elo, wins, losses, kills) in enumerate(rows, 1):
        msg += f"\n{i}. {name}: {elo} ELO (W:{wins} L:{losses} K:{kills})"
    msg += "```"
    await interaction.followup.send(msg)

@bot.tree.command(name="war", description="Nhập kết quả War (Admin)")
async def war(interaction: discord.Interaction, result: str, player1: discord.Member, kills1: int, player2: discord.Member = None, kills2: int = 0):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Không có quyền!", ephemeral=True)
    result = result.lower()
    if result not in ["win", "lose"]:
        return await interaction.response.send_message("❌ Chỉ win hoặc lose", ephemeral=True)
    await interaction.response.defer()
    async with aiosqlite.connect(bot.db_path) as db:
        players = [(player1, kills1)]
        if player2: players.append((player2, kills2))
        for p, k in players:
            await db.execute('''
                INSERT INTO players (discord_id, discord_name, elo, kills) VALUES (?, ?, ?, ?) 
                ON CONFLICT(discord_id) DO UPDATE SET elo = elo + ?, kills = kills + ?
            ''', (str(p.id), p.name, k, k, k, k))
            await db.execute("INSERT INTO elo_history (discord_id, elo_change, reason) VALUES (?, ?, ?)", (str(p.id), k, f"War {result.upper()}"))
        if result == "win":
            await db.execute("UPDATE clan_stats SET wars_won = wars_won + 1, total_clan_elo = total_clan_elo + 50 WHERE clan_name = ?", (CLAN_NAME,))
        else:
            await db.execute("UPDATE clan_stats SET total_clan_elo = total_clan_elo - 30 WHERE clan_name = ?", (CLAN_NAME,))
        await db.commit()
    await interaction.followup.send(f"✅ Đã cập nhật War! Kết quả: **{result.upper()}**")

@bot.tree.command(name="1v1", description="Báo cáo kết quả 1v1 (Tỷ số 5)")
async def duel(interaction: discord.Interaction, winner: discord.Member, loser: discord.Member, winner_kills: int, loser_kills: int):
    if winner == loser: return await interaction.response.send_message("❌ Không thể tự đấu!", ephemeral=True)
    if winner_kills < 5: return await interaction.response.send_message("❌ Người thắng phải đạt ít nhất 5 kill!", ephemeral=True)
    await interaction.response.defer()
    async with aiosqlite.connect(bot.db_path) as db:
        diff = winner_kills - loser_kills
        elo_gain = 10 + diff
        elo_loss = 10 + diff
        await db.execute('''
            INSERT INTO players (discord_id, discord_name, elo, kills, wins) VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(discord_id) DO UPDATE SET elo = elo + ?, kills = kills + ?, wins = wins + 1
        ''', (str(winner.id), winner.name, elo_gain, winner_kills, elo_gain, winner_kills))
        await db.execute("INSERT INTO elo_history (discord_id, elo_change, reason) VALUES (?, ?, ?)", (str(winner.id), elo_gain, f"1v1 Win ({winner_kills}-{loser_kills})"))
        await db.execute('''
            INSERT INTO players (discord_id, discord_name, elo, losses) VALUES (?, ?, ?, 1)
            ON CONFLICT(discord_id) DO UPDATE SET elo = elo - ?, losses = losses + 1
        ''', (str(loser.id), loser.name, elo_loss))
        await db.execute("INSERT INTO elo_history (discord_id, elo_change, reason) VALUES (?, ?, ?)", (str(loser.id), -elo_loss, f"1v1 Lose ({winner_kills}-{loser_kills})"))
        await db.commit()
    await interaction.followup.send(f"⚔️ Kết quả 1v1: {winner.mention} thắng (+{elo_gain}), {loser.mention} thua (-{elo_loss})")

@bot.tree.command(name="join", description="Tham gia hàng đợi")
async def join_queue(interaction: discord.Interaction):
    if interaction.user in match_queue: return await interaction.response.send_message("❌ Đã có trong hàng đợi!", ephemeral=True)
    match_queue.append(interaction.user)
    await interaction.response.send_message(f"✅ {interaction.user.mention} đã tham gia! ({len(match_queue)} người chờ)")

@bot.tree.command(name="leave", description="Rời khỏi hàng đợi")
async def leave_queue(interaction: discord.Interaction):
    if interaction.user in match_queue:
        match_queue.remove(interaction.user)
        await interaction.response.send_message("✅ Đã rời hàng đợi.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Bạn không có trong hàng đợi.", ephemeral=True)

@bot.tree.command(name="match", description="(Admin) Ghép 2 người ngẫu nhiên")
async def match_random(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator: return await interaction.response.send_message("❌ Chỉ Admin!", ephemeral=True)
    if len(match_queue) < 2: return await interaction.response.send_message("❌ Cần ít nhất 2 người.", ephemeral=True)
    p1 = random.choice(match_queue); match_queue.remove(p1)
    p2 = random.choice(match_queue); match_queue.remove(p2)
    await interaction.response.send_message(f"🎲 {p1.mention} vs {p2.mention}! Dùng /1v1 báo kết quả.")

# ---------------- LỆNH CLAN & ROLE ----------------
@bot.tree.command(name="link", description="Liên kết tên Discord với tên trong Blockman Go")
async def link_name(interaction: discord.Interaction, game_name: str):
    async with aiosqlite.connect(bot.db_path) as db:
        cursor = await db.execute("SELECT discord_id FROM players WHERE game_name = ?", (game_name,))
        if await cursor.fetchone():
            return await interaction.response.send_message("❌ Tên game này đã được người khác đăng ký!", ephemeral=True)
        await db.execute('''
            INSERT INTO players (discord_id, discord_name, game_name, elo, kills, wins, losses) 
            VALUES (?, ?, ?, 1000, 0, 0, 0) 
            ON CONFLICT(discord_id) DO UPDATE SET game_name = excluded.game_name
        ''', (str(interaction.user.id), interaction.user.name, game_name))
        await db.commit()
        await interaction.response.send_message(f"✅ Đã liên kết tên game **{game_name}** với Discord của bạn!")

@bot.tree.command(name="invite", description="Mời người chơi vào Clan (Owner/Co-owner)")
async def invite_clan(interaction: discord.Interaction, member: discord.Member):
    async with aiosqlite.connect(bot.db_path) as db:
        role = await get_member_role(db, interaction.user.id)
        if role not in ["owner", "co_owner"]:
            return await interaction.response.send_message("❌ Chỉ Owner và Co-owner mới có quyền mời!", ephemeral=True)
        if await get_member_role(db, member.id):
            return await interaction.response.send_message(f"❌ {member.mention} đã ở trong Clan rồi!", ephemeral=True)
        await db.execute("INSERT INTO clan_members (discord_id, name, role) VALUES (?, ?, ?)", (str(member.id), member.name, "member"))
        await db.commit()
        await interaction.response.send_message(f"✅ Đã mời {member.mention} vào Clan với role **Member**!")

@bot.tree.command(name="add_role", description="Thêm Role cho thành viên (Chỉ Owner)")
async def add_role(interaction: discord.Interaction, member: discord.Member, new_role: str):
    allowed = ["owner", "co_owner", "comander", "headcomander", "leader", "def_comander", "recruiter"]
    if new_role.lower() not in allowed:
        return await interaction.response.send_message(f"❌ Role không hợp lệ!", ephemeral=True)
    async with aiosqlite.connect(bot.db_path) as db:
        if await get_member_role(db, interaction.user.id) != "owner":
            return await interaction.response.send_message("❌ Chỉ Owner mới có quyền dùng lệnh này!", ephemeral=True)
        limits = {"comander": 10, "headcomander": 1, "leader": 10, "def_comander": 10, "recruiter": 10}
        count = (await (await db.execute("SELECT COUNT(*) FROM clan_members WHERE role = ?", (new_role,))).fetchone())[0]
        if new_role in limits and count >= limits[new_role]:
            return await interaction.response.send_message(f"❌ Role **{new_role}** đã đạt tối đa!", ephemeral=True)
        await db.execute("UPDATE clan_members SET role = ? WHERE discord_id = ?", (new_role, str(member.id)))
        await db.commit()
        await interaction.response.send_message(f"✅ Đã gán Role **{new_role.upper()}** cho {member.mention}!")

@bot.tree.command(name="kick", description="Đuổi thành viên khỏi Clan")
async def kick_clan(interaction: discord.Interaction, member: discord.Member):
    async with aiosqlite.connect(bot.db_path) as db:
        kicker = await get_member_role(db, interaction.user.id)
        target = await get_member_role(db, member.id)
        if not kicker or kicker == "member": return await interaction.response.send_message("❌ Bạn không có quyền Kick!", ephemeral=True)
        if not target: return await interaction.response.send_message("❌ Người này không có trong Clan.", ephemeral=True)
        if kicker == target: return await interaction.response.send_message("❌ Không thể Kick người cùng Role!", ephemeral=True)
        if not has_permission_to_kick(kicker, target):
            return await interaction.response.send_message("❌ Bạn chỉ có thể Kick người Role thấp hơn!", ephemeral=True)
        await db.execute("DELETE FROM clan_members WHERE discord_id = ?", (str(member.id),))
        await db.commit()
        await interaction.response.send_message(f"🗑️ Đã Kick {member.mention} khỏi Clan!")

@bot.tree.command(name="myrole", description="Xem Role của bạn trong Clan")
async def myrole(interaction: discord.Interaction):
    async with aiosqlite.connect(bot.db_path) as db:
        role = await get_member_role(db, interaction.user.id)
        if not role: return await interaction.response.send_message("❌ Bạn chưa có trong Clan.", ephemeral=True)
        await interaction.response.send_message(f"🏅 Role của bạn: **{role.upper()}**", ephemeral=True)

@bot.tree.command(name="clan_rank", description="Xem bảng xếp hạng ELO trong Clan")
async def clan_rank(interaction: discord.Interaction):
    await interaction.response.defer()
    async with aiosqlite.connect(bot.db_path) as db:
        rows = await (await db.execute('''
            SELECT p.discord_name, p.elo, p.kills, p.wins, p.losses, c.role 
            FROM players p JOIN clan_members c ON p.discord_id = c.discord_id ORDER BY p.elo DESC LIMIT 10
        ''')).fetchall()
    if not rows: return await interaction.followup.send("Clan chưa có dữ liệu.")
    msg = "**🏆 Bảng Xếp Hạng Clan**\n```"
    for i, row in enumerate(rows, 1):
        name, elo, kills, wins, losses, role = row
        msg += f"\n{i}. {name} [{role.upper()}]: {elo} ELO (W:{wins} L:{losses})"
    msg += "```"
    await interaction.followup.send(msg)

@bot.tree.command(name="rules", description="Xem nội quy Clan")
async def rules(interaction: discord.Interaction):
    async with aiosqlite.connect(bot.db_path) as db:
        rows = await (await db.execute("SELECT rule_text, created_by FROM clan_rules ORDER BY id DESC LIMIT 5")).fetchall()
        if not rows: return await interaction.response.send_message("Clan chưa có nội quy.")
        msg = "**📜 Nội quy Clan:**\n"
        for i, (rule, author) in enumerate(rows, 1):
            msg += f"\n{i}. {rule} (Tạo bởi: {author})"
        await interaction.response.send_message(msg)

@bot.tree.command(name="add_rule", description="Thêm nội quy (Owner/Co-owner)")
async def add_rule(interaction: discord.Interaction, rule: str):
    async with aiosqlite.connect(bot.db_path) as db:
        role = await get_member_role(db, interaction.user.id)
        if role not in ["owner", "co_owner"]:
            return await interaction.response.send_message("❌ Chỉ Owner và Co-owner mới được thêm nội quy!", ephemeral=True)
        await db.execute("INSERT INTO clan_rules (rule_text, created_by) VALUES (?, ?)", (rule, interaction.user.name))
        await db.commit()
        await interaction.response.send_message(f"✅ Đã thêm nội quy: **{rule}**")

@bot.tree.command(name="clan", description="Xem thống kê Clan fxl")
async def clan(interaction: discord.Interaction):
    async with aiosqlite.connect(bot.db_path) as db:
        row = await (await db.execute("SELECT wars_won, total_clan_elo FROM clan_stats WHERE clan_name = ?", (CLAN_NAME,))).fetchone()
        if not row: return await interaction.response.send_message("Chưa có dữ liệu.")
        wins, elo = row
        await interaction.response.send_message(f"**📊 {CLAN_NAME}**\nWars Won: {wins}\nClan ELO: {elo}")

# ---------------- CHẠY BOT (2 LUỒNG) ----------------
def run_flask():
    app.run(host='0.0.0.0', port=10000)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    bot.run(TOKEN)
