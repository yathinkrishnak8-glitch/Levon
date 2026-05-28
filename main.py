import os
import asyncio
import logging
from datetime import datetime, timedelta
import discord
from discord.ext import commands, tasks

# Import our database logic (File 2)
from database import NovelDatabase

# Configure Logging for production/GitHub transparency
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("NovelBot")

class NovelBot(commands.Bot):
    def __init__(self):
        # Configure required gateway intents
        intents = discord.Intents.default()
        intents.message_content = True  # Required to read uploaded .txt/.epub files
        intents.guilds = True
        intents.members = True

        super().__init__(
            command_prefix="!", 
            intents=intents, 
            activity=discord.Game(name="Reading Novels | /read"),
            status=discord.Status.online
        )
        
        # Initialize our database helper class
        self.db = NovelDatabase("novel_library.db")

    async def setup_hook(self):
        """Executed before the bot logs into Discord. Used to load components and views."""
        logger.info("Initializing database schemas...")
        self.db.initialize_tables()

        # Load our core feature Cog (File 3)
        logger.info("Loading extensions...")
        await self.load_extension("cogs.reading")

        # Crucial: Register persistent views here so buttons continue working after a bot restart
        # We import inside the hook to prevent circular dependency issues
        from cogs.reading import ReadingView
        self.add_view(ReadingView(self, self.db, user_id=None, dynamic_persistent=True))
        
        # Start our automated server background maintenance tasks
        self.auto_cleanup_inactive_sessions.start()
        
        logger.info("Syncing global slash commands...")
        await self.tree.sync()

    async def on_ready(self):
        logger.info("--------------------------------------------------")
        logger.info(f"Logged in successfully as: {self.user.name} ({self.user.id})")
        logger.info(f"Connected to {len(self.guilds)} servers.")
        logger.info("--------------------------------------------------")

    async def on_guild_join(self, guild: discord.Guild):
        """Triggered when the bot is invited to a new server. Sends an interactive onboarding panel."""
        logger.info(f"Joined a new server: {guild.name} ({guild.id})")
        
        # Find the best text channel to drop a welcome notification
        target_channel = next(
            (ch for ch in guild.text_channels if ch.permissions_for(guild.me).send_messages), 
            None
        )
        
        if not target_channel:
            return

        embed = discord.Embed(
            title=f"📚 Welcome to your Ultimate Reading Assistant!",
            description=(
                f"Thank you for adding me to **{guild.name}**!\n\n"
                "I can convert text/epubs into distraction-free reading environments where "
                "users can seamlessly read with button-based scroll systems.\n\n"
                "### 🛠️ Next Steps:\n"
                "An administrator needs to run the setup command to build out the "
                "dedicated **Reading Corner category and system channels**.\n\n"
                "Use the slash command below to complete deployment:"
            ),
            color=discord.Color.brand_green()
        )
        embed.add_field(name="Setup Command", value="`/setup [optional_custom_bot_name]`", inline=False)
        embed.set_footer(text="Powered by NovelBot Engine")

        try:
            await target_channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to send welcome message in guild {guild.id}: {e}")

    @tasks.loop(hours=1.0)
    async def auto_cleanup_inactive_sessions(self):
        """
        Background safety task. Checks active reading channels hourly.
        If a channel has been inactive for more than 48 hours, it automatically 
        archives (deletes) it to keep the server below Discord's 500-channel limit.
        """
        logger.info("Running automated inactivity check on reading channels...")
        active_sessions = self.db.get_all_active_sessions()

        for session in active_sessions:
            user_id, channel_id, last_interacted_str = session
            if not channel_id:
                continue

            try:
                last_interacted = datetime.fromisoformat(last_interacted_str)
            except ValueError:
                continue

            # Check if the channel has been sitting idle for over 48 hours
            if datetime.utcnow() - last_interacted > timedelta(hours=48):
                channel = self.get_channel(channel_id)
                if channel:
                    try:
                        # 1. Notify the user if accessible
                        user = self.get_user(user_id)
                        if user:
                            await user.send(
                                f"💤 Your reading session for channel `#{channel.name}` was closed due to "
                                "48 hours of inactivity. Your progress has been safely saved! "
                                "Type `/read` anywhere in the server to pick up right where you left off."
                            )
                        
                        # 2. Delete the channel safely
                        await channel.delete(reason="Automated session cleanup due to user inactivity.")
                        logger.info(f"Archived inactive channel {channel_id} for User {user_id}")
                    except Exception as e:
                        logger.error(f"Failed to cleanly delete inactive channel {channel_id}: {e}")
                
                # Update database: remove the active channel reference but preserve chapter progress
                self.db.clear_active_channel(user_id)

    @auto_cleanup_inactive_sessions.before_loop
    async def before_cleanup_loop(self):
        # Wait until the bot connection is completely ready before executing loop cycles
        await self.wait_until_ready()


if __name__ == "__main__":
    # Ensure a proper discord token variable is set inside your local environments
    TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    if not TOKEN:
        logger.critical("CRITICAL ERROR: 'DISCORD_BOT_TOKEN' environment variable not found.")
        print("Please set your bot token using: export DISCORD_BOT_TOKEN='your_token'")
    else:
        bot = NovelBot()
        bot.run(TOKEN)
