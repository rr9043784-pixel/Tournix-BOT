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

REWARDS = {
    1: 40,
    2: 30,
    3: 25,
    4: 20,
    5: 15,
    6: 12,
    7: 10,
    8: 8,
    9: 5,
    10: 3,
}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tournix")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
)

# ============================== RAILWAY WEB SERVER ==============================

web = Flask(__name__)


@web.get("/")
def health():
    return "Tournament Bot is online!", 200


def run_web():
    web.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        use_reloader=False,
    )


Thread(target=run_web, daemon=True).start()

# ============================== STORAGE ==============================

def fresh_data() -> dict[str, Any]:
    return {
        "next_id": 1,
        "tournaments": {},
        "reward_transactions": [],
        "stats": {},
        "team_invites": {},
    }


def load_data():
    if not DATA_FILE.exists():
        return fresh_data()

    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            obj = json.load(f)

        if not isinstance(obj, dict):
            raise ValueError("Invalid data format")

        base = fresh_data()
        base.update(obj)
        return base

    except Exception:
        log.exception(
            "Could not read tournament data; starting with empty data"
        )
        return fresh_data()


data = load_data()
data_lock = asyncio.Lock()


def save_data():
    tmp = DATA_FILE.with_suffix(".tmp")

    with tmp.open("w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2,
        )
        f.flush()
        os.fsync(f.fileno())

    tmp.replace(DATA_FILE)


def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def get_tour(tid: str):
    return data["tournaments"].get(str(tid))


def tour_by_channel(channel_id: int):
    for tid, t in data["tournaments"].items():
        if (
            int(t.get("channel_id", 0)) == channel_id
            and t.get("status") not in ("deleted",)
        ):
            return tid, t

    return None, None


def is_owner(user: discord.abc.User) -> bool:
    return user.id == OWNER_ID


def has_host_role(member: discord.abc.User) -> bool:
    return (
        isinstance(member, discord.Member)
        and any(role.id == HOST_ROLE_ID for role in member.roles)
    )


def may_host(member: discord.abc.User) -> bool:
    return is_owner(member) or has_host_role(member)


def host_allowed(member: discord.abc.User, t: dict) -> bool:
    if (
        is_training(t)
        and member.id == int(t.get("training_student_id", 0))
    ):
        return True

    return is_owner(member) or has_host_role(member)


def is_training(t: dict) -> bool:
    return bool(t.get("training"))


    # ============================== EMBEDS / VIEWS ==============================
def tournament_embed(tid: str) -> discord.Embed:
    t = get_tour(tid)
    if not t:
        return discord.Embed(
            title="Tournament not found",
            color=discord.Color.red(),
        )

    players = t.get("players", [])
    start = int(t.get("start_ts", now_ts()))
    registration_open = (
        now_ts() >= start - 1200
        and t.get("status") == "registration"
    )

    status = (
        "Registration open"
        if registration_open
        else (
            "Registration opens soon"
            if now_ts() < start - 1200
            else t.get("status", "Setup").replace("_", " ").title()
        )
    )

    em = discord.Embed(
        title=f"🏆 {t.get('name', 'Tournament')}",
        description=t.get("description") or "No description provided.",
        color=discord.Color.blurple(),
    )

    em.add_field(name="Game version", value=t.get("version", "—"), inline=True)
    em.add_field(name="Map", value=t.get("map", "—"), inline=True)
    em.add_field(name="Format", value=t.get("format", "1v1"), inline=True)
    em.add_field(name="Ability", value=t.get("ability", "—"), inline=True)
    em.add_field(
        name="Start time",
        value=f"<t:{start}:F>\n<t:{start}:R>",
        inline=True,
    )
    em.add_field(name="Status", value=status, inline=True)

    max_players = int(t.get("max_players", 0))
    em.add_field(
        name="Players",
        value=f"{len(players)}/{max_players if max_players else '∞'}",
        inline=True,
    )

    if players:
        lines = []

        for i, p in enumerate(players[:25], 1):
            if t.get("training"):
                members = discord.utils.escape_markdown(
                    p.get("nickname", "Training Bot")
                )
            else:
                members = " / ".join(
                    f"<@{uid}>" for uid in p["members"]
                )

            nick = discord.utils.escape_markdown(
                p.get("nickname", "Player")
            )
            lines.append(f"**{i}.** {members}\n-#➜{nick}")

        if len(players) > 25:
            lines.append(f"…and {len(players)-25} more")

        em.add_field(
            name="Registered players / teams",
            value="\n".join(lines),
            inline=False,
        )
    else:
        em.add_field(
            name="Registered players",
            value="No one has registered yet.",
            inline=False,
        )

    em.set_footer(
        text=f"Tournament ID: {tid} • Register 20 minutes before start"
    )
    return em


class NicknameModal(discord.ui.Modal, title="Tournament registration"):
    nickname = discord.ui.TextInput(
        label="In-game nickname",
        placeholder="Enter your exact in-game nickname",
        max_length=32,
    )

    def __init__(self, tid: str):
        super().__init__()
        self.tid = tid

    async def on_submit(self, interaction: discord.Interaction):
        t = get_tour(self.tid)

        if (
            not t
            or t.get("status") != "registration"
            or now_ts() < int(t.get("start_ts", 0)) - 1200
        ):
            await interaction.response.send_message(
                "Registration is not open yet.",
                ephemeral=True,
            )
            return

        if now_ts() >= int(t.get("start_ts", 0)):
            await interaction.response.send_message(
                "Registration is closed.",
                ephemeral=True,
            )
            return

        uid = interaction.user.id

        try:
            team_size = int(
                str(t.get("format", "1v1")).split("v", 1)[0]
            )
        except (ValueError, IndexError):
            team_size = 1

        if team_size != 1:
            await interaction.response.send_message(
                "This tournament requires a team. Use `!team` to create one.",
                ephemeral=True,
            )
            return

        if any(uid in p["members"] for p in t["players"]):
            await interaction.response.send_message(
                "You are already registered for this tournament.",
                ephemeral=True,
            )
            return

        nick = str(self.nickname.value).strip()

        if not nick:
            await interaction.response.send_message(
                "Please enter a valid nickname.",
                ephemeral=True,
            )
            return

        if any(
            p["nickname"].casefold() == nick.casefold()
            for p in t["players"]
        ):
            await interaction.response.send_message(
                "That in-game nickname is already registered.",
                ephemeral=True,
            )
            return

        cap = int(t.get("max_players", 0))

        if cap and len(t["players"]) >= cap:
            await interaction.response.send_message(
                "This tournament is full.",
                ephemeral=True,
            )
            return

        t["players"].append({
            "members": [uid],
            "nickname": nick,
            "team_name": None,
        })

        save_data()

        await interaction.response.send_message(
            "You are registered successfully!",
            ephemeral=True,
        )

        await refresh_panel(self.tid)


