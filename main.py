
from __future__ import annotations

import os
import json
import asyncio
import secrets
import threading
from pathlib import Path
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask, jsonify


# =========================
# CONFIGURATION
# =========================

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

# Rewards are normal chests, by placement.
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


# =========================
# BOT
# =========================

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
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


threading.Thread(target=run_web, daemon=True).start()


# =========================
# JSON STORAGE
# =========================

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

    except (json.JSONDecodeError, OSError):
        return {"tournaments": {}, "next_id": 1}


data = load_data()


async def save_data():
    async with data_lock:
        temp_file = DATA_FILE.with_suffix(".tmp")

        with temp_file.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)

        temp_file.replace(DATA_FILE)


def get_tournament(tournament_id: str):
    return data["tournaments"].get(str(tournament_id))


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def has_host_role(member: discord.Member) -> bool:
    return any(role.id == HOST_ROLE_ID for role in member.roles)


def can_host(member: discord.Member) -> bool:
    return is_owner(member.id) or has_host_role(member)


def parse_start_time(value: str):
    value = value.strip()

    try:
        # Accept ISO format, e.g. 2026-10-15 18:30
        parsed = datetime.fromisoformat(value)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except ValueError:
        return None


def discord_time(timestamp: int, style: str = "F") -> str:
    return f"<t:{timestamp}:{style}>"


def tournament_channel_name(name: str) -> str:
    cleaned = "".join(
        character.lower()
        if character.isalnum() or character in "-_"
        else "-"
        for character in name
    )

    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return f"🎮┃{cleaned[:80] or 'tournament'}"


def player_display(guild: discord.Guild, user_id: str) -> str:
    member = guild.get_member(int(user_id))
    return member.mention if member else f"<@{user_id}>"


# =========================
# EMBEDS
# =========================

def tournament_embed(tournament: dict) -> discord.Embed:
    start_timestamp = int(tournament["start_timestamp"])

    embed = discord.Embed(
        title=f"🎮 {tournament['name']}",
        description=tournament.get("description") or "No description provided.",
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name="Game Version",
        value=tournament["version"],
        inline=True,
    )
    embed.add_field(
        name="Map",
        value=tournament["map"],
        inline=True,
    )
    embed.add_field(
        name="Format",
        value=tournament["format"],
        inline=True,
    )
    embed.add_field(
        name="Maximum Players",
        value=str(tournament["max_players"]),
        inline=True,
    )
    embed.add_field(
        name="Ability",
        value=tournament.get("ability", "Not selected"),
        inline=True,
    )
    embed.add_field(
        name="Start Time",
        value=discord_time(start_timestamp, "F"),
        inline=False,
    )
    embed.add_field(
        name="Registration",
        value=(
            "🟢 Open"
            if tournament.get("registration_open")
            else "🔒 Closed"
        ),
        inline=True,
    )
    embed.add_field(
        name="Players",
        value=(
            f"{len(tournament['players'])}/"
            f"{tournament['max_players']}"
        ),
        inline=True,
    )

    embed.set_footer(text=f"Tournament ID: {tournament['id']}")
    return embed


def player_list_embed(guild: discord.Guild, tournament: dict):
    embed = discord.Embed(
        title=f"👥 Players — {tournament['name']}",
        color=discord.Color.blue(),
    )

    players = tournament["players"]

    if not players:
        description = "No players registered yet."
    else:
        lines = []

        for index, (user_id, player) in enumerate(players.items(), start=1):
            nickname = discord.utils.escape_markdown(
                player.get("nickname", "Player")
            )
            lines.append(
                f"**{index}.** {nickname} — <@{user_id}>"
            )

        description = "\n".join(lines)

    embed.description = description
    embed.set_footer(
        text=f"{len(players)}/{tournament['max_players']} players"
    )
    return embed


async def refresh_tournament_messages(tournament: dict):
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
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


# =========================
# CHEST API
# =========================

