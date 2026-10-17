from __future__ import annotations

import os
import json
import random
import asyncio
import logging
import re
from pathlib import Path
from datetime import datetime, timezone
from threading import Thread
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask

# ============================== CONFIG ==============================
TOKEN = os.environ["DISCORD_TOKEN"]
OWNER_ID = 1176149190192152626
PANEL_CHANNEL_ID = 1559030600114118769
ANNOUNCEMENTS_CHANNEL_ID = 1559029372835467284
RESULTS_CHANNEL_ID = 1559029648795369522
TOURNAMENT_CATEGORY_ID = 1559030166414430208
TOURNAMENT_PING_ROLE_ID = 1541715118872731768
HOST_ROLE_ID = 1542739534343700500
DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / "tournament_data.json"
CHEST_API_URL = os.environ.get("CHEST_API_URL", "").rstrip("/")
CHEST_API_KEY = os.environ.get("CHEST_API_KEY", "")
REWARDS = {1: 40, 2: 30, 3: 25, 4: 20, 5: 15, 6: 12, 7: 10, 8: 8, 9: 5, 10: 3}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tournix")
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Lightweight health endpoint for Railway.
web = Flask(__name__)
@web.get("/")
def health():
    return "Tournament Bot is online!", 200

def run_web():
    web.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), use_reloader=False)
Thread(target=run_web, daemon=True).start()

# ============================== STORAGE ==============================
def fresh_data() -> dict[str, Any]:
    return {"next_id": 1, "tournaments": {}, "reward_transactions": [], "stats": {}, "team_invites": {},
            "linked_accounts": {}, "account_requests": {}, "account_dm_flows": {}, "account_guild_id": None}

def load_data():
    source_file = DATA_FILE
    if not source_file.exists():
        legacy_file = Path("tournament_data.json")
        if legacy_file.exists() and legacy_file.resolve() != DATA_FILE.resolve():
            source_file = legacy_file
        else:
            return fresh_data()
    try:
        with source_file.open("r", encoding="utf-8") as f:
            obj = json.load(f)
        if not isinstance(obj, dict):
            raise ValueError("Invalid data format")
        base = fresh_data()
        base.update(obj)
        return base
    except Exception:
        log.exception("Could not read tournament data; starting with empty data")
        return fresh_data()

data = load_data()
for _key, _default in (("linked_accounts", {}), ("account_requests", {}), ("account_dm_flows", {}), ("account_guild_id", None)):
    data.setdefault(_key, _default)
for _t in data.get("tournaments", {}).values():
    _t.setdefault("round_messages", {})
data_lock = asyncio.Lock()

def save_data():
    tmp = DATA_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(DATA_FILE)

def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())

def get_tour(tid: str):
    return data["tournaments"].get(str(tid))

def tour_by_channel(channel_id: int):
    for tid, t in data["tournaments"].items():
        if int(t.get("channel_id", 0)) == channel_id and t.get("status") not in ("deleted",):
            return tid, t
    return None, None

def is_owner(user: discord.abc.User) -> bool:
    return user.id == OWNER_ID

def has_host_role(member: discord.abc.User) -> bool:
    return isinstance(member, discord.Member) and any(r.id == HOST_ROLE_ID for r in member.roles)

def may_host(member: discord.abc.User) -> bool:
    return is_owner(member) or has_host_role(member)

def host_allowed(member: discord.abc.User, t: dict) -> bool:
    if is_training(t) and member.id == int(t.get("training_student_id", 0)):
        return True
    if is_owner(member):
        return True
    host_ids = t.get("host_ids") or [int(t.get("host_id", 0))]
    return member.id in [int(uid) for uid in host_ids[:3]]

def is_training(t: dict) -> bool:
    return bool(t.get("training"))

# ============================== EMBEDS / VIEWS ==============================
def tournament_embed(tid: str) -> discord.Embed:
    t = get_tour(tid)
    if not t:
        return discord.Embed(title="Tournament not found", color=discord.Color.red())
    players = t.get("players", [])
    start = int(t.get("start_ts", now_ts()))
    registration_open = now_ts() >= start - 1800 and t.get("status") == "registration"
    if t.get("status") == "registration" and not registration_open:
        status = f"Registration opens in {max(0, (start - 1800 - now_ts() + 59)//60)} min"
    else:
        status = "Registration open" if registration_open else t.get("status", "Setup").replace("_", " ").title()
    em = discord.Embed(title=f"🏆 {t.get('name', 'Tournament')}", description=t.get("description") or "No description provided.", color=discord.Color.blurple())
    em.add_field(name="Region", value=t.get("region", "Global"), inline=True)
    em.add_field(name="Game version", value=t.get("version", "—"), inline=True)
    em.add_field(name="Map", value=t.get("map", "—"), inline=True)
    em.add_field(name="Format", value=t.get("format", "1v1"), inline=True)
    em.add_field(name="Ability", value=t.get("ability", "—"), inline=True)
    em.add_field(name="Start time", value=f"<t:{start}:F>\n<t:{start}:R>", inline=True)
    em.add_field(name="Status", value=status, inline=True)
    maximum = int(t.get("max_players", 0))
    em.add_field(name="Players", value=f"{len(players)}/{maximum if maximum else '∞'}", inline=True)
    em.add_field(name="Rewards", value="\n".join(f"**#{place}:** {amount} Normal Chest(s)" for place, amount in REWARDS.items()) + "\n✨ 1% chance: 1 bonus Ultra Chest for the champion", inline=False)
    lines=[]
    for idx, player in enumerate(players[:40], 1):
        if t.get("training"):
            mention = discord.utils.escape_markdown(player.get("nickname", "Training Bot"))
        else:
            mention = " / ".join(f"<@{uid}>" for uid in player["members"])
        nick = discord.utils.escape_markdown(player.get("nickname", "Player"))
        lines.append(f"**{idx}.** {mention}\n-# <:player_name:1558926926675054652> {nick}")
    roster_text = "\n".join(lines) if lines else "No one has registered yet."
    if len(roster_text) > 1000:
        roster_text = roster_text[:970] + "…\n-# Open the tournament bracket for the full player list."
    em.add_field(name="Registered players", value=roster_text, inline=False)
    em.set_footer(text=f"Tournament ID: {tid} • Registration opens 30 minutes before start")
    if t.get("image_url", "").startswith(("https://", "http://")):
        em.set_image(url=t["image_url"])
    return em

def linked_account(user_id: int):
    return data.get("linked_accounts", {}).get(str(user_id))


def nickname_for_user(user_id: int) -> str | None:
    account = linked_account(user_id)
    return str(account.get("nickname", "")).strip() if account else None


def nickname_reserved(nickname: str, exclude_user_id: int | None = None, exclude_request_id: str | None = None) -> bool:
    key = nickname.strip().casefold()
    for uid, account in data.get("linked_accounts", {}).items():
        if exclude_user_id is not None and int(uid) == int(exclude_user_id):
            continue
        if str(account.get("nickname", "")).strip().casefold() == key:
            return True
    for rid, request in data.get("account_requests", {}).items():
        if rid == exclude_request_id or request.get("status") != "pending":
            continue
        reserved = request.get("nickname") if request.get("type") == "register" else request.get("new_nickname") if request.get("type") == "change" else None
        if reserved and str(reserved).strip().casefold() == key:
            return True
    return False


async def register_linked_player(interaction: discord.Interaction, tid: str):
    t = get_tour(tid)
    if not t or t.get("status") != "registration" or now_ts() < int(t.get("start_ts", 0)) - 1800:
        await interaction.response.send_message("Registration is not open yet.", ephemeral=True); return
    if now_ts() >= int(t.get("start_ts", 0)):
        await interaction.response.send_message("Registration is closed.", ephemeral=True); return
    account = linked_account(interaction.user.id)
    if not account:
        await interaction.response.send_message("You must link your Stumble Guys account using the Link Account panel before registering.", ephemeral=True); return
    uid = interaction.user.id
    try:
        team_size = int(str(t.get("format", "1v1")).split("v", 1)[0])
    except (ValueError, IndexError):
        team_size = 1
    if team_size != 1:
        await interaction.response.send_message("This tournament requires a team. Use `!team` to create one.", ephemeral=True); return
    if any(uid in p.get("members", []) for p in t.get("players", [])):
        await interaction.response.send_message("You are already registered for this tournament.", ephemeral=True); return
    nick = str(account.get("nickname", "")).strip()
    if not nick:
        await interaction.response.send_message("Your linked account is invalid. Please contact the bot owner.", ephemeral=True); return
    if any(str(p.get("nickname", "")).casefold() == nick.casefold() for p in t.get("players", [])):
        await interaction.response.send_message("That linked nickname is already registered.", ephemeral=True); return
    cap = int(t.get("max_players", 0))
    if cap and len(t.get("players", [])) >= cap:
        await interaction.response.send_message("This tournament is full.", ephemeral=True); return
    t["players"].append({"members": [uid], "nickname": nick, "team_name": None})
    save_data()
    await interaction.response.send_message(f"You are registered as **{discord.utils.escape_markdown(nick)}**!", ephemeral=True)
    await refresh_panel(tid)
    if cap and len(t["players"]) >= cap and not t.get("rounds"):
        await start_round_one(tid)

class AssignHostSelect(discord.ui.UserSelect):
    def __init__(self, tid: str):
        super().__init__(placeholder="Choose a member to assign as host", min_values=1, max_values=1)
        self.tid = str(tid)

    async def callback(self, interaction: discord.Interaction):
        t = get_tour(self.tid)
        if not t:
            await interaction.response.send_message(embed=discord.Embed(description="❌ Tournament not found.", color=discord.Color.red()), ephemeral=True); return
        ids = t.get("host_ids") or [int(t.get("host_id", 0))]
        if not (is_owner(interaction.user) or int(interaction.user.id) in [int(uid) for uid in ids[:3]]):
            await interaction.response.send_message(embed=discord.Embed(description="❌ Only the bot owner or an assigned host can assign hosts.", color=discord.Color.red()), ephemeral=True); return
        member = self.values[0]
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(embed=discord.Embed(description="❌ Please select a server member.", color=discord.Color.red()), ephemeral=True); return
        if not has_host_role(member) and not is_owner(member):
            await interaction.response.send_message(embed=discord.Embed(description="❌ The selected member must have the Host role.", color=discord.Color.red()), ephemeral=True); return
        ids = t.setdefault("host_ids", [int(t.get("host_id", 0))])
        if member.id in [int(uid) for uid in ids]:
            await interaction.response.send_message(embed=discord.Embed(description=f"✅ {discord.utils.escape_markdown(member.display_name)} is already a host.", color=discord.Color.green()), ephemeral=True); return
        if len(ids) >= 3:
            await interaction.response.send_message(embed=discord.Embed(description="❌ This tournament already has 3 hosts.", color=discord.Color.red()), ephemeral=True); return
        ids.append(member.id)
        save_data()
        try:
            channel = interaction.guild.get_channel(int(t.get("channel_id", 0))) if interaction.guild else None
            if channel:
                await channel.set_permissions(member, view_channel=True, send_messages=False, read_message_history=True, embed_links=False, attach_files=False)
        except discord.HTTPException:
            log.exception("Could not update channel permissions for host %s", member.id)
        names = []
        for uid in ids[:3]:
            m = interaction.guild.get_member(int(uid)) if interaction.guild else None
            names.append(discord.utils.escape_markdown(m.display_name if m else f"User {uid}"))
        em = discord.Embed(title="Host Assigned", description=f"✅ **{discord.utils.escape_markdown(member.display_name)}** was assigned as a host.\n\n**Current hosts:**\n" + "\n".join(f"✅ {name}" for name in names), color=discord.Color.green())
        em.set_footer(text=f"{len(ids[:3])}/3 hosts assigned")
        await interaction.response.send_message(embed=em, ephemeral=True)

class AssignHostView(discord.ui.View):
    def __init__(self, tid: str):
        super().__init__(timeout=120)
        self.add_item(AssignHostSelect(tid))