class TournamentView(discord.ui.View):
    def __init__(self, tid: str):
        super().__init__(timeout=None)
        self.tid = str(tid)

        for label, style, cid, callback in [
            (
                "Register",
                discord.ButtonStyle.success,
                f"tour:register:{tid}",
                self.register,
            ),
            (
                "Unregister",
                discord.ButtonStyle.secondary,
                f"tour:unregister:{tid}",
                self.unregister,
            ),
            (
                "My Code",
                discord.ButtonStyle.primary,
                f"tour:mycode:{tid}",
                self.mycode,
            ),
            (
                "Qualified",
                discord.ButtonStyle.secondary,
                f"tour:qualified:{tid}",
                self.qualified,
            ),
        ]:
            b = discord.ui.Button(
                label=label,
                style=style,
                custom_id=cid,
            )
            b.callback = callback
            self.add_item(b)

    async def register(self, i):
        t = get_tour(self.tid)

        if (
            not t
            or t.get("status") != "registration"
            or now_ts() < int(t.get("start_ts", 0)) - 1200
        ):
            await i.response.send_message(
                "Registration is not open yet.",
                ephemeral=True,
            )
            return

        await i.response.send_modal(NicknameModal(self.tid))

    async def unregister(self, i):
        t = get_tour(self.tid)

        if (
            not t
            or t.get("status") != "registration"
            or now_ts() >= int(t.get("start_ts", 0))
        ):
            await i.response.send_message(
                "Registration changes are closed.",
                ephemeral=True,
            )
            return

        before = len(t["players"])

        t["players"] = [
            p for p in t["players"]
            if i.user.id not in p["members"]
        ]

        if before == len(t["players"]):
            await i.response.send_message(
                "You are not registered.",
                ephemeral=True,
            )
            return

        save_data()

        await i.response.send_message(
            "You have been unregistered.",
            ephemeral=True,
        )

        await refresh_panel(self.tid)

    async def mycode(self, i):
        t = get_tour(self.tid)

        if not t or not any(
            i.user.id in p["members"]
            for p in t.get("players", [])
        ):
            await i.response.send_message(
                "You are not a participant in this tournament.",
                ephemeral=True,
            )
            return

        match = next(
            (
                m
                for rnd in t.get("rounds", [])
                for m in rnd
                if (
                    i.user.id
                    in m.get("members1", []) + m.get("members2", [])
                    and m.get("status") == "PROCESSING"
                )
            ),
            None,
        )

        if not match:
            if any(
                (
                    i.user.id
                    in m.get("members1", []) + m.get("members2", [])
                    and m.get("status") == "FINISHED"
                )
                for rnd in t.get("rounds", [])
                for m in rnd
            ):
                msg = "Your match has already been completed."
            else:
                msg = (
                    "Your match has not started yet. "
                    "Please wait for the host."
                )

            await i.response.send_message(msg, ephemeral=True)
            return

        await i.response.send_message(
            f"Your current room code is: **{match.get('code', 'Not provided')}**",
            ephemeral=True,
        )

    async def qualified(self, i):
        t = get_tour(self.tid)

        if not t:
            await i.response.send_message(
                "Tournament not found.",
                ephemeral=True,
            )
            return

        sections = []
        rounds = t.get("rounds", [])
        current_round = rounds[-1] if rounds else []
        qualified_ids = []
        pending_lines = []
        bye_lines = []

        # Derive the live status from the current round.
        for match in current_round:
            winner_ids = match.get("winner") or []
            status = str(match.get("status", "WAITING")).upper()

            if status == "BYE":
                for uid in winner_ids:
                    if uid not in qualified_ids:
                        qualified_ids.append(uid)

                    bye_lines.append(
                        f"• <@{uid}> — automatic advance (BYE) "
                        f"· `{match.get('id', '?')}`"
                    )

            elif status == "FINISHED":
                for uid in winner_ids:
                    if uid not in qualified_ids:
                        qualified_ids.append(uid)

            else:
                left = match.get("nickname1", "Player 1")
                right = match.get("nickname2", "Player 2")

                pending_lines.append(
                    f"• `{match.get('id', '?')}` — "
                    f"{left} vs {right}: **WAITING FOR RESULT**"
                )

        # Include stored qualified IDs, including team members.
        for uid in t.get("qualified", []):
            if uid not in qualified_ids:
                qualified_ids.append(uid)

        if qualified_ids:
            sections.append(
                "**Qualified for the next round**\n"
                + "\n".join(f"• <@{uid}>" for uid in qualified_ids)
            )
        else:
            sections.append(
                "**Qualified for the next round**\n"
                "No winners recorded yet."
            )

        if bye_lines:
            sections.append(
                "**BYE qualifiers**\n" + "\n".join(bye_lines)
            )

        if pending_lines:
            sections.append(
                "**Pending matches**\n" + "\n".join(pending_lines)
            )

        if not rounds:
            sections = ["The bracket has not started yet."]

        message = "\n\n".join(sections)

        if len(message) > 1900:
            message = message[:1890] + "…"

        await i.response.send_message(
            message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def refresh_panel(tid: str):
    t = get_tour(tid)

    if not t:
        return

    try:
        channel = (
            bot.get_channel(int(t["channel_id"]))
            or await bot.fetch_channel(int(t["channel_id"]))
        )

        msg = await channel.fetch_message(
            int(t["panel_message_id"])
        )

        await msg.edit(
            embed=tournament_embed(tid),
            view=TournamentView(tid),
        )

    except (
        KeyError,
        discord.HTTPException,
        discord.NotFound,
        discord.Forbidden,
    ):
        log.exception(
            "Could not refresh tournament panel %s",
            tid,
    )

# ============================== CREATION FLOW ==============================

class TourDetailsModal(discord.ui.Modal, title="Classic Tournament — Details"):
    start_time = discord.ui.TextInput(
        label="Start time (UTC)",
        placeholder="2026-10-20 18:30",
        max_length=20,
    )
    description = discord.ui.TextInput(
        label="Description",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=800,
    )

    def __init__(self, channel_id: int, creator_id: int, first: dict):
        super().__init__()
        self.channel_id = channel_id
        self.creator_id = creator_id
        self.first = first

    async def on_submit(self, i: discord.Interaction):
        try:
            dt = datetime.strptime(
                str(self.start_time.value).strip(),
                "%Y-%m-%d %H:%M",
            ).replace(tzinfo=timezone.utc)
            start_ts = int(dt.timestamp())
        except ValueError:
            await i.response.send_message(
                "Use UTC format `YYYY-MM-DD HH:MM`, "
                "for example `2026-10-20 18:30`.",
                ephemeral=True,
            )
            return

        if start_ts <= now_ts() + 1200:
            await i.response.send_message(
                "Start time must be more than 20 minutes in the future.",
                ephemeral=True,
            )
            return

        await i.response.send_message(
            "Choose the tournament ability:",
            view=AbilitySelectView(
                self.first,
                self.creator_id,
                start_ts,
                str(self.description.value).strip(),
            ),
            ephemeral=True,
        )


ABILITY_OPTIONS = [
    ("Double Jump", "Double Jump"),
    ("Invisibility", "Invisibility"),
    ("Shield", "Shield"),
    ("Speed Boost", "Speed Boost"),
    ("Punch", "Punch"),
    ("Kick", "Kick"),
    ("Block", "Block"),
    ("Hug", "Hug"),
    ("Ball", "Ball"),
    ("Banana", "Banana"),
    ("No Ability", "No Ability"),
    ("Custom / Other", "Custom / Other"),
]


class AbilitySelect(discord.ui.Select):
    def __init__(
        self,
        first: dict,
        creator_id: int,
        start_ts: int,
        description: str,
    ):
        self.first = first
        self.creator_id = creator_id
        self.start_ts = start_ts
        self.description_text = description

        opts = [
            discord.SelectOption(label=label, value=value)
            for label, value in ABILITY_OPTIONS
        ]

        super().__init__(
            placeholder="Select an ability…",
            min_values=1,
            max_values=1,
            options=opts,
            custom_id="tour_ability_select",
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message(
                "Only the person who started this setup can choose the ability.",
                ephemeral=True,
            )
            return

        await create_tournament(
            interaction,
            self.first,
            self.creator_id,
            self.start_ts,
            self.description_text,
            self.values[0],
        )


class AbilitySelectView(discord.ui.View):
    def __init__(
        self,
        first: dict,
        creator_id: int,
        start_ts: int,
        description: str,
    ):
        super().__init__(timeout=300)
        self.add_item(
            AbilitySelect(first, creator_id, start_ts, description)
        )


async def create_tournament(
    i: discord.Interaction,
    first: dict,
    creator_id: int,
    start_ts: int,
    description: str,
    ability: str,
):
    await i.response.defer(ephemeral=True, thinking=True)

    guild = i.guild
    category = (
        guild.get_channel(TOURNAMENT_CATEGORY_ID)
        if guild else None
    )

    if not isinstance(category, discord.CategoryChannel):
        await i.followup.send(
            "The tournament category was not found. "
            "Check TOURNAMENT_CATEGORY_ID.",
            ephemeral=True,
        )
        return

    slug = discord.utils.slugify(first["name"])[:70] or "tournament"

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
        ),
    }

    try:
        channel = await guild.create_text_channel(
            f"🎮┃{slug}",
            category=category,
            overwrites=overwrites,
            reason="Tournament created",
        )
    except discord.HTTPException:
        log.exception("Failed creating tournament channel")
        await i.followup.send(
            "I could not create the tournament channel. "
            "Check my channel permissions.",
            ephemeral=True,
        )
        return

    tid = str(data["next_id"])
    data["next_id"] += 1

    t = {
        **first,
        "id": tid,
        "guild_id": guild.id,
        "channel_id": channel.id,
        "host_id": creator_id,
        "created_by": creator_id,
        "start_ts": start_ts,
        "description": description,
        "ability": ability,
        "status": "registration",
        "players": [],
        "rounds": [],
        "matches": [],
        "qualified": [],
        "ping_30_sent": False,
        "ping_open_sent": False,
        "cancelled_compensation_sent": False,
        "rewarded": [],
        "training": False,
        "panel_message_id": None,
    }

    data["tournaments"][tid] = t
    save_data()

    await channel.send(
        content=f"<@&{TOURNAMENT_PING_ROLE_ID}>",
        allowed_mentions=discord.AllowedMentions(roles=True),
    )

    panel = await channel.send(
        embed=tournament_embed(tid),
        view=TournamentView(tid),
    )

    t["panel_message_id"] = panel.id
    save_data()

    ann = bot.get_channel(ANNOUNCEMENTS_CHANNEL_ID)

    if ann:
        await ann.send(
            embed=discord.Embed(
                title=f"🏆 {t['name']}",
                description=(
                    "A new tournament is available!\n"
                    f"[Open tournament channel]({channel.jump_url})"
                ),
                color=discord.Color.blurple(),
            )
        )

    await i.followup.send(
        f"Tournament created: {channel.mention}",
        ephemeral=True,
    )


class ClassicTourModal(
    discord.ui.Modal,
    title="Classic Tournament — Setup",
):
    name = discord.ui.TextInput(
        label="Tournament name",
        placeholder="rull",
        max_length=80,
    )
    version = discord.ui.TextInput(
        label="Game version",
        placeholder="0.103",
        max_length=20,
    )
    map_name = discord.ui.TextInput(
        label="Map",
        placeholder="Block Dash Classic",
        max_length=80,
    )
    format_name = discord.ui.TextInput(
        label="Format",
        placeholder="1v1, 2v2, 3v3",
        max_length=10,
    )
    max_players = discord.ui.TextInput(
        label="Maximum players / teams",
        placeholder="16",
        max_length=4,
    )

    def __init__(self, channel_id: int, creator_id: int):
        super().__init__()
        self.channel_id = channel_id
        self.creator_id = creator_id

    async def on_submit(self, i: discord.Interaction):
        try:
            maximum = int(str(self.max_players.value).strip())
        except ValueError:
            await i.response.send_message(
                "Maximum players must be a number.",
                ephemeral=True,
            )
            return

        if maximum < 2 or maximum > 500:
            await i.response.send_message(
                "Maximum players must be between 2 and 500.",
                ephemeral=True,
            )
            return

        fmt = str(self.format_name.value).strip().lower()

        if not re.fullmatch(r"([1-9][0-9]?)v\1", fmt):
            await i.response.send_message(
                "Format must use matching team sizes, "
                "such as `1v1`, `2v2`, or `3v3`.",
                ephemeral=True,
            )
            return

        first = {
            "name": str(self.name.value).strip(),
            "version": str(self.version.value).strip(),
            "map": str(self.map_name.value).strip(),
            "format": fmt,
            "max_players": maximum,
        }

        await i.response.send_message(
            "Setup details are ready. Continue to enter "
            "the start time, description, and ability.",
            view=ContinueDetailsView(
                self.channel_id,
                self.creator_id,
                first,
            ),
            ephemeral=True,
        )