async def give_chests(user_id: int, amount: int, chest_type: str = "normal"):
    if not CHEST_API_URL or not CHEST_API_KEY:
        print("Chest rewards skipped: Chest API settings are missing.")
        return False

    transaction_id = secrets.token_hex(16)

    url = f"{CHEST_API_URL}/api/chests/add"

    headers = {
        "Authorization": f"Bearer {CHEST_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "user_id": str(user_id),
        "amount": amount,
        "chest_type": chest_type,
        "transaction_id": transaction_id,
    }

    try:
        timeout = aiohttp.ClientTimeout(total=15)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url,
                headers=headers,
                json=payload,
            ) as response:
                response_text = await response.text()

                if response.status not in (200, 201, 202):
                    print(
                        f"Chest API error {response.status}: "
                        f"{response_text[:500]}"
                    )
                    return False

                return True

    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        print(f"Chest API request failed: {error}")
        return False


# =========================
# REGISTRATION MODAL
# =========================

class RegistrationModal(discord.ui.Modal, title="Tournament Registration"):
    nickname = discord.ui.TextInput(
        label="In-game nickname",
        placeholder="Enter your game nickname",
        min_length=1,
        max_length=32,
    )

    def __init__(self, tournament_id: str):
        super().__init__()
        self.tournament_id = tournament_id

    async def on_submit(self, interaction: discord.Interaction):
        tournament = get_tournament(self.tournament_id)

        if not tournament:
            await interaction.response.send_message(
                "This tournament no longer exists.",
                ephemeral=True,
            )
            return

        if not tournament.get("registration_open"):
            await interaction.response.send_message(
                "Registration is currently closed.",
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
        }

        await save_data()
        await refresh_tournament_messages(tournament)

        await interaction.response.send_message(
            "You have successfully registered!",
            ephemeral=True,
        )


# =========================
# MY CODE MODAL
# =========================

class PlayerCodeModal(discord.ui.Modal, title="Submit Your Game Code"):
    game_code = discord.ui.TextInput(
        label="Game code",
        placeholder="Enter your match code",
        min_length=1,
        max_length=100,
    )

    def __init__(self, tournament_id: str):
        super().__init__()
        self.tournament_id = tournament_id

    async def on_submit(self, interaction: discord.Interaction):
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
                "You are not registered in this tournament.",
                ephemeral=True,
            )
            return

        tournament["players"][user_id]["code"] = str(
            self.game_code.value
        ).strip()

        await save_data()

        await interaction.response.send_message(
            "Your game code has been saved.",
            ephemeral=True,
        )


# =========================
# TOURNAMENT BUTTONS
# =========================

class TournamentView(discord.ui.View):
    def __init__(self, tournament_id: str):
        super().__init__(timeout=None)
        self.tournament_id = str(tournament_id)

        self.register_button.custom_id = f"tour:{tournament_id}:register"
        self.remove_button.custom_id = f"tour:{tournament_id}:remove"
        self.players_button.custom_id = f"tour:{tournament_id}:players"
        self.host_button.custom_id = f"tour:{tournament_id}:host"
        self.code_button.custom_id = f"tour:{tournament_id}:code"
        self.qualified_button.custom_id = f"tour:{tournament_id}:qualified"

    @discord.ui.button(
        label="Register",
        style=discord.ButtonStyle.success,
        emoji="📝",
    )
    async def register_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        tournament = get_tournament(self.tournament_id)

        if not tournament:
            await interaction.response.send_message(
                "Tournament not found.",
                ephemeral=True,
            )
            return

        if not tournament.get("registration_open"):
            await interaction.response.send_message(
                "Registration is not open yet.",
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
    async def remove_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        tournament = get_tournament(self.tournament_id)

        if not tournament:
            await interaction.response.send_message(
                "Tournament not found.",
                ephemeral=True,
            )
            return

        if tournament.get("bracket_started"):
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
        await refresh_tournament_messages(tournament)

        await interaction.response.send_message(
            "Your registration has been removed.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Players",
        style=discord.ButtonStyle.primary,
        emoji="👥",
    )
    async def players_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        tournament = get_tournament(self.tournament_id)

        if not tournament:
            await interaction.response.send_message(
                "Tournament not found.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=player_list_embed(interaction.guild, tournament),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Host",
        style=discord.ButtonStyle.secondary,
        emoji="🎤",
    )
    async def host_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        tournament = get_tournament(self.tournament_id)

        if not tournament:
            await interaction.response.send_message(
                "Tournament not found.",
                ephemeral=True,
            )
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This action is only available in a server.",
                ephemeral=True,
            )
            return

        if not can_host(interaction.user):
            await interaction.response.send_message(
                "You do not have permission to host this tournament.",
                ephemeral=True,
            )
            return

        tournament["host_id"] = interaction.user.id
        await save_data()

        await interaction.response.send_message(
            f"{interaction.user.mention} is now the tournament host.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="My Code",
        style=discord.ButtonStyle.secondary,
        emoji="🔢",
    )
    async def code_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        tournament = get_tournament(self.tournament_id)

        if not tournament:
            await interaction.response.send_message(
                "Tournament not found.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            PlayerCodeModal(self.tournament_id)
        )

    @discord.ui.button(
        label="Qualified",
        style=discord.ButtonStyle.secondary,
        emoji="🏆",
    )
    async def qualified_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        tournament = get_tournament(self.tournament_id)

        if not tournament:
            await interaction.response.send_message(
                "Tournament not found.",
                ephemeral=True,
            )
            return

        qualified = tournament.get("qualified", [])

        if not qualified:
            message = "No players have qualified yet."
        else:
            message = "\n".join(
                f"• <@{user_id}>" for user_id in qualified
            )

        await interaction.response.send_message(
            f"**Qualified Players**\n{message}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


# =========================
# CREATE TOURNAMENT FLOW
# =========================

class ContinueTournamentView(discord.ui.View):
    def __init__(self, creator_id: int, details: dict):
        super().__init__(timeout=300)
        self.creator_id = creator_id
        self.details = details

    @discord.ui.button(
        label="Continue",
        style=discord.ButtonStyle.primary,
        emoji="➡️",
    )
    async def continue_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message(
                "This form belongs to another user.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            TournamentScheduleModal(
                creator_id=self.creator_id,
                details=self.details,
            )
        )


