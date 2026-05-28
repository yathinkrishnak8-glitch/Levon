import discord
from discord.ext import commands
from discord.commands import Option, SlashCommandGroup
import io

class ReadingView(discord.ui.View):
    def __init__(self, bot, db, user_id=None, dynamic_persistent=False):
        # Timeout=None makes the buttons completely persistent across bot restarts
        super().__init__(timeout=None)
        self.bot = bot
        self.db = db
        self.user_id = user_id
        self.dynamic_persistent = dynamic_persistent

    async def _resolve_user_session(self, interaction: discord.Interaction):
        """Helper to identify the correct reader when buttons are in persistent fallback mode."""
        if not self.dynamic_persistent:
            return self.user_id
            
        # Fallback: Query the database to find out who owns this active channel
        with self.db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM reading_sessions WHERE channel_id = ?", (interaction.channel_id,))
            res = cursor.fetchone()
            return res[0] if res else None

    @discord.ui.button(label="⏬ Load Next Content", style=discord.ButtonStyle.green, custom_id="novel_btn_next")
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        
        target_user_id = await self._resolve_user_session(interaction)
        if not target_user_id or (interaction.user.id != target_user_id and not interaction.user.guild_permissions.administrator):
            await interaction.followup.send("⚠️ This is a private reading room. You cannot control this book.", ephemeral=True)
            return

        session = self.db.get_active_session_by_user(target_user_id)
        if not session:
            await interaction.followup.send("❌ No active reading session found in database.", ephemeral=True)
            return

        channel_id, novel_id, current_chunk = session
        next_chunk_index = current_chunk + 1

        next_text = self.db.fetch_chunk(novel_id, next_chunk_index)
        if next_text:
            self.db.update_session_progress(target_user_id, next_chunk_index)
            
            # Remove the old navigation buttons from the previous block to prevent layout spam
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass

            # Drop the fresh paragraph chunk below it with a clean new view instance
            fresh_view = ReadingView(self.bot, self.db, user_id=target_user_id)
            await interaction.channel.send(content=next_text, view=fresh_view)
        else:
            await interaction.channel.send("🎉 **Wonderful! You have completely finished reading this novel asset volume!**")

    @discord.ui.button(label="🔖 Save & Close Room", style=discord.ButtonStyle.danger, custom_id="novel_btn_close")
    async def close_room(self, interaction: discord.Interaction, button: discord.ui.Button):
        target_user_id = await self._resolve_user_session(interaction)
        if not target_user_id or (interaction.user.id != target_user_id and not interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("⚠️ You do not have permissions to shut down this session.", ephemeral=True)
            return

        await interaction.response.send_message("🔒 Saving metrics and archiving channel canvas...", ephemeral=True)
        
        # Free up server channel slots by stripping bindings
        self.db.clear_active_channel(target_user_id)
        await interaction.channel.delete(reason="User executed close interface button manually.")


class ReadingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db

    # Setup core slash subcommands
    novel_admin = discord.SlashCommandGroup("noveladmin", "Library management dashboard utilities.")

    @discord.slash_command(name="setup", description="Deploys the dedicated reading category hub structure.")
    @commands.has_permissions(administrator=True)
    async def server_setup(self, ctx, custom_name: Option(str, "Change the bot's display nickname locally", required=False)):
        await ctx.defer(ephemeral=True)
        guild = ctx.guild

        # Dynamically shift bot name on server if custom parameter is provided
        if custom_name:
            try:
                await guild.me.edit(nick=custom_name)
            except Exception:
                pass

        # Create localized clean structural spaces
        category = await guild.create_category("📚 READING CORNER")
        search_ch = await guild.create_text_channel("┃novel-search", category=category)
        lounge_ch = await guild.create_text_channel("┃reading-lounge", category=category)

        # Commit structural data IDs right into server configurations
        self.db.save_guild_settings(guild.id, category.id, search_ch.id, lounge_ch.id)

        # Alter visual layout for searching room instantly
        welcome_embed = discord.Embed(
            title="🎯 Welcome to the Search Hub",
            description="Use `/search` to find entries, `/upload` to submit novel text assets, or `/read` to begin your session.",
            color=discord.Color.blue()
        )
        await search_ch.send(embed=welcome_embed)

        await ctx.respond("✅ Server deployment sequence completed successfully!", ephemeral=True)

    @novel_admin.command(name="togglecleanup", description="Turns the 48-hour automated inactivity channel cleaner ON or OFF.")
    @commands.has_permissions(administrator=True)
    async def toggle_cleanup(self, ctx):
        """Single command toggle utilizing basic SQLite flag updates."""
        await ctx.defer(ephemeral=True)
        guild_id = ctx.guild.id
        
        # Read or construct baseline configuration parameters
        settings = self.db.get_guild_settings(guild_id)
        if not settings:
            await ctx.respond("❌ Please execute the `/setup` configuration utility first.", ephemeral=True)
            return
            
        with self.db._get_connection() as conn:
            cursor = conn.cursor()
            # Ensure safety check mapping configuration parameters dynamically
            cursor.execute("PRAGMA table_info(guild_settings)")
            columns = [col[1] for col in cursor.fetchall()]
            
            # Dynamically attach toggle parameters on the fly if needed
            if "cleanup_disabled" not in columns:
                cursor.execute("ALTER TABLE guild_settings ADD COLUMN cleanup_disabled INTEGER DEFAULT 0")
                conn.commit()

            cursor.execute("SELECT cleanup_disabled FROM guild_settings WHERE guild_id = ?", (guild_id,))
            current_state = cursor.fetchone()[0]
            
            # Binary toggle inversion logic
            new_state = 1 if current_state == 0 else 0
            cursor.execute("UPDATE guild_settings SET cleanup_disabled = ? WHERE guild_id = ?", (new_state, guild_id))
            conn.commit()

        status_text = "❌ **DISABLED** (Reading rooms will remain open indefinitely until manual closing)." if new_state == 1 else "✅ **ENABLED** (Idle channels will be swept away after 48 hours of silence)."
        
        embed = discord.Embed(
            title="⚙️ System Automation Toggled",
            description=f"The background automatic inactivity sweeping loop has been changed:\n\n{status_text}",
            color=discord.Color.orange()
        )
        await ctx.respond(embed=embed, ephemeral=True)

    @discord.slash_command(name="upload", description="Submit a raw .txt or manuscript file directly into the server database library.")
    async def upload_novel(self, ctx, file: discord.Attachment, title: str, author: str):
        await ctx.defer(ephemeral=True)

        if not file.filename.endswith('.txt'):
            await ctx.respond("❌ Format validation failure! Please provide standard raw plain text files (`.txt`).", ephemeral=True)
            return

        # Read byte lines completely from attachment stream safely
        file_bytes = await file.read()
        raw_text = file_bytes.decode('utf-8', errors='ignore')

        # Clean ID construction string normalization mapping
        novel_id = re.sub(r'[^a-zA-Z0-9]', '_', title.lower().strip())

        # Feed right into internal parse calculation operations
        total_chunks = self.db.import_text_novel(novel_id, title, raw_text, author=author)

        embed = discord.Embed(
            title="💾 Book Successfully Ingested",
            description=f"**{title}** by *{author}* has been processed into layout frames.",
            color=discord.Color.green()
        )
        embed.add_field(name="Unique Registry Reference Key", value=f"`{novel_id}`", inline=True)
        embed.add_field(name="Total Page Segments Created", value=f"`{total_chunks}` blocks", inline=True)
        
        await ctx.respond(embed=embed, ephemeral=True)

    @discord.slash_command(name="search", description="Lookup registered books across database index tables.")
    async def search_novels(self, ctx, query: str):
        await ctx.defer()
        results = self.db.search_novels(query)

        if not results:
            await ctx.respond("🔍 No database matches found. Try an alternate phrase pattern.")
            return

        embed = discord.Embed(title=f"🔎 Search Indexes for: '{query}'", color=discord.Color.blurple())
        for novel_id, title, author in results:
            embed.add_field(
                name=f"📚 {title}", 
                value=f"**Author:** {author}\n**Launch Key:** `/read novel_id:{novel_id}`", 
                inline=False
            )
        await ctx.respond(embed=embed)

    @discord.slash_command(name="read", description="Launch an isolated user-focused text frame workspace.")
    async def read_novel(self, ctx, novel_id: str):
        await ctx.defer(ephemeral=True)
        guild = ctx.guild

        novel_data = self.db.get_novel_details(novel_id)
        if not novel_data:
            await ctx.respond("❌ Specified catalog key not found. Query using `/search` first.", ephemeral=True)
            return

        title, author, total_chunks = novel_data

        # Check for pre-existing execution states
        existing = self.db.get_active_session_by_user(ctx.author.id)
        if existing and existing[0]:
            await ctx.respond(f"⚠️ Active space already running. Tap visibility link: <#{existing[0]}>", ephemeral=True)
            return

        # Setup targeted permission masks
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            ctx.author: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True)
        }

        # Resolve category mapping reference bounds
        settings = self.db.get_guild_settings(guild.id)
        category_id = settings[0] if settings else None
        category = discord.utils.get(guild.categories, id=category_id)

        # Build clean canvas room spaces
        room_name = f"📖-{ctx.author.name}-{novel_id}"
        reading_channel = await guild.create_text_channel(name=room_name, category=category, overwrites=overwrites)

        # Anchor tracking context metrics data indexes
        current_chunk = self.db.register_or_get_session(ctx.author.id, reading_channel.id, novel_id)

        # Fetch first narrative text segment block strings
        text_block = self.db.fetch_chunk(novel_id, current_chunk)
        if not text_block:
            text_block = "📂 *No text chunks discovered inside storage frameworks.*"

        # Display content structures alongside custom interactive navigation components
        interactive_view = ReadingView(self.bot, self.db, user_id=ctx.author.id)
        
        header_embed = discord.Embed(
            title=f"📖 Reading: {title}",
            description=f"By **{author}** • Segment Block Frame `{current_chunk + 1}/{total_chunks}`\n*Move naturally lower down the interface as content drops via click controls.*",
            color=discord.Color.dark_teal()
        )
        
        await reading_channel.send(embed=header_embed)
        await reading_channel.send(content=text_block, view=interactive_view)

        await ctx.respond(f"🚀 Canvas environment operational. Head over to: {reading_channel.mention}", ephemeral=True)


def setup(bot):
    bot.add_cog(ReadingCog(bot))