class ContinueDetailsView(discord.ui.View):
    def __init__(
        self,
        channel_id: int,
        creator_id: int,
        first: dict,
    ):
        super().__init__(timeout=300)
        self.channel_id = channel_id
        self.creator_id = creator_id
        self.first = first

    @discord.ui.button(
        label="Continue setup",
        style=discord.ButtonStyle.primary,
    )
    async def continue_setup(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message(
                "Only the person who started this setup can continue.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            TourDetailsModal(
                self.channel_id,
                self.creator_id,
                self.first,
            )
        )


class CreationPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

        b = discord.ui.Button(
            label="Classic Tour",
            style=discord.ButtonStyle.primary,
            emoji="🏆",
            custom_id="tour:create:classic",
        )
        b.callback = self.classic
        self.add_item(b)

    async def classic(self, i: discord.Interaction):
        if i.channel_id != PANEL_CHANNEL_ID:
            await i.response.send_message(
                "Use the tournament creation panel in the designated channel.",
                ephemeral=True,
            )
            return

        if not may_host(i.user):
            await i.response.send_message(
                "Only the bot owner or a member with the Host role "
                "can create tournaments.",
                ephemeral=True,
            )
            return

        await i.response.send_modal(
            ClassicTourModal(i.channel_id, i.user.id)
        )


class CreateGroup(app_commands.Group):
    def __init__(self):
        super().__init__(
            name="create",
            description="Create bot panels",
        )

    @app_commands.command(
        name="tour",
        description="Post the tournament creation panel",
    )
    @app_commands.describe(
        channel="Channel where the creation panel should be posted"
    )
    async def tour(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        if not (
            is_owner(interaction.user)
            or has_host_role(interaction.user)
        ):
            await interaction.response.send_message(
                "Only the bot owner or a member with the Host role "
                "can create the panel.",
                ephemeral=True,
            )
            return

        if channel.id != PANEL_CHANNEL_ID:
            await interaction.response.send_message(
                f"Please select <#{PANEL_CHANNEL_ID}>.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🏆 Tournament Creation",
            description="Choose a tournament type to begin.",
            color=discord.Color.blurple(),
        )

        await channel.send(
            embed=embed,
            view=CreationPanelView(),
        )

        await interaction.response.send_message(
            "Creation panel posted.",
            ephemeral=True,
        )


bot.tree.add_command(CreateGroup())

# ============================== BRACKET / RESULTS ==============================

def current_matches(t):
    if not t.get("rounds"):
        return []
    return t["rounds"][-1]


def player_label(t, uid):
    for p in t.get("players", []):
        if uid in p["members"]:
            return p["nickname"]
    return f"User {uid}"


def create_round(t, players, round_no):
    pool = list(players)
    random.shuffle(pool)
    matches = []
    n = 1

    while pool:
        a = pool.pop()

        if not pool:
            matches.append({
                "id": f"m{n}",
                "members1": list(a["members"]),
                "members2": [],
                "nickname1": a["nickname"],
                "nickname2": "BYE",
                "status": "BYE",
                "winner": list(a["members"]),
                "code": None
            })
            t["qualified"].extend(a["members"])
            break

        b = pool.pop()

        matches.append({
            "id": f"m{n}",
            "members1": list(a["members"]),
            "members2": list(b["members"]),
            "nickname1": a["nickname"],
            "nickname2": b["nickname"],
            "status": "WAITING",
            "winner": None,
            "code": None
        })
        n += 1

    t["rounds"].append(matches)
    return matches


async def send_round(tid, round_no):
    t = get_tour(tid)
    if not t:
        return

    ch = bot.get_channel(int(t["channel_id"]))
    if not ch:
        return

    matches = t["rounds"][round_no - 1]
    lines = []

    for m in matches:
        if t.get("training"):
            a = discord.utils.escape_markdown(
                m.get("nickname1", "Training Bot")
            )
            b = (
                discord.utils.escape_markdown(
                    m.get("nickname2", "BYE")
                )
                if m.get("members2")
                else "**BYE**"
            )
        else:
            a = " / ".join(
                f"<@{x}>" for x in m["members1"]
            )
            b = (
                " / ".join(
                    f"<@{x}>" for x in m["members2"]
                )
                if m["members2"]
                else "**BYE**"
            )

        lines.append(
            f"**{m['id']}** · {a} vs {b} · `{m['status']}`"
        )

    em = discord.Embed(
        title=f"⚔️ Round {round_no}",
        description="\n".join(lines) or "No matches.",
        color=discord.Color.blurple()
    )
    await ch.send(embed=em)


@bot.command(name="bracket")
async def bracket_cmd(ctx, round_label: str = ""):
    tid, t = tour_by_channel(ctx.channel.id)

    if not t:
        await ctx.reply(
            "This command must be used in a tournament channel."
        )
        return

    if not host_allowed(ctx.author, t):
        await ctx.reply(
            "Only the bot owner or a Host can manage this tournament."
        )
        return

    try:
        rn = int(round_label.lower().replace("r", ""))
    except ValueError:
        await ctx.reply(
            "Use `!bracket r1`, `!bracket r2`, and so on."
        )
        return

    if rn < 1:
        await ctx.reply("Round number must be at least 1.")
        return

    if rn == 1:
        if t["rounds"]:
            await ctx.reply("Round 1 has already been created.")
            return

        if len(t["players"]) < 2:
            await ctx.reply(
                "At least 2 registered players or teams are required."
            )
            return

        t["status"] = "bracket_started"
        t["registration_locked_at"] = now_ts()

        create_round(t, t["players"], 1)
        save_data()

        await send_round(tid, 1)
        await refresh_panel(tid)

        await ctx.reply(
            "Round 1 created. Registration and setup are now locked."
        )
        return

    if rn != len(t["rounds"]) + 1:
        await ctx.reply(
            f"The next available round is R{len(t['rounds']) + 1}."
        )
        return

    prev = t["rounds"][-1]

    if any(
        m["status"] not in ("FINISHED", "BYE")
        for m in prev
    ):
        await ctx.reply("The previous round is not complete yet.")
        return

    winner_groups = []

    for m in prev:
        if m.get("winner"):
            winner_groups.append(m["winner"])

    if len(winner_groups) <= 1:
        await finish_tournament(tid)
        await ctx.reply("Tournament finished.")
        return

    roster = []

    for members in winner_groups:
        p = next(
            (
                x for x in t["players"]
                if set(members).issubset(set(x["members"]))
            ),
            None
        )

        if p and p not in roster:
            roster.append(p)

    t["qualified"] = []
    create_round(t, roster, rn)
    save_data()

    await send_round(tid, rn)
    await ctx.reply(f"Round {rn} created.")

    if (
        all(
            m["status"] in ("FINISHED", "BYE")
            for m in t["rounds"][-1]
        )
        and len(t["rounds"][-1]) == 1
    ):
        await finish_tournament(tid)


@bot.command(name="match")
async def match_cmd(ctx, match_id: str = "", code: str = ""):
    tid, t = tour_by_channel(ctx.channel.id)

    if not t:
        await ctx.reply(
            "Use this command inside a tournament channel."
        )
        return

    if not host_allowed(ctx.author, t):
        await ctx.reply(
            "Only the bot owner or a Host can manage matches."
        )
        return

    m = next(
        (
            m for m in current_matches(t)
            if m["id"].lower() == match_id.lower()
        ),
        None
    )

    if not m:
        await ctx.reply(
            "Match not found. Use `!match m1 267899` "
            "for the current round."
        )
        return

    if m["status"] in ("FINISHED", "BYE"):
        await ctx.reply(
            "This match has already been completed."
        )
        return

    if not code.isdigit() or not 4 <= len(code) <= 12:
        await ctx.reply("Please provide a valid room code.")
        return

    m["code"] = code
    m["status"] = "PROCESSING"
    m["code_sent_at"] = now_ts()
    m["reminder_sent"] = False
    m["code_author_id"] = ctx.author.id
    save_data()

    ids = m["members1"] + m["members2"]

    if t.get("training"):
        await ctx.reply(
            f"🎓 Training simulation: room code `{code}` assigned "
            f"to `{m['id']}`. Use `!qual {m['id']}` to simulate "
            "the result; virtual participants do not receive DMs."
        )
        return

    for uid in ids:
        try:
            await bot.fetch_user(int(uid)).send(
                f"Your tournament room code is **{code}**. "
                f"Match: **{m['id']}**."
            )
        except (discord.HTTPException, discord.NotFound):
            await ctx.send(
                f"<@{uid}> I couldn't DM you. "
                "Click **My Code** to view the room code.",
                allowed_mentions=discord.AllowedMentions(
                    users=True
                )
            )

    await ctx.reply(
        f"Match `{m['id']}` is now **PROCESSING**. "
        "I sent the room code to all participants."
    )


@bot.command(name="qual")
async def qual_cmd(
    ctx,
    match_id: str = "",
    winner: discord.Member = None
):
    tid, t = tour_by_channel(ctx.channel.id)

    if not t:
        await ctx.reply(
            "Use this command inside a tournament channel."
        )
        return

    if not host_allowed(ctx.author, t):
        await ctx.reply(
            "Only the bot owner or a Host can qualify winners."
        )
        return

    m = next(
        (
            m for m in current_matches(t)
            if m["id"].lower() == match_id.lower()
        ),
        None
    )

    if not m:
        await ctx.reply(
            "Match not found. Example: `!qual m1 @player`."
        )
        return

    if m["status"] in ("FINISHED", "BYE"):
        await ctx.reply("This match is already completed.")
        return

    if t.get("training"):
        winning_side = random.choice([1, 2])

        m["winner"] = list(
            m["members1"] if winning_side == 1
            else m["members2"]
        )

        if not m["winner"]:
            m["winner"] = list(
                m["members1"] or m["members2"]
            )

        m["status"] = "FINISHED"
        t["qualified"].extend(m["winner"])
        save_data()

        virtual_winner = (
            m["nickname1"]
            if m["winner"] == m["members1"]
            else m["nickname2"]
        )

        await ctx.reply(
            f"🎓 Training simulation: `{m['id']}` finished. "
            f"Virtual winner: **{virtual_winner}**. "
            f"In a real tournament, use `!qual {m['id']} @winner`."
        )

        if (
            t.get("rounds")
            and len(t["rounds"][-1]) == 1
            and all(
                x["status"] in ("FINISHED", "BYE")
                for x in t["rounds"][-1]
            )
        ):
            await finish_tournament(tid)

        return

    if (
        winner is None
        or winner.id not in m["members1"] + m["members2"]
    ):
        await ctx.reply(
            "The winner must be one of the participants in that match."
        )
        return

    # Teams advance together if any team member is selected.
    m["winner"] = list(
        m["members1"]
        if winner.id in m["members1"]
        else m["members2"]
    )

    m["status"] = "FINISHED"
    t["qualified"].extend(m["winner"])
    save_data()

    await ctx.reply(
        f"🏆 <@{winner.id}> qualified from `{m['id']}`."
    )

    if (
        t.get("rounds")
        and len(t["rounds"][-1]) == 1
        and all(
            x["status"] in ("FINISHED", "BYE")
            for x in t["rounds"][-1]
        )
    ):
        await finish_tournament(tid)

# ============================== REWARDS / RESULTS ==============================

async def issue_chest(
    uid: int,
    amount: int,
    chest_type: str,
    key: str
):
    if not CHEST_API_URL or not CHEST_API_KEY:
        log.error(
            "Chest API is not configured; reward %s was not issued",
            key
        )
        return False

    if key in data["reward_transactions"]:
        return True

    try:
        timeout = aiohttp.ClientTimeout(total=12)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                CHEST_API_URL + "/api/chests/add",
                json={
                    "user_id": uid,
                    "amount": amount,
                    "chest_type": chest_type,
                    "transaction_id": key
                },
                headers={
                    "Authorization": f"Bearer {CHEST_API_KEY}"
                }
            ) as resp:

                if resp.status != 200:
                    log.error(
                        "Chest API failed (%s): %s",
                        resp.status,
                        (await resp.text())[:300]
                    )
                    return False

                result = await resp.json()

                if result.get("ok"):
                    data["reward_transactions"].append(key)
                    save_data()
                    return True

    except Exception:
        log.exception("Chest API request failed for %s", key)

    return False