class TournamentDetailsModal(
    discord.ui.Modal,
    title="Classic Tour — Details",
):
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
        placeholder="1v1",
        default="1v1",
        max_length=10,
    )
    max_players_input = discord.ui.TextInput(
        label="Maximum Players",
        placeholder="8",
        default="8",
        max_length=3,
    )

    def __init__(self, creator_id: int):
        super().__init__()
        self.creator_id = creator_id

    async def on_submit(self, interaction: discord.Interaction):
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


class TournamentScheduleModal(
    discord.ui.Modal,
    title="Tournament Schedule",
):
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

    def __init__(self, creator_id: int, details: dict):
        super().__init__()
        self.creator_id = creator_id
        self.details = details

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message(
                "This form belongs to another user.",
                ephemeral=True,
            )
            return

        start_time = parse_start_time(str(self.start_input.value))

        if start_time is None:
            await interaction.response.send_message(
                "Invalid time. Use YYYY-MM-DD HH:MM in UTC.",
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
            "Schedule saved! Choose the tournament ability.",
            view=AbilityView(
                creator_id=self.creator_id,
                details=self.details,
                start_timestamp=int(start_time.timestamp()),
                description=str(self.description_input.value).strip(),
                guild_id=interaction.guild_id,
            ),
            ephemeral=True,
        )