class TournamentView(discord.ui.View):
    def __init__(self, tid: str):
        super().__init__(timeout=None)
        self.tid=str(tid); t=get_tour(self.tid) or {}
        start=int(t.get("start_ts",now_ts())); opened=now_ts() >= start-1800 and t.get("status")=="registration"
        maxp=int(t.get("max_players",0)); count=len(t.get("players",[])); locked=t.get("status")!="registration" or (maxp and count>=maxp)
        remaining=max(0,(start-1800-now_ts()+59)//60)
        reg=discord.ui.Button(label=(f"Register {count}/{maxp}" if opened else f"Opens in {remaining} min"),style=discord.ButtonStyle.success if opened and not locked else discord.ButtonStyle.secondary,custom_id=f"tour:register:{tid}",disabled=not opened or bool(locked))
        reg.callback=self.register; self.add_item(reg)
        unreg=discord.ui.Button(label="Remove Registration",style=discord.ButtonStyle.danger,custom_id=f"tour:unregister:{tid}",disabled=not opened or bool(locked))
        unreg.callback=self.unregister; self.add_item(unreg)
        host_button = discord.ui.Button(label="Host", style=discord.ButtonStyle.primary, custom_id=f"tour:host:{tid}")
        host_button.callback = self.host_assign; self.add_item(host_button)
        players_button = discord.ui.Button(label="Players", style=discord.ButtonStyle.secondary, custom_id=f"tour:players:{tid}")
        players_button.callback = self.show_players; self.add_item(players_button)
        prizes_button = discord.ui.Button(label="Prizes Details", style=discord.ButtonStyle.secondary, custom_id=f"tour:prizes:{tid}")
        prizes_button.callback = self.prizes_details; self.add_item(prizes_button)
    async def register(self,i):
        t=get_tour(self.tid)
        if not t or t.get("status")!="registration" or now_ts()<int(t.get("start_ts",0))-1800:
            await i.response.send_message("Registration is not open yet.",ephemeral=True); return
        cap=int(t.get("max_players",0))
        if cap and len(t.get("players",[]))>=cap:
            await i.response.send_message("This tournament is full.",ephemeral=True); return
        await register_linked_player(i, self.tid)
    async def unregister(self,i):
        t=get_tour(self.tid)
        if not t or t.get("status")!="registration" or now_ts()<int(t.get("start_ts",0))-1800 or now_ts()>=int(t.get("start_ts",0)):
            await i.response.send_message("Registration changes are closed.",ephemeral=True); return
        before=len(t["players"]); t["players"]=[p for p in t["players"] if i.user.id not in p["members"]]
        if before==len(t["players"]): await i.response.send_message("You are not registered.",ephemeral=True); return
        save_data(); await i.response.send_message("You have been unregistered.",ephemeral=True); await refresh_panel(self.tid)
    async def host_assign(self, i):
        t = get_tour(self.tid)
        if not t:
            await i.response.send_message(embed=discord.Embed(description="❌ Tournament not found.", color=discord.Color.red()), ephemeral=True); return
        if not (is_owner(i.user) or has_host_role(i.user)):
            await i.response.send_message(embed=discord.Embed(description="❌ You need the Host role to become a host of this tournament.", color=discord.Color.red()), ephemeral=True); return
        ids = [int(uid) for uid in (t.get("host_ids") or [int(t.get("host_id", 0))]) if int(uid)]
        if i.user.id in ids:
            await i.response.send_message(embed=discord.Embed(description="✅ You are already a host of this tournament.", color=discord.Color.green()), ephemeral=True); return
        if len(ids) >= 3:
            await i.response.send_message(embed=discord.Embed(description="❌ This tournament already has the maximum of 3 hosts.", color=discord.Color.red()), ephemeral=True); return
        ids.append(i.user.id)
        t["host_ids"] = ids[:3]
        save_data()
        try:
            channel = i.guild.get_channel(int(t.get("channel_id", 0))) if i.guild else None
            if channel and isinstance(i.user, discord.Member):
                await channel.set_permissions(i.user, view_channel=True, send_messages=True, read_message_history=True, embed_links=False, attach_files=False)
        except discord.HTTPException:
            log.exception("Could not grant tournament channel permissions to host %s", i.user.id)
        em = discord.Embed(title="✅ Host Assigned", description=f"{i.user.mention} is now a host of this tournament. You can use bot commands in the tournament channel.", color=discord.Color.green())
        em.set_footer(text=f"{len(ids)}/3 hosts assigned")
        await i.response.send_message(embed=em, ephemeral=True)

    async def show_players(self, i):
        t = get_tour(self.tid)
        if not t:
            await i.response.send_message(embed=discord.Embed(description="❌ Tournament not found.", color=discord.Color.red()), ephemeral=True); return
        lines = []
        for index, player in enumerate(t.get("players", []), 1):
            if t.get("training"):
                lines.append(f"**{index}.** {discord.utils.escape_markdown(player.get('nickname', 'Training Bot'))}")
            else:
                mentions = " / ".join(f"<@{int(uid)}>" for uid in player.get("members", []))
                lines.append(f"**{index}.** {mentions or 'Unknown player'}")
        em = discord.Embed(title=f"👥 Registered Players — {t.get('name', 'Tournament')}", description="\n".join(lines) if lines else "No players have registered yet.", color=discord.Color.blurple())
        em.set_footer(text=f"Total: {len(t.get('players', []))}")
        await i.response.send_message(embed=em, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    async def prizes_details(self, i):
        em = discord.Embed(title="🎁 Prizes Details", description="\n".join(f"**#{place}:** {amount} Normal Chest(s)" for place, amount in REWARDS.items()) + "\n✨ Champion: 1% chance to receive 1 bonus Ultra Chest", color=discord.Color.gold())
        await i.response.send_message(embed=em, ephemeral=True)

async def refresh_panel(tid: str):
    t = get_tour(tid)
    if not t: return
    try:
        channel = bot.get_channel(int(t["channel_id"])) or await bot.fetch_channel(int(t["channel_id"]))
        msg = await channel.fetch_message(int(t["panel_message_id"]))
        await msg.edit(embed=tournament_embed(tid), view=TournamentView(tid))
    except (KeyError, discord.HTTPException, discord.NotFound, discord.Forbidden):
        log.exception("Could not refresh tournament panel %s", tid)

# ============================== CREATION FLOW ==============================
class TourDetailsModal(discord.ui.Modal, title="Classic Tournament — Details"):
    start_time = discord.ui.TextInput(label="Start time (UTC)", placeholder="2026-10-20 18:30", max_length=20)
    description = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, required=False, max_length=800)
    region = discord.ui.TextInput(label="Region", placeholder="EU / Global / ...", required=False, max_length=40)
    image_url = discord.ui.TextInput(label="Image URL (optional)", placeholder="https://...", required=False, max_length=300)
    def __init__(self, channel_id: int, creator_id: int, first: dict):
        super().__init__(); self.channel_id=channel_id; self.creator_id=creator_id; self.first=first
    async def on_submit(self, i: discord.Interaction):
        try:
            dt = datetime.strptime(str(self.start_time.value).strip(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            start_ts = int(dt.timestamp())
        except ValueError:
            await i.response.send_message("Use UTC format `YYYY-MM-DD HH:MM`, for example `2026-10-20 18:30`.", ephemeral=True); return
        if start_ts <= now_ts()+1800:
            await i.response.send_message("Start time must be more than 30 minutes in the future.", ephemeral=True); return
        self.first["region"] = str(self.region.value).strip() or "Global"
        self.first["image_url"] = str(self.image_url.value).strip()
        await i.response.send_message("Choose one or more abilities. `All` replaces individual abilities.", view=AbilitySelectView(self.first, self.creator_id, start_ts, str(self.description.value).strip()), ephemeral=True)

ABILITY_OPTIONS = [
    ("Shield", "<:Shield:1559402546328764466>"),
    ("Rock Slam", "<:Rock_Slam:1559402523604033616>"),
    ("Invisible", "<:Invisible:1559402543023525888>"),
    ("Sword", "<:Sword:1559402540913664030>"),
    ("Ball", "<:Ball:1559402538829086751>"),
    ("Double Jump", "<:Double_Jump:1559402537268805712>"),
    ("Rake", "<:Rake:1559402534161088574>"),
    ("Stickybomb", "<:Stickybomb:1559402530256191528>"),
    ("Boost", "<:Boost:1559402527559131216>"),
    ("Speed Gel", "<:SpeedGeel:1559402525302456430>"),
    ("Shutdown", "<:Shutdown:1559402521544626307>"),
    ("Bumper Field", "<:Bumber_Field:1559402519241691208>"),
    ("Hat Hop", "<:Hat_Hop:1559402518180790362>"),
    ("Pollo Stick", "<:Pollo_Stick:1559402516280639508>"),
    ("Briefcase", "<:BriefCase:1559402493195059250>"),
    ("Block Throw", "<:Block_Throw:1559402476476698634>"),
    ("Hug", "<:Hug:1559402511981486180>"),
    ("Chicken Bomb", "<:Chicken_Bomb:1559402509859037214>"),
    ("Punch", "<:Punch:1559402508303073301>"),
    ("Rewind", "<:Rewind:1559402506524696636>"),
    ("Kick", "<:Kick:1559402503013933156>"),
    ("Gust", "<:Gust:1559402499943833600>"),
    ("Banane", "<:Banane:1559402496965869598>"),
    ("Glader", "<:Glader:1559402494994681898>"),
    ("Switch", "<:Switch:1559402491513413732>"),
    ("Block Wall", "<:Block_Wall:1559402486765326426>"),
    ("Dice Roll", "<:Dice_Roll:1559402488447377568>"),
    ("Bolt", "<:Bolt:1559402484135628921>"),
    ("Hook Dash", "<:Hook_Dash:1559402482495660144>"),
    ("SnowBall", "<:SnowBall:1559402480167690321>"),
    ("Spit", "<:Spit:1559402478003429396>"),
    ("Chop", "<:Chop:1559402474798841916>"),
    ("Slal", "<:Slal:1559402472412545155>"),
]
ALL_ABILITY = ("All", "All")

class AbilitySelect(discord.ui.Select):
    def __init__(self, first: dict, creator_id: int, start_ts: int, description: str, page: int = 0, selected=None):
        self.first, self.creator_id, self.start_ts, self.description_text = first, creator_id, start_ts, description
        self.page = page
        self.selected = list(selected or [])
        page_size = 22
        page_items = ABILITY_OPTIONS[page * page_size:(page + 1) * page_size]
        opts = [discord.SelectOption(label=name, value=name, emoji=discord.PartialEmoji.from_str(emoji)) for name, emoji in page_items]
        opts.append(discord.SelectOption(label="All", value="__ALL__", description="Use all 33 abilities"))
        opts.append(discord.SelectOption(label="None", value="__NONE__", description="Tournament without abilities"))
        if page < 1:
            opts.append(discord.SelectOption(label="Next Page ➜", value="__NEXT__", description="See more abilities"))
        else:
            opts.append(discord.SelectOption(label="⬅ Previous Page", value="__PREV__", description="Return to the previous page"))
        super().__init__(placeholder=f"Choose abilities — page {page + 1}/2", min_values=1, max_values=len(opts), options=opts, custom_id=f"tour_ability_select_{page}")
    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message("Only the person who started this setup can choose abilities.", ephemeral=True); return
        values = list(self.values)
        if "__NEXT__" in values or "__PREV__" in values:
            page = 1 if "__NEXT__" in values else 0
            picked = [v for v in values if not v.startswith("__")]
            if "__ALL__" in values:
                picked = ["All"]
            elif "__NONE__" in values:
                picked = ["None"]
            elif self.selected and self.selected not in (["All"], ["None"]):
                picked = list(dict.fromkeys(self.selected + picked))
            await interaction.response.edit_message(content="Choose one or more abilities. `All` replaces individual abilities.", view=AbilitySelectView(self.first, self.creator_id, self.start_ts, self.description_text, page, picked))
            return
        if "__ALL__" in values:
            selected = ["All"]
        elif "__NONE__" in values:
            selected = ["None"]
        else:
            selected = list(dict.fromkeys(self.selected + values))
            selected = [v for v in selected if v not in ("All", "None")]
        # Keep the select to 25 visible options; submit with the Finish button.
        await interaction.response.edit_message(content="Selected: " + (", ".join(selected) if selected else "All") + "\nPress **Use selected abilities** when finished.", view=AbilitySelectView(self.first, self.creator_id, self.start_ts, self.description_text, self.page, selected))

class AbilitySelectView(discord.ui.View):
    def __init__(self, first: dict, creator_id: int, start_ts: int, description: str, page: int = 0, selected=None):
        super().__init__(timeout=300)
        self.first, self.creator_id, self.start_ts, self.description_text = first, creator_id, start_ts, description
        self.page, self.selected = page, list(selected or [])
        self.add_item(AbilitySelect(first, creator_id, start_ts, description, page, self.selected))
    @discord.ui.button(label="Use selected abilities", style=discord.ButtonStyle.success, row=1)
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message("Only the person who started this setup can finish it.", ephemeral=True); return
        selected = self.selected or ["All"]
        if "All" in selected:
            ability = "All"
        elif "None" in selected:
            ability = "None"
        else:
            ability = ", ".join(f"{dict(ABILITY_OPTIONS).get(name, '')} {name}".strip() for name in selected)
        await create_tournament(interaction, self.first, self.creator_id, self.start_ts, self.description_text, ability)

async def create_tournament(i: discord.Interaction, first: dict, creator_id: int, start_ts: int, description: str, ability: str):
    await i.response.defer(ephemeral=True, thinking=True)
    guild = i.guild
    category = guild.get_channel(TOURNAMENT_CATEGORY_ID) if guild else None
    if not isinstance(category, discord.CategoryChannel):
        await i.followup.send("The tournament category was not found. Check TOURNAMENT_CATEGORY_ID.", ephemeral=True); return
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(first.get("name", "tournament")).lower()).strip("-_")[:70] or "tournament"
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, embed_links=True, attach_files=True),
    }
    # Read-only access role requested for tournament channels.
    viewer_role = guild.get_role(1546987754968326154)
    if viewer_role:
        overwrites[viewer_role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=True, embed_links=False, attach_files=False)
    # The creator and Host role can view and manage the tournament channel.
    creator = guild.get_member(creator_id)
    if creator:
        overwrites[creator] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, embed_links=False, attach_files=False)
    host_role = guild.get_role(HOST_ROLE_ID)
    if host_role:
        # Members with the Host role can send normal messages; bot commands still check assigned host status.
        overwrites[host_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, embed_links=False, attach_files=False)
    owner_member = guild.get_member(OWNER_ID)
    if owner_member:
        overwrites[owner_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, embed_links=False, attach_files=False)
    try:
        channel = await guild.create_text_channel(f"🎮┃{slug}", category=category, overwrites=overwrites, reason="Tournament created")
    except discord.HTTPException:
        log.exception("Failed creating tournament channel")
        await i.followup.send("I could not create the tournament channel. Check my channel permissions.", ephemeral=True); return
    tid = str(data["next_id"]); data["next_id"] += 1
    t = {**first, "id": tid, "guild_id": guild.id, "channel_id": channel.id,
         "host_id": creator_id, "host_ids": [creator_id], "created_by": creator_id, "start_ts": start_ts,
         "description": description, "ability": ability,
         "status": "registration", "players": [], "rounds": [], "matches": [], "qualified": [],
         "ping_30_sent": False, "ping_open_sent": False, "cancelled_compensation_sent": False,
         "rewarded": [], "training": False, "panel_message_id": None}
    data["tournaments"][tid] = t; save_data()
    await channel.send(content=f"<@&{TOURNAMENT_PING_ROLE_ID}>", allowed_mentions=discord.AllowedMentions(roles=True))
    panel = await channel.send(embed=tournament_embed(tid), view=TournamentView(tid))
    t["panel_message_id"] = panel.id; save_data()
    ann = bot.get_channel(ANNOUNCEMENTS_CHANNEL_ID)
    if ann:
        announcement = discord.Embed(title=f"🏆 {t['name']}", description=f"A new tournament is available!\n[Open tournament channel]({channel.jump_url})", color=discord.Color.blurple())
        announcement.add_field(name="Start time", value=f"<t:{start_ts}:F>\n<t:{start_ts}:R>", inline=True)
        announcement.add_field(name="Region", value=t.get("region", "Global"), inline=True)
        announcement.add_field(name="Version / Format", value=f"{t.get('version','—')} · {t.get('format','1v1')}", inline=True)
        announcement.add_field(name="Map", value=t.get("map", "—"), inline=True)
        announcement.add_field(name="Ability", value=t.get("ability", "—"), inline=False)
        announcement.add_field(name="Rewards", value="\n".join(f"**#{place}:** {amount} Normal Chest(s)" for place, amount in REWARDS.items()) + "\n✨ 1% chance: 1 bonus Ultra Chest for the champion", inline=False)
        if t.get("image_url", "").startswith(("https://", "http://")): announcement.set_image(url=t["image_url"])
        await ann.send(embed=announcement)
    await i.followup.send(f"Tournament created: {channel.mention}", ephemeral=True)

