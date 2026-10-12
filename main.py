
from __future__ import annotations

import os
import json
import asyncio
import random
import secrets
import threading
from pathlib import Path
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask, jsonify


# ==================================================
# CONFIG
# ==================================================

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

REGISTRATION_OPEN_MINUTES = 20

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


# ==================================================
# BOT AND WEB SERVER
# ==================================================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
app = Flask(__name__)


@app.get("/")
@app.get("/health")
def health():
    return jsonify({"status": "online", "bot": "Tournament Bot"})


def run_web():
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
    )


threading.Thread(target=run_web, daemon=True).start()


# ==================================================
# STORAGE
# ==================================================

data_lock = asyncio.Lock()


def load_data():
    if not DATA_FILE.exists():
        return {"tournaments": {}, "next_id": 1}

    try:
        with DATA_FILE.open("r", encoding="utf-8") as file:
            result = json.load(file)

        result.setdefault("tournaments", {})
        result.setdefault("next_id", 1)
        return result

    except (OSError, json.JSONDecodeError):
        return {"tournaments": {}, "next_id": 1}


data = load_data()


async def save_data():
    async with data_lock:
        temporary = DATA_FILE.with_suffix(".tmp")

        with temporary.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)

        temporary.replace(DATA_FILE)


def get_tournament(tournament_id):
    return data["tournaments"].get(str(tournament_id))


def is_owner(user_id):
    return user_id == OWNER_ID


def can_host(member):
    return (
        member.id == OWNER_ID
        or any(role.id == HOST_ROLE_ID for role in member.roles)
    )


def parse_time(value):
    try:
        result = datetime.fromisoformat(value.strip())

        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)

        return result.astimezone(timezone.utc)
    except ValueError:
        return None


def discord_time(timestamp, style="F"):
    return f"<t:{int(timestamp)}:{style}>"


def get_tournament_by_channel(channel_id, guild_id):
    for tournament in data["tournaments"].values():
        if (
            int(tournament["channel_id"]) == channel_id
            and int(tournament["guild_id"]) == guild_id
        ):
            return tournament

    return None


async def host_tournament(ctx):
    if not isinstance(ctx.author, discord.Member):
        return None

    if not can_host(ctx.author):
        return None

    return get_tournament_by_channel(ctx.channel.id, ctx.guild.id)


async def delete_command_message(ctx):
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass


async def temporary_reply(ctx, content=None, **kwargs):
    try:
        return await ctx.send(
            content,
            delete_after=6,
            **kwargs,
        )
    except discord.HTTPException:
        return None


# ==================================================
# EMBEDS
# ==================================================

def tournament_embed(tournament):
    embed = discord.Embed(
        title=f"🎮 {tournament['name']}",
        description=tournament.get("description") or "No description provided.",
        color=discord.Color.blurple(),
    )

    embed.add_field(name="Version", value=tournament["version"], inline=True)
    embed.add_field(name="Map", value=tournament["map"], inline=True)
    embed.add_field(name="Format", value=tournament["format"], inline=True)
    embed.add_field(
        name="Maximum Players",
        value=str(tournament["max_players"]),
        inline=True,
    )
    embed.add_field(
        name="Ability",
        value=tournament["ability"],
        inline=True,
    )
    embed.add_field(
        name="Start Time",
        value=discord_time(tournament["start_timestamp"]),
        inline=False,
    )
    embed.add_field(
        name="Registration",
        value="🟢 Open" if tournament["registration_open"] else "🔒 Closed",
        inline=True,
    )
    embed.add_field(
        name="Players",
        value=f"{len(tournament['players'])}/{tournament['max_players']}",
        inline=True,
    )
    embed.set_footer(text=f"Tournament ID: {tournament['id']}")
    return embed


def players_embed(guild, tournament):
    embed = discord.Embed(
        title=f"👥 Players — {tournament['name']}",
        color=discord.Color.blue(),
    )

    if not tournament["players"]:
        embed.description = "No players registered yet."
    else:
        lines = []

        for number, (user_id, player) in enumerate(
            tournament["players"].items(), start=1
        ):
            nickname = discord.utils.escape_markdown(
                player.get("nickname", "Player")
            )
            lines.append(f"**{number}.** {nickname} — <@{user_id}>")

        embed.description = "\n".join(lines)

    embed.set_footer(
        text=f"{len(tournament['players'])}/{tournament['max_players']} players"
    )
    return embed


async def refresh_tournament(tournament):
    guild = bot.get_guild(int(tournament["guild_id"]))
    if not guild:
        return

    channel = guild.get_channel(int(tournament["channel_id"]))
    if not isinstance(channel, discord.TextChannel):
        return

    message_id = tournament.get("panel_message_id")
    if not message_id:
        return

    try:
        message = await channel.fetch_message(int(message_id))
        await message.edit(
            embed=tournament_embed(tournament),
            view=TournamentView(tournament["id"]),
        )
    except discord.HTTPException:
        pass