class AbilityView(discord.ui.View):
    def __init__(
        self,
        creator_id: int,
        details: dict,
        start_timestamp: int,
        description: str,
        guild_id: int | None,
    ):
        super().__init__(timeout=300)
        self.creator_id = creator_id
        self.details = details
        self.start_timestamp = start_timestamp
        self.description = description
        self.guild_id = guild_id
        self.ability = "No Ability"

        options = [
            discord.SelectOption(label="No Ability", value="No Ability"),
            discord.SelectOption(label="Punch", value="Punch"),
            discord.SelectOption(label="Block", value="Block"),
            discord.SelectOption(label="Invisibility", value="Invisibility"),
            discord.SelectOption(label="Double Jump", value="Double Jump"),
        ]

        self.ability_select.options = options

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
    async def ability_select(
        self,
        interaction: discord.Interaction,
        select: discord.ui.Select,
    ):
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
    async def create_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message(
                "This menu belongs to another user.",
                ephemeral=True,
            )
            return

        guild = interaction.guild

        if not guild:
            await interaction.response.send_message(
                "This must be used inside a server.",
                ephemeral=True,
            )
            return

        category = guild.get_channel(TOURNAMENT_CATEGORY_ID)

        if category is not None and not isinstance(
            category, discord.CategoryChannel
        ):
            category = None

        tournament_id = str(data["next_id"])
        data["next_id"] += 1

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                read_message_history=True,
            ),
        }

        channel = await guild.create_text_channel(
            name=tournament_channel_name(self.details["name"]),
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
            "name": self.details["name"],
            "version": self.details["version"],
            "map": self.details["map"],
            "format": self.details["format"],
            "max_players": self.details["max_players"],
            "start_timestamp": self.start_timestamp,
            "description": self.description,
            "ability": self.ability,
            "players": {},
            "qualified": [],
            "rounds": {},
            "placements": [],
            "registration_open": False,
            "bracket_started": False,
            "status": "scheduled",
            "rewarded": False,
            "created_at": int(datetime.now(timezone.utc).timestamp()),
        }

        data["tournaments"][tournament_id] = tournament
        await save_data()

        message = await channel.send(
            content=(
                f"Tournament created by <@{self.creator_id}>!\n"
                f"Registration opens 20 minutes before the start time.\n"
                f"Start: {discord_time(self.start_timestamp, 'F')}"
            ),
            embed=tournament_embed(tournament),
            view=TournamentView(tournament_id),
            allowed_mentions=discord.AllowedMentions.none(),
        )

        tournament["panel_message_id"] = message.id
        await save_data()

        announcement_channel = guild.get_channel(
            ANNOUNCEMENTS_CHANNEL_ID
        )

        if isinstance(announcement_channel, discord.TextChannel):
            await announcement_channel.send(
                f"🎮 **New Tournament: {tournament['name']}**\n"
                f"Start: {discord_time(self.start_timestamp, 'F')}\n"
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
    async def classic_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This must be used in a server.",
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
            TournamentDetailsModal(interaction.user.id)
        )


# =========================
# SLASH COMMANDS
# =========================

@bot.tree.command(
    name="create",
    description="Create a tournament panel",
)
@app_commands.describe(channel="Channel where the panel will be posted")
async def create_command(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
):
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "This command must be used in a server.",
            ephemeral=True,
        )
        return

    if not can_host(interaction.user):
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


# =========================
# HOST COMMANDS
# =========================

async def get_host_tournament(ctx, tournament_id: str | None = None):
    if not isinstance(ctx.author, discord.Member):
        return None

    if not can_host(ctx.author):
        return None

    if tournament_id:
        tournament = get_tournament(tournament_id)

        if tournament and int(tournament["guild_id"]) == ctx.guild.id:
            return tournament

        return None

    channel_id = ctx.channel.id

    for tournament in data["tournaments"].values():
        if (
            int(tournament["channel_id"]) == channel_id
            and int(tournament["guild_id"]) == ctx.guild.id
        ):
            return tournament

    return None


@bot.command(name="bracket")
async def bracket_command(ctx, round_name: str = "r1"):
    tournament = await get_host_tournament(ctx)

    if not tournament:
        await ctx.reply(
            "You need the Host role or bot owner permission, "
            "and you must use this command in a tournament channel."
        )
        return

    if tournament.get("status") == "finished":
        await ctx.reply("This tournament has already finished.")
        return

    if len(tournament["players"]) < 2:
        await ctx.reply("At least two players are required.")
        return

    round_name = round_name.lower()

    if not round_name.startswith("r") or not round_name[1:].isdigit():
        await ctx.reply("Usage: `!bracket r1`")
        return

    round_number = int(round_name[1:])

    if round_number < 1:
        await ctx.reply("Invalid round number.")
        return

    if round_name in tournament["rounds"]:
        await ctx.reply("This round already exists.")
        return

    if round_number == 1:
        player_ids = list(tournament["players"].keys())
        secrets.SystemRandom().shuffle(player_ids)
    else:
        previous_round = tournament["rounds"].get(f"r{round_number - 1}")

        if not previous_round:
            await ctx.reply("Create the previous round first.")
            return

        player_ids = tournament.get("qualified", [])

        if len(player_ids) < 2:
            await ctx.reply(
                "At least two qualified players are needed for this round."
            )
            return

        secrets.SystemRandom().shuffle(player_ids)

    matches = []

    for index in range(0, len(player_ids), 2):
        player_a = player_ids[index]
        player_b = (
            player_ids[index + 1]
            if index + 1 < len(player_ids)
            else None
        )

        match_number = len(matches) + 1

        matches.append({
            "match_id": f"m{match_number}",
            "player_a": player_a,
            "player_b": player_b,
            "winner": None,
        })

    tournament["rounds"][round_name] = matches
    tournament["qualified"] = []
    tournament["bracket_started"] = True
    tournament["registration_open"] = False
    tournament["status"] = "running"

    await save_data()
    await refresh_tournament_messages(tournament)

    embed = discord.Embed(
        title=f"🏆 {tournament['name']} — {round_name.upper()}",
        color=discord.Color.gold(),
    )

    for match in matches:
        a = player_display(ctx.guild, match["player_a"])
        b = (
            player_display(ctx.guild, match["player_b"])
            if match["player_b"]
            else "**EMPTY (BYE)**"
        )

        embed.add_field(
            name=match["match_id"],
            value=f"{a}  vs  {b}\n**WAITING...**",
            inline=False,
        )

    await ctx.send(embed=embed)