async def finish_tournament(tid: str):
    t = get_tour(tid)

    if not t or t.get("status") == "finished":
        return

    t["status"] = "finished"
    save_data()

    if t.get("training"):
        ch = bot.get_channel(int(t.get("channel_id", 0)))

        if ch:
            await ch.send(
                "🎓 Training completed! This practice session did not "
                "issue rewards or affect regular tournament statistics."
            )

        save_data()
        return

    # Single-elimination placement bands:
    # champion #1, finalist #2, then groups such as #3-4 and #5-8.
    rounds = t.get("rounds", [])
    champion = []
    placement_groups = []

    if (
        rounds
        and len(rounds[-1]) == 1
        and rounds[-1][0].get("winner")
    ):
        final_match = rounds[-1][0]
        champion = list(final_match["winner"])

        finalist = list(
            final_match["members2"]
            if champion == final_match["members1"]
            else final_match["members1"]
        )

        placement_groups.append((1, 1, [champion]))

        if finalist:
            placement_groups.append((2, 2, [finalist]))

        next_place = 3

        for rnd in reversed(rounds[:-1]):
            losers = []

            for match in rnd:
                if (
                    match.get("status") != "FINISHED"
                    or not match.get("winner")
                ):
                    continue

                loser = (
                    match["members2"]
                    if match.get("winner") == match.get("members1")
                    else match["members1"]
                )

                if loser:
                    losers.append(list(loser))

            if losers:
                placement_groups.append(
                    (
                        next_place,
                        next_place + len(losers) - 1,
                        losers
                    )
                )
                next_place += len(losers)

    results = []
    ultra_awarded = False
    ultra_roll_won = bool(champion) and random.random() < 0.01

    for start_place, end_place, slots in placement_groups:
        label_range = (
            f"#{start_place}"
            if start_place == end_place
            else f"#{start_place}-{end_place}"
        )

        for slot_index, ids in enumerate(slots):
            place = start_place + slot_index
            label = " / ".join(f"<@{uid}>" for uid in ids)
            amount = REWARDS.get(place, 0) if place <= 10 else 0

            if place <= 10:
                results.append(
                    f"**{label_range}** {label} — "
                    f"🎁 {amount} Normal Chest(s)"
                )

            for uid in ids:
                if amount:
                    key = (
                        f"tournament:{tid}:place:{place}:"
                        f"user:{uid}:normal"
                    )

                    await issue_chest(
                        int(uid),
                        amount,
                        "chest",
                        key
                    )

                if (
                    place == 1
                    and ultra_roll_won
                    and not ultra_awarded
                ):
                    ultra_ok = await issue_chest(
                        int(ids[0]),
                        1,
                        "ultra",
                        f"tournament:{tid}:place:1:ultra-bonus"
                    )

                    if ultra_ok:
                        results.append(
                            f"✨ <@{ids[0]}> won **1 Ultra Chest** "
                            "(1% bonus)!"
                        )
                        ultra_awarded = True

                if place <= 10:
                    try:
                        dm_text = (
                            f"Your tournament placement: #{place}. "
                            f"Reward: {amount} Normal Chest(s)."
                        )

                        if place == 1 and ultra_awarded:
                            if ids[0] == uid:
                                dm_text += (
                                    " You also won the 1% Ultra Chest bonus!"
                                )

                        await bot.fetch_user(int(uid)).send(dm_text)

                    except discord.HTTPException:
                        log.info("Could not DM winner %s", uid)

    channel = bot.get_channel(RESULTS_CHANNEL_ID)

    if channel:
        await channel.send(
            embed=discord.Embed(
                title=f"🏁 Results — {t['name']}",
                description="\n".join(results)
                or "No final results recorded.",
                color=discord.Color.gold()
            )
        )

    save_data()


# ============================== DELETE TOURNAMENT ==============================

@bot.command(name="delete")
async def delete_tour(ctx, target: str = ""):
    if target.lower() != "tour":
        await ctx.reply("Use `!delete tour`.")
        return

    tid, t = tour_by_channel(ctx.channel.id)

    if not t:
        await ctx.reply(
            "This is not an active tournament channel."
        )
        return

    if not (
        is_owner(ctx.author)
        or (
            has_host_role(ctx.author)
            and ctx.author.id == int(t.get("host_id", 0))
        )
    ):
        await ctx.reply(
            "Only the bot owner or this tournament's host can delete it."
        )
        return

    t["status"] = "deleted"
    save_data()

    await ctx.reply("Deleting this tournament channel…")

    try:
        await ctx.channel.delete(
            reason=f"Tournament deleted by {ctx.author}"
        )
    except discord.HTTPException:
        log.exception("Failed to delete tournament channel")

# ============================== EDIT QUALIFIED ==============================

class EditQualifiedModal(discord.ui.Modal, title="Edit Qualified Player"):
    old_user_id = discord.ui.TextInput(
        label="Old qualified user ID",
        placeholder="Enter the current qualified player's ID",
        required=True,
        max_length=25
    )

    round_number = discord.ui.TextInput(
        label="Round number",
        placeholder="Example: 1",
        required=True,
        max_length=3
    )

    new_user_id = discord.ui.TextInput(
        label="New qualified user ID",
        placeholder="Enter the replacement player's ID",
        required=True,
        max_length=25
    )

    async def on_submit(self, interaction: discord.Interaction):
        tid, t = tour_by_channel(interaction.channel_id)

        if not t:
            await interaction.response.send_message(
                "This is not a tournament channel.",
                ephemeral=True
            )
            return

        if not host_allowed(interaction.user, t):
            await interaction.response.send_message(
                "You cannot edit this tournament.",
                ephemeral=True
            )
            return

        try:
            old_id = int(str(self.old_user_id.value).strip())
            round_no = int(str(self.round_number.value).strip())
            new_id = int(str(self.new_user_id.value).strip())
        except ValueError:
            await interaction.response.send_message(
                "Please enter valid numeric IDs and a round number.",
                ephemeral=True
            )
            return

        rounds = t.get("rounds", [])

        if round_no < 1 or round_no > len(rounds):
            await interaction.response.send_message(
                "That round does not exist.",
                ephemeral=True
            )
            return

        changed = False

        for match in rounds[round_no - 1]:
            winner = match.get("winner") or []

            if old_id in winner:
                if new_id not in (
                    match.get("members1", [])
                    + match.get("members2", [])
                ):
                    await interaction.response.send_message(
                        "The replacement must be a participant in that match.",
                        ephemeral=True
                    )
                    return

                match["winner"] = [
                    new_id if uid == old_id else uid
                    for uid in winner
                ]

                match["status"] = "FINISHED"
                changed = True
                break

        if not changed:
            await interaction.response.send_message(
                "The old player was not found among the qualified players "
                "in that round.",
                ephemeral=True
            )
            return

        # Update the qualified-player list for the current tournament.
        qualified = t.get("qualified", [])
        t["qualified"] = [
            new_id if uid == old_id else uid
            for uid in qualified
        ]

        save_data()

        await interaction.response.send_message(
            f"Updated qualified player <@{old_id}> to <@{new_id}> "
            f"in Round {round_no}.",
            ephemeral=True
        )