# ==================================================
# CHEST API
# ==================================================

async def give_chests(user_id, amount, chest_type="normal"):
    if not CHEST_API_URL or not CHEST_API_KEY:
        print("Chest API is not configured.")
        return False

    payload = {
        "user_id": str(user_id),
        "amount": amount,
        "chest_type": chest_type,
        "transaction_id": secrets.token_hex(16),
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

                if response.status not in (200, 201, 202):
                    print(f"Chest API error {response.status}: {body[:400]}")
                    return False

                return True

    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        print(f"Chest API request failed: {error}")
        return False


# ==================================================
# REGISTRATION AND GAME CODE MODALS
# ==================================================

class RegistrationModal(discord.ui.Modal, title="Tournament Registration"):
    nickname = discord.ui.TextInput(
        label="In-game nickname",
        placeholder="Enter your game nickname",
        min_length=1,
        max_length=32,
    )

    def __init__(self, tournament_id):
        super().__init__()
        self.tournament_id = str(tournament_id)

    async def on_submit(self, interaction):
        tournament = get_tournament(self.tournament_id)

        if not tournament:
            await interaction.response.send_message(
                "Tournament not found.",
                ephemeral=True,
            )
            return

        if not tournament["registration_open"]:
            await interaction.response.send_message(
                "Registration is closed.",
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

        nickname = str(self.nickname.value).strip()

        if not nickname:
            await interaction.response.send_message(
                "Enter a valid nickname.",
                ephemeral=True,
            )
            return

        tournament["players"][user_id] = {
            "nickname": nickname,
            "code": None,
        }

        await save_data()
        await refresh_tournament(tournament)

        await interaction.response.send_message(
            "Registration successful!",
            ephemeral=True,
        )


class GameCodeModal(discord.ui.Modal, title="Submit Game Code"):
    code = discord.ui.TextInput(
        label="Game code",
        placeholder="Enter your match code",
        min_length=1,
        max_length=100,
    )

    def __init__(self, tournament_id):
        super().__init__()
        self.tournament_id = str(tournament_id)

    async def on_submit(self, interaction):
        tournament = get_tournament(self.tournament_id)

        if not tournament:
            await interaction.response.send_message(
                "Tournament not found.",
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

        tournament["players"][user_id]["code"] = str(self.code.value).strip()
        await save_data()

        await interaction.response.send_message(
            "Your game code has been saved.",
            ephemeral=True,
        )


# ==================================================
# PRIVATE MATCH CODE BUTTON
# ==================================================

class MatchCodeView(discord.ui.View):
    def __init__(self, tournament_id, round_name, match_id):
        super().__init__(timeout=None)
        self.tournament_id = str(tournament_id)
        self.round_name = round_name
        self.match_id = match_id

        self.show_code.custom_id = (
            f"matchcode:{tournament_id}:{round_name}:{match_id}"
        )

    @discord.ui.button(
        label="My Code",
        style=discord.ButtonStyle.primary,
        emoji="🔐",
    )
    async def show_code(self, interaction, button):
        tournament = get_tournament(self.tournament_id)

        if not tournament:
            await interaction.response.send_message(
                "Tournament not found.",
                ephemeral=True,
            )
            return

        matches = tournament["rounds"].get(self.round_name, [])
        match = next(
            (item for item in matches if item["match_id"] == self.match_id),
            None,
        )

        if not match:
            await interaction.response.send_message(
                "Match not found.",
                ephemeral=True,
            )
            return

        user_id = str(interaction.user.id)

        if user_id not in (match["player_a"], match["player_b"]):
            await interaction.response.send_message(
                "Only players in this match can view the code.",
                ephemeral=True,
            )
            return

        code = match.get("code")

        if not code:
            await interaction.response.send_message(
                "The host has not posted the match code yet.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"🔐 **{match['match_id'].upper()} — Match Code**\n"
            f"```{code}```",
            ephemeral=True,
        )


# ==================================================
# TOURNAMENT PANEL BUTTONS
# ==================================================

class TournamentView(discord.ui.View):
    def __init__(self, tournament_id):
        super().__init__(timeout=None)
        self.tournament_id = str(tournament_id)

        self.register_button.custom_id = f"tour:{tournament_id}:register"
        self.remove_button.custom_id = f"tour:{tournament_id}:remove"
        self.players_button.custom_id = f"tour:{tournament_id}:players"
        self.host_button.custom_id = f"tour:{tournament_id}:host"

    @discord.ui.button(
        label="Register",
        style=discord.ButtonStyle.success,
        emoji="📝",
    )
    async def register_button(self, interaction, button):
        tournament = get_tournament(self.tournament_id)

        if not tournament or not tournament["registration_open"]:
            await interaction.response.send_message(
                "Registration is not open.",
                ephemeral=True,
            )
            return

        if str(interaction.user.id) in tournament["players"]:
            await interaction.response.send_message(
                "You are already registered.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            RegistrationModal(self.tournament_id)
        )

    @discord.ui.button(
        label="Remove Registration",
        style=discord.ButtonStyle.danger,
        emoji="❌",
    )
    async def remove_button(self, interaction, button):
        tournament = get_tournament(self.tournament_id)

        if not tournament:
            await interaction.response.send_message(
                "Tournament not found.",
                ephemeral=True,
            )
            return

        if tournament["bracket_started"]:
            await interaction.response.send_message(
                "Registration cannot be changed after the bracket starts.",
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
        await save_data()
        await refresh_tournament(tournament)

        await interaction.response.send_message(
            "Your registration has been removed.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Players",
        style=discord.ButtonStyle.primary,
        emoji="👥",
    )
    async def players_button(self, interaction, button):
        tournament = get_tournament(self.tournament_id)

        if not tournament:
            await interaction.response.send_message(
                "Tournament not found.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=players_embed(interaction.guild, tournament),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Host",
        style=discord.ButtonStyle.secondary,
        emoji="🎤",
    )
    async def host_button(self, interaction, button):
        tournament = get_tournament(self.tournament_id)

        if not tournament:
            await interaction.response.send_message(
                "Tournament not found.",
                ephemeral=True,
            )
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Use this button in the server.",
                ephemeral=True,
            )
            return

        if not can_host(interaction.user):
            await interaction.response.send_message(
                "You do not have permission to host.",
                ephemeral=True,
            )
            return

        tournament["host_id"] = interaction.user.id
        await save_data()

        await interaction.response.send_message(
            f"{interaction.user.mention} is now the host.",
            ephemeral=True,
        )


# ==================================================
# CREATE TOURNAMENT FLOW
# ==================================================

class ContinueTournamentView(discord.ui.View):
    def __init__(self, creator_id, details):
        super().__init__(timeout=300)
        self.creator_id = creator_id
        self.details = details

    @discord.ui.button(
        label="Continue",
        style=discord.ButtonStyle.primary,
        emoji="➡️",
    )
    async def continue_button(self, interaction, button):
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message(
                "This form belongs to another user.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            ScheduleModal(self.creator_id, self.details)
        )


class DetailsModal(discord.ui.Modal, title="Classic Tour — Details"):
    name_input = discord.ui.TextInput(
        label="Tournament Name",
        placeholder="rull",
        max_length=50,
    )
    version_input = discord.ui.TextInput(
        label="Game Version",
        placeholder="0.103",
        max_length=20,
    )
    map_input = discord.ui.TextInput(
        label="Map",
        placeholder="Block Dash Classic",
        max_length=80,
    )
    format_input = discord.ui.TextInput(
        label="Format",
        default="1v1",
        max_length=10,
    )
    max_players_input = discord.ui.TextInput(
        label="Maximum Players",
        default="8",
        max_length=3,
    )

    def __init__(self, creator_id):
        super().__init__()
        self.creator_id = creator_id

    async def on_submit(self, interaction):
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message(
                "This form belongs to another user.",
                ephemeral=True,
            )
            return

        tournament_format = str(self.format_input.value).strip()

        if tournament_format.lower() != "1v1":
            await interaction.response.send_message(
                "Only 1v1 format is currently supported.",
                ephemeral=True,
            )
            return

        try:
            max_players = int(self.max_players_input.value)
        except ValueError:
            await interaction.response.send_message(
                "Maximum Players must be a number.",
                ephemeral=True,
            )
            return

        if not 2 <= max_players <= 128:
            await interaction.response.send_message(
                "Maximum Players must be between 2 and 128.",
                ephemeral=True,
            )
            return

        details = {
            "name": str(self.name_input.value).strip(),
            "version": str(self.version_input.value).strip(),
            "map": str(self.map_input.value).strip(),
            "format": tournament_format,
            "max_players": max_players,
        }

        await interaction.response.send_message(
            "Details saved! Click below to continue.",
            view=ContinueTournamentView(self.creator_id, details),
            ephemeral=True,
        )


class ScheduleModal(discord.ui.Modal, title="Tournament Schedule"):
    start_input = discord.ui.TextInput(
        label="Start Time (UTC)",
        placeholder="2026-10-15 18:30",
        max_length=30,
    )
    description_input = discord.ui.TextInput(
        label="Description",
        placeholder="Tournament information",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, creator_id, details):
        super().__init__()
        self.creator_id = creator_id
        self.details = details

    async def on_submit(self, interaction):
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message(
                "This form belongs to another user.",
                ephemeral=True,
            )
            return

        start_time = parse_time(str(self.start_input.value))

        if start_time is None:
            await interaction.response.send_message(
                "Use YYYY-MM-DD HH:MM in UTC.",
                ephemeral=True,
            )
            return

        if start_time.timestamp() <= datetime.now(timezone.utc).timestamp():
            await interaction.response.send_message(
                "The start time must be in the future.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Schedule saved! Choose an ability.",
            view=AbilityView(
                self.creator_id,
                self.details,
                int(start_time.timestamp()),
                str(self.description_input.value).strip(),
            ),
            ephemeral=True,
        )


class AbilityView(discord.ui.View):
    def __init__(self, creator_id, details, start_timestamp, description):
        super().__init__(timeout=300)
        self.creator_id = creator_id
        self.details = details
        self.start_timestamp = start_timestamp
        self.description = description
        self.ability = "No Ability"

    @discord.ui.select(
        placeholder="Choose an ability",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(label="No Ability", value="No Ability"),
            discord.SelectOption(label="Punch", value="Punch"),
            discord.SelectOption(label="Block", value="Block"),
            discord.SelectOption(label="Invisibility", value="Invisibility"),
            discord.SelectOption(label="Double Jump", value="Double Jump"),
        ],
    )
    async def ability_select(self, interaction, select):
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message(
                "This menu belongs to another user.",
                ephemeral=True,
            )
            return

        self.ability = select.values[0]
        await interaction.response.send_message(
            f"Selected ability: **{self.ability}**",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Create Tournament",
        style=discord.ButtonStyle.success,
        emoji="🎮",
    )
    async def create_button(self, interaction, button):
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message(
                "This menu belongs to another user.",
                ephemeral=True,
            )
            return

        guild = interaction.guild

        if guild is None:
            await interaction.response.send_message(
                "Use this inside a server.",
                ephemeral=True,
            )
            return

        category = guild.get_channel(TOURNAMENT_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            category = None

        tournament_id = str(data["next_id"])
        data["next_id"] += 1

        safe_name = "".join(
            char.lower() if char.isalnum() or char in "-_" else "-"
            for char in self.details["name"]
        )
        safe_name = "-".join(part for part in safe_name.split("-") if part)
        channel_name = f"🎮┃{safe_name[:80] or 'tournament'}"

        bot_member = guild.me
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            ),
        }

        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                read_message_history=True,
            )

        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason="Tournament created",
        )

        tournament = {
            "id": tournament_id,
            "guild_id": guild.id,
            "channel_id": channel.id,
            "creator_id": self.creator_id,
            "host_id": self.creator_id,
            **self.details,
            "start_timestamp": self.start_timestamp,
            "description": self.description,
            "ability": self.ability,
            "players": {},
            "rounds": {},
            "qualified": [],
            "placements": [],
            "registration_open": False,
            "bracket_started": False,
            "status": "scheduled",
            "rewarded": False,
        }

        data["tournaments"][tournament_id] = tournament
        await save_data()

        message = await channel.send(
            f"🎮 Tournament created by <@{self.creator_id}>!\n"
            f"Registration opens 20 minutes before the start.\n"
            f"Start: {discord_time(self.start_timestamp)}",
            embed=tournament_embed(tournament),
            view=TournamentView(tournament_id),
            allowed_mentions=discord.AllowedMentions.none(),
        )

        tournament["panel_message_id"] = message.id
        await save_data()

        announcement_channel = guild.get_channel(ANNOUNCEMENTS_CHANNEL_ID)

        if isinstance(announcement_channel, discord.TextChannel):
            await announcement_channel.send(
                f"🎮 **New Tournament: {tournament['name']}**\n"
                f"Start: {discord_time(self.start_timestamp)}\n"
                f"Channel: {channel.mention}",
                allowed_mentions=discord.AllowedMentions.none(),
            )

        await interaction.response.edit_message(
            content=f"Tournament created: {channel.mention}",
            view=None,
        )
        self.stop()


class CreatePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Classic Tour",
        style=discord.ButtonStyle.primary,
        emoji="🏆",
        custom_id="create_tournament:classic",
    )
    async def classic_button(self, interaction, button):
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Use this inside a server.",
                ephemeral=True,
            )
            return

        if not can_host(interaction.user):
            await interaction.response.send_message(
                "You do not have permission to create tournaments.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            DetailsModal(interaction.user.id)
        )


# ==================================================
# SLASH COMMAND: /create
# ==================================================

@bot.tree.command(name="create", description="Create a tournament panel")
@app_commands.describe(channel="Channel where the panel will be posted")
async def create_command(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
):
    if not isinstance(interaction.user, discord.Member) or not can_host(
        interaction.user
    ):
        await interaction.response.send_message(
            "You do not have permission to use this command.",
            ephemeral=True,
        )
        return

    await channel.send(
        embed=discord.Embed(
            title="🏆 Tournament System",
            description="Click **Classic Tour** to create a tournament.",
            color=discord.Color.blurple(),
        ),
        view=CreatePanelView(),
    )

    await interaction.response.send_message(
        f"Tournament panel posted in {channel.mention}.",
        ephemeral=True,
    )


# ==================================================
# PREFIX HELP
# ==================================================

@bot.command(name="h")
async def help_command(ctx, section=None):
    if not section or section.lower() != "tour":
        await temporary_reply(ctx, "Use `!h tour` to view tournament commands.")
        await delete_command_message(ctx)
        return

    embed = discord.Embed(
        title="🏆 Tournament Commands",
        description="Tournament and training command guide.",
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name="Tournament",
        value=(
            "`/create channel:#channel` — Create a tournament panel\n"
            "`!bracket r1` — Create the first round\n"
            "`!bracket r2` — Create the next round\n"
            "`!match m1 CODE` — Set and send a match code\n"
            "`!qual m1 @player` — Choose the match winner\n"
            "`!editqualified @old @new` — Replace a qualified player\n"
            "`!delete tour` — Delete a tournament"
        ),
        inline=False,
    )

    embed.add_field(
        name="Training",
        value=(
            "`!training tour @user` — Create a training simulation\n"
            "The training channel demonstrates four simulated matches."
        ),
        inline=False,
    )

    # Help is intentionally kept visible so users can read it.
    await ctx.send(embed=embed)
    await delete_command_message(ctx)


