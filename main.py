import os
import asyncio
import logging
import traceback
from datetime import datetime, timedelta
import discord
from discord.ext import commands, tasks
from discord import app_commands

# We assume database.py is upgraded to handle aiosqlite (async operations)
from database import NovelDatabase

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("NovelBot")

class NovelBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  
        intents.guilds = True
        intents.members = True

        super().__init__(
            command_prefix="!", 
            intents=intents, 
            activity=discord.Game(name="Reading Novels | /read"),
            status=discord.Status.online
        )
        
        # The database object will now use asynchronous methods
        self.db = NovelDatabase("novel_library.db")

    async def setup_hook(self):
        """Executed before the bot logs in. Loads components, views, and syncs commands."""
        logger.info("Initializing async database schemas...")
        await self.db.initialize_tables()

        logger.info("Loading extensions...")
        await self.load_extension("cogs.reading")

        # Register persistent views so buttons work after restarts
        from cogs.reading import ReadingView, InventoryView
        self.add_view(ReadingView(self, self.db, user_id=None, dynamic_persistent=True))
        self.add_view(InventoryView(self, self.db, user_id=None)) 
        
        self.auto_cleanup_inactive_sessions.start()
        
        # Attach the global error handler to the application command tree
        self.tree.on_error = self.on_app_command_error
        
        logger.info("Syncing global slash commands...")
        await self.tree.sync()

    async def on_ready(self):
        logger.info("--------------------------------------------------")
        logger.info(f"Logged in successfully as: {self.user.name} ({self.user.id})")
        logger.info(f"Connected to {len(self.guilds)} servers.")
        logger.info("--------------------------------------------------")

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """Global error handler to catch bugs and missing permissions smoothly."""
        if isinstance(error, app_commands.MissingPermissions):
            msg = "❌ You don't have the required administrative permissions to run this command."
        elif isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ Slow down! Try again in {round(error.retry_after, 2)} seconds."
        else:
            msg = f"⚠️ An unexpected error occurred: `{str(error)}`"
            logger.error(f"Command Exception in {interaction.command.name}: {error}")
            traceback.print_exception(type(error), error, error.__traceback__)

        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    async def on_guild_join(self, guild: discord.Guild):
        """Onboarding panel for new servers."""
        target_channel = next(
            (ch for ch in guild.text_channels if ch.permissions_for(guild.me).send_messages), 
            None
        )
        if not target_channel:
            return

        embed = discord.Embed(
            title="📚 Welcome to your Ultimate Reading Assistant!",
            description=(
                f"Thank you for adding me to **{guild.name}**!\n\n"
                "I create clean, dedicated reading environments using a responsive UI.\n\n"
                "### 🛠️ Next Steps:\n"
                "An administrator needs to run `/setup` to build out the "
                "dedicated Reading Corner category and system channels."
            ),
            color=discord.Color.brand_green()
        )
        embed.set_footer(text="Powered by NovelBot Engine")

        try:
            await target_channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to send welcome message in guild {guild.id}: {e}")

    @tasks.loop(hours=1.0)
    async def auto_cleanup_inactive_sessions(self):
        """Checks for active reading channels idle for 48+ hours and safely deletes them."""
        logger.info("Running automated inactivity check on reading channels...")
        active_sessions = await self.db.get_all_active_sessions()

        for session in active_sessions:
            user_id, channel_id, last_interacted_str = session
            if not channel_id:
                continue

            try:
                last_interacted = datetime.fromisoformat(last_interacted_str)
            except ValueError:
                continue

            if datetime.utcnow() - last_interacted > timedelta(hours=48):
                channel = self.get_channel(channel_id)
                if channel:
                    # Check the toggle setting for this specific server
                    settings = await self.db.get_guild_settings(channel.guild.id)
                    # settings structure: (category_id, search_id, lounge_id, cleanup_disabled)
                    if settings and len(settings) > 3 and settings[3] == 1:
                        continue  # Skip if the admin disabled auto-cleanup

                    try:
                        user = self.get_user(user_id)
                        if user:
                            await user.send(
                                f"💤 Your reading session for `#{channel.name}` was archived due to "
                                "48 hours of inactivity. Your progress is saved! Use `/inventory` to resume."
                            )
                        
                        await channel.delete(reason="Automated inactivity cleanup.")
                        logger.info(f"Archived inactive channel {channel_id} for User {user_id}")
                    except Exception as e:
                        logger.error(f"Failed to delete channel {channel_id}: {e}")
                
                # Strip the active channel link but keep the chapter progress
                await self.db.clear_active_channel(user_id)

    @auto_cleanup_inactive_sessions.before_loop
    async def before_cleanup_loop(self):
        await self.wait_until_ready()

if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    if not TOKEN:
        logger.critical("CRITICAL ERROR: 'DISCORD_BOT_TOKEN' environment variable not found.")
    else:
        bot = NovelBot()
        bot.run(TOKEN)