@bot.command(name="edit")
async def edit_cmd(ctx, target: str = ""):
    if target.lower() != "qualified":
        await ctx.reply("Use `!edit qualified`.")
        return

    tid, t = tour_by_channel(ctx.channel.id)

    if not t:
        await ctx.reply(
            "Use this command inside a tournament channel."
        )
        return

    if not host_allowed(ctx.author, t):
        await ctx.reply(
            "Only the bot owner or a Host can edit qualified players."
        )
        return

    view = discord.ui.View(timeout=180)

    button = discord.ui.Button(
        label="Edit Qualified",
        style=discord.ButtonStyle.primary
    )

    async def button_callback(interaction: discord.Interaction):
        if not host_allowed(interaction.user, t):
            await interaction.response.send_message(
                "You cannot edit this tournament.",
                ephemeral=True
            )
            return

        await interaction.response.send_modal(EditQualifiedModal())

    button.callback = button_callback
    view.add_item(button)

    await ctx.send(
        "Click below to correct a qualified player's ID.",
        view=view
    )


# ============================== MY CODE / QUALIFIED ==============================

@bot.command(name="mycode")
async def mycode_cmd(ctx):
    tid, t = tour_by_channel(ctx.channel.id)

    if not t:
        await ctx.reply(
            "Use this command inside a tournament channel."
        )

import os
import re
import json
import asyncio
import secrets
import threading
from pathlib import Path
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask, jsonify


# ============================================================
# CONFIGURATION
# ============================================================

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

REWARDS = {
    1: 40,
    2: 30,
    3: 25,
    4: 20,
    5: 15,
    6: 12,
    7: 10,
    8: 8,
    9: 5,
    10: 3,
}

REGISTRATION_OPEN_MINUTES = 20

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

data_lock = asyncio.Lock()


# ============================================================
# FLASK HEALTH CHECK FOR RAILWAY
# ============================================================

app = Flask(__name__)


@app.get("/")
def home():
    return jsonify({
        "status": "online",
        "bot": "Tournament Bot",
        "discord_connected": bot.is_ready(),
    })


@app.get("/health")
def health():
    return jsonify({"status": "healthy"})


def run_web_server():
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)


threading.Thread(target=run_web_server, daemon=True).start()


# ============================================================
# DATA STORAGE
# ============================================================

def load_data():
    if not DATA_FILE.exists():
        return {"tournaments": {}}

    try:
        with DATA_FILE.open("r", encoding="utf-8") as file:
            result = json.load(file)

        if not isinstance(result, dict):
            return {"tournaments": {}}

        result.setdefault("tournaments", {})
        return result

    except (OSError, json.JSONDecodeError):
        return {"tournaments": {}}


data = load_data()


def save_data():
    temporary = DATA_FILE.with_suffix(".tmp")

    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)

    temporary.replace(DATA_FILE)


def get_tournament(tournament_id: str):
    return data["tournaments"].get(str(tournament_id))


def get_tournament_by_channel(channel_id: int):
    for tournament in data["tournaments"].values():
        if tournament.get("channel_id") == channel_id:
            return tournament
    return None


def now_utc():
    return datetime.now(timezone.utc)


def parse_start_time(value: str):
    """
    Accepted examples:
    2026-12-25 18:00
    2026-12-25T18:00

    Times are interpreted as UTC.
    """
    value = value.strip()

    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


def discord_timestamp(timestamp: int, style: str = "F"):
    return f"<t:{timestamp}:{style}>"


def make_slug(value: str):
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value[:70] or "tournament"


def is_owner(user_id: int):
    return user_id == OWNER_ID


def has_host_role(member: discord.Member):
    return any(role.id == HOST_ROLE_ID for role in member.roles)


def can_manage_tournament(member: discord.Member, tournament: dict):
    return (
        is_owner(member.id)
        or has_host_role(member)
        or tournament.get("host_id") == member.id
    )


def get_player_ids(tournament: dict):
    return [int(uid) for uid in tournament.get("players", {}).keys()]


def tournament_embed(tournament: dict):
    start = int(tournament["start_timestamp"])

    embed = discord.Embed(
        title=f"🎮 {tournament['name']}",
        description=tournament.get("description") or "No description provided.",
        color=discord.Color.blurple(),
    )

    embed.add_field(name="Game Version", value=tournament["version"], inline=True)
    embed.add_field(name="Map", value=tournament["map"], inline=True)
    embed.add_field(name="Format", value=tournament["format"], inline=True)
    embed.add_field(
        name="Ability",
        value=tournament.get("ability", "None"),
        inline=True,
    )
    embed.add_field(
        name="Players",
        value=f"{len(tournament['players'])}/{tournament['max_players']}",
        inline=True,
    )
    embed.add_field(
        name="Registration",
        value="🟢 Open" if tournament["registration_open"] else "🔒 Closed",
        inline=True,
    )
    embed.add_field(
        name="Start Time",
        value=f"{discord_timestamp(start, 'F')}\n{discord_timestamp(start, 'R')}",
        inline=False,
    )
    embed.add_field(
        name="Host",
        value=f"<@{tournament['host_id']}>",
        inline=True,
    )
    embed.set_footer(text=f"Tournament ID: {tournament['id']}")

    return embed