# ==================================================
# MATCH CODE COMMAND
# ==================================================

@bot.command(name="match")
async def match_command(ctx, match_id: str = None, *, code: str = None):
    tournament = await host_tournament(ctx)

    if not tournament:
        await temporary_reply(
            ctx,
            "You need the Host role or bot owner permission in a tournament channel.",
        )
        await delete_command_message(ctx)
        return

    if not match_id or not code:
        await temporary_reply(ctx, "Usage: `!match m1 YOUR_CODE`")
        await delete_command_message(ctx)
        return

    round_name = None
    match = None

    for name, matches in tournament["rounds"].items():
        candidate = next(
            (
                item for item in matches
                if item["match_id"].lower() == match_id.lower()
            ),
            None,
        )
        if candidate:
            round_name = name
            match = candidate
            break

    if not match:
        await temporary_reply(ctx, "Match not found.")
        await delete_command_message(ctx)
        return

    match["code"] = code.strip()
    await save_data()

    guild = ctx.guild
    failed_dm_users = []

    for user_id in (match["player_a"], match["player_b"]):
        if not user_id:
            continue

        try:
            user = bot.get_user(int(user_id)) or await bot.fetch_user(int(user_id))
            await user.send(
                f"🎮 **Match code — {tournament['name']}**\n"
                f"Match: **{match['match_id'].upper()}**\n"
                f"```{code.strip()}```"
            )
        except (discord.Forbidden, discord.HTTPException):
            failed_dm_users.append(user_id)

    for user_id in failed_dm_users:
        await ctx.send(
            f"<@{user_id}> your DMs are closed. "
            "Click the button below to view your match code.",
            view=MatchCodeView(
                tournament["id"],
                round_name,
                match["match_id"],
            ),
            allowed_mentions=discord.AllowedMentions(users=True),
        )

    await ctx.send(f"Match code saved for **{match['match_id'].upper()}**.")
    await delete_command_message(ctx)


