import asyncio
import json
import os
import random
import re
import time
import uuid
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from discord import app_commands

from chest_api import give_chests, ChestAPIError


OWNER_ID = 1176149190192152626
HOST_ROLE_ID = 1542739534343700500
TOURNAMENT_ROLE_ID = 1541715118872731768

ANNOUNCEMENTS_CHANNEL_ID = 1559029372835467284
RESULTS_CHANNEL_ID = 1559029648795369522
TOURNAMENT_CATEGORY_ID = 1559030166414430208

DATA_FILE = "tournaments.json"

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


def load_data():
    if not os.path.exists(DATA_FILE):
        return {"tournaments": {}}

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            return {"tournaments": {}}
        data.setdefault("tournaments", {})
        return data
    except (json.JSONDecodeError, OSError):
        return {"tournaments": {}}


def save_data(data):
    temporary_file = DATA_FILE + ".tmp"
    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    os.replace(temporary_file, DATA_FILE)


def parse_start_time(value):
    value = value.strip()

    if value.isdigit():
        timestamp = int(value)
        if timestamp <= int(time.time()):
            raise ValueError("Start time must be in the future.")
        return timestamp

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            "Use a Unix timestamp or ISO time, e.g. 2026-12-25T18:00:00+04:00."
        ) from exc

    if parsed.tzinfo is None:
        raise ValueError("Include a timezone, e.g. +04:00.")

    timestamp = int(parsed.timestamp())
    if timestamp <= int(time.time()):
        raise ValueError("Start time must be in the future.")

    return timestamp


def safe_channel_name(name):
    name = re.sub(r"[^a-zA-Z0-9-]", "-", name.lower())
    name = re.sub(r"-+", "-", name).strip("-")
    return (name or "tournament")[:80]


def member_is_host(member):
    return (
        member.id == OWNER_ID
        or any(role.id == HOST_ROLE_ID for role in getattr(member, "roles", []))
    )