async def update_tournament_message(tournament: dict):
    channel = bot.get_channel(tournament.get("channel_id", 0))

    if channel is None:
        try:
            channel = await bot.fetch_channel(tournament["channel_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    message_id = tournament.get("panel_message_id")
    if not message_id:
        return

    try:
        message = await channel.fetch_message(message_id)
        await message.edit(
            embed=tournament_embed(tournament),
            view=TournamentView(),
        )
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


async def get_text_channel(channel_id: int):
    channel = bot.get_channel(channel_id)

    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    return channel if isinstance(channel, discord.TextChannel) else None


# ============================================================
# CHEST API
# ============================================================

async def give_chest(user_id: int, amount: int, chest_type: str = "normal"):
    """
    Requires your Chest API to accept:
    POST /api/chests/add

    JSON:
    {
        "user_id": "Discord ID",
        "amount": 40,
        "chest_type": "normal",
        "transaction_id": "unique ID"
    }

    Authentication:
    Authorization: Bearer YOUR_API_KEY
    """
    if not CHEST_API_URL or not CHEST_API_KEY:
        print("Chest API is not configured; rewards were not issued.")
        return False

    try:
        import aiohttp
    except ImportError:
        print("aiohttp is missing. Add aiohttp to requirements.txt.")
        return False

    payload = {
        "user_id": str(user_id),
        "amount": int(amount),
        "chest_type": chest_type,
        "transaction_id": f"tournament-{secrets.token_hex(12)}",
    }

    headers = {
        "Authorization": f"Bearer {CHEST_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        timeout = aiohttp.ClientTimeout(total=15)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{CHEST_API_URL}/api/chests/add",
                json=payload,
                headers=headers,
            ) as response:
                body = await response.text()

                if 200 <= response.status < 300:
                    print(f"Chest reward delivered to {user_id}: {body}")
                    return True

                print(
                    f"Chest API failed for {user_id}: "
                    f"{response.status} {body}"
                )
                return False

    except Exception as exc:
        print(f"Chest API error: {exc}")
        return False


# ============================================================
# REGISTRATION
# ============================================================

class RegistrationModal(discord.ui.Modal, title="Tournament Registration"):
    nickname = discord.ui.TextInput(
        label="In-game nickname",
        placeholder="Enter your game nickname",
        max_length=32,
        required=True,
    )

    def __init__(self, tournament_id: str):
        super().__init__()
        self.tournament_id = tournament_id

    async def on_submit(self, interaction: discord.Interaction):
        async with data_lock:
            tournament = get_tournament(self.tournament_id)

            if not tournament:
                await interaction.response.send_message(
                    "This tournament no longer exists.",
                    ephemeral=True,
                )
                return

            if tournament["status"] != "scheduled":
                await interaction.response.send_message(
                    "Registration is no longer available.",
                    ephemeral=True,
                )
                return

            if not tournament["registration_open"]:
                await interaction.response.send_message(
                    "Registration is not open yet.",
                    ephemeral=True,
                )
                return

            user_id = str(interaction.user.id)

            if user_id in tournament["players"]:
                await interaction.response.send_message(
                    "You are already registered.",
                    ephemeral=True,
                )
                return

            if len(tournament["players"]) >= tournament["max_players"]:
                await interaction.response.send_message(
                    "This tournament is full.",
                    ephemeral=True,
                )
                return

            tournament["players"][user_id] = {
                "nickname": str(self.nickname.value).strip(),
                "code": None,
                "registered_at": int(now_utc().timestamp()),
            }

            save_data()

        await interaction.response.send_message(
            "You have successfully registered!",
            ephemeral=True,
        )

        await update_tournament_message(tournament)


# ============================================================
# PLAYER CODE MODAL
# ============================================================

class CodeModal(discord.ui.Modal, title="Submit Your Room Code"):
    room_code = discord.ui.TextInput(
        label="Room code",
        placeholder="Enter your room code",
        max_length=80,
        required=True,
    )

    def __init__(self, tournament_id: str):
        super().__init__()
        self.tournament_id = tournament_id

    async def on_submit(self, interaction: discord.Interaction):
        async with data_lock:
            tournament = get_tournament(self.tournament_id)

            if not tournament:
                await interaction.response.send_message(
                    "Tournament not found.",
                    ephemeral=True,
                )
                return

            if str(interaction.user.id) not in tournament["players"]:
                await interaction.response.send_message(
                    "You are not registered for this tournament.",
                    ephemeral=True,
                )
                return

            if tournament["status"] != "running":
                await interaction.response.send_message(
                    "Room codes can only be submitted after the tournament starts.",
                    ephemeral=True,
                )
                return

            tournament["players"][str(interaction.user.id)]["code"] = (
                str(self.room_code.value).strip()
            )

            save_data()

        await interaction.response.send_message(
            "Your room code has been saved.",
            ephemeral=True,
        )


# ============================================================
# TOURNAMENT BUTTONS
# ============================================================

class TournamentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Register",
        style=discord.ButtonStyle.success,
        custom_id="tournament:register",
        emoji="📝",
    )
    async def register_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        tournament = get_tournament_by_channel(interaction.channel_id)

        if not tournament:
            await interaction.response.send_message(
                "Tournament not found.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            RegistrationModal(tournament["id"])
        )

    @discord.ui.button(
        label="Remove Registration",
        style=discord.ButtonStyle.danger,
        custom_id="tournament:unregister",
        emoji="❌",
    )
    async def unregister_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        async with data_lock:
            tournament = get_tournament_by_channel(interaction.channel_id)

            if not tournament:
                await interaction.response.send_message(
                    "Tournament not found.",
                    ephemeral=True,
                )
                return

            if tournament["status"] != "scheduled":
                await interaction.response.send_message(
                    "Registration cannot be removed after the tournament starts.",
                    ephemeral=True,
                )
                return

            if not tournament["registration_open"]:
                await interaction.response.send_message(
                    "Registration is currently closed.",
                    ephemeral=True,
                )
                return

            user_id = str(interaction.user.id)

            if user_id not in tournament["players"]:
                await interaction.response.send_message(
                    "You are not registered.",
                    ephemeral=True,
                )
                return

            del tournament["players"][user_id]
            save_data()

        await interaction.response.send_message(
            "Your registration has been removed.",
            ephemeral=True,
        )

        await update_tournament_message(tournament)

    @discord.ui.button(
        label="Players",
        style=discord.ButtonStyle.primary,
        custom_id="tournament:players",
        emoji="👥",
    )
    async def players_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        tournament = get_tournament_by_channel(interaction.channel_id)

        if not tournament:
            await interaction.response.send_message(
                "Tournament not found.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"Players — {tournament['name']}",
            color=discord.Color.blurple(),
        )

        if tournament["players"]:
            lines = []

            for index, (user_id, player) in enumerate(
                tournament["players"].items(),
                start=1,
            ):
                lines.append(
                    f"**{index}.** {player['nickname']} — <@{user_id}>"
                )

            description = "\n".join(lines)
        else:
            description = "No players registered yet."

        embed.description = description[:4000]

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Host",
        style=discord.ButtonStyle.secondary,
        custom_id="tournament:host",
        emoji="👑",
    )
    async def host_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        tournament = get_tournament_by_channel(interaction.channel_id)

        if not tournament:
            await interaction.response.send_message(
                "Tournament not found.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"**Tournament Host:** <@{tournament['host_id']}>\n"
            f"**Tournament:** {tournament['name']}\n"
            f"**Status:** {tournament['status']}",
            ephemeral=True,
        )

    @discord.ui.button(
        label="My Code",
        style=discord.ButtonStyle.secondary,
        custom_id="tournament:mycode",
        emoji="🔑",
    )
    async def my_code_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        tournament = get_tournament_by_channel(interaction.channel_id)

        if not tournament:
            await interaction.response.send_message(
                "Tournament not found.",
                ephemeral=True,
            )
            return

        player = tournament["players"].get(str(interaction.user.id))

        if not player:
            await interaction.response.send_message(
                "You are not registered.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(CodeModal(tournament["id"]))

    @discord.ui.button(
        label="Qualified",
        style=discord.ButtonStyle.primary,
        custom_id="tournament:qualified",
        emoji="🏆",
    )
    async def qualified_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        tournament = get_tournament_by_channel(interaction.channel_id)

        if not tournament:
            await interaction.response.send_message(
                "Tournament not found.",
                ephemeral=True,
            )
            return

        qualified = tournament.get("qualified", [])

        if qualified:
            description = "\n".join(
                f"• <@{user_id}>" for user_id in qualified
            )
        else:
            description = "No qualified players yet."

        embed = discord.Embed(
            title="Qualified Players",
            description=description,
            color=discord.Color.green(),
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )


# ============================================================
# TOURNAMENT CREATION PANEL
# ============================================================

class TournamentDetailsModal(
    discord.ui.Modal,
    title="Create Classic Tournament",
):
    name_input = discord.ui.TextInput(
        label="Tournament name",
        placeholder="RULL",
        max_length=70,
        required=True,
    )

    version_input = discord.ui.TextInput(
        label="Game version",
        placeholder="0.103",
        max_length=20,
        required=True,
    )

    map_input = discord.ui.TextInput(
        label="Map",
        placeholder="Block Dash Classic",
        max_length=80,
        required=True,
    )

    format_input = discord.ui.TextInput(
        label="Format",
        placeholder="1v1",
        default="1v1",
        max_length=10,
        required=True,
    )

    max_players_input = discord.ui.TextInput(
        label="Maximum players (2-128)",
        placeholder="8",
        default="8",
        max_length=3,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            max_players = int(self.max_players_input.value)
        except ValueError:
            await interaction.response.send_message(
                "Maximum players must be a number.",
                ephemeral=True,
            )
            return

        if not 2 <= max_players <= 128:
            await interaction.response.send_message(
                "Maximum players must be between 2 and 128.",
                ephemeral=True,
            )
            return

        tournament_format = self.format_input.value.strip().lower()

        if tournament_format not in ("1v1", "2v2", "3v3", "4v4"):
            await interaction.response.send_message(
                "Choose one of these formats: 1v1, 2v2, 3v3 or 4v4.",
                ephemeral=True,
            )
            return

        if tournament_format != "1v1":
            await interaction.response.send_message(
                "This version of the bot currently supports player "
                "registration for 1v1 tournaments only. Team registration "
                "for 2v2, 3v3 and 4v4 must be implemented before using "
                "those formats.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            TournamentScheduleModal(
                name=str(self.name_input.value).strip(),
                version=str(self.version_input.value).strip(),
                map_name=str(self.map_input.value).strip(),
                tournament_format=tournament_format,
                max_players=max_players,
            )
        )


class TournamentScheduleModal(
    discord.ui.Modal,
    title="Tournament Schedule",
):
    start_input = discord.ui.TextInput(
        label="Start time (UTC)",
        placeholder="2026-12-25 18:00",
        max_length=16,
        required=True,
    )

    description_input = discord.ui.TextInput(
        label="Description",
        placeholder="Tournament details...",
        style=discord.TextStyle.paragraph,
        max_length=800,
        required=False,
    )

    def __init__(
        self,
        name: str,
        version: str,
        map_name: str,
        tournament_format: str,
        max_players: int,
    ):
        super().__init__()
        self.tournament_name = name
        self.version = version
        self.map_name = map_name
        self.tournament_format = tournament_format
        self.max_players = max_players

    async def on_submit(self, interaction: discord.Interaction):
        start_time = parse_start_time(str(self.start_input.value))

        if start_time is None:
            await interaction.response.send_message(
                "Invalid time format. Use YYYY-MM-DD HH:MM in UTC.",
                ephemeral=True,
            )
            return

        if start_time <= now_utc():
            await interaction.response.send_message(
                "The start time must be in the future.",
                ephemeral=True,
            )
            return

        if not interaction.guild:
            await interaction.response.send_message(
                "This command must be used in a server.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Choose an ability for the tournament.",
            view=AbilityView(
                creator_id=interaction.user.id,
                guild_id=interaction.guild.id,
                name=self.tournament_name,
                version=self.version,
                map_name=self.map_name,
                tournament_format=self.tournament_format,
                max_players=self.max_players,
                start_timestamp=int(start_time.timestamp()),
                description=str(self.description_input.value).strip(),
            ),
            ephemeral=True,
        )


class AbilitySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="None", value="None"),
            discord.SelectOption(label="Punch", value="Punch"),
            discord.SelectOption(label="Shield", value="Shield"),
            discord.SelectOption(label="Invisibility", value="Invisibility"),
            discord.SelectOption(label="Double Jump", value="Double Jump"),
            discord.SelectOption(label="Speed", value="Speed"),
            discord.SelectOption(label="Random Ability", value="Random"),
        ]

        super().__init__(
            placeholder="Select an ability",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="tournament:create:ability",
        )

    async def callback(self, interaction: discord.Interaction):
        view: AbilityView = self.view

        if interaction.user.id != view.creator_id:
            await interaction.response.send_message(
                "Only the person creating this tournament can select its ability.",
                ephemeral=True,
            )
            return

        view.ability = self.values[0]

        await interaction.response.edit_message(
            content=f"Selected ability: **{view.ability}**\n"
                    "Press **Create Tournament** to finish.",
            view=view,
        )