# ==================================================
# BRACKET COMMAND
# ==================================================

@bot.command(name="bracket")
async def bracket_command(ctx, round_name: str = "r1"):
    tournament = await host_tournament(ctx)

    if not tournament:
        await ctx.send(
            "You need the Host role or bot owner permission, "
            "and must use this in a tournament channel."
        )
        return

    if tournament["status"] == "finished":
        await ctx.send("This tournament has already finished.")
        return

    round_name = round_name.lower()

    if not round_name.startswith("r") or not round_name[1:].isdigit():
        await ctx.send("Usage: `!bracket r1`")
        return

    round_number = int(round_name[1:])

    if round_number < 1:
        await ctx.send("Invalid round number.")
        return

    if round_name in tournament["rounds"]:
        await ctx.send("This round already exists.")
        return

    if round_number == 1:
        player_ids = list(tournament["players"].keys())
        random.shuffle(player_ids)
    else:
        previous_round = tournament["rounds"].get(f"r{round_number - 1}")

        if not previous_round:
            await ctx.send("Create the previous round first.")
            return

        player_ids = list(tournament.get("qualified", []))

        if len(player_ids) < 2:
            await ctx.send("At least two qualified players are needed.")
            return

        random.shuffle(player_ids)

    if len(player_ids) < 2:
        await ctx.send("At least two registered players are required.")
        return

    matches = []

    for index in range(0, len(player_ids), 2):
        player_a = player_ids[index]
        player_b = player_ids[index + 1] if index + 1 < len(player_ids) else None

        matches.append({
            "match_id": f"m{len(matches) + 1}",
            "player_a": player_a,
            "player_b": player_b,
            "winner": None,
            "code": None,
        })

    # BYE players automatically advance.
    for match in matches:
        if match["player_b"] is None:
            match["winner"] = match["player_a"]

    tournament["rounds"][round_name] = matches
    tournament["qualified"] = [
        match["player_a"]
        for match in matches
        if match["player_b"] is None
    ]
    tournament["bracket_started"] = True
    tournament["registration_open"] = False
    tournament["status"] = "running"

    await save_data()
    await refresh_tournament(tournament)

    embed = discord.Embed(
        title=f"🏆 {tournament['name']} — {round_name.upper()}",
        color=discord.Color.gold(),
    )

    for match in matches:
        player_a = f"<@{match['player_a']}>"
        player_b = (
            f"<@{match['player_b']}>"
            if match["player_b"]
            else "**EMPTY (BYE)**"
        )

        status = (
            f"🏆 Qualified: {player_a}"
            if match["winner"]
            else "**WAITING...**"
        )

        embed.add_field(
            name=match["match_id"].upper(),
            value=f"{player_a} vs {player_b}\n{status}",
            inline=False,
        )

    await ctx.send(embed=embed, view=BracketView(tournament["id"], round_name))

    if all(match["winner"] for match in matches):
        await announce_qualified(ctx, tournament, round_name)