class ClassicTourModal(discord.ui.Modal, title="Classic Tournament — Setup"):
    name = discord.ui.TextInput(label="Tournament name", placeholder="rull", max_length=80)
    version = discord.ui.TextInput(label="Game version", placeholder="0.103", max_length=20)
    map_name = discord.ui.TextInput(label="Map", placeholder="Block Dash Classic", max_length=80)
    format_name = discord.ui.TextInput(label="Format", placeholder="1v1, 2v2, 3v3", max_length=10)
    max_players = discord.ui.TextInput(label="Maximum players / teams", placeholder="16", max_length=4)
    def __init__(self, channel_id: int, creator_id: int):
        super().__init__(); self.channel_id=channel_id; self.creator_id=creator_id
    async def on_submit(self, i: discord.Interaction):
        try: maximum = int(str(self.max_players.value).strip())
        except ValueError:
            await i.response.send_message("Maximum players must be a number.", ephemeral=True); return
        if maximum < 2 or maximum > 500:
            await i.response.send_message("Maximum players must be between 2 and 500.", ephemeral=True); return
        fmt = str(self.format_name.value).strip().lower()
        if not re.fullmatch(r"([1-9][0-9]?)v\1", fmt):
            await i.response.send_message("Format must use matching team sizes, such as `1v1`, `2v2`, or `3v3`.", ephemeral=True); return
        first = {"name": str(self.name.value).strip(), "version": str(self.version.value).strip(), "map": str(self.map_name.value).strip(), "format": fmt, "max_players": maximum}
        await i.response.send_message(
            "Setup details are ready. Continue to enter the start time, description, and ability.",
            view=ContinueDetailsView(self.channel_id, self.creator_id, first), ephemeral=True
        )

