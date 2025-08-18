import os
import asyncio
import random
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks
from discord import app_commands

# ====== 設定 ======
TOKEN = os.getenv("DISCORD_TOKEN")  # 必須: Railway/Render等の環境変数に設定
GUILD_ID = 1398607685158440991      # ★即時反映したいギルドIDを入れてください
MATCH_CATEGORY_ID = 1403371745301495848  # ★固定カテゴリ（プライベートVCを作る先）

READY_DELAY_SECONDS = 60
MATCH_LOOP_INTERVAL = 10

JST = timezone(timedelta(hours=9))

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# guild_id -> { user_id: ready_at(datetime, JST) }
match_queues: dict[int, dict[int, datetime]] = {}

def vc_name_for(u1: discord.Member, u2: discord.Member) -> str:
    n1 = u1.display_name[:12]
    n2 = u2.display_name[:12]
    return f"Match: {n1} & {n2}"

class MatchView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="ランダムマッチングに参加",
        style=discord.ButtonStyle.primary,
        custom_id="random_match_join",
        emoji="🎲"
    )
    async def join_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user = interaction.user

        if guild is None or not isinstance(user, discord.Member):
            await interaction.response.send_message("ギルド内で実行してください。", ephemeral=True)
            return

        q = match_queues.setdefault(guild.id, {})

        if user.id in q:
            ready_at = q[user.id]
            remaining = (ready_at - datetime.now(JST)).total_seconds()
            if remaining > 0:
                await interaction.response.send_message(
                    f"すでに登録済みです。マッチング開始まで残り **{int(remaining)}秒**",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "すでに登録済みです。まもなくマッチング処理にかかります。",
                    ephemeral=True
                )
            return

        ready_at = datetime.now(JST) + timedelta(seconds=READY_DELAY_SECONDS)
        q[user.id] = ready_at
        await interaction.response.send_message(
            f"✅ 参加を受け付けました！ **{READY_DELAY_SECONDS}秒後** にマッチングを開始します。\n"
            "人数が奇数の場合は、次のラウンドへ持ち越されます。",
            ephemeral=True
        )

@tasks.loop(seconds=MATCH_LOOP_INTERVAL)
async def match_loop():
    now = datetime.now(JST)

    for guild_id, q in list(match_queues.items()):
        if not q:
            continue

        ready_users = [uid for uid, t in q.items() if t <= now]
        if len(ready_users) < 2:
            continue

        random.shuffle(ready_users)

        pairs = []
        while len(ready_users) >= 2:
            u1 = ready_users.pop()
            u2 = ready_users.pop()
            pairs.append((u1, u2))

        guild = bot.get_guild(guild_id)
        if guild is None:
            continue

        category = guild.get_channel(MATCH_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            continue

        for uid1, uid2 in pairs:
            q.pop(uid1, None)
            q.pop(uid2, None)

            m1 = guild.get_member(uid1)
            m2 = guild.get_member(uid2)
            if not m1 or not m2:
                continue

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
                m1: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, stream=True),
                m2: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, stream=True),
            }

            try:
                vc = await guild.create_voice_channel(
                    name=vc_name_for(m1, m2),
                    category=category,
                    overwrites=overwrites,
                    reason="ランダムマッチング"
                )
            except Exception as e:
                for m in (m1, m2):
                    try:
                        await m.send(f"❌ VC作成に失敗しました。管理者にお問い合わせください。\n```\n{e}\n```")
                    except:
                        pass
                continue

            for m in (m1, m2):
                try:
                    await m.send(f"✅ マッチングが成立しました！ ボイスチャットへどうぞ → {vc.mention}")
                except:
                    pass

@match_loop.before_loop
async def before_loop():
    await bot.wait_until_ready()

@tree.command(name="post_panel", description="ランダムマッチング用の参加ボタンを送信します。")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def post_panel(interaction: discord.Interaction):
    view = MatchView()
    await interaction.response.send_message(
        "🎲 **ランダムマッチング**\nボタンを押して1分後にランダムでマッチングします！",
        view=view
    )

@tree.command(name="cancel_match", description="自分のキュー登録を取り消します。")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def cancel_match(interaction: discord.Interaction):
    guild = interaction.guild
    user = interaction.user
    if guild is None:
        await interaction.response.send_message("ギルド内で実行してください。", ephemeral=True)
        return

    q = match_queues.setdefault(guild.id, {})
    if q.pop(user.id, None) is not None:
        await interaction.response.send_message("🟡 キュー登録を取り消しました。", ephemeral=True)
    else:
        await interaction.response.send_message("キューに登録されていません。", ephemeral=True)

@tree.command(name="queue_status", description="現在のキュー人数を表示します。")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def queue_status(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("ギルド内で実行してください。", ephemeral=True)
        return

    q = match_queues.setdefault(guild.id, {})
    now = datetime.now(JST)
    ready = sum(1 for t in q.values() if t <= now)
    waiting = sum(1 for t in q.values() if t > now)
    await interaction.response.send_message(
        f"📊 現在のキュー: **{len(q)}人**（準備完了: **{ready}** / 待機中: **{waiting}**）",
        ephemeral=True
    )

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)

    if not match_loop.is_running():
        match_loop.start()

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("環境変数 DISCORD_TOKEN が設定されていません。")
    bot.run(TOKEN)