class BracketView(discord.ui.View):
    def __init__(self, tournament_id, round_name):
        super().__init__(timeout=None)
        self.tournament_id = str(tournament_id)
        self.round_name = round_name
        self.my_code.custom_id = f"bracketcode:{tournament_id}:{round_name}"

    @discord.ui.button(
        label="My Code",
        style=discord.ButtonStyle.primary,
        emoji="🔐",
    )
    async def my_code(self, interaction, button):
        tournament = get_tournament(self.tournament_id)

        if not tournament:
            await interaction.response.send_message(
                "Tournament not found.",
                ephemeral=True,
            )
            return

        matches = tournament["rounds"].get(self.round_name, [])

        match = next(
            (
                item for item in matches
                if str(interaction.user.id) in (
                    item["player_a"],
                    item["player_b"],
                )
            ),
            None,
        )

        if not match:
            await interaction.response.send_message(
                "You are not playing in this round.",
                ephemeral=True,
            )
            return

        code = match.get("code")

        if not code:
            await interaction.response.send_message(
                "The host has not posted your match code yet.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"🔐 **{match['match_id'].upper()} — Match Code**\n"
            f"```{code}```",
            ephemeral=True,
        )


async def announce_qualified(ctx, tournament, round_name):
    matches = tournament["rounds"].get(round_name, [])
    qualified = []

    for match in matches:
        if match["winner"] and match["winner"] not in qualified:
            qualified.append(match["winner"])

    tournament["qualified"] = qualified
    await save_data()

    next_round = int(round_name[1:]) + 1

    if len(qualified) == 1:
        tournament["placements"] = qualified[:]
        await save_data()
        await ctx.send(
            f"🏆 **Tournament Winner:** <@{qualified[0]}>"
        )
        await finish_tournament(ctx.guild, tournament)
        return

    if not qualified:
        await ctx.send("No players qualified for the next round.")
        return

    mentions = "\n".join(
        f"**{index}.** <@{user_id}>"
        for index, user_id in enumerate(qualified, start=1)
    )

    await ctx.send(
        f"✅ **Round {round_name[1:]} completed!**\n"
        f"🏆 **Players advancing to Round {next_round}:**\n{mentions}\n\n"
        f"Host: use `!bracket r{next_round}` to create the next round.",
        allowed_mentions=discord.AllowedMentions.none(),
    )


# ==================================================
# QUALIFY COMMAND
# ==================================================

@bot.command(name="qual")
async def qualify_command(ctx, match_id: str = None, winner: discord.Member = None):
    tournament = await host_tournament(ctx)

    if not tournament:
        await temporary_reply(ctx, "You do not have permission to host this tournament.")
        await delete_command_message(ctx)
        return

    if not match_id or winner is None:
        await temporary_reply(ctx, "Usage: `!qual m1 @player`")
        await delete_command_message(ctx)
        return

    current_round = max(
        tournament["rounds"],
        key=lambda name: int(name[1:]),
        default=None,
    )

    if current_round is None:
        await temporary_reply(ctx, "Create a bracket first.")
        await delete_command_message(ctx)
        return

    matches = tournament["rounds"][current_round]
    match = next(
        (item for item in matches if item["match_id"].lower() == match_id.lower()),
        None,
    )

    if not match:
        await temporary_reply(ctx, "Match not found in the current round.")
        await delete_command_message(ctx)
        return

    winner_id = str(winner.id)

    if winner_id not in (match["player_a"], match["player_b"]):
        await temporary_reply(ctx, "The winner must be one of the players in this match.")
        await delete_command_message(ctx)
        return

    if match["winner"]:
        await temporary_reply(ctx, "This match already has a winner.")
        await delete_command_message(ctx)
        return

    match["winner"] = winner_id

    if winner_id not in tournament["qualified"]:
        tournament["qualified"].append(winner_id)

    await save_data()

    loser_id = (
        match["player_b"]
        if winner_id == match["player_a"]
        else match["player_a"]
    )

    await ctx.send(
        f"✅ **Match {match['match_id'].upper()} Played**\n"
        f"🏆 Winner: <@{winner_id}>\n"
        f"~~Loser: <@{loser_id}>~~",
        allowed_mentions=discord.AllowedMentions.none(),
    )

    if all(item["winner"] for item in matches):
        await announce_qualified(ctx, tournament, current_round)