class ContinueDetailsView(discord.ui.View):
    def __init__(self, channel_id: int, creator_id: int, first: dict):
        super().__init__(timeout=300)
        self.channel_id = channel_id
        self.creator_id = creator_id
        self.first = first
    @discord.ui.button(label="Continue setup", style=discord.ButtonStyle.primary)
    async def continue_setup(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message("Only the person who started this setup can continue.", ephemeral=True)
            return
        await interaction.response.send_modal(TourDetailsModal(self.channel_id, self.creator_id, self.first))

class CreationPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        b = discord.ui.Button(label="Classic Tour", style=discord.ButtonStyle.primary, emoji="🏆", custom_id="tour:create:classic")
        b.callback = self.classic; self.add_item(b)
    async def classic(self, i: discord.Interaction):
        if i.channel_id != PANEL_CHANNEL_ID:
            await i.response.send_message("Use the tournament creation panel in the designated channel.", ephemeral=True); return
        if not may_host(i.user):
            await i.response.send_message("Only the bot owner or a member with the Host role can create tournaments.", ephemeral=True); return
        await i.response.send_modal(ClassicTourModal(i.channel_id, i.user.id))

class CreateGroup(app_commands.Group):
    def __init__(self): super().__init__(name="create", description="Create bot panels")
    @app_commands.command(name="tour", description="Post the tournament creation panel")
    @app_commands.describe(channel="Channel where the creation panel should be posted")
    async def tour(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not (is_owner(interaction.user) or has_host_role(interaction.user)):
            await interaction.response.send_message("Only the bot owner or a member with the Host role can create the panel.", ephemeral=True); return
        if channel.id != PANEL_CHANNEL_ID:
            await interaction.response.send_message(f"Please select <#{PANEL_CHANNEL_ID}>.", ephemeral=True); return
        embed = discord.Embed(title="🏆 Tournament Creation", description="Choose a tournament type to begin.", color=discord.Color.blurple())
        await channel.send(embed=embed, view=CreationPanelView())
        await interaction.response.send_message("Creation panel posted.", ephemeral=True)

bot.tree.add_command(CreateGroup())

# ============================== BRACKET / RESULTS ==============================
def current_matches(t):
    return t.get("rounds", [])[-1] if t.get("rounds") else []

def player_label(t, uid):
    for p in t.get("players", []):
        if uid in p["members"]: return p["nickname"]
    return f"User {uid}"

def player_record(t, uid):
    return next((p for p in t.get("players", []) if uid in p.get("members", [])), None)

def match_embed(t, match, round_no):
    status = match.get("status", "WAITING")
    emoji = {"WAITING":"<:WAITING:1559395620593209374>", "PROCESSING":"<:progress:1558926971331805306>", "FINISHED":"<:finished:1558926985508421772>", "BYE":"<:finished:1558926985508421772>"}.get(status, "⏳")
    label = {"WAITING":"WAITING", "PROCESSING":"PROGRESS", "FINISHED":"FINISHED", "BYE":"FINISHED"}.get(status, status)
    def side(key, nickkey):
        ids = match.get(key, [])
        if not ids: return "**EMPTY**"
        mentions = " / ".join(f"<@{uid}>" for uid in ids if int(uid) > 0)
        nick = discord.utils.escape_markdown(match.get(nickkey, "Player"))
        return (mentions + "\n-# <:player_name:1558926926675054652> " + nick).strip()
    em = discord.Embed(title=f"{emoji} MATCH #{match.get('number', 1)} {label}", color=discord.Color.green() if status == "FINISHED" else discord.Color.red() if status == "PROCESSING" else discord.Color.blurple())
    em.description = f"{side('members1','nickname1')}\n\n<:vs:1558927022321967104> **Vs**\n\n{side('members2','nickname2')}"
    em.add_field(name="Round", value=f"R{round_no}", inline=True)
    em.add_field(name="Region", value=t.get("region", "Global"), inline=True)
    em.add_field(name="Version", value=t.get("version", "—"), inline=True)
    em.add_field(name="Format", value=t.get("format", "1v1"), inline=True)
    em.add_field(name="Map", value=t.get("map", "—"), inline=True)
    em.add_field(name="Ability", value=t.get("ability", "—"), inline=False)
    em.add_field(name="Tournament starts", value=f"<t:{int(t.get('start_ts', now_ts()))}:F>", inline=False)
    if t.get("image_url", "").startswith(("https://", "http://")): em.set_image(url=t["image_url"])
    if match.get("winner"):
        winner_id = match["winner"][0]
        em.add_field(name="Qualified", value=f"⬇️➡️ <@{winner_id}> — **{discord.utils.escape_markdown(player_label(t, winner_id))}**", inline=False)
    return em

async def replace_match_message(tid, match, round_no):
    # Match state is displayed only in the single round bracket embed.
    await update_round_bracket(tid, round_no)

class BracketView(discord.ui.View):
    def __init__(self, tid: str, round_no: int):
        super().__init__(timeout=None)
        self.tid = str(tid); self.round_no = int(round_no)
        q = discord.ui.Button(label="Qualified", style=discord.ButtonStyle.secondary, custom_id=f"bracket:qualified:{tid}:{round_no}")
        q.callback = self.qualified; self.add_item(q)
        code = discord.ui.Button(label="My Code", style=discord.ButtonStyle.primary, custom_id=f"bracket:mycode:{tid}:{round_no}")
        code.callback = self.mycode; self.add_item(code)
        t = get_tour(self.tid) or {}; matches = t.get("rounds", [])
        final_ready = bool(matches and len(matches[-1]) == 1 and matches[-1][0].get("status") not in ("FINISHED", "BYE"))
        if final_ready:
            win = discord.ui.Button(label="Set Winner", style=discord.ButtonStyle.success, custom_id=f"bracket:winner:{tid}:{round_no}")
            win.callback = self.set_winner; self.add_item(win)
    async def qualified(self, interaction: discord.Interaction):
        t = get_tour(self.tid)
        if not t or self.round_no > len(t.get("rounds", [])):
            await interaction.response.send_message(embed=discord.Embed(description="❌ Round not found.", color=discord.Color.red()), ephemeral=True); return
        matches = t["rounds"][self.round_no-1]
        ids=[]
        for m in matches:
            for uid in (m.get("winner") or []):
                if uid not in ids: ids.append(uid)
        desc = "\n".join(f"✅ {discord.utils.escape_markdown(player_label(t, uid))}" for uid in ids) or "No players have qualified yet."
        await interaction.response.send_message(embed=discord.Embed(title=f"Qualified — R{self.round_no}", description=desc, color=discord.Color.green()), ephemeral=True)
    async def mycode(self, interaction: discord.Interaction):
        t = get_tour(self.tid)
        if not t or not any(interaction.user.id in p.get("members", []) for p in t.get("players", [])):
            await interaction.response.send_message(embed=discord.Embed(description="❌ You are not a participant in this tournament.", color=discord.Color.red()), ephemeral=True); return
        match = next((m for rnd in t.get("rounds", []) for m in rnd if interaction.user.id in m.get("members1", []) + m.get("members2", []) and m.get("status") == "PROCESSING"), None)
        if not match or not match.get("code"):
            await interaction.response.send_message(embed=discord.Embed(description="❌ Your room code is not available right now.", color=discord.Color.red()), ephemeral=True); return
        await interaction.response.send_message(embed=discord.Embed(title="🔐 My Code", description=f"Room code: **`{match['code']}`**", color=discord.Color.blurple()), ephemeral=True)
    async def set_winner(self, interaction: discord.Interaction):
        t = get_tour(self.tid)
        if not t or not host_allowed(interaction.user, t):
            await interaction.response.send_message(embed=discord.Embed(description="❌ Only a host can set the winner.", color=discord.Color.red()), ephemeral=True); return
        matches = current_matches(t)
        if len(matches) != 1 or matches[0].get("status") in ("FINISHED", "BYE"):
            await interaction.response.send_message(embed=discord.Embed(description="❌ Set Winner is available only for an unfinished final match.", color=discord.Color.red()), ephemeral=True); return
        m = matches[0]
        options=[]
        guild = interaction.guild
        for uid in m.get("members1", []) + m.get("members2", []):
            if int(uid) > 0:
                member = guild.get_member(int(uid)) if guild else None
                if member is None and guild:
                    try:
                        member = await guild.fetch_member(int(uid))
                    except (discord.HTTPException, discord.NotFound):
                        member = None
                discord_name = member.display_name if member else f"User {uid}"
                options.append(discord.SelectOption(label=discord_name[:100], value=str(uid)))
            elif int(uid) < 0:
                options.append(discord.SelectOption(label=f"Training Bot {abs(int(uid))}", value=str(uid)))
        if not options:
            await interaction.response.send_message(embed=discord.Embed(description="❌ No final-match players found.", color=discord.Color.red()), ephemeral=True); return
        view = FinalWinnerSelect(self.tid, options)
        await interaction.response.send_message(embed=discord.Embed(title="Set Winner", description="Choose the final winner.", color=discord.Color.blurple()), view=view, ephemeral=True)

class FinalWinnerSelect(discord.ui.View):
    def __init__(self, tid: str, options):
        super().__init__(timeout=120); self.tid = str(tid)
        select = discord.ui.Select(placeholder="Select winner", min_values=1, max_values=1, options=options)
        select.callback = self.choose; self.add_item(select)
    async def choose(self, interaction: discord.Interaction):
        t = get_tour(self.tid)
        if not t or not host_allowed(interaction.user, t):
            await interaction.response.send_message(embed=discord.Embed(description="❌ You cannot set this winner.", color=discord.Color.red()), ephemeral=True); return
        matches = current_matches(t)
        if len(matches) != 1:
            await interaction.response.send_message(embed=discord.Embed(description="❌ Final match not found.", color=discord.Color.red()), ephemeral=True); return
        m = matches[0]; uid = int(interaction.data["values"][0])
        if uid not in m.get("members1", []) + m.get("members2", []):
            await interaction.response.send_message(embed=discord.Embed(description="❌ This player is not in the final match.", color=discord.Color.red()), ephemeral=True); return
        m["winner"] = list(m["members1"] if uid in m["members1"] else m["members2"]); m["status"] = "FINISHED"
        t.setdefault("qualified", [])
        for x in m["winner"]:
            if x not in t["qualified"]: t["qualified"].append(x)
        save_data(); await update_round_bracket(self.tid, len(t.get("rounds", [])))
        await finish_tournament(self.tid)
        winner_name = f"Training Bot {abs(uid)}" if uid < 0 else player_label(t, uid)
        await interaction.response.send_message(embed=discord.Embed(description=f"✅ Winner set: **{discord.utils.escape_markdown(winner_name)}**. Results have been published.", color=discord.Color.green()), ephemeral=True)

async def update_round_bracket(tid, round_no):
    t=get_tour(tid); ch=bot.get_channel(int(t["channel_id"])) if t else None
    if not t or not ch or round_no < 1 or round_no > len(t.get("rounds", [])): return
    old_id=(t.get("round_messages") or {}).get(str(round_no))
    matches=t["rounds"][round_no-1]
    lines=[]; qualified=[]
    for m in matches:
        status=m.get("status", "WAITING")
        icon={"WAITING":"<:WAITING:1559395620593209374>","PROCESSING":"<:progress:1558926971331805306>","FINISHED":"<:finished:1558926985508421772>","BYE":"<:finished:1558926985508421772>"}.get(status,"⏳")
        def bracket_side(member_key, nickname_key, empty_label):
            member_ids = [int(uid) for uid in m.get(member_key, []) if int(uid) > 0]
            nickname = discord.utils.escape_markdown(m.get(nickname_key, empty_label))
            mentions = " / ".join(f"<@{uid}>" for uid in member_ids) if member_ids else f"**{empty_label}**"
            return f"{mentions}\n-# ➜ {nickname}"
        side_a = bracket_side("members1", "nickname1", "EMPTY")
        side_b = bracket_side("members2", "nickname2", "EMPTY")
        winner=m.get("winner") or []
        line=f"{icon} **Match #{m.get('number', '?')}** * **{status}**\n\n{side_a}\n\n<:vs:1558927022321967104> **VS**\n\n{side_b}"
        if winner:
            labels=[discord.utils.escape_markdown(player_label(t, uid)) for uid in winner]
            line += "\n-# <:finished:1558926985508421772> Qualified: " + ", ".join(labels)
            qualified.extend(uid for uid in winner if uid not in qualified)
        lines.append(line)
    desc="\n\n".join(lines) or "No matches."
    if all(m.get("status") in ("FINISHED","BYE") for m in matches):
        desc += "\n\n**Qualified for the next round**\n" + ("\n".join(f"✅ {discord.utils.escape_markdown(player_label(t, uid))}" for uid in qualified) if qualified else "No qualified players.")
    em=discord.Embed(title=f"🏆 {t['name']} — Round {round_no}", description=desc[:4000], color=discord.Color.blurple())
    em.add_field(name="Game version",value=t.get("version","—"),inline=True)
    em.add_field(name="Map",value=t.get("map","—"),inline=True)
    em.add_field(name="Format",value=t.get("format","1v1"),inline=True)
    em.add_field(name="Ability",value=t.get("ability","—"),inline=True)
    view=BracketView(tid, round_no)
    if old_id:
        try:
            old_msg=await ch.fetch_message(int(old_id)); await old_msg.edit(embed=em, view=view)
            return
        except (discord.HTTPException, discord.NotFound): pass
    msg=await ch.send(embed=em, view=view)
    t.setdefault("round_messages",{})[str(round_no)]=msg.id
    save_data()

def create_round(t, players, round_no):
    pool=list(players); random.shuffle(pool); matches=[]; n=1
    while pool:
        a=pool.pop()
        if not pool:
            matches.append({"id":str(n),"number":n,"members1":list(a["members"]),"members2":[],"nickname1":a["nickname"],"nickname2":"EMPTY","status":"BYE","winner":list(a["members"]),"code":None})
            for uid in a["members"]:
                if uid not in t.setdefault("qualified",[]): t["qualified"].append(uid)
            break
        b=pool.pop()
        matches.append({"id":str(n),"number":n,"members1":list(a["members"]),"members2":list(b["members"]),"nickname1":a["nickname"],"nickname2":b["nickname"],"status":"WAITING","winner":None,"code":None})
        n+=1
    t.setdefault("rounds",[]).append(matches)
    return matches

async def start_round_one(tid):
    t=get_tour(tid)
    if not t or t.get("rounds") or len(t.get("players",[]))<2: return
    t["status"]="bracket_started"; t["registration_locked_at"]=now_ts()
    create_round(t,t["players"],1)
    # Normalize all matches to stable numeric IDs for commands and display.
    for idx,m in enumerate(t["rounds"][0],1):
        m["number"]=idx; m["id"]=str(idx)
    save_data()
    await update_round_bracket(tid,1)
    await refresh_panel(tid)

@bot.command(name="bracket")
async def bracket_cmd(ctx, round_label: str = ""):
    tid,t=tour_by_channel(ctx.channel.id)
    async def error(msg):
        try: await ctx.message.delete()
        except discord.HTTPException: pass
        m=await ctx.send(msg)
        await asyncio.sleep(1)
        try: await m.delete()
        except discord.HTTPException: pass
    try: await ctx.message.delete()
    except discord.HTTPException: pass
    if not t: await error("This command must be used in a tournament channel."); return
    if not host_allowed(ctx.author,t): await error("Only the bot owner or a Host can manage this tournament."); return
    try: rn=int(round_label.lower().replace("r",""))
    except ValueError: await error("Use `!bracket r1`, `!bracket r2`, and so on."); return
    if rn==1:
        if t.get("rounds"): await error("Round 1 has already been created."); return
        if len(t["players"])<2: await error("At least 2 registered players are required."); return
        await start_round_one(tid); return
    if rn != len(t.get("rounds",[]))+1: await error(f"The next available round is R{len(t.get('rounds',[]))+1}."); return
    prev=t["rounds"][-1]
    if any(m.get("status") not in ("FINISHED","BYE") for m in prev): await error("The previous round is not complete yet."); return
    winner_groups=[m.get("winner") for m in prev if m.get("winner")]
    roster=[]
    for members in winner_groups:
        pl=next((x for x in t["players"] if set(members).issubset(set(x["members"]))),None)
        if pl and pl not in roster: roster.append(pl)
    if len(roster)<=1:
        await finish_tournament(tid); return
    # Delete prior round bracket and replace with next round's bracket only when requested.
    old=(t.get("round_messages") or {}).get(str(rn-1))
    if old:
        try: await (await ctx.channel.fetch_message(int(old))).delete()
        except (discord.HTTPException,discord.NotFound): pass
    create_round(t,roster,rn)
    for idx,m in enumerate(t["rounds"][rn-1],1): m["number"]=idx; m["id"]=str(idx)
    t["qualified"]=[]; save_data()
    await update_round_bracket(tid,rn)
    if len(t["rounds"][rn-1])==1 and t["rounds"][rn-1][0].get("status")=="BYE": await finish_tournament(tid)

async def send_match_start_reminder(tid: str, match_id: str, code_sent_at: int):
    """Send the start-now notice 120 seconds after a room code is set."""
    await asyncio.sleep(120)
    t = get_tour(tid)
    if not t:
        return
    match = next((item for rnd in t.get("rounds", []) for item in rnd
                  if str(item.get("id")) == str(match_id)), None)
    if not match or match.get("status") != "PROCESSING":
        return
    if int(match.get("code_sent_at", 0) or 0) != code_sent_at or match.get("reminder_sent"):
        return
    host_ids = t.get("host_ids") or [int(t.get("host_id", 0))]
    for host_id in host_ids[:3]:
        try:
            await bot.fetch_user(int(host_id)).send(embed=discord.Embed(title="⏱️ Time's up!", description=f"Start Match #{match.get('number', match_id)} now!", color=discord.Color.gold()))
        except (discord.HTTPException, discord.NotFound):
            log.info("Could not DM host %s the match reminder", host_id)
    match["reminder_sent"] = True
    save_data()


@bot.command(name="match")
async def match_cmd(ctx, match_id: str = "", code: str = ""):
    tid, t = tour_by_channel(ctx.channel.id)
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass

    async def error(msg):
        m = await ctx.send(embed=discord.Embed(description=f"❌ {msg}", color=discord.Color.red()))
        await asyncio.sleep(3)
        try: await m.delete()
        except discord.HTTPException: pass

    if not t:
        await error("Use this command inside a tournament channel.")
        return
    if not host_allowed(ctx.author, t):
        await error("Only the bot owner or a Host can manage matches.")
        return
    m = next((item for item in current_matches(t)
              if str(item.get("number", item.get("id"))).lower().replace("m", "")
              == match_id.lower().replace("m", "")), None)
    if not m:
        await error("Match not found. Example: `!match m1 269ZWZ`.")
        return
    if m.get("status") in ("FINISHED", "BYE"):
        await error("This match has already been completed.")
        return
    # Room codes may contain letters and numbers, but never whitespace or mentions.
    if not re.fullmatch(r"[A-Za-z0-9]{4,12}", code):
        await error("Please provide a valid room code (4–12 letters or numbers).")
        return

    m["code"] = code
    m["status"] = "PROCESSING"
    m["code_sent_at"] = now_ts()
    m["reminder_sent"] = False
    m["code_author_id"] = ctx.author.id
    sent_at = int(m["code_sent_at"])
    save_data()
    rn = len(t.get("rounds", []))
    await replace_match_message(tid, m, rn)  # Public embed intentionally never includes the code.

    participant_ids = [int(uid) for uid in (m.get("members1", []) + m.get("members2", [])) if int(uid) > 0]
    dm_embed = discord.Embed(
        title=f"<:progress:1558926971331805306> MATCH #{m.get('number')} PROGRESS",
        description=(f"Tournament: **{t['name']}**\nRound: **R{rn}**\n"
                     f"Map: **{t.get('map', '—')}**\nAbility: **{t.get('ability', '—')}**\n"
                     f"Room code: **`{code}`**\n\nUse **My Code** in the bracket message if you need to view it again."),
        color=discord.Color.red()
    )
    failed_dm_ids = []
    if not t.get("training"):
        for uid in participant_ids:
            try:
                await (await bot.fetch_user(uid)).send(embed=dm_embed)
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                failed_dm_ids.append(uid)

    # The host always receives the code privately, including in training mode.
    host_id = int(ctx.author.id)
    try:
        host_dm = discord.Embed(
            title=f"🔐 Host match details — Match #{m.get('number')}",
            description=(f"Tournament: **{t['name']}**\nRound: **R{rn}**\n"
                         f"Players: **{m.get('nickname1', 'Player 1')} vs {m.get('nickname2', 'EMPTY')}**\n"
                         f"Map: **{t.get('map', '—')}**\nAbility: **{t.get('ability', '—')}**\n"
                         f"Room code: **`{code}`**"),
            color=discord.Color.blurple()
        )
        await (await bot.fetch_user(host_id)).send(embed=host_dm)
    except (discord.HTTPException, discord.NotFound, discord.Forbidden):
        log.warning("Could not DM host %s the room code for match %s", host_id, m.get("id"))

    for uid in failed_dm_ids:
        try:
            # Plain text (not an embed) when a participant's DMs are closed.
            notice = await ctx.send(
                f"❌ <@{uid}> I couldn't send you the room code because your DMs are closed. Please enable DMs and ask the host to resend it.",
                allowed_mentions=discord.AllowedMentions(users=True),
            )
            async def delete_notice_later(message):
                await asyncio.sleep(60)
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
            asyncio.create_task(delete_notice_later(notice))
        except discord.HTTPException:
            log.exception("Could not notify participant %s that their DM failed", uid)

    await ctx.send(embed=discord.Embed(description=f"✅ Room code sent privately for Match #{m.get('number')}.", color=discord.Color.green()), delete_after=5)
    # Separate timer gives a precise 120-second reminder; scheduler is a recovery fallback.
    asyncio.create_task(send_match_start_reminder(tid, str(m.get("id")), sent_at))

@bot.command(name="qual")
async def qual_cmd(ctx, *, target: str = ""):
    tid, t = tour_by_channel(ctx.channel.id)
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass

    async def error(msg):
        message = await ctx.send(embed=discord.Embed(description=f"❌ {msg}", color=discord.Color.red()))
        await asyncio.sleep(3)
        try:
            await message.delete()
        except discord.HTTPException:
            pass

    if not t:
        await error("Use this command inside a tournament channel.")
        return
    if not host_allowed(ctx.author, t):
        await error("Only the bot owner or a Host can qualify winners.")
        return
    matches_now = current_matches(t)
    if len(matches_now) == 1 and matches_now[0].get("status") not in ("FINISHED", "BYE"):
        await error("Use the **Set Winner** button for the final match. The `!qual` command is disabled for the final.")
        return

    # Training syntax: !qual training bot 2 (selects virtual Training Bot 2).
    training_match = re.fullmatch(r"training\s+bot\s+(\d+)", target.strip(), flags=re.IGNORECASE)
    if training_match:
        if not is_training(t):
            await error("`!qual training bot 2` can only be used in a training channel.")
            return
        bot_number = int(training_match.group(1))
        if bot_number < 1:
            await error("Training bot number must be 1 or higher.")
            return
        winner_id = -bot_number
        match = next((item for item in current_matches(t)
                      if winner_id in item.get("members1", []) + item.get("members2", [])), None)
        if not match:
            await error(f"Training Bot {bot_number} is not in a current match.")
            return
        if match.get("status") in ("FINISHED", "BYE"):
            await error("This match is already completed.")
            return
        winner_ids = list(match["members1"] if winner_id in match.get("members1", []) else match["members2"])
        match["winner"] = winner_ids
        match["status"] = "FINISHED"
        t.setdefault("qualified", []).extend(uid for uid in winner_ids if uid not in t.get("qualified", []))
        save_data()
        round_no = len(t.get("rounds", []))
        await replace_match_message(tid, match, round_no)
        await update_round_bracket(tid, round_no)
        await ctx.send(embed=discord.Embed(
            description=f"✅ **Training Bot {bot_number}** qualified from Match #{match.get('number', match.get('id'))}.",
            color=discord.Color.green()), delete_after=5)
        if t.get("rounds"):
            last_round = t["rounds"][-1]
            if len(last_round) == 1 and all(item.get("status") in ("FINISHED", "BYE") for item in last_round):
                await finish_tournament(tid)
        return

    # Real-player syntax: !qual @player. Do not accept virtual IDs in regular tournaments.
    winner = ctx.message.mentions[0] if ctx.message.mentions else None
    if winner is None:
        await error("Use `!qual @player` for real players, or `!qual training bot 2` in a training channel.")
        return
    if is_training(t):
        await error("This is a training channel. Use `!qual training bot 2` to qualify a training bot.")
        return

    match = next((item for item in current_matches(t)
                  if winner.id in item.get("members1", []) + item.get("members2", [])), None)
    if not match:
        await error("This player is not in a current match.")
        return
    if match.get("status") in ("FINISHED", "BYE"):
        await error("This match is already completed.")
        return
    winner_ids = list(match["members1"] if winner.id in match.get("members1", []) else match["members2"])
    match["winner"] = winner_ids
    match["status"] = "FINISHED"
    t.setdefault("qualified", []).extend(uid for uid in winner_ids if uid not in t.get("qualified", []))
    save_data()
    round_no = len(t.get("rounds", []))
    await replace_match_message(tid, match, round_no)
    await update_round_bracket(tid, round_no)
    await ctx.send(embed=discord.Embed(
        description=f"✅ **{discord.utils.escape_markdown(player_label(t, winner_ids[0]))}** qualified from Match #{match.get('number', match.get('id'))}.",
        color=discord.Color.green()), delete_after=5)
    if t.get("rounds"):
        last_round = t["rounds"][-1]
        if len(last_round) == 1 and all(item.get("status") in ("FINISHED", "BYE") for item in last_round):
            await finish_tournament(tid)

async def issue_chest(uid: int, amount: int, chest_type: str, key: str):
    if not CHEST_API_URL or not CHEST_API_KEY:
        log.error("Chest API is not configured; reward %s was not issued", key); return False
    if key in data["reward_transactions"]: return True
    try:
        timeout=aiohttp.ClientTimeout(total=12)
        endpoint = CHEST_API_URL if CHEST_API_URL.endswith("/api/chests/add") else CHEST_API_URL + "/api/chests/add"
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(endpoint, json={"user_id":uid,"amount":amount,"chest_type":chest_type,"transaction_id":key}, headers={"Authorization":f"Bearer {CHEST_API_KEY}"}) as resp:
                if resp.status != 200:
                    log.error("Chest API failed (%s): %s",resp.status,(await resp.text())[:300]); return False
                result=await resp.json()
                if result.get("ok"):
                    data["reward_transactions"].append(key); save_data(); return True
    except Exception:
        log.exception("Chest API request failed for %s", key)
    return False

async def finish_tournament(tid: str):
    t=get_tour(tid)
    if not t or t.get("status")=="finished": return
    t["status"]="finished"; save_data()
    if t.get("training"):
        ch = bot.get_channel(int(t.get("channel_id", 0)))
        if ch:
            await ch.send("🎓 Training completed! This practice session did not issue rewards or affect regular tournament statistics.")
        save_data()
        return
    # Single-elimination placement bands: champion #1, finalist #2, then groups
    # such as #3-4 and #5-8 for players eliminated in the same round.
    rounds = t.get("rounds", [])
    champion = []
    placement_groups: list[tuple[int, int, list[list[int]]]] = []
    if rounds and len(rounds[-1]) == 1 and rounds[-1][0].get("winner"):
        final_match = rounds[-1][0]
        champion = list(final_match["winner"])
        finalist = list(final_match["members2"] if champion == final_match["members1"] else final_match["members1"])
        placement_groups.append((1, 1, [champion]))
        if finalist:
            placement_groups.append((2, 2, [finalist]))
        next_place = 3
        for rnd in reversed(rounds[:-1]):
            losers = []
            for match in rnd:
                if match.get("status") != "FINISHED" or not match.get("winner"):
                    continue
                loser = match["members2"] if match.get("winner") == match.get("members1") else match["members1"]
                if loser:
                    losers.append(list(loser))
            if losers:
                placement_groups.append((next_place, next_place + len(losers) - 1, losers))
                next_place += len(losers)
    results=[]
    ultra_awarded = False
    ultra_roll_won = bool(champion) and random.random() < 0.01
    for start_place, end_place, slots in placement_groups:
        label_range = f"#{start_place}" if start_place == end_place else f"#{start_place}-{end_place}"
        labels = []
        for slot_index, ids in enumerate(slots):
            place = start_place + slot_index
            label = " / ".join(f"<@{uid}>" for uid in ids)
            amount = REWARDS.get(place, 0) if place <= 10 else 0
            labels.append(label)
            if place <= 10:
                results.append(f"**{label_range}** {label} — 🎁 {amount} Normal Chest(s)")
            for uid in ids:
                if amount:
                    key=f"tournament:{tid}:place:{place}:user:{uid}:normal"
                    await issue_chest(int(uid), amount, "chest", key)
                if place == 1 and ultra_roll_won and not ultra_awarded:
                    ultra_ok = await issue_chest(int(ids[0]), 1, "ultra", f"tournament:{tid}:place:1:ultra-bonus")
                    if ultra_ok:
                        results.append(f"✨ <@{ids[0]}> won **1 Ultra Chest** (1% bonus)!")
                        ultra_awarded = True
                if place <= 10:
                    try:
                        dm = discord.Embed(title=f"🏁 Tournament Results — {t['name']}", description=f"Your placement: **#{place}**\nReward: **{amount} Normal Chest(s)**", color=discord.Color.gold())
                        dm.add_field(name="Region", value=t.get("region", "Global"), inline=True)
                        dm.add_field(name="Version / Format", value=f"{t.get('version','—')} · {t.get('format','1v1')}", inline=True)
                        dm.add_field(name="Map", value=t.get("map", "—"), inline=True)
                        dm.add_field(name="Ability", value=t.get("ability", "—"), inline=False)
                        if place == 1 and ultra_awarded and ids[0] == uid:
                            dm.add_field(name="Bonus", value="✨ You also won the 1% Ultra Chest bonus!", inline=False)
                        if t.get("image_url", "").startswith(("https://", "http://")): dm.set_image(url=t["image_url"])
                        await bot.fetch_user(int(uid)).send(embed=dm)
                    except discord.HTTPException:
                        log.info("Could not DM winner %s", uid)
    channel=bot.get_channel(RESULTS_CHANNEL_ID)
    if channel:
        result_embed = discord.Embed(title=f"🏁 Results — {t['name']}", description="\n".join(results) or "No final results recorded.", color=discord.Color.gold())
        result_embed.add_field(name="Region", value=t.get("region", "Global"), inline=True)
        result_embed.add_field(name="Game version", value=t.get("version", "—"), inline=True)
        result_embed.add_field(name="Format", value=t.get("format", "1v1"), inline=True)
        result_embed.add_field(name="Map", value=t.get("map", "—"), inline=True)
        result_embed.add_field(name="Ability", value=t.get("ability", "—"), inline=False)
        result_embed.add_field(name="Start time", value=f"<t:{int(t.get('start_ts', now_ts()))}:F>", inline=True)
        if t.get("image_url", "").startswith(("https://", "http://")): result_embed.set_image(url=t["image_url"])
        await channel.send(embed=result_embed)
    save_data()


@bot.command(name="host")
async def assign_host(ctx, member: discord.Member = None):
    tid, t = tour_by_channel(ctx.channel.id)
    try: await ctx.message.delete()
    except discord.HTTPException: pass
    async def reply_error(text):
        msg = await ctx.send(embed=discord.Embed(description=f"❌ {text}", color=discord.Color.red()))
        await asyncio.sleep(3)
        try: await msg.delete()
        except discord.HTTPException: pass
    if not t:
        await reply_error("Use this command inside a tournament channel."); return
    if not (is_owner(ctx.author) or int(ctx.author.id) in (t.get("host_ids") or [int(t.get("host_id", 0))])):
        await reply_error("Only an assigned host can manage hosts."); return
    if member is None:
        await reply_error("Use `!host @member` to add a host."); return
    if not has_host_role(member) and not is_owner(member):
        await reply_error("The selected member must have the Host role."); return
    ids = t.setdefault("host_ids", [int(t.get("host_id", 0))])
    if member.id in ids:
        await ctx.send(embed=discord.Embed(description=f"✅ {discord.utils.escape_markdown(member.display_name)} is already a host.", color=discord.Color.green())); return
    if len(ids) >= 3:
        await reply_error("This tournament already has 3 hosts."); return
    ids.append(member.id); save_data()
    await ctx.send(embed=discord.Embed(description=f"✅ {discord.utils.escape_markdown(member.display_name)} was added as a host.", color=discord.Color.green()))
    # Grant view-only access; commands are invoked by authorized host users elsewhere, not by typing in this channel.
    try: await ctx.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True, embed_links=False, attach_files=False)
    except discord.HTTPException: pass

@bot.command(name="delete")
async def delete_tour(ctx, target: str = ""):
    try: await ctx.message.delete()
    except discord.HTTPException: pass
    async def error(msg):
        m=await ctx.send(embed=discord.Embed(description=f"❌ {msg}", color=discord.Color.red()))
        await asyncio.sleep(3)
        try: await m.delete()
        except discord.HTTPException: pass
    if target.lower()!="tour": await error("Use `!delete tour`."); return
    tid,t=tour_by_channel(ctx.channel.id)
    if not t: await error("This is not an active tournament channel."); return
    if not host_allowed(ctx.author, t):
        await error("Only the bot owner or an assigned tournament host can delete it."); return
    t["status"]="deleted"; save_data()
    await ctx.send("<:delete:1558926983277322401> Deleting this tournament channel…")
    try: await ctx.channel.delete(reason=f"Tournament deleted by {ctx.author}")
    except discord.HTTPException: log.exception("Failed to delete tournament channel")

# ============================== TEAMS / TRAINING / QUALIFICATION EDIT ==============================
class TeamInviteView(discord.ui.View):
    def __init__(self, invite_id: str):
        super().__init__(timeout=None)
        self.invite_id = invite_id
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id:
                child.custom_id = f"{child.custom_id}:{invite_id}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        inv = data.get("team_invites", {}).get(self.invite_id)
        if not inv or inv.get("status") != "pending":
            await interaction.response.send_message("This team invitation is no longer active.", ephemeral=True)
            return False
        if interaction.user.id not in inv["invitees"]:
            await interaction.response.send_message("Only invited players can respond to this invitation.", ephemeral=True)
            return False
        if interaction.user.id in inv.get("responses", {}):
            await interaction.response.send_message("You have already responded to this invitation.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="team_invite_accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        inv = data["team_invites"][self.invite_id]
        inv.setdefault("responses", {})[str(interaction.user.id)] = "accepted"
        if all(inv["responses"].get(str(uid)) == "accepted" for uid in inv["invitees"]):
            inv["status"] = "accepted"
            save_data()
            await interaction.response.edit_message(content="Team created successfully! Select a tournament to register your team.", embed=None, view=TeamTournamentSelectView(self.invite_id))
        else:
            save_data()
            accepted = sum(1 for value in inv["responses"].values() if value == "accepted")
            await interaction.response.send_message(f"Invitation accepted ({accepted}/{len(inv['invitees'])}). Waiting for the other invitee(s).", ephemeral=True)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, custom_id="team_invite_decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        inv = data["team_invites"][self.invite_id]
        inv["responses"][str(interaction.user.id)] = "declined"
        inv["status"] = "declined"
        save_data()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=f"Team invitation declined by {interaction.user.mention}. The team was not created.", view=self)

class TeamTournamentSelect(discord.ui.Select):
    def __init__(self, invite_id: str):
        self.invite_id = invite_id
        inv = data.get("team_invites", {}).get(invite_id, {})
        members = [int(inv.get("creator_id", 0))] + [int(x) for x in inv.get("invitees", [])]
        options = []
        for tid, t in data.get("tournaments", {}).items():
            # Keep finished/deleted/cancelled tournaments out; active tournaments remain selectable
            # so the callback can explain when registration is closed.
            if t.get("status") not in ("registration", "bracket_started") or t.get("training"):
                continue
            try:
                team_size = int(str(t.get("format", "1v1")).split("v", 1)[0])
            except (ValueError, IndexError):
                team_size = 1
            start_ts = int(t.get("start_ts", 0))
            if t.get("status") == "bracket_started":
                state = "Tournament already started"
            elif now_ts() < start_ts - 1800:
                state = "Registration is not open yet"
            elif now_ts() >= start_ts:
                state = "Registration is closed"
            elif int(t.get("max_players", 0)) and len(t.get("players", [])) >= int(t.get("max_players", 0)):
                state = "Tournament is full"
            elif team_size != len(members):
                state = f"Format {t.get('format', '1v1')} needs {team_size} player(s)"
            else:
                state = "Registration open"
            options.append(discord.SelectOption(
                label=str(t.get("name", f"Tournament {tid}"))[:100],
                value=tid,
                description=f"{t.get('format', '1v1')} • {state}"[:100]
            ))
        if not options:
            options = [discord.SelectOption(label="No active tournaments", value="none", description="There are no active tournaments right now.")]
        super().__init__(placeholder="Choose a tournament…", min_values=1, max_values=1, options=options[:25], custom_id=f"team_tour_select:{invite_id}", disabled=not any(o.value != "none" for o in options))

    async def callback(self, interaction: discord.Interaction):
        inv = data.get("team_invites", {}).get(self.invite_id)
        if not inv or inv.get("status") != "accepted":
            await interaction.response.send_message("This team invitation is no longer active.", ephemeral=True); return
        tid = self.values[0]
        if tid == "none":
            await interaction.response.send_message(embed=discord.Embed(title="❌ No Active Tournaments", description="There are no active tournaments right now.", color=discord.Color.red()), ephemeral=True); return
        t = get_tour(tid)
        if not t or t.get("status") not in ("registration", "bracket_started"):
            await interaction.response.send_message("This tournament is no longer active.", ephemeral=True); return
        if t.get("status") != "registration" or now_ts() < int(t.get("start_ts", 0)) - 1800 or now_ts() >= int(t.get("start_ts", 0)):
            await interaction.response.send_message(embed=discord.Embed(title="🔒 Tournament Registration Closed", description=f"Registration for **{t.get('name', 'this tournament')}** is closed or has not opened yet.", color=discord.Color.red()), ephemeral=True); return
        members = [int(inv["creator_id"])] + [int(x) for x in inv["invitees"]]
        try:
            team_size = int(str(t.get("format", "1v1")).split("v", 1)[0])
        except (ValueError, IndexError):
            team_size = 1
        if team_size != len(members):
            await interaction.response.send_message("This tournament format does not match your team size.", ephemeral=True); return
        if any(any(uid in p.get("members", []) for p in t.get("players", [])) for uid in members):
            await interaction.response.send_message("One or more team members are already registered in this tournament.", ephemeral=True); return
        if len(t.get("players", [])) >= int(t.get("max_players", 0)):
            await interaction.response.send_message("This tournament is full.", ephemeral=True); return
        nicknames = []
        for uid in members:
            nickname = nickname_for_user(uid)
            if not nickname:
                await interaction.response.send_message(f"<@{uid}> must link their Stumble Guys account before team registration.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none()); return
            nicknames.append(nickname)
        t["players"].append({"members": members, "nickname": " / ".join(nicknames), "team_name": None})
        inv["status"] = "registered"
        inv["registered_tournament_id"] = tid
        save_data()
        await interaction.response.edit_message(content=f"Your team has been registered for **{t['name']}**. The tournament selector is now disabled.", embed=None, view=TeamTournamentSelectView(self.invite_id, disabled=True))
        await refresh_panel(tid)
        cap=int(t.get("max_players",0))
        if cap and len(t["players"])>=cap and not t.get("rounds"):
            await start_round_one(tid)

class TeamTournamentSelectView(discord.ui.View):
    def __init__(self, invite_id: str, disabled: bool = False):
        super().__init__(timeout=None)
        select = TeamTournamentSelect(invite_id)
        self.add_item(select)
        if select.disabled:
            self.add_item(discord.ui.Button(label="No compatible open tournaments", style=discord.ButtonStyle.secondary, disabled=True))
        if disabled:
            for child in self.children:
                child.disabled = True

@bot.command(name="team")
async def team_cmd(ctx, *members: discord.Member):
    try: await ctx.message.delete()
    except discord.HTTPException: pass
    active_tournaments = [
        t for t in data.get("tournaments", {}).values()
        if t.get("status") in ("registration", "bracket_started")
    ]
    if not active_tournaments:
        embed = discord.Embed(
            title="❌ No Active Tournaments",
            description="There are no active tournaments right now.",
            color=discord.Color.red(),
        )
        await ctx.send(embed=embed, delete_after=10)
        return

    if len(members) not in (1, 2) or ctx.author in members or len({m.id for m in members}) != len(members):
        await ctx.reply("Use `!team @user` for a duo or `!team @user1 @user2` for a trio."); return
    all_members = [ctx.author.id] + [m.id for m in members]
    missing_accounts = [uid for uid in all_members if not nickname_for_user(uid)]
    if missing_accounts:
        try: await ctx.message.delete()
        except discord.HTTPException: pass
        msg = await ctx.send("❌ Every team member must link their Stumble Guys account before using `!team`. Missing account(s): " + ", ".join(f"<@{uid}>" for uid in missing_accounts), allowed_mentions=discord.AllowedMentions.none())
        await asyncio.sleep(6)
        try: await msg.delete()
        except discord.HTTPException: pass
        return
    if not hasattr(data, "get"):
        return
    data.setdefault("team_invites", {})
    invite_id = f"{ctx.guild.id}-{ctx.message.id}"
    data["team_invites"][invite_id] = {"creator_id": ctx.author.id, "invitees": [m.id for m in members], "responses": {}, "status": "pending", "created_at": now_ts()}
    save_data()
    mentions = " ".join(m.mention for m in members)
    embed = discord.Embed(title="Team Invitation", description=f"{ctx.author.mention} wants to create a team with {mentions}. All invited players must accept.", color=discord.Color.blurple())
    await ctx.send(embed=embed, view=TeamInviteView(invite_id), allowed_mentions=discord.AllowedMentions(users=True))

class EditQualifiedModal(discord.ui.Modal, title="Edit Qualified User"):
    wrong_user_id = discord.ui.TextInput(label="Wrong Qualified User ID", placeholder="Discord user ID", max_length=22)
    round_number = discord.ui.TextInput(label="Round", placeholder="1", max_length=3)
    new_user_id = discord.ui.TextInput(label="New Qualified User ID", placeholder="Discord user ID", max_length=22)

    def __init__(self, tid: str):
        super().__init__()
        self.tid = tid

    async def on_submit(self, interaction: discord.Interaction):
        t = get_tour(self.tid)
        if not t or not host_allowed(interaction.user, t):
            await interaction.response.send_message("You are not allowed to edit this tournament.", ephemeral=True); return
        try:
            wrong = int(str(self.wrong_user_id.value).strip())
            rn = int(str(self.round_number.value).strip())
            correct = int(str(self.new_user_id.value).strip())
        except ValueError:
            await interaction.response.send_message("Enter valid numeric Discord user IDs and a round number.", ephemeral=True); return
        if rn < 1 or rn > len(t.get("rounds", [])):
            await interaction.response.send_message("That round does not exist.", ephemeral=True); return
        previous_round = t["rounds"][rn - 1]
        wrong_match = next((m for m in previous_round if m.get("winner") and wrong in m.get("winner", [])), None)
        correct_match = next((m for m in previous_round if correct in m.get("members1", []) + m.get("members2", [])), None)
        if not wrong_match:
            await interaction.response.send_message("The wrong user is not qualified from that round.", ephemeral=True); return
        if not correct_match or correct_match.get("status") not in ("FINISHED", "WAITING", "PROCESSING"):
            await interaction.response.send_message("The replacement user did not participate in that round.", ephemeral=True); return
        if correct_match.get("status") != "FINISHED":
            await interaction.response.send_message("The replacement user's match must be completed first.", ephemeral=True); return
        correct_winner = correct_match.get("winner") or []
        if correct not in correct_winner:
            await interaction.response.send_message("The replacement user was not the winner of their match.", ephemeral=True); return
        if wrong_match is correct_match:
            await interaction.response.send_message("The selected users belong to the same match; no correction is needed.", ephemeral=True); return
        wrong_match["corrected_at"] = now_ts()
        wrong_match["corrected_by"] = interaction.user.id
        correct_group = list(correct_winner)
        # If the next round has not been published, update the qualified roster only.
        if rn == len(t["rounds"]):
            t["qualified"] = [uid for uid in t.get("qualified", []) if uid != wrong]
            for uid in correct_group:
                if uid not in t["qualified"]:
                    t["qualified"].append(uid)
        else:
            next_round = t["rounds"][rn]
            affected = [m for m in next_round if wrong in m.get("members1", []) + m.get("members2", [])]
            if any(m.get("status") == "PROCESSING" for m in affected):
                await interaction.response.send_message("Correction blocked: the affected next-round match is already processing. Correct it before starting that match.", ephemeral=True); return
            if any(m.get("status") == "FINISHED" for m in affected):
                # The result is invalidated only for the match whose participant is being replaced.
                for m in affected:
                    m["status"] = "WAITING"; m["winner"] = None; m["code"] = None; m.pop("code_sent_at", None)
            for m in affected:
                for side in ("members1", "members2"):
                    if wrong in m.get(side, []):
                        m[side] = correct_group
                        m["nickname1" if side == "members1" else "nickname2"] = next((p["nickname"] for p in t["players"] if set(correct_group).issubset(set(p["members"]))), f"Player {correct}")
                        m["status"] = "WAITING"; m["winner"] = None; m["code"] = None
                        m["corrected_at"] = now_ts()
            t["qualified"] = [uid for uid in t.get("qualified", []) if uid != wrong]
            for uid in correct_group:
                if uid not in t["qualified"]:
                    t["qualified"].append(uid)
        save_data()
        await interaction.response.send_message("Qualified player corrected. Unaffected match results were preserved.", ephemeral=True)
        await refresh_panel(self.tid)
        ch = bot.get_channel(int(t["channel_id"]))
        if ch:
            await ch.send(f"Qualification for Round {rn} was corrected by {interaction.user.mention}. Please review the affected next-round matchups.")

class EditQualifiedView(discord.ui.View):
    def __init__(self, tid: str):
        super().__init__(timeout=300)
        self.tid = tid
    @discord.ui.button(label="Edit Qualified User", style=discord.ButtonStyle.primary, emoji=discord.PartialEmoji.from_str("<:edit_qualified:1558926940638019584>"))
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        t = get_tour(self.tid)
        if not t or not host_allowed(interaction.user, t):
            await interaction.response.send_message("You are not allowed to edit this tournament.", ephemeral=True); return
        await interaction.response.send_modal(EditQualifiedModal(self.tid))

@bot.command(name="edit")
async def edit_cmd(ctx, what: str = ""):
    try: await ctx.message.delete()
    except discord.HTTPException: pass
    if what.lower() != "qualified":
        msg=await ctx.send("Use `!edit qualified`."); await asyncio.sleep(1)
        try: await msg.delete()
        except discord.HTTPException: pass
        return
    tid, t = tour_by_channel(ctx.channel.id)
    if not t:
        msg=await ctx.send("Use this command inside a tournament channel."); await asyncio.sleep(1)
        try: await msg.delete()
        except discord.HTTPException: pass
        return
    if not host_allowed(ctx.author, t):
        msg=await ctx.send("Only the bot owner or a Host can edit qualified players."); await asyncio.sleep(1)
        try: await msg.delete()
        except discord.HTTPException: pass
        return
    await ctx.send("Use the button below to correct a qualified player.", view=EditQualifiedView(tid))

@bot.command(name="training")
async def training_cmd(ctx, tour: str = "", member: discord.Member = None):
    if tour.lower() != "tour" or member is None:
        await ctx.reply("Use `!training tour @user` to start a training session."); return
    if not may_host(ctx.author):
        await ctx.reply("Only the bot owner or a Host can start training."); return
    guild = ctx.guild
    category = guild.get_channel(TOURNAMENT_CATEGORY_ID)
    if not isinstance(category, discord.CategoryChannel):
        await ctx.reply("The tournament category was not found."); return
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        ctx.author: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    try:
        channel = await guild.create_text_channel(f"🎓┃training-{re.sub(r"[^\w-]+", "-", member.display_name.lower()).strip("-_ ")[:45] or "host"}", category=category, overwrites=overwrites, reason="Tournament hosting training")
    except discord.HTTPException:
        await ctx.reply("I could not create the training channel. Check my channel permissions."); return
    tid = str(data["next_id"]); data["next_id"] += 1
    fake_players = [{"members": [-(i+1)], "nickname": f"Training Bot {i+1}", "team_name": None} for i in range(4)]
    t = {"id": tid, "name": "Training Tournament", "version": "Practice", "map": "Practice Map", "format": "1v1", "max_players": 4, "guild_id": guild.id, "channel_id": channel.id, "host_id": member.id, "training_student_id": member.id, "created_by": ctx.author.id, "start_ts": now_ts()+1800, "description": "Practice hosting here. This session has virtual participants and no rewards.", "ability": "Training", "status": "registration", "players": fake_players, "rounds": [], "matches": [], "qualified": [], "ping_30_sent": True, "ping_open_sent": True, "cancelled_compensation_sent": True, "rewarded": [], "training": True, "panel_message_id": None}
    data["tournaments"][tid] = t; save_data()
    await channel.send(f"Welcome {member.mention}! This is an isolated training tournament. Practice with `!bracket r1`, `!match 1 123456`, and `!qual 1 @winner`. Virtual players auto-advance; no rewards or normal tournament statistics are affected.", allowed_mentions=discord.AllowedMentions(users=True))
    panel = await channel.send(embed=tournament_embed(tid), view=TournamentView(tid))
    t["panel_message_id"] = panel.id; save_data()
    await ctx.reply(f"Training channel created for {member.mention}: {channel.mention}")

# ============================== LINK ACCOUNT (COMPONENTS V2) ==============================
def account_request_channel_name(request_id: str, request: dict) -> str:
    kind = request.get("type", "request")
    return f"account-{kind}-{request_id[-6:]}"[:90]


async def get_account_guild():
    guild_id = data.get("account_guild_id")
    if guild_id:
        guild = bot.get_guild(int(guild_id))
        if guild:
            return guild
    return bot.guilds[0] if bot.guilds else None


async def create_account_review(request_id: str):
    request = data.get("account_requests", {}).get(request_id)
    if not request or request.get("review_channel_id"):
        return
    guild = await get_account_guild()
    if guild is None:
        log.error("Cannot create account review channel: no guild is available")
        return
    owner = guild.get_member(OWNER_ID)
    if owner is None:
        try: owner = await guild.fetch_member(OWNER_ID)
        except (discord.HTTPException, discord.NotFound): owner = None
    bot_member = guild.me
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=False
        )
    }

    if owner:
        overwrites[owner] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True
        )

    if bot_member:
        overwrites[bot_member] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
            attach_files=True,
            embed_links=True
        )
    try:
        category = guild.get_channel(TOURNAMENT_CATEGORY_ID)
        kwargs = {"name": account_request_channel_name(request_id, request), "overwrites": overwrites, "reason": "Private Link Account review"}
        if isinstance(category, discord.CategoryChannel): kwargs["category"] = category
        channel = await guild.create_text_channel(**kwargs)
        request["review_channel_id"] = channel.id
        save_data()
        user_id = int(request["user_id"])
        user = bot.get_user(user_id)
        if user is None:
            try: user = await bot.fetch_user(user_id)
            except (discord.HTTPException, discord.NotFound): user = None
        username = f"{user} ({user_id})" if user else f"User {user_id}"
        kind = request.get("type")
        if kind == "register":
            desc = f"**Discord user:** {username}\n**Discord ID:** `{user_id}`\n**Game nickname:** `{request.get('nickname', '—')}`"
            view = AccountRegisterReviewView(request_id)
            title = "Link Account — Registration Review"
        elif kind == "change":
            desc = f"**Discord user:** {username}\n**Discord ID:** `{user_id}`\n**Old nickname:** `{request.get('old_nickname', '—')}`\n**New nickname:** `{request.get('new_nickname', '—')}`"
            view = AccountChangeReviewView(request_id)
            title = "Link Account — Change Name Review"
        else:
            desc = f"**Discord user:** {username}\n**Discord ID:** `{user_id}`\n**Linked nickname:** `{request.get('nickname', '—')}`\n\nConfirm to unlink this account, or reject to keep it linked."
            view = AccountUnregisterReviewView(request_id)
            title = "Link Account — Unregister Review"
        await channel.send(embed=discord.Embed(title=title, description=desc, color=discord.Color.blurple()), view=view)
        request["review_message_created"] = True
        save_data()
    except discord.HTTPException:
        log.exception("Failed to create private account review channel")