class TournamentCreateModal(discord.ui.Modal, title="Create Classic Tour"):
    tour_name = discord.ui.TextInput(
        label="Tournament name",
        placeholder="RULL",
        max_length=50,
    )
    version = discord.ui.TextInput(
        label="Game version",
        placeholder="0.103",
        max_length=20,
    )
    game_map = discord.ui.TextInput(
        label="Map",
        placeholder="Block Dash Classic",
        max_length=60,
    )
    tour_format = discord.ui.TextInput(
        label="Format",
        placeholder="1v1",
        max_length=20,
    )
    max_players = discord.ui.TextInput(
        label="Maximum players",
        placeholder="8",
        max_length=4,
    )
    start_time = discord.ui.TextInput(
        label="Start time (Unix timestamp or ISO with timezone)",
        placeholder="1790000000",
        max_length=40,
    )
    description = discord.ui.TextInput(
        label="Description",
        placeholder="Tournament details",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )
    ability = discord.ui.TextInput(
        label="Ability",
        placeholder="Punch",
        max_length=60,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not member_is_host(interaction.user):
            await interaction.response.send_message(
                "You do not have permission to create tournaments.",
                ephemeral=True,
            )
            return

        try:
            start_timestamp = parse_start_time(str(self.start_time))
            maximum = int(str(self.max_players))
            if not 2 <= maximum <= 256:
                raise ValueError("Maximum players must be between 2 and 256.")
        except ValueError as exc:
            await interaction.response.send_message(
                f"Invalid settings: {exc}",
                ephemeral=True,
            )
            return

        cog = interaction.client.get_cog("TournamentCog")
        if cog is None:
            await interaction.response.send_message(
                "Tournament system is not ready. Try again later.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "Use this command inside a server.",
                ephemeral=True,
            )
            return

        category = guild.get_channel(TOURNAMENT_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            await interaction.followup.send(
                "Tournament category was not found. Check the category ID.",
                ephemeral=True,
            )
            return

        channel_name = "🎮┃" + safe_channel_name(str(self.tour_name))

        try:
            channel = await guild.create_text_channel(
                channel_name,
                category=category,
                reason=f"Tournament created by {interaction.user}",
            )
        except discord.HTTPException:
            await interaction.followup.send(
                "Could not create the tournament channel. Check bot permissions.",
                ephemeral=True,
            )
            return

        tournament_id = str(channel.id)
        tournament = {
            "id": tournament_id,
            "guild_id": guild.id,
            "channel_id": channel.id,
            "name": str(self.tour_name),
            "version": str(self.version),
            "map": str(self.game_map),
            "format": str(self.tour_format),
            "max_players": maximum,
            "start_time": start_timestamp,
            "description": str(self.description) or "No description provided.",
            "ability": str(self.ability),
            "host_id": interaction.user.id,
            "players": {},
            "rounds": [],
            "created_at": int(time.time()),
            "registration_open": False,
            "finished": False,
            "cancelled": False,
            "reward_transaction_ids": [],
        }

        cog.data["tournaments"][tournament_id] = tournament
        save_data(cog.data)

        view = TournamentView(cog, tournament_id)
        await channel.send(
            content=(
                f"<@&{TOURNAMENT_ROLE_ID}>\n"
                f"**A new tournament has been created!**\n"
                f"Registration opens 20 minutes before the start."
            ),
            embed=cog.make_embed(tournament),
            view=view,
            allowed_mentions=discord.AllowedMentions(
                roles=True, users=False, everyone=False
            ),
        )

        announcement_channel = guild.get_channel(ANNOUNCEMENTS_CHANNEL_ID)
        if isinstance(announcement_channel, discord.TextChannel):
            announcement_embed = cog.make_embed(tournament)
            announcement_embed.add_field(
                name="Tournament channel",
                value=channel.mention,
                inline=False,
            )
            await announcement_channel.send(embed=announcement_embed)

        await interaction.followup.send(
            f"Tournament created: {channel.mention}",
            ephemeral=True,
        )


class CreateClassicView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Classic Tour",
        style=discord.ButtonStyle.primary,
        custom_id="tournament:create_classic",
        emoji="🎮",
    )
    async def classic(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.send_modal(TournamentCreateModal())


class TournamentView(discord.ui.View):
    def __init__(self, cog, tournament_id):
        super().__init__(timeout=None)
        self.cog = cog
        self.tournament_id = str(tournament_id)

        self.add_item(RegisterButton(self.cog, self.tournament_id))
        self.add_item(UnregisterButton(self.cog, self.tournament_id))
        self.add_item(PlayersButton(self.cog, self.tournament_id))

    def get_tournament(self):
        return self.cog.data["tournaments"].get(self.tournament_id)


class RegisterModal(discord.ui.Modal, title="Tournament Registration"):
    nickname = discord.ui.TextInput(
        label="In-game nickname",
        placeholder="RULL[LEGEND]",
        max_length=40,
    )

    def __init__(self, cog, tournament_id):
        super().__init__()
        self.cog = cog
        self.tournament_id = str(tournament_id)

    async def on_submit(self, interaction: discord.Interaction):
        tournament = self.cog.data["tournaments"].get(self.tournament_id)

        if not tournament or tournament["finished"] or tournament["cancelled"]:
            await interaction.response.send_message(
                "This tournament is no longer available.",
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
        nickname = str(self.nickname).strip()

        if not nickname:
            await interaction.response.send_message(
                "Please enter your in-game nickname.",
                ephemeral=True,
            )
            return

        if user_id in tournament["players"]:
            await interaction.response.send_message(
                "You are already registered.",
                ephemeral=True,
            )
            return

        normalized = nickname.casefold()
        for player in tournament["players"].values():
            if player["nickname"].casefold() == normalized:
                await interaction.response.send_message(
                    "That in-game nickname is already registered.",
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
            "nickname": nickname,
            "username": interaction.user.name,
            "registered_at": int(time.time()),
        }
        save_data(self.cog.data)

        await self.cog.refresh_panel(tournament)
        await interaction.response.send_message(
            "You have registered successfully!",
            ephemeral=True,
        )


class RegisterButton(discord.ui.Button):
    def __init__(self, cog, tournament_id):
        super().__init__(
            label="Register",
            style=discord.ButtonStyle.success,
            custom_id=f"tournament:register:{tournament_id}",
        )
        self.cog = cog
        self.tournament_id = str(tournament_id)

    async def callback(self, interaction: discord.Interaction):
        tournament = self.cog.data["tournaments"].get(self.tournament_id)

        if not tournament or not tournament["registration_open"]:
            await interaction.response.send_message(
                "Registration is not open yet.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            RegisterModal(self.cog, self.tournament_id)
        )


class UnregisterButton(discord.ui.Button):
    def __init__(self, cog, tournament_id):
        super().__init__(
            label="Remove registration",
            style=discord.ButtonStyle.danger,
            custom_id=f"tournament:unregister:{tournament_id}",
        )
        self.cog = cog
        self.tournament_id = str(tournament_id)

    async def callback(self, interaction: discord.Interaction):
        tournament = self.cog.data["tournaments"].get(self.tournament_id)

        if not tournament or not tournament["registration_open"]:
            await interaction.response.send_message(
                "Registration is closed.",
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
        save_data(self.cog.data)
        await self.cog.refresh_panel(tournament)

        await interaction.response.send_message(
            "Your registration has been removed.",
            ephemeral=True,
        )


class PlayersButton(discord.ui.Button):
    def __init__(self, cog, tournament_id):
        super().__init__(
            label="Players",
            style=discord.ButtonStyle.secondary,
            custom_id=f"tournament:players:{tournament_id}",
        )
        self.cog = cog
        self.tournament_id = str(tournament_id)

    async def callback(self, interaction: discord.Interaction):
        tournament = self.cog.data["tournaments"].get(self.tournament_id)

        if not tournament:
            await interaction.response.send_message(
                "Tournament not found.",
                ephemeral=True,
            )
            return

        embed = self.cog.players_embed(tournament)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class TournamentCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data = load_data()
        self.timer_loop.start()

    def cog_unload(self):
        self.timer_loop.cancel()

    def get_current_tournament(self, channel_id):
        for tournament in self.data["tournaments"].values():
            if tournament["channel_id"] == channel_id:
                if not tournament["finished"] and not tournament["cancelled"]:
                    return tournament
        return None

    def make_embed(self, tournament):
        embed = discord.Embed(
            title=f"🎮 {tournament['name']}",
            color=discord.Color.blurple(),
            description=tournament["description"],
        )
        embed.add_field(name="Version", value=tournament["version"], inline=True)
        embed.add_field(name="Map", value=tournament["map"], inline=True)
        embed.add_field(name="Format", value=tournament["format"], inline=True)
        embed.add_field(
            name="Maximum players",
            value=str(tournament["max_players"]),
            inline=True,
        )
        embed.add_field(
            name="Ability",
            value=tournament["ability"],
            inline=True,
        )
        embed.add_field(
            name="Host",
            value=f"<@{tournament['host_id']}>",
            inline=True,
        )
        embed.add_field(
            name="Starts",
            value=f"<t:{tournament['start_time']}:F>\n<t:{tournament['start_time']}:R>",
            inline=False,
        )
        embed.add_field(
            name="Registration",
            value=(
                "🟢 Open"
                if tournament["registration_open"]
                else "🔒 Closed"
            ),
            inline=True,
        )
        embed.add_field(
            name="Players",
            value=f"{len(tournament['players'])}/{tournament['max_players']}",
            inline=True,
        )
        embed.set_footer(text=f"Tournament ID: {tournament['id']}")
        return embed

    def players_embed(self, tournament):
        embed = discord.Embed(
            title=f"Players — {tournament['name']}",
            color=discord.Color.blurple(),
        )

        if not tournament["players"]:
            embed.description = "No players registered yet."
        else:
            lines = []
            for index, (user_id, player) in enumerate(
                tournament["players"].items(), start=1
            ):
                lines.append(
                    f"**{index}.** <@{user_id}>\n"
                    f"-#➜{discord.utils.escape_markdown(player['nickname'])}"
                )
            embed.description = "\n".join(lines)

        return embed

    async def refresh_panel(self, tournament):
        channel = self.bot.get_channel(tournament["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return

        async for message in channel.history(limit=30):
            if (
                message.author.id == self.bot.user.id
                and message.embeds
                and message.embeds[0].footer
                and message.embeds[0].footer.text
                == f"Tournament ID: {tournament['id']}"
            ):
                await message.edit(
                    embed=self.make_embed(tournament),
                    view=TournamentView(self, tournament["id"]),
                )
                return

    async def publish_results(self, tournament, champion_id):
        guild = self.bot.get_guild(tournament["guild_id"])
        if guild is None:
            return

        results_channel = guild.get_channel(RESULTS_CHANNEL_ID)
        champion = tournament["players"].get(str(champion_id))

        if not isinstance(results_channel, discord.TextChannel) or not champion:
            return

        embed = discord.Embed(
            title=f"🏆 {tournament['name']} — Results",
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="🥇 1st Place",
            value=f"<@{champion_id}> — {champion['nickname']}\n🎁 40 Normal Chests",
            inline=False,
        )
        await results_channel.send(embed=embed)

        transaction_id = f"{tournament['id']}:reward:{champion_id}:normal"
        try:
            await give_chests(
                int(champion_id),
                40,
                "chest",
                transaction_id,
            )
            if random.random() < 0.01:
                await give_chests(
                    int(champion_id),
                    1,
                    "ultra",
                    f"{tournament['id']}:reward:{champion_id}:ultra",
                )
        except ChestAPIError as exc:
            print(f"Could not award tournament reward: {exc}")

        try:
            user = await self.bot.fetch_user(int(champion_id))
            await user.send(
                f"Congratulations! You won **{tournament['name']}** "
                "and received 40 Normal Chests."
            )
        except (discord.HTTPException, discord.NotFound):
            pass

        tournament["finished"] = True
        save_data(self.data)

    async def close_registration(self, tournament):
        if tournament["registration_open"]:
            tournament["registration_open"] = False
            save_data(self.data)
            await self.refresh_panel(tournament)

            channel = self.bot.get_channel(tournament["channel_id"])
            if isinstance(channel, discord.TextChannel):
                await channel.send("🔒 Registration is now closed.")

    @tasks.loop(seconds=30)
    async def timer_loop(self):
        now = int(time.time())

        for tournament in list(self.data["tournaments"].values()):
            if tournament["finished"] or tournament["cancelled"]:
                continue

            start = tournament["start_time"]
            channel = self.bot.get_channel(tournament["channel_id"])

            if not isinstance(channel, discord.TextChannel):
                continue

            remaining = start - now

            if remaining <= 20 * 60 and remaining > 0:
                if not tournament["registration_open"]:
                    tournament["registration_open"] = True
                    save_data(self.data)
                    await self.refresh_panel(tournament)

                    await channel.send(
                        f"🟢 Registration is now open for "
                        f"<t:{start}:R>!"
                    )

            if remaining <= 0 and tournament["registration_open"]:
                await self.close_registration(tournament)

    @timer_loop.before_loop
    async def before_timer_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="create_tour",
        description="Post the Classic Tour creation panel",
    )
    @app_commands.describe(channel="Channel where the creation panel will be posted")
    async def create_tour(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        if not member_is_host(interaction.user):
            await interaction.response.send_message(
                "You do not have permission to create tournaments.",
                ephemeral=True,
            )
            return

        await channel.send(
            "Press the button below to create a Classic Tour.",
            view=CreateClassicView(),
        )
        await interaction.response.send_message(
            f"Creation panel posted in {channel.mention}.",
            ephemeral=True,
        )

    @commands.command(name="delete")
    async def delete_tour(self, ctx, *, target="tour"):
        if target.lower() != "tour":
            return

        tournament = self.get_current_tournament(ctx.channel.id)
        if tournament is None:
            await ctx.send("This channel is not an active tournament.")
            return

        if ctx.author.id != OWNER_ID and ctx.author.id != tournament["host_id"]:
            await ctx.send("Only the owner or this tournament's host can delete it.")
            return

        tournament["cancelled"] = True
        save_data(self.data)
        await ctx.send("Tournament is being deleted.")
        await asyncio.sleep(2)
        await ctx.channel.delete(reason=f"Tournament deleted by {ctx.author}")


    @commands.command(name="bracket")
    async def bracket(self, ctx, round_name: str = ""):
        tournament = self.get_current_tournament(ctx.channel.id)

        if tournament is None:
            await ctx.send("This is not an active tournament channel.")
            return

        if not member_is_host(ctx.author):
            await ctx.send("Only the owner or a host can manage the bracket.")
            return

        match = re.fullmatch(r"r(\d+)", round_name.lower())
        if not match:
            await ctx.send("Usage: `!bracket r1`")
            return

        round_number = int(match.group(1))
        rounds = tournament["rounds"]

        if round_number != len(rounds) + 1:
            await ctx.send("Create rounds in order: `!bracket r1`, then `!bracket r2`.")
            return

        if round_number == 1:
            players = list(tournament["players"].keys())
            if len(players) < 2:
                await ctx.send("At least 2 registered players are required.")
                return
            random.shuffle(players)
        else:
            previous = rounds[-1]
            if any(m["status"] not in ("FINISHED", "BYE") for m in previous["matches"]):
                await ctx.send("Finish every match in the previous round first.")
                return
            players = []
            for item in previous["matches"]:
                if item.get("winner"):
                    players.append(item["winner"])

        if len(players) < 2:
            if players:
                await self.publish_results(tournament, players[0])
                await ctx.send("🏆 Tournament finished!")
            return

        matches = []
        for index in range(0, len(players), 2):
            player_a = players[index]
            player_b = players[index + 1] if index + 1 < len(players) else None
            match_id = f"m{len(matches) + 1}"
            status = "WAITING" if player_b else "BYE"
            matches.append({
                "id": match_id,
                "player_a": player_a,
                "player_b": player_b,
                "winner": player_a if player_b is None else None,
                "status": status,
                "code": None,
            })

        rounds.append({
            "number": round_number,
            "matches": matches,
        })
        save_data(self.data)

        embed = discord.Embed(
            title=f"🏆 Round {round_number}",
            color=discord.Color.blurple(),
        )
        lines = []
        for item in matches:
            player_a = tournament["players"].get(item["player_a"], {})
            name_a = player_a.get("nickname", item["player_a"])
            if item["player_b"]:
                player_b = tournament["players"].get(item["player_b"], {})
                name_b = player_b.get("nickname", item["player_b"])
                lines.append(
                    f"**{item['id']}** — {name_a} vs {name_b} · `{item['status']}`"
                )
            else:
                lines.append(
                    f"**{item['id']}** — {name_a} vs **EMPTY** · `BYE`"
                )

        embed.description = "\n".join(lines)
        await ctx.send(embed=embed)

    @commands.command(name="match")
    async def set_match_code(self, ctx, match_id: str, *, code: str):
        tournament = self.get_current_tournament(ctx.channel.id)

        if tournament is None:
            await ctx.send("This is not an active tournament channel.")
            return

        if not member_is_host(ctx.author):
            await ctx.send("Only the owner or a host can set match codes.")
            return

        if not tournament["rounds"]:
            await ctx.send("Create the bracket first with `!bracket r1`.")
            return

        current_round = tournament["rounds"][-1]
        item = next(
            (
                match for match in current_round["matches"]
                if match["id"].lower() == match_id.lower()
            ),
            None,
        )

        if item is None:
            await ctx.send("Match not found in the current round.")
            return

        if item["status"] in ("FINISHED", "BYE"):
            await ctx.send("This match is already finished.")
            return

        item["code"] = code
        item["status"] = "PROCESSING"
        save_data(self.data)

        sent = []
        for user_id in (item["player_a"], item["player_b"]):
            if not user_id:
                continue
            try:
                user = await self.bot.fetch_user(int(user_id))
                await user.send(
                    f"🎮 Match code for **{tournament['name']}** "
                    f"({item['id']}): `{code}`"
                )
                sent.append(user_id)
            except (discord.HTTPException, discord.NotFound):
                pass

        await ctx.send(
            f"Match `{item['id']}` is PROCESSING. "
            f"Code sent by DM to {len(sent)} player(s)."
        )

    @commands.command(name="mycode")
    async def my_code(self, ctx):
        tournament = self.get_current_tournament(ctx.channel.id)

        if tournament is None:
            await ctx.send("This is not an active tournament channel.")
            return

        user_id = str(ctx.author.id)

        for round_data in reversed(tournament["rounds"]):
            for item in round_data["matches"]:
                if user_id not in (item["player_a"], item["player_b"]):
                    continue

                if not item["code"]:
                    await ctx.send("Your match code has not been set yet.")
                    return

                if item["status"] == "FINISHED":
                    await ctx.send("This match has already finished.")
                    return

                try:
                    await ctx.author.send(
                        f"Your match code for **{tournament['name']}**: "
                        f"`{item['code']}`"
                    )
                    await ctx.send("I sent your code by DM.")
                except discord.HTTPException:
                    await ctx.send("I could not DM you. Please enable server DMs.")
                return

        await ctx.send("You are not in a current tournament match.")

    @commands.command(name="qual")
    async def qualify(self, ctx, match_id: str, winner: discord.Member):
        tournament = self.get_current_tournament(ctx.channel.id)

        if tournament is None:
            await ctx.send("This is not an active tournament channel.")
            return

        if not member_is_host(ctx.author):
            await ctx.send("Only the owner or a host can qualify players.")
            return

        if not tournament["rounds"]:
            await ctx.send("Create the bracket first.")
            return

        current_round = tournament["rounds"][-1]
        item = next(
            (
                match for match in current_round["matches"]
                if match["id"].lower() == match_id.lower()
            ),
            None,
        )

        if item is None:
            await ctx.send("Match not found.")
            return

        winner_id = str(winner.id)
        if winner_id not in (item["player_a"], item["player_b"]):
            await ctx.send("The winner must be one of the two players in this match.")
            return

        if item["status"] in ("FINISHED", "BYE"):
            await ctx.send("This match is already finished.")
            return

        item["winner"] = winner_id
        item["status"] = "FINISHED"
        save_data(self.data)

        await ctx.send(
            f"✅ {winner.mention} qualified from match `{item['id']}`."
        )

        if all(
            match["status"] in ("FINISHED", "BYE")
            for match in current_round["matches"]
        ):
            winners = [
                match["winner"]
                for match in current_round["matches"]
                if match.get("winner")
            ]
            if len(winners) == 1:
                await self.publish_results(tournament, winners[0])

    async def cog_load(self):
        self.bot.add_view(CreateClassicView())

        for tournament in self.data["tournaments"].values():
            if tournament.get("finished") or tournament.get("cancelled"):
                continue
            self.bot.add_view(
                TournamentView(self, tournament["id"])
            )


async def setup(bot):
    await bot.add_cog(TournamentCog(bot))