# ==================================================
# EDIT QUALIFIED PLAYER
# ==================================================

@bot.command(name="editqualified")
async def edit_qualified_command(ctx, old_player: discord.Member = None,
                                 new_player: discord.Member = None):
    tournament = await host_tournament(ctx)

    if not tournament:
        await temporary_reply(ctx, "You do not have permission to edit qualified players.")
        await delete_command_message(ctx)
        return

    if old_player is None or new_player is None:
        await temporary_reply(ctx, "Usage: `!editqualified @old @new`")
        await delete_command_message(ctx)
        return

    old_id = str(old_player.id)
    new_id = str(new_player.id)

    if old_id not in tournament["qualified"]:
        await temporary_reply(ctx, "The original player is not qualified.")
        await delete_command_message(ctx)
        return

    if new_id not in tournament["players"]:
        await temporary_reply(ctx, "The replacement player must be registered.")
        await delete_command_message(ctx)
        return

    tournament["qualified"] = [
        new_id if user_id == old_id else user_id
        for user_id in tournament["qualified"]
    ]

    await save_data()
    await temporary_reply(ctx, "Qualified player updated.")
    await delete_command_message(ctx)


# ==================================================
# DELETE TOURNAMENT
# ==================================================

@bot.command(name="delete")
async def delete_command(ctx, target=None):
    tournament = await host_tournament(ctx)

    if not tournament or not target or target.lower() != "tour":
        await temporary_reply(ctx, "Usage: `!delete tour` in a tournament channel.")
        await delete_command_message(ctx)
        return

    channel = ctx.channel
    data["tournaments"].pop(str(tournament["id"]), None)
    await save_data()

    await delete_command_message(ctx)

    try:
        await channel.delete(reason="Tournament deleted by host")
    except discord.HTTPException:
        pass


# ==================================================
# TRAINING SIMULATION
# ==================================================

@bot.command(name="training")
async def training_command(ctx, mode=None, member: discord.Member = None):
    if mode is None or mode.lower() != "tour" or member is None:
        await temporary_reply(ctx, "Usage: `!training tour @user`")
        await delete_command_message(ctx)
        return

    if not isinstance(ctx.author, discord.Member) or not can_host(ctx.author):
        await temporary_reply(ctx, "You need the Host role or bot owner permission.")
        await delete_command_message(ctx)
        return

    category = ctx.guild.get_channel(TOURNAMENT_CATEGORY_ID)
    if not isinstance(category, discord.CategoryChannel):
        category = None

    safe_name = "".join(
        char.lower() if char.isalnum() or char in "-_" else "-"
        for char in member.display_name
    )
    safe_name = "-".join(part for part in safe_name.split("-") if part)
    channel_name = f"training-{safe_name[:80] or member.id}"

    overwrites = {
        ctx.guild.default_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        ),
    }

    bot_member = ctx.guild.me
    if bot_member:
        overwrites[bot_member] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            read_message_history=True,
        )

    channel = await ctx.guild.create_text_channel(
        name=channel_name,
        category=category,
        overwrites=overwrites,
        reason="Training simulation created",
    )

    await delete_command_message(ctx)

    intro = discord.Embed(
        title="🎮 Training Tournament",
        description=(
            f"Training for {member.mention}\n\n"
            "This is a simulation for learning how to manage tournament matches. "
            "The four training bots are virtual players, not real Discord accounts."
        ),
        color=discord.Color.blurple(),
    )

    intro.add_field(
        name="How to practise",
        value=(
            "1. Review each simulated match.\n"
            "2. Learn how a host records winners.\n"
            "3. See who advances to the next round.\n"
            "4. Review the final example results."
        ),
        inline=False,
    )

    await channel.send(embed=intro)

    simulated_bots = [
        "Training Bot 1",
        "Training Bot 2",
        "Training Bot 3",
        "Training Bot 4",
    ]

    wins = 0
    results = []

    for index, bot_name in enumerate(simulated_bots, start=1):
        user_score = random.randint(0, 3)
        bot_score = random.randint(0, 3)

        if user_score == bot_score:
            user_score = (user_score + 1) % 4

        user_won = user_score > bot_score
        if user_won:
            wins += 1

        result_text = (
            f"🏆 Winner: {member.mention}"
            if user_won
            else f"🏆 Winner: **{bot_name}**"
        )

        results.append({
            "name": bot_name,
            "user_score": user_score,
            "bot_score": bot_score,
            "user_won": user_won,
        })

        embed = discord.Embed(
            title=f"Match {index}",
            description=(
                f"👤 {member.mention} — **{user_score} points**\n"
                f"🤖 **{bot_name}** — **{bot_score} points**\n\n"
                f"{result_text}"
            ),
            color=discord.Color.green() if user_won else discord.Color.red(),
        )
        embed.set_footer(text="SIMULATION — not a real game result")

        await channel.send(embed=embed)

    summary = discord.Embed(
        title="📊 Training Results",
        description=(
            f"Player: {member.mention}\n"
            f"Matches simulated: **4**\n"
            f"Example wins: **{wins}/4**\n\n"
            "These results were randomly generated for practice only."
        ),
        color=discord.Color.gold(),
    )

    summary.add_field(
        name="What you learned",
        value=(
            "• Each match has two opponents.\n"
            "• The host records the winner.\n"
            "• Winners advance through the bracket.\n"
            "• A simulation does not affect real tournaments or rewards."
        ),
        inline=False,
    )

    await channel.send(embed=summary)