@bot.command(name="qual")
async def qualify_command(ctx, match_id: str, winner: discord.Member):
    tournament = await get_host_tournament(ctx)

    if not tournament:
        await ctx.reply(
            "You need the Host role or bot owner permission, "
            "and must use this in a tournament channel."
        )
        return

    if not tournament.get("rounds"):
        await ctx.reply("Create a bracket first using `!bracket r1`.")
        return

    if str(winner.id) not in tournament["players"]:
        await ctx.reply("The selected winner is not registered.")
        return

    current_round_name = list(tournament["rounds"].keys())[-1]
    matches = tournament["rounds"][current_round_name]

    match = next(
        (
            item for item in matches
            if item["match_id"].lower() == match_id.lower()
        ),
        None,
    )

    if not match:
        await ctx.reply("Match not found in the current round.")
        return

    allowed_winners = {
        match["player_a"],
        match["player_b"],
    }

    if str(winner.id) not in allowed_winners:
        await ctx.reply("The winner must be one of the players in this match.")
        return

    if match["winner"]:
        await ctx.reply("This match already has a winner.")
        return

    match["winner"] = str(winner.id)

    if str(winner.id) not in tournament["qualified"]:
        tournament["qualified"].append(str(winner.id))

    await save_data()

    await ctx.send(
        f"🏆 {winner.mention} qualified from **{match_id.upper()}**."
    )

    all_finished = all(item["winner"] for item in matches)

    if all_finished:
        qualified = tournament["qualified"]

        if len(qualified) == 1:
            tournament["placements"] = qualified[:]
            await save_data()
            await finish_tournament(ctx.guild, tournament)
        else:
            await ctx.send(
                f"Round finished! Use `!bracket r{int(current_round_name[1:]) + 1}` "
                "to create the next round."
            )


async def finish_tournament(guild: discord.Guild, tournament: dict):
    if tournament.get("rewarded"):
        return

    placements = tournament.get("placements", [])

    if not placements:
        placements = tournament.get("qualified", [])

    if not placements:
        return

    tournament["status"] = "finished"
    tournament["registration_open"] = False
    tournament["rewarded"] = True
    tournament["placements"] = placements

    await save_data()

    embed = discord.Embed(
        title=f"🏆 Tournament Results — {tournament['name']}",
        color=discord.Color.gold(),
    )

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}

    for place, user_id in enumerate(placements[:10], start=1):
        amount = REWARDS.get(place, 0)
        medal = medals.get(place, f"**#{place}**")

        embed.add_field(
            name=f"{medal} Place {place}",
            value=f"<@{user_id}> — 🎁 {amount} Normal Chests",
            inline=False,
        )

    results_channel = guild.get_channel(RESULTS_CHANNEL_ID)

    if isinstance(results_channel, discord.TextChannel):
        await results_channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    for place, user_id in enumerate(placements[:10], start=1):
        amount = REWARDS.get(place, 0)

        if amount <= 0:
            continue

        success = await give_chests(
            user_id=int(user_id),
            amount=amount,
            chest_type="normal",
        )

        user = bot.get_user(int(user_id))

        if user is None:
            try:
                user = await bot.fetch_user(int(user_id))
            except discord.HTTPException:
                user = None

        if user:
            try:
                if success:
                    await user.send(
                        f"🏆 You placed **#{place}** in "
                        f"**{tournament['name']}** and received "
                        f"**{amount} Normal Chests**!"
                    )
                else:
                    await user.send(
                        f"🏆 You placed **#{place}** in "
                        f"**{tournament['name']}**. Your reward could not "
                        "be confirmed automatically. Please contact the host."
                    )
            except discord.HTTPException:
                pass