async def account_dm(user_id: int, content: str, view=None):
    try:
        user = bot.get_user(int(user_id)) or await bot.fetch_user(int(user_id))
        await user.send(content, view=view)
        return True
    except (discord.HTTPException, discord.NotFound, discord.Forbidden):
        return False


async def finish_account_request(request_id: str, status: str, dm_text: str):
    request = data.get("account_requests", {}).get(request_id)
    if not request:
        return
    request["status"] = status
    request["decided_at"] = now_ts()
    data.get("account_dm_flows", {}).pop(str(request.get("user_id")), None)
    save_data()
    await account_dm(int(request["user_id"]), f"**SG:** {dm_text}")
    channel_id = request.get("review_channel_id")
    if channel_id:
        channel = bot.get_channel(int(channel_id))
        if channel:
            try: await channel.send(embed=discord.Embed(description=f"Request **{status.upper()}**. {dm_text}", color=discord.Color.green() if status == "accepted" else discord.Color.red()))
            except discord.HTTPException: pass


class AccountReviewBase(discord.ui.View):
    def __init__(self, request_id: str):
        super().__init__(timeout=None)
        self.request_id = str(request_id)
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id:
                child.custom_id = f"{child.custom_id}:{self.request_id}"
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("Only the bot owner can review account requests.", ephemeral=True)
            return False
        request = data.get("account_requests", {}).get(self.request_id)
        if not request or request.get("status") != "pending":
            await interaction.response.send_message("This request is no longer pending.", ephemeral=True)
            return False
        return True