class AbilityView(discord.ui.View):
    def __init__(
        self,
        creator_id: int,
        guild_id: int,
        name: str,
        version: str,
        map_name: str,
        tournament_format: str,
        max_players: int,
        start_timestamp: int,
        description: str,
    ):
        super().__init__(timeout=300)

        self.creator_id = creator_id
        self.guild_id = guild_id
        self.name = name
        self.version = version
        self.map_name = map_name
        self.tournament_format = tournament_format
        self.max_players = max_players
        self.start_timestamp = start_timestamp
        self.description = description
        self.ability = "None"

        self.add_item(AbilitySelect())

    @discord.ui.button(
        label="Create Tournament",
        style=discord.ButtonStyle.success,
        emoji="🎮",
    )
    async def create_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message(
                "Only the person creating this tournament can finish it.",
                ephemeral=True,
            )
            return

        guild = bot.get_guild(self.guild_id)

        if guild is None:
            await interaction.response.send_message(
                "Server not found.",
                ephemeral=True,
            )
            return

        category = guild.get_channel(TOURNAMENT_CATEGORY_ID)

        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "Tournament category was not found. Check "
                "TOURNAMENT_CATEGORY_ID.",
                ephemeral=True,
            )
            return

        slug = make_slug(self.name)
        channel_name = f"🎮┃{slug}"[:100]

        existing = discord.utils.get(guild.text_channels, name=channel_name)

        if existing:
            channel_name = f"{channel_name[:85]}-{secrets.token_hex(3)}"

        try:
            channel = await guild.create_text_channel(
                channel_name,
                category=category,
                reason=f"Tournament created by {interaction.user}",
            )
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"Could not create tournament channel: {exc}",
                ephemeral=True,
            )
            return

        tournament_id = secrets.token_hex(5)

        tournament = {
            "id": tournament_id,
            "guild_id": guild.id,
            "channel_id": channel.id,
            "panel_message_id": None,
            "name": self.name,
            "version": self.version,
            "map": self.map_name,
            "format": self.tournament_format,
            "max_players": self.max_players,
            "start_timestamp": self.start_timestamp,
            "description": self.description,
            "ability": self.ability,
            "host_id": interaction.user.id,
            "players": {},
            "qualified": [],
            "rounds": {},
            "status": "scheduled",
            "registration_open": False,
            "rewards_given": False,
            "created_at": int(now_utc().timestamp()),
        }

        async with data_lock:
            data["tournaments"][tournament_id] = tournament
            save_data()

        embed = tournament_embed(tournament)

        try:
            message = await channel.send(
                f"Welcome to **{self.name}**!\n"
                f"Registration will open automatically when the tournament "
                f"is within {REGISTRATION_OPEN_MINUTES} minutes of starting.\n"
                f"Start time: {discord_timestamp(self.start_timestamp, 'F')}",
                embed=embed,
                view=TournamentView(),
            )

            tournament["panel_message_id"] = message.id

            async with data_lock:
                save_data()

        except discord.HTTPException as exc:
            await channel.send(
                f"Tournament created, but the panel could not be posted: {exc}"
            )

        announcement_channel = await get_text_channel(
            ANNOUNCEMENTS_CHANNEL_ID
        )

        if announcement_channel:
            try:
                await announcement_channel.send(
                    f"🎮 **New Tournament: {self.name}**\n"
                    f"Format: **{self.tournament_format}**\n"
                    f"Map: **{self.map_name}**\n"
                    f"Start: {discord_timestamp(self.start_timestamp, 'F')}\n"
                    f"Channel: {channel.mention}"
                )
            except discord.HTTPException:
                pass

        await interaction.response.edit_message(
            content=f"Tournament created successfully: {channel.mention}",
            view=None,
        )


class CreatePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Classic Tour",
        style=discord.ButtonStyle.primary,
        emoji="🏆",
        custom_id="tournament:create:classic",
    )
    async def classic_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "This button can only be used in a server.",
                ephemeral=True,
            )
            return

        if not (
            is_owner(interaction.user.id)
            or (
                isinstance(interaction.user, discord.Member)
                and has_host_role(interaction.user)
            )
        ):
            await interaction.response.send_message(
                "You need the Host role to create tournaments.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(TournamentDetailsModal())


# ============================================================
# SLASH COMMANDS
# ============================================================

class CreateGroup(app_commands.Group):
    def __init__(self):
        super().__init__(
            name="create",
            description="Create tournament tools",
        )

    @app_commands.command(
        name="tour",
        description="Send a tournament creation panel",
    )
    @app_commands.describe(
        channel="Channel where the creation panel will be sent"
    )
    async def tour(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        if not (
            is_owner(interaction.user.id)
            or (
                isinstance(interaction.user, discord.Member)
                and has_host_role(interaction.user)
            )
        ):
            await interaction.response.send_message(
                "You need the Host role to use this command.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🏆 Tournament Creation",
            description=(
                "Click **Classic Tour** to create a tournament.\n\n"
                "Configure the name, game version, map, format, player limit, "
                "start time, description and ability."
            ),
            color=discord.Color.blurple(),
        )

        await channel.send(embed=embed, view=CreatePanelView())

        await interaction.response.send_message(
            f"Creation panel sent to {channel.mention}.",
            ephemeral=True,
        )


bot.tree.add_command(CreateGroup())


# ============================================================
# BRACKET HELPERS
# ============================================================

def round_number(round_name: str):
    match = re.fullmatch(r"r(\d+)", round_name.lower().strip())

    if not match:
        return None

    number = int(match.group(1))

    if number < 1 or number > 20:
        return None

    return number


def match_number(match_name: str):
    match = re.fullmatch(r"m(\d+)", match_name.lower().strip())

    if not match:
        return None

    return int(match.group(1))


async def send_round(tournament: dict, round_name: str):
    channel = await get_text_channel(tournament["channel_id"])

    if not channel:
        return False

    matches = tournament["rounds"].get(round_name, [])

    embed = discord.Embed(
        title=f"🏆 {tournament['name']} — {round_name.upper()}",
        color=discord.Color.blurple(),
    )

    for match in matches:
        player_a = match.get("player_a")
        player_b = match.get("player_b")
        winner = match.get("winner")

        a_text = f"<@{player_a}>" if player_a else "EMPTY"
        b_text = f"<@{player_b}>" if player_b else "EMPTY"

        if winner:
            status = f"Winner: <@{winner}>"
        elif not player_b and player_a:
            status = "BYE — waiting for host confirmation"
        else:
            status = "WAITING..."

        embed.add_field(
            name=match["id"].upper(),
            value=f"{a_text} vs {b_text}\n{status}",
            inline=False,
        )

    await channel.send(embed=embed)
    return True


def get_all_round_winners(tournament: dict, round_name: str):
    winners = []

    for match in tournament["rounds"].get(round_name, []):
        winner = match.get("winner")

        if winner and winner not in winners:
            winners.append(winner)

    return winners


# ============================================================
# HOST COMMANDS
# ============================================================

async def host_command_check(ctx: commands.Context):
    tournament = get_tournament_by_channel(ctx.channel.id)

    if not tournament:
        await ctx.reply(
            "This command must be used inside a tournament channel."
        )
        return None

    if not isinstance(ctx.author, discord.Member):
        await ctx.reply("This command must be used in a server.")
        return None

    if not can_manage_tournament(ctx.author, tournament):
        await ctx.reply("You do not have permission to manage this tournament.")
        return None

    return tournament


@bot.command(name="bracket")
async def bracket_cmd(ctx: commands.Context, round_name: str = None):
    tournament = await host_command_check(ctx)

    if not tournament:
        return

    if not round_name:
        await ctx.reply("Usage: `!bracket r1`")
        return

    number = round_number(round_name)

    if number is None:
        await ctx.reply("Round format must look like `r1`, `r2`, etc.")
        return

    async with data_lock:
        if tournament["status"] == "finished":
            await ctx.reply("This tournament has already finished.")
            return

        if not tournament["registration_open"] and tournament["status"] == "scheduled":
            await ctx.reply("Registration has not opened yet.")
            return

        if len(tournament["players"]) < 2:
            await ctx.reply("At least two players must be registered.")
            return

        if round_name.lower() in tournament["rounds"]:
            await ctx.reply("This round has already been created.")
            return

        if number > 1:
            previous_round = f"r{number - 1}"

            if previous_round not in tournament["rounds"]:
                await ctx.reply(
                    f"Create `{previous_round}` before creating `{round_name}`."
                )
                return

            previous_matches = tournament["rounds"][previous_round]

            if any(not match.get("winner") for match in previous_matches):
                await ctx.reply(
                    f"All matches in `{previous_round}` must have a winner first."
                )
                return

            players = get_all_round_winners(tournament, previous_round)

        else:
            players = list(tournament["players"].keys())

        if len(players) < 2:
            if len(players) == 1:
                tournament["qualified"] = players
                tournament["status"] = "finished"
                save_data()
            await ctx.reply("Not enough players remain to create this round.")
            return

        secrets.SystemRandom().shuffle(players)

        matches = []

        for index in range(0, len(players), 2):
            player_a = players[index]
            player_b = players[index + 1] if index + 1 < len(players) else None

            matches.append({
                "id": f"m{len(matches) + 1}",
                "player_a": player_a,
                "player_b": player_b,
                "winner": None,
                "room_code": None,
            })

        tournament["rounds"][round_name.lower()] = matches
        tournament["status"] = "running"
        save_data()

    await ctx.reply(f"Created **{round_name.upper()}**.")
    await send_round(tournament, round_name.lower())


@bot.command(name="match")
async def match_cmd(
    ctx: commands.Context,
    match_name: str = None,
    room_code: str = None,
):
    tournament = await host_command_check(ctx)

    if not tournament:
        return

    if not match_name or not room_code:
        await ctx.reply("Usage: `!match m1 ROOMCODE`")
        return

    number = match_number(match_name)

    if number is None:
        await ctx.reply("Match format must look like `m1`, `m2`, etc.")
        return

    found = None
    round_name = None

    for current_round, matches in tournament["rounds"].items():
        for match in matches:
            if match["id"] == match_name.lower():
                found = match
                round_name = current_round
                break
        if found:
            break

    if not found:
        await ctx.reply("Match not found.")
        return

    async with data_lock:
        found["room_code"] = room_code
        save_data()

    await ctx.reply(
        f"Room code for **{match_name.upper()}** in **{round_name.upper()}** "
        f"has been saved: `{room_code}`"
    )


async def announce_qualified(tournament: dict, user_id: int):
    channel = await get_text_channel(tournament["channel_id"])

    if channel:
        await channel.send(
            f"🏆 <@{user_id}> has qualified for the next round!"
        )


async def finish_tournament(tournament: dict, champion_id: int):
    if tournament.get("rewards_given"):
        return

    tournament["status"] = "finished"
    tournament["champion_id"] = champion_id
    tournament["rewards_given"] = True
    tournament["finished_at"] = int(now_utc().timestamp())
    save_data()

    results_channel = await get_text_channel(RESULTS_CHANNEL_ID)

    placements = tournament.get("placements", [])

    if champion_id not in placements:
        placements.insert(0, champion_id)

    placements = placements[:10]

    reward_lines = []

    for place, user_id in enumerate(placements, start=1):
        amount = REWARDS.get(place, 0)

        reward_lines.append(
            f"**#{place}** <@{user_id}> — {amount} Normal Chests"
        )

        if amount:
            delivered = await give_chest(user_id, amount, "normal")

            try:
                user = await bot.fetch_user(int(user_id))

                if delivered:
                    await user.send(
                        f"🏆 You finished #{place} in **{tournament['name']}** "
                        f"and received **{amount} Normal Chests**!"
                    )
                else:
                    await user.send(
                        f"🏆 You finished #{place} in **{tournament['name']}**. "
                        "Your reward could not be confirmed by the Chest API. "
                        "Please contact the tournament host."
                    )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    if results_channel:
        embed = discord.Embed(
            title=f"🏆 Tournament Results — {tournament['name']}",
            description="\n".join(reward_lines) or "No placements recorded.",
            color=discord.Color.gold(),
        )

        embed.add_field(
            name="Champion",
            value=f"<@{champion_id}>",
            inline=False,
        )

        await results_channel.send(embed=embed)

    channel = await get_text_channel(tournament["channel_id"])

    if channel:
        await channel.send(
            f"🏆 **{tournament['name']} has finished!**\n"
            f"Champion: <@{champion_id}>"
        )


@bot.command(name="qual")
async def qual_cmd(
    ctx: commands.Context,
    match_name: str = None,
    winner: discord.Member = None,
):
    tournament = await host_command_check(ctx)

    if not tournament:
        return

    if not match_name or not winner:
        await ctx.reply("Usage: `!qual m1 @winner`")
        return

    found = None
    round_name = None

    for current_round, matches in tournament["rounds"].items():
        for match in matches:
            if match["id"] == match_name.lower():
                found = match
                round_name = current_round
                break
        if found:
            break

    if not found:
        await ctx.reply("Match not found.")
        return

    allowed_winners = {
        str(found["player_a"]),
        str(found["player_b"]),
    }

    if str(winner.id) not in allowed_winners:
        await ctx.reply("The winner must be one of the players in that match.")
        return

    async with data_lock:
        found["winner"] = str(winner.id)
        save_data()

    await ctx.reply(
        f"Winner for **{match_name.upper()}**: {winner.mention}"
    )

    await announce_qualified(tournament, winner.id)

    matches = tournament["rounds"][round_name]

    if all(match.get("winner") for match in matches):
        winners = get_all_round_winners(tournament, round_name)

        if len(winners) == 1:
            tournament["placements"] = winners
            await finish_tournament(tournament, int(winners[0]))
        else:
            await ctx.send(
                f"All matches in **{round_name.upper()}** are complete. "
                "Create the next round with `!bracket r"
                f"{(round_number(round_name) or 1) + 1}`."
            )


@bot.command(name="edit")
async def edit_cmd(
    ctx: commands.Context,
    subcommand: str = None,
    old_user_id: str = None,
    round_name: str = None,
    new_user_id: str = None,
):
    tournament = await host_command_check(ctx)

    if not tournament:
        return

    if (
        subcommand != "qualified"
        or not old_user_id
        or not round_name
        or not new_user_id
    ):
        await ctx.reply(
            "Usage: `!edit qualified OLD_USER_ID r1 NEW_USER_ID`"
        )
        return

    if round_number(round_name) is None:
        await ctx.reply("Round must look like `r1`, `r2`, etc.")
        return

    matches = tournament["rounds"].get(round_name.lower())

    if not matches:
        await ctx.reply("Round not found.")
        return

    changed = False

    async with data_lock:
        for match in matches:
            if match.get("winner") == old_user_id:
                if new_user_id not in (
                    str(match.get("player_a")),
                    str(match.get("player_b")),
                ):
                    await ctx.reply(
                        "The new winner must be a player in that match."
                    )
                    return

                match["winner"] = new_user_id
                changed = True
                break

        if changed:
            save_data()

    if not changed:
        await ctx.reply("The specified qualified player was not found.")
        return

    await ctx.reply(
        f"Updated the winner in **{round_name.upper()}** to <@{new_user_id}>."
    )


@bot.command(name="delete")
async def delete_cmd(
    ctx: commands.Context,
    subcommand: str = None,
):
    if subcommand != "tour":
        await ctx.reply("Usage: `!delete tour`")
        return

    tournament = get_tournament_by_channel(ctx.channel.id)

    if not tournament:
        await ctx.reply("This channel is not linked to a tournament.")
        return

    if not isinstance(ctx.author, discord.Member):
        await ctx.reply("This command must be used in a server.")
        return

    if not can_manage_tournament(ctx.author, tournament):
        await ctx.reply("You cannot delete this tournament.")
        return

    async with data_lock:
        data["tournaments"].pop(tournament["id"], None)
        save_data()

    await ctx.reply("Tournament deleted. This channel will now be removed.")

    try:
        await ctx.channel.delete(reason="Tournament deleted")
    except discord.HTTPException:
        pass


@bot.command(name="mycode")
async def mycode_cmd(ctx: commands.Context):
    tournament = get_tournament_by_channel(ctx.channel.id)

    if not tournament:
        await ctx.reply("This is not a tournament channel.")
        return

    player = tournament["players"].get(str(ctx.author.id))

    if not player:
        await ctx.reply("You are not registered.")
        return

    code = player.get("code")

    if code:
        await ctx.reply(f"Your saved room code: `{code}`")
    else:
        await ctx.reply("You have not submitted a room code yet.")


@bot.command(name="qualified")
async def qualified_cmd(ctx: commands.Context):
    tournament = get_tournament_by_channel(ctx.channel.id)

    if not tournament:
        await ctx.reply("This is not a tournament channel.")
        return

    qualified = tournament.get("qualified", [])

    if not qualified:
        await ctx.reply("No qualified players have been recorded yet.")
        return

    await ctx.reply(
        "**Qualified players:**\n"
        + "\n".join(f"• <@{uid}>" for uid in qualified)
    )


@bot.command(name="tourstatus")
async def tourstatus_cmd(ctx: commands.Context):
    tournament = get_tournament_by_channel(ctx.channel.id)

    if not tournament:
        await ctx.reply("This is not a tournament channel.")
        return

    embed = tournament_embed(tournament)
    await ctx.reply(embed=embed)


# ============================================================
# SCHEDULED REGISTRATION
# ============================================================

@tasks.loop(seconds=30)
async def tournament_scheduler():
    current_time = int(now_utc().timestamp())

    for tournament in list(data["tournaments"].values()):
        if tournament.get("status") != "scheduled":
            continue

        start_timestamp = int(tournament["start_timestamp"])
        seconds_until_start = start_timestamp - current_time

        if seconds_until_start <= REGISTRATION_OPEN_MINUTES * 60:
            if not tournament["registration_open"]:
                async with data_lock:
                    tournament["registration_open"] = True
                    save_data()

                channel = await get_text_channel(tournament["channel_id"])

                if channel:
                    role_mention = f"<@&{TOURNAMENT_PING_ROLE_ID}>"

                    await channel.send(
                        f"{role_mention}\n"
                        f"🟢 **Registration is now open for "
                        f"{tournament['name']}!**\n"
                        f"Starting: {discord_timestamp(start_timestamp, 'R')}",
                        allowed_mentions=discord.AllowedMentions(
                            roles=True,
                            users=False,
                            everyone=False,
                        ),
                    )

                await update_tournament_message(tournament)

        if seconds_until_start <= 0:
            channel = await get_text_channel(tournament["channel_id"])

            if len(tournament["players"]) < 2:
                tournament["status"] = "cancelled"
                save_data()

                if channel:
                    await channel.send(
                        "❌ Tournament cancelled because fewer than two "
                        "players registered."
                    )

                continue

            if channel:
                await channel.send(
                    "⏰ **Tournament start time reached!**\n"
                    "The host can now start the first round with `!bracket r1`."
                )

            # Prevent repeating the start announcement every 30 seconds.
            tournament["status"] = "waiting_for_host"
            save_data()


@tournament_scheduler.before_loop
async def before_tournament_scheduler():
    await bot.wait_until_ready()


# ============================================================
# READY / PERSISTENT VIEWS / COMMAND SYNC
# ============================================================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    bot.add_view(CreatePanelView())
    bot.add_view(TournamentView())

    if not tournament_scheduler.is_running():
        tournament_scheduler.start()

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except Exception as exc:
        print(f"Slash command sync failed: {exc}")


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(
            f"Missing argument: `{error.param.name}`."
        )
        return

    print(f"Command error: {error}")

    try:
        await ctx.reply("An unexpected error occurred while running the command.")
    except discord.HTTPException:
        pass


# ============================================================
# RUN
# ============================================================

bot.run(TOKEN)
