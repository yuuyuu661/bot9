# bot.py
import os
import asyncio
import random
from datetime import datetime, timedelta, timezone
from typing import Dict, Set

import discord
from discord.ext import commands, tasks
from discord import app_commands

# ========= 設定 =========
TOKEN = os.getenv("DISCORD_TOKEN")  # Railway Variables などに設定
GUILD_ID = 1398607685158440991      # ★あなたのサーバーID
MATCH_CATEGORY_ID = 1403371745301495848  # ★固定カテゴリ（プライベートVC作成先）

# 性別ロール
MALE_ROLE_ID = 1399390214295785623
FEMALE_ROLE_ID = 1399390384756363264

# 待機→マッチングの遅延
READY_DELAY_SECONDS = 60
# マッチングポーリング
MATCH_LOOP_INTERVAL = 10
# VC作成後、5分入室が無ければ削除
VC_IDLE_DELETE_SECONDS = 5 * 60

JST = timezone(timedelta(hours=9))

intents = discord.Intents.default()
intents.guilds = True
intents.members = True          # 権限付与・ロール判定で必要
intents.voice_states = True     # VC入退室の監視・自動削除に必要

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ========= 内部状態 =========
# 一般キュー: guild_id -> { user_id: ready_at }
general_queues: Dict[int, Dict[int, datetime]] = {}

# 性別キュー: guild_id -> {"male": {user_id: ready_at}, "female": {user_id: ready_at}}
gender_queues: Dict[int, Dict[str, Dict[int, datetime]]] = {}

# 作成したVCのライフサイクル追跡
class VCState:
    __slots__ = ("guild_id", "member_ids", "created_at", "ever_joined")
    def __init__(self, guild_id: int, member_ids: Set[int]):
        self.guild_id = guild_id
        self.member_ids = set(member_ids)
        self.created_at = datetime.now(JST)
        self.ever_joined = False  # 一度でも入室があったか

vc_states: Dict[int, VCState] = {}  # vc_id -> VCState


def vc_name_for(u1: discord.Member, u2: discord.Member) -> str:
    n1 = u1.display_name[:12]
    n2 = u2.display_name[:12]
    return f"Match: {n1} & {n2}"


# ========= ビュー（ボタン） =========
class GeneralMatchView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="ランダムマッチングに参加",
        style=discord.ButtonStyle.primary,
        custom_id="random_match_join_general",
        emoji="🎲"
    )
    async def join_general(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            await interaction.response.send_message("ギルド内で実行してください。", ephemeral=True)
            return

        q = general_queues.setdefault(guild.id, {})
        if user.id in q:
            remaining = (q[user.id] - datetime.now(JST)).total_seconds()
            msg = ("すでに登録済みです。まもなくマッチング処理にかかります。"
                   if remaining <= 0 else f"すでに登録済みです。開始まで残り **{int(remaining)}秒**")
            await interaction.response.send_message(msg, ephemeral=True)
            return

        q[user.id] = datetime.now(JST) + timedelta(seconds=READY_DELAY_SECONDS)
        await interaction.response.send_message(
            f"✅ 参加を受け付けました！ **{READY_DEL_**