class AccountMessageModal(discord.ui.Modal, title="Send message to applicant"):
    message_text = discord.ui.TextInput(label="Message", style=discord.TextStyle.paragraph, max_length=1800)
    def __init__(self, request_id: str):
        super().__init__(); self.request_id = str(request_id)
    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("Only the bot owner can message applicants.", ephemeral=True); return
        request = data.get("account_requests", {}).get(self.request_id)
        if not request or request.get("status") != "pending":
            await interaction.response.send_message("This request is no longer pending.", ephemeral=True); return
        sent = await account_dm(int(request["user_id"]), f"**SG:** {self.message_text.value}")
        await interaction.response.send_message("Message sent to the applicant." if sent else "Could not DM the applicant. They may have DMs disabled.", ephemeral=True)


class AccountRegisterReviewView(AccountReviewBase):
    @discord.ui.button(label="Send Message", style=discord.ButtonStyle.primary, custom_id="account:register:message")
    async def send_message(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AccountMessageModal(self.request_id))
    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="account:register:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        request = data["account_requests"][self.request_id]
        uid = int(request["user_id"]); nickname = str(request.get("nickname", "")).strip()
        if nickname_reserved(nickname, exclude_user_id=uid, exclude_request_id=self.request_id):
            await interaction.response.send_message("This nickname is already linked or reserved by another request.", ephemeral=True); return
        if linked_account(uid):
            await interaction.response.send_message("This user already has a linked account.", ephemeral=True); return
        data["linked_accounts"][str(uid)] = {"nickname": nickname, "linked_at": now_ts(), "linked_by": OWNER_ID}
        save_data()
        await finish_account_request(self.request_id, "accepted", f"Your Stumble Guys account `{nickname}` has been linked successfully.")
        for child in self.children: child.disabled = True
        await interaction.response.edit_message(view=self)
    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="account:register:close")
    async def close_request(self, interaction: discord.Interaction, button: discord.ui.Button):
        await finish_account_request(self.request_id, "closed", "Your account registration request was closed and not approved.")
        for child in self.children: child.disabled = True
        await interaction.response.edit_message(view=self)


