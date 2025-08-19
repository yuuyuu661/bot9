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
TOKEN = os.getenv("DISCORD_TOKEN")             # Railway Variables 等に設定
GUILD_ID = 1398607685158440991                 # ★あなたのサーバーID
MATCH_CATEGORY_ID = 1403371745301495848        # ★固定カテゴリ（プライベートVC作成先）

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
intents.members = True          # ロール判定・権限付与で必要
intents.voice_states = True     # VC入退室監視で必要

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ========= 内部状態 =========
# 一般キュー: guild_id -> { user_id: ready_at }
general_queues: Dict[int, Dict[int, datetime]] = {}

# 性別キュー: guild_id -> {"male": {user_id: ready_at}, "female": {user_id: ready_at}}
gender_queues: Dict[int, Dict[str, Dict[int, datetime]]] = {}

# 作成したVCのライフサイクル追跡
class VCState:
    __slots__ = ("guild_id", "member_ids", "created_at", "ever_joined", "both_joined")
    def __init__(self, guild_id: int, member_ids: Set[int]):
        self.guild_id = guild_id
        self.member_ids = set(member_ids)
        self.created_at = datetime.now(JST)
        self.ever_joined = False   # 誰か一度でも入室したか
        self.both_joined = False   # 2人とも入室したか

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
            f"✅ 参加を受け付けました！ **{READY_DELAY_SECONDS}秒後** にランダムマッチングします。",
            ephemeral=True
        )


