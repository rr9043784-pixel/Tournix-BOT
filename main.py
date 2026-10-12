
import os
import discord

from discord.ext import commands
from flask import Flask
from threading import Thread

# =========================
# SETTINGS
# =========================

TOKEN = os.environ["DISCORD_TOKEN"]
OWNER_ID = 1176149190192152626

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    owner_id=OWNER_ID
)

# =========================
# FLASK — RAILWAY
# =========================

app = Flask(__name__)


@app.route("/")
def home():
    return "Tournament Bot is online!"


def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(
        host="0.0.0.0",
        port=port,
        use_reloader=False
    )


# =========================
# LOAD EXTENSIONS
# =========================

@bot.event
async def setup_hook():
    for extension in ("chest_api", "tournament"):
        await bot.load_extension(extension)
        print(f"Loaded extension: {extension}")

    await bot.tree.sync()
    print("Slash commands synced.")


# =========================
# BOT EVENTS
# =========================

@bot.event
async def on_ready():
    print(f"Bot online: {bot.user}")
    print(f"Bot ID: {bot.user.id}")


# =========================
# OWNER COMMAND
# =========================

@bot.command(name="ping")
@commands.is_owner()
async def ping(ctx):
    await ctx.send(
        f"🏓 Pong! `{round(bot.latency * 1000)} ms`"
    )


# =========================
# SLASH COMMAND
# =========================

@bot.tree.command(
    name="ping",
    description="Check the bot's latency"
)
async def slash_ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🏓 Pong! `{round(bot.latency * 1000)} ms`",
        ephemeral=True
    )


# =========================
# START BOT
# =========================

if __name__ == "__main__":
    Thread(
        target=run_flask,
        daemon=True
    ).start()

    bot.run(TOKEN)