class AccountChangeReviewView(AccountReviewBase):
    @discord.ui.button(label="Send Message", style=discord.ButtonStyle.primary, custom_id="account:change:message")
    async def send_message(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AccountMessageModal(self.request_id))
    @discord.ui.button(label="Change", style=discord.ButtonStyle.success, custom_id="account:change:accept")
    async def accept_change(self, interaction: discord.Interaction, button: discord.ui.Button):
        request = data["account_requests"][self.request_id]; uid = int(request["user_id"])
        old = linked_account(uid); new_nick = str(request.get("new_nickname", "")).strip()
        if not old:
            await interaction.response.send_message("This user no longer has a linked account.", ephemeral=True); return
        if nickname_reserved(new_nick, exclude_user_id=uid, exclude_request_id=self.request_id):
            await interaction.response.send_message("This nickname is already linked or reserved by another request.", ephemeral=True); return
        old_nick = old.get("nickname", "")
        old["nickname"] = new_nick; old["changed_at"] = now_ts(); old["changed_by"] = OWNER_ID
        save_data()
        await finish_account_request(self.request_id, "accepted", f"Your Stumble Guys nickname was changed from `{old_nick}` to `{new_nick}`.")
        for child in self.children: child.disabled = True
        await interaction.response.edit_message(view=self)
    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="account:change:reject")
    async def reject_change(self, interaction: discord.Interaction, button: discord.ui.Button):
        await finish_account_request(self.request_id, "rejected", "Your nickname change was rejected. Your previous nickname remains linked. You can submit a new request.")
        for child in self.children: child.disabled = True
        await interaction.response.edit_message(view=self)