# ==================================================
# SCHEDULER
# ==================================================

@tasks.loop(seconds=30)
async def tournament_scheduler():
    now = int(datetime.now(timezone.utc).timestamp())

    for tournament in list(data["tournaments"].values()):
        if tournament.get("status") == "finished":
            continue

        start = int(tournament["start_timestamp"])
        remaining = start - now

        if (
            0 < remaining <= REGISTRATION_OPEN_MINUTES * 60
            and not tournament["registration_open"]
            and not tournament["bracket_started"]
        ):
            tournament["registration_open"] = True
            tournament["status"] = "registration_open"
            await save_data()

            guild = bot.get_guild(int(tournament["guild_id"]))
            if guild:
                channel = guild.get_channel(int(tournament["channel_id"]))

                if isinstance(channel, discord.TextChannel):
                    role_ping = f"<@&{TOURNAMENT_PING_ROLE_ID}>"

                    await channel.send(
                        f"{role_ping}\n"
                        "🟢 **Registration is now open!**\n"
                        f"Starts {discord_time(start, 'R')}.",
                        allowed_mentions=discord.AllowedMentions(
                            roles=True,
                            users=False,
                            everyone=False,
                        ),
                    )

                    await refresh_tournament(tournament)

        if (
            remaining <= 0
            and tournament.get("status") in ("scheduled", "registration_open")
        ):
            tournament["status"] = "waiting_for_host"
            await save_data()

            guild = bot.get_guild(int(tournament["guild_id"]))
            if guild:
                channel = guild.get_channel(int(tournament["channel_id"]))

                if isinstance(channel, discord.TextChannel):
                    await channel.send(
                        "⏰ The scheduled start time has arrived. "
                        "The host can start with `!bracket r1`."
                    )


# ==================================================
# COMMAND MESSAGE CLEANUP
# ==================================================

@bot.event
async def on_command_completion(ctx):
    # Keep bracket messages visible. Help output is also kept for reference.
    if ctx.command and ctx.command.name in ("bracket", "h"):
        return

    await delete_command_message(ctx)


@bot.event
async def on_command_error(ctx, error):
    # Errors are intentionally left visible.
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(
            f"❌ Missing argument: `{error.param.name}`"
        )
        return

    if isinstance(error, commands.BadArgument):
        await ctx.send(
            "❌ Invalid argument. Check the command format with `!h tour`."
        )
        return

    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You do not have permission to use this command.")
        return

    print(f"Command error: {type(error).__name__}: {error}")
    await ctx.send("❌ An unexpected error occurred. Check the bot logs.")


# ==================================================
# STARTUP
# ==================================================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")

    bot.add_view(CreatePanelView())

    for tournament_id, tournament in data["tournaments"].items():
        bot.add_view(TournamentView(tournament_id))

        for round_name, matches in tournament.get("rounds", {}).items():
            bot.add_view(BracketView(tournament_id, round_name))

            for match in matches:
                if match.get("code"):
                    bot.add_view(
                        MatchCodeView(
                            tournament_id,
                            round_name,
                            match["match_id"],
                        )
                    )

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except discord.HTTPException as error:
        print(f"Slash sync failed: {error}")

    if not tournament_scheduler.is_running():
        tournament_scheduler.start()


bot.run(TOKEN)