@bot.command(name="editqualified")
async def edit_qualified_command(
    ctx,
    old_player: discord.Member,
    new_player: discord.Member,
):
    tournament = await get_host_tournament(ctx)

    if not tournament:
        await ctx.reply("You do not have permission to edit qualified players.")
        return

    old_id = str(old_player.id)
    new_id = str(new_player.id)

    if old_id not in tournament["qualified"]:
        await ctx.reply("The original player is not in the qualified list.")
        return

    if new_id not in tournament["players"]:
        await ctx.reply("The replacement player must be registered.")
        return

    tournament["qualified"] = [
        new_id if player_id == old_id else player_id
        for player_id in tournament["qualified"]
    ]

    await save_data()

    await ctx.reply(
        f"Updated qualified player: {old_player.mention} → {new_player.mention}"
    )


@bot.command(name="delete")
async def delete_command(ctx, target: str = "tour"):
    if target.lower() != "tour":
        await ctx.reply("Usage: `!delete tour`")
        return

    tournament = await get_host_tournament(ctx)

    if not tournament:
        await ctx.reply(
            "Only the bot owner or a user with the Host role can delete "
            "a tournament from its channel."
        )
        return

    channel = ctx.channel
    tournament_id = tournament["id"]

    data["tournaments"].pop(tournament_id, None)
    await save_data()

    await ctx.reply("Tournament deleted.")

    if isinstance(channel, discord.TextChannel):
        try:
            await channel.delete(reason="Tournament deleted by host")
        except discord.HTTPException:
            pass


# =========================
# REGISTRATION SCHEDULER
# =========================

@tasks.loop(seconds=30)
async def tournament_scheduler():
    now = int(datetime.now(timezone.utc).timestamp())

    for tournament in list(data["tournaments"].values()):
        if tournament.get("status") == "finished":
            continue

        start_timestamp = int(tournament["start_timestamp"])
        seconds_until_start = start_timestamp - now

        # Open registration 20 minutes before start.
        if (
            seconds_until_start <= REGISTRATION_OPEN_MINUTES * 60
            and seconds_until_start > 0
            and not tournament.get("registration_open")
            and not tournament.get("bracket_started")
        ):
            tournament["registration_open"] = True
            tournament["status"] = "registration_open"
            await save_data()

            guild = bot.get_guild(int(tournament["guild_id"]))

            if guild:
                channel = guild.get_channel(int(tournament["channel_id"]))

                if isinstance(channel, discord.TextChannel):
                    role_mention = (
                        f"<@&{TOURNAMENT_PING_ROLE_ID}>"
                        if TOURNAMENT_PING_ROLE_ID
                        else ""
                    )

                    await channel.send(
                        f"{role_mention}\n"
                        f"🟢 **Registration is now open!**\n"
                        f"Tournament starts {discord_time(start_timestamp, 'R')}.",
                        allowed_mentions=discord.AllowedMentions(
                            roles=True,
                            users=False,
                            everyone=False,
                        ),
                    )

                    await refresh_tournament_messages(tournament)

        # Remind the channel when the scheduled start time arrives.
        if (
            seconds_until_start <= 0
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
                        "The host can now start the bracket with `!bracket r1`."
                    )


# =========================
# STARTUP
# =========================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")

    bot.add_view(CreatePanelView())

    for tournament_id in data["tournaments"]:
        bot.add_view(TournamentView(tournament_id))

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands.")
    except discord.HTTPException as error:
        print(f"Slash command sync failed: {error}")

    if not tournament_scheduler.is_running():
        tournament_scheduler.start()


bot.run(TOKEN)