class AccountUnregisterReviewView(AccountReviewBase):
    @discord.ui.button(label="Send Message", style=discord.ButtonStyle.primary, custom_id="account:unregister:message")
    async def send_message(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AccountMessageModal(self.request_id))
    @discord.ui.button(label="Confirm Unregister", style=discord.ButtonStyle.danger, custom_id="account:unregister:confirm")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        request = data["account_requests"][self.request_id]; uid = str(request["user_id"])
        data.get("linked_accounts", {}).pop(uid, None)
        save_data()
        await finish_account_request(self.request_id, "accepted", "Your Stumble Guys account has been unlinked.")
        for child in self.children: child.disabled = True
        await interaction.response.edit_message(view=self)
    @discord.ui.button(label="Reject", style=discord.ButtonStyle.secondary, custom_id="account:unregister:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await finish_account_request(self.request_id, "rejected", "Your unlink request was rejected. Your account remains linked.")
        for child in self.children: child.disabled = True
        await interaction.response.edit_message(view=self)


class StartAccountRegistrationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="Start Registration", style=discord.ButtonStyle.success, custom_id="account:registration:start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = str(interaction.user.id)
        if linked_account(interaction.user.id):
            await interaction.response.send_message("Your account is already linked.", ephemeral=True); return
        if any(r.get("user_id") == interaction.user.id and r.get("status") == "pending" for r in data.get("account_requests", {}).values()):
            await interaction.response.send_message("You already have a pending account request.", ephemeral=True); return
        data.setdefault("account_dm_flows", {})[uid] = {"type": "register", "step": "nickname", "started_at": now_ts()}
        save_data()
        await interaction.response.send_message("Please send your exact Stumble Guys nickname as a message in this DM. You can send evidence/photos after submitting it.", ephemeral=True)


class AccountPanelV2(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        container = discord.ui.Container(accent_colour=discord.Colour.blurple())
        container.add_item(discord.ui.TextDisplay("# 🔗 Link Account\nLink your Stumble Guys account to your Discord profile. Account registrations and changes are reviewed by the bot owner."))
        row = discord.ui.ActionRow()
        register = discord.ui.Button(label="Register", style=discord.ButtonStyle.success, custom_id="account:panel:register", emoji="📝")
        unregister = discord.ui.Button(label="Unregister", style=discord.ButtonStyle.danger, custom_id="account:panel:unregister", emoji="🔓")
        change = discord.ui.Button(label="Change Name", style=discord.ButtonStyle.primary, custom_id="account:panel:change", emoji="✏️")
        register.callback = self.register_click; unregister.callback = self.unregister_click; change.callback = self.change_click
        row.add_item(register); row.add_item(unregister); row.add_item(change)
        container.add_item(row); self.add_item(container)
    async def register_click(self, interaction: discord.Interaction):
        uid = interaction.user.id
        if linked_account(uid):
            await interaction.response.send_message("Your Stumble Guys account is already linked.", ephemeral=True); return
        if any(r.get("user_id") == uid and r.get("status") == "pending" for r in data.get("account_requests", {}).values()):
            await interaction.response.send_message("You already have a pending account request.", ephemeral=True); return
        sent = await account_dm(uid, "Press the button below to start.", StartAccountRegistrationView())
        await interaction.response.send_message("Check your DMs to start account registration." if sent else "I could not DM you. Enable DMs from server members and try again.", ephemeral=True)
    async def unregister_click(self, interaction: discord.Interaction):
        uid = interaction.user.id; account = linked_account(uid)
        if not account:
            await interaction.response.send_message("You do not have a linked account.", ephemeral=True); return
        if any(r.get("user_id") == uid and r.get("status") == "pending" for r in data.get("account_requests", {}).values()):
            await interaction.response.send_message("You already have a pending account request.", ephemeral=True); return
        rid = f"unreg-{uid}-{now_ts()}"
        data["account_requests"][rid] = {"type":"unregister", "user_id":uid, "nickname":account.get("nickname", ""), "status":"pending", "created_at":now_ts()}
        save_data(); await create_account_review(rid)
        await interaction.response.send_message("Your unlink request has been sent for owner review. Your account remains linked until it is approved.", ephemeral=True)
    async def change_click(self, interaction: discord.Interaction):
        uid = interaction.user.id; account = linked_account(uid)
        if not account:
            await interaction.response.send_message("Link your Stumble Guys account before changing its nickname.", ephemeral=True); return
        if any(r.get("user_id") == uid and r.get("status") == "pending" for r in data.get("account_requests", {}).values()):
            await interaction.response.send_message("You already have a pending account request.", ephemeral=True); return
        data.setdefault("account_dm_flows", {})[str(uid)] = {"type":"change", "step":"nickname", "old_nickname":account.get("nickname", ""), "started_at":now_ts()}
        save_data()
        sent = await account_dm(uid, "**SG:** Send your new Stumble Guys nickname in this DM. After that, send any text or photo as evidence for the owner to review.")
        await interaction.response.send_message("I sent you a DM with the next steps." if sent else "I could not DM you. Enable DMs and try again.", ephemeral=True)


@bot.command(name="account")
async def account_cmd(ctx, subcommand: str = ""):
    try: await ctx.message.delete()
    except discord.HTTPException: pass
    if subcommand.lower() != "panel":
        try: await ctx.author.send("**SG:** Use `!account panel` to post the Link Account panel.")
        except discord.HTTPException: pass
        return
    if ctx.author.id != OWNER_ID:
        try: await ctx.author.send("**SG:** You are not allowed to use this command.")
        except discord.HTTPException:
            try: await ctx.send("You are not allowed to use this command.", delete_after=5)
            except discord.HTTPException: pass
        return
    if not ctx.guild:
        try: await ctx.author.send("**SG:** Run this command in the server where the panel should be posted.")
        except discord.HTTPException: pass
        return
    data["account_guild_id"] = ctx.guild.id; save_data()
    await ctx.send(view=AccountPanelV2())
    try: await ctx.author.send("**SG:** Link Account panel posted.")
    except discord.HTTPException: pass


@bot.command(name="commands")
async def commands_system_cmd(ctx, section: str = ""):
    try: await ctx.message.delete()
    except discord.HTTPException: pass
    if section.casefold() != "system":
        try: await ctx.author.send("**SG:** Use `!commands System` to view system commands.")
        except discord.HTTPException: pass
        return
    em = discord.Embed(title="System Commands", description="`!account panel` — post the Link Account panel (bot owner only)\n`!team @user` — invite a player to form a team\n`!team @user1 @user2` — invite two players to form a trio\n`!bracket r1` — create Round 1\n`!bracket r2` — create the next round\n`!match m1 CODE` — set a match room code\n`!qual @player` — qualify a player\n`!host @member` — add a tournament host\n`!edit qualified` — correct a qualified player\n`!delete tour` — delete the current tournament\n`!training tour @user` — create a training tournament", color=discord.Color.blurple())
    await ctx.send(embed=em, delete_after=30)

async def process_account_dm(message: discord.Message) -> bool:
    if message.guild is not None or message.author.bot:
        return False
    uid = str(message.author.id)
    flow = data.get("account_dm_flows", {}).get(uid)
    if not flow:
        return False
    request_type = flow.get("type")
    if flow.get("step") == "nickname":
        nickname = (message.content or "").strip()
        if not nickname or len(nickname) > 32 or message.attachments:
            await account_dm(int(uid), "**SG:** Please send only your Stumble Guys nickname as text (1–32 characters).")
            return True
        if nickname_reserved(nickname, exclude_user_id=int(uid)):
            await account_dm(int(uid), "**SG:** That nickname is already linked or reserved. Please send a different nickname.")
            return True
        if request_type == "register" and linked_account(int(uid)):
            data["account_dm_flows"].pop(uid, None); save_data()
            await account_dm(int(uid), "**SG:** Your account is already linked.")
            return True
        if request_type == "change":
            account = linked_account(int(uid))
            if not account:
                data["account_dm_flows"].pop(uid, None); save_data()
                await account_dm(int(uid), "**SG:** You do not have a linked account. Please register first.")
                return True
            if nickname.casefold() == str(account.get("nickname", "")).casefold():
                await account_dm(int(uid), "**SG:** That is already your linked nickname. Please send a different nickname.")
                return True
        rid = f"{request_type}-{uid}-{now_ts()}"
        request = {"type":request_type, "user_id":int(uid), "status":"pending", "created_at":now_ts()}
        if request_type == "register": request["nickname"] = nickname
        else:
            request["old_nickname"] = flow.get("old_nickname", "")
            request["new_nickname"] = nickname
        data["account_requests"][rid] = request
        flow["step"] = "evidence"; flow["request_id"] = rid; flow["nickname"] = nickname
        save_data()
        await create_account_review(rid)
        await account_dm(int(uid), "**SG:** Your request is pending review. You can now send any text or photos as evidence. The owner will review your request.")
        return True
    if flow.get("step") == "evidence":
        rid = flow.get("request_id"); request = data.get("account_requests", {}).get(rid)
        if not request or request.get("status") != "pending":
            data["account_dm_flows"].pop(uid, None); save_data(); return True
        channel_id = request.get("review_channel_id")
        channel = bot.get_channel(int(channel_id)) if channel_id else None
        if channel is None and channel_id:
            try: channel = await bot.fetch_channel(int(channel_id))
            except discord.HTTPException: channel = None
        if channel:
            content = message.content.strip() if message.content else ""
            embed = discord.Embed(description=f"**Evidence from <@{uid}>**\n{discord.utils.escape_mentions(content) if content else '*Attachment(s) received.*'}", color=discord.Color.blurple())
            try: await channel.send(embed=embed, files=[await a.to_file() for a in message.attachments] if message.attachments else [])
            except (discord.HTTPException, discord.Forbidden): log.exception("Could not forward account evidence from %s", uid)
        return True
    return False

# ============================== SCHEDULER / STARTUP ==============================
@tasks.loop(seconds=30)
async def tournament_scheduler():
    ts=now_ts()
    for tid,t in list(data["tournaments"].items()):
        if t.get("training"): continue
        if t.get("status") in ("deleted","finished","cancelled"): continue
        start=int(t.get("start_ts",0)); channel=bot.get_channel(int(t.get("channel_id",0)))
        if not channel: continue
        if ts < start-1800 and t.get("status") == "registration":
            countdown_minute = max(0, (start-1800-ts+59)//60)
            if t.get("last_countdown_minute") != countdown_minute:
                t["last_countdown_minute"] = countdown_minute
                save_data()
                await refresh_panel(tid)
        if ts >= start-2400 and not t.get("ping_30_sent"):
            await channel.send(f"<@&{TOURNAMENT_PING_ROLE_ID}> Registration opens in 10 minutes, at <t:{start-1800}:t>.", allowed_mentions=discord.AllowedMentions(roles=True))
            t["ping_30_sent"]=True; save_data()
        if ts >= start-1800 and not t.get("ping_open_sent") and t.get("status")=="registration":
            await channel.send(f"<@&{TOURNAMENT_PING_ROLE_ID}> Registration is now open!", allowed_mentions=discord.AllowedMentions(roles=True))
            t["ping_open_sent"]=True; save_data(); await refresh_panel(tid)
        for rnd in t.get("rounds", []):
            for match in rnd:
                sent_at = int(match.get("code_sent_at", 0) or 0)
                if match.get("status") == "PROCESSING" and sent_at and ts >= sent_at + 120 and not match.get("reminder_sent"):
                    for host_id in (t.get("host_ids") or [int(t.get("host_id", 0))])[:3]:
                        try:
                            await bot.fetch_user(int(host_id)).send(embed=discord.Embed(title="⏱️ Time's up!", description=f"Start Match #{match.get('number', match.get('id'))} now!", color=discord.Color.gold()))
                        except (discord.HTTPException, discord.NotFound):
                            pass
                    match["reminder_sent"] = True
                    save_data()
        if ts >= start+3600 and not t.get("rounds") and not t.get("cancelled_compensation_sent"):
            t["status"]="cancelled"; t["cancelled_compensation_sent"]=True; save_data()
            for p in t.get("players",[]):
                for uid in p["members"]:
                    await issue_chest(int(uid),10,"chest",f"tournament:{tid}:cancel-compensation:user:{uid}")
                    try: await bot.fetch_user(int(uid)).send(f"Tournament **{t['name']}** was cancelled because Round 1 did not start within one hour of the scheduled start. You received 10 Normal Chests as compensation.")
                    except discord.HTTPException: pass
            try: await bot.fetch_user(int(t["host_id"])).send(f"Tournament **{t['name']}** was cancelled because Round 1 was not started within one hour of the scheduled start.")
            except discord.HTTPException: pass
            await channel.send("This tournament was cancelled because Round 1 did not start within one hour of the scheduled start.")
            try: await channel.delete(reason="Tournament cancelled after start-time grace period")
            except discord.HTTPException: pass

@tournament_scheduler.before_loop
async def before_scheduler(): await bot.wait_until_ready()

@bot.event
async def on_ready():
    for tid in data.get("tournaments",{}):
        bot.add_view(TournamentView(tid))
    for invite_id, invite in data.get("team_invites", {}).items():
        if invite.get("status") == "pending":
            bot.add_view(TeamInviteView(invite_id))
        elif invite.get("status") == "accepted":
            bot.add_view(TeamTournamentSelectView(invite_id))
    bot.add_view(CreationPanelView())
    bot.add_view(AccountPanelV2())
    bot.add_view(StartAccountRegistrationView())
    for request_id, request in data.get("account_requests", {}).items():
        if request.get("status") == "pending" and request.get("review_channel_id"):
            if request.get("type") == "register": bot.add_view(AccountRegisterReviewView(request_id))
            elif request.get("type") == "change": bot.add_view(AccountChangeReviewView(request_id))
            elif request.get("type") == "unregister": bot.add_view(AccountUnregisterReviewView(request_id))
    try:
        synced=await bot.tree.sync(); log.info("Synced %s slash commands",len(synced))
    except discord.HTTPException: log.exception("Command sync failed")
    if not tournament_scheduler.is_running(): tournament_scheduler.start()
    log.info("Logged in as %s (%s)",bot.user,bot.user.id)

@bot.event
async def on_command_error(ctx,error):
    if isinstance(error,commands.CommandNotFound): return
    if isinstance(error,commands.MissingRequiredArgument):
        m=await ctx.send("Missing information. Check the command format."); await asyncio.sleep(1)
        try: await m.delete()
        except discord.HTTPException: pass
        return
    if isinstance(error,commands.BadArgument):
        m=await ctx.send("Invalid user or argument."); await asyncio.sleep(1)
        try: await m.delete()
        except discord.HTTPException: pass
        return
    log.error("Command error",exc_info=error)
    await ctx.reply("An unexpected error occurred. The bot owner can check Railway logs.")


@bot.command(name="h")
async def h_cmd(ctx, section: str = ""):
    try: await ctx.message.delete()
    except discord.HTTPException: pass
    if section.lower() != "tour":
        msg=await ctx.send("Use `!h tour`."); await asyncio.sleep(1)
        try: await msg.delete()
        except discord.HTTPException: pass
        return
    em=discord.Embed(title="Tournament Bot — Commands", description="""`/create tour channel:#channel` — post the creation panel
`!bracket r1` — create Round 1
`!bracket r2` — create the next round
`!match 1 CODE` — set a match code and mark PROGRESS
`!qual @player` — qualify the player from their current match
`!host @member` — add a Host (maximum 3)
`!edit qualified` — correct a qualified player
`!delete tour` — delete this tournament
`!training tour @user` — create a practice tournament""", color=discord.Color.blurple())
    await ctx.send(embed=em)

@bot.tree.command(name="tournament_help", description="Show tournament commands")
async def tournament_help(i: discord.Interaction):
    if not is_owner(i.user):
        await i.response.send_message("Only the bot owner can use this command.", ephemeral=True)
        return
    e=discord.Embed(title="Tournament Bot — Help", description="""`/create tour channel:#channel` — post the creation panel
`!bracket r1` — create Round 1
`!bracket r2` — create next round
`!match 1 267899` — send a room code
`!qual @winner` — qualify the player from their current match
`!host @member` — add a Host (maximum 3)
`!delete tour` — delete this tournament
`!training tour @user` — training mode
`!team @user` — create a duo
`!team @user1 @user2` — create a trio
`!edit qualified` — correct a qualified player""", color=discord.Color.blurple())
    await i.response.send_message(embed=e,ephemeral=True)


@bot.event
async def on_message(message: discord.Message):
    # Tournament channels allow normal chat for participants and hosts.
    # Commands are still processed normally; each command enforces its own permissions.
    if message.author.bot:
        return
    if message.guild is None:
        handled = await process_account_dm(message)
        if handled:
            return
    # Delete every prefix-command message globally before command dispatch.
    if message.content.startswith("!"):
        try: await message.delete()
        except discord.HTTPException: pass
    await bot.process_commands(message)

if __name__ == "__main__":
    bot.run(TOKEN)