class GenderMatchView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="男女マッチングに参加",
        style=discord.ButtonStyle.success,
        custom_id="random_match_join_gender",
        emoji="💞"
    )
    async def join_gender(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            await interaction.response.send_message("ギルド内で実行してください。", ephemeral=True)
            return

        # ロール判定
        has_male = any(r.id == MALE_ROLE_ID for r in user.roles)
        has_female = any(r.id == FEMALE_ROLE_ID for r in user.roles)

        if not (has_male or has_female):
            await interaction.response.send_message(
                "参加条件（男ロール or 女ロール）がありません。管理者にロール付与を依頼してください。",
                ephemeral=True
            )
            return
        if has_male and has_female:
            await interaction.response.send_message(
                "男・女の両方のロールが付いています。どちらか片方だけにしてください。",
                ephemeral=True
            )
            return

        qg = gender_queues.setdefault(guild.id, {"male": {}, "female": {}})
        bucket = "male" if has_male else "female"
        if user.id in qg[bucket]:
            remaining = (qg[bucket][user.id] - datetime.now(JST)).total_seconds()
            msg = ("すでに登録済みです。まもなくマッチング処理にかかります。"
                   if remaining <= 0 else f"すでに登録済みです。開始まで残り **{int(remaining)}秒**")
            await interaction.response.send_message(msg, ephemeral=True)
            return

        qg[bucket][user.id] = datetime.now(JST) + timedelta(seconds=READY_DELAY_SECONDS)
        await interaction.response.send_message(
            f"✅ 参加を受け付けました！ **{READY_DELAY_SECONDS}秒後** に男女マッチングします。",
            ephemeral=True
        )


# ========= ユーティリティ =========
async def create_private_vc_and_notify(guild: discord.Guild, m1: discord.Member, m2: discord.Member) -> None:
    category = guild.get_channel(MATCH_CATEGORY_ID)
    if not isinstance(category, discord.CategoryChannel):
        for m in (m1, m2):
            try:
                await m.send("❌ マッチング用のカテゴリが見つかりません。管理者に連絡してください。")
            except:
                pass
        return

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
        return

    # 状態登録（アイドル削除監視＆入退出監視用）
    st = VCState(guild.id, {m1.id, m2.id})
    vc_states[vc.id] = st

    # DM通知
    for m in (m1, m2):
        try:
            await m.send(f"✅ マッチング成立！ ボイスチャットへどうぞ → {vc.mention}")
        except:
            pass


# ========= マッチングループ =========
@tasks.loop(seconds=MATCH_LOOP_INTERVAL)
async def match_loop():
    now = datetime.now(JST)

    # ---- 一般キュー処理 ----
    for guild_id, q in list(general_queues.items()):
        ready = [uid for uid, t in q.items() if t <= now]
        if len(ready) >= 2:
            random.shuffle(ready)
            pairs = []
            while len(ready) >= 2:
                u1 = ready.pop()
                u2 = ready.pop()
                pairs.append((u1, u2))

            guild = bot.get_guild(guild_id)
            if guild:
                for uid1, uid2 in pairs:
                    q.pop(uid1, None)
                    q.pop(uid2, None)
                    m1 = guild.get_member(uid1)
                    m2 = guild.get_member(uid2)
                    if m1 and m2:
                        await create_private_vc_and_notify(guild, m1, m2)

    # ---- 性別キュー処理（男×女）----
    for guild_id, buckets in list(gender_queues.items()):
        male_q = buckets.setdefault("male", {})
        female_q = buckets.setdefault("female", {})
        ready_m = [uid for uid, t in male_q.items() if t <= now]
        ready_f = [uid for uid, t in female_q.items() if t <= now]
        if len(ready_m) >= 1 and len(ready_f) >= 1:
            random.shuffle(ready_m)
            random.shuffle(ready_f)
            guild = bot.get_guild(guild_id)
            if guild:
                while ready_m and ready_f:
                    uid_m = ready_m.pop()
                    uid_f = ready_f.pop()
                    male_q.pop(uid_m, None)
                    female_q.pop(uid_f, None)
                    m = guild.get_member(uid_m)
                    f = guild.get_member(uid_f)
                    if m and f:
                        await create_private_vc_and_notify(guild, m, f)


@match_loop.before_loop
async def before_match_loop():
    await bot.wait_until_ready()


# ========= VCアイドル監視（5分誰も入らなければ削除）=========
@tasks.loop(seconds=30)
async def vc_idle_watchdog():
    now = datetime.now(JST)
    for vc_id, st in list(vc_states.items()):
        guild = bot.get_guild(st.guild_id)
        if not guild:
            vc_states.pop(vc_id, None)
            continue
        ch = guild.get_channel(vc_id)
        if not isinstance(ch, discord.VoiceChannel):
            vc_states.pop(vc_id, None)
            continue

        # 「誰も一度も入室していない」かつ「5分経過」かつ「現在も無人」→ 削除
        if (not st.ever_joined
            and (now - st.created_at).total_seconds() >= VC_IDLE_DELETE_SECONDS
            and len(ch.members) == 0):
            try:
                await ch.delete(reason="5分間入室なしのため自動削除")
            except:
                pass
            vc_states.pop(vc_id, None)


@vc_idle_watchdog.before_loop
async def before_watchdog():
    await bot.wait_until_ready()


# ========= VC入退室イベント =========
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # 入室側
    if after and after.channel and after.channel.id in vc_states:
        st = vc_states[after.channel.id]
        ch = after.channel
        if isinstance(ch, discord.VoiceChannel):
            st.ever_joined = True  # 誰か入った
            # 2人とも入室しているか
            present_ids = {m.id for m in ch.members}
            if st.member_ids.issubset(present_ids):
                st.both_joined = True

    # 退出側
    ch = before.channel
    if ch and ch.id in vc_states:
        st = vc_states[ch.id]
        if isinstance(ch, discord.VoiceChannel):
            # 無人 かつ 「両方入室済み」 なら削除
            if len(ch.members) == 0 and st.both_joined:
                try:
                    await ch.delete(reason="両方入室後、全員退出したため自動削除")
                except:
                    pass
                vc_states.pop(ch.id, None)


# ========= スラッシュコマンド =========
@tree.command(name="post_panel", description="（一般）ランダムマッチングの参加ボタンを設置します。")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def post_panel(interaction: discord.Interaction):
    view = GeneralMatchView()
    await interaction.response.send_message(
        "🎲 **ランダムマッチング（一般）**\nボタンを押して1分後にランダムでマッチングします！",
        view=view
    )

@tree.command(name="post_gender_panel", description="（男女）ロール必須のマッチングボタンを設置します。")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def post_gender_panel(interaction: discord.Interaction):
    view = GenderMatchView()
    await interaction.response.send_message(
        "💞 **男女マッチング**\n男 or 女ロールが必要です。ボタンを押して1分後にマッチングします！",
        view=view
    )

@tree.command(name="cancel_match", description="（一般）自分の一般キュー登録を取り消します。")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def cancel_match(interaction: discord.Interaction):
    guild = interaction.guild
    user = interaction.user
    if guild is None:
        await interaction.response.send_message("ギルド内で実行してください。", ephemeral=True)
        return
    q = general_queues.setdefault(guild.id, {})
    if q.pop(user.id, None) is not None:
        await interaction.response.send_message("🟡 一般キューから取り消しました。", ephemeral=True)
    else:
        await interaction.response.send_message("一般キューに登録されていません。", ephemeral=True)

@tree.command(name="queue_status", description="現在のキュー人数を表示（一般/男女）。")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def queue_status(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("ギルド内で実行してください。", ephemeral=True)
        return

    now = datetime.now(JST)
    gq = general_queues.setdefault(guild.id, {})
    ready_g = sum(1 for t in gq.values() if t <= now)
    wait_g = sum(1 for t in gq.values() if t > now)

    gg = gender_queues.setdefault(guild.id, {"male": {}, "female": {}})
    m_ready = sum(1 for t in gg["male"].values() if t <= now)
    m_wait = sum(1 for t in gg["male"].values() if t > now)
    f_ready = sum(1 for t in gg["female"].values() if t <= now)
    f_wait = sum(1 for t in gg["female"].values() if t > now)

    msg = (
        f"📊 **一般**: 合計 {len(gq)}（準備完了: {ready_g} / 待機中: {wait_g}）\n"
        f"📊 **男女**: 男 合計 {len(gg['male'])}（準備: {m_ready} / 待機: {m_wait}）、"
        f"女 合計 {len(gg['female'])}（準備: {f_ready} / 待機: {f_wait}）"
    )
    await interaction.response.send_message(msg, ephemeral=True)


# ========= 起動時 =========
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)

    if not match_loop.is_running():
        match_loop.start()
    if not vc_idle_watchdog.is_running():
        vc_idle_watchdog.start()


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("環境変数 DISCORD_TOKEN が設定されていません。")
    bot.run(TOKEN)
