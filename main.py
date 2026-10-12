from __future__ import annotations

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
