import discord
from discord.ext import commands
from discord import app_commands
import re

# ==========================================
# UI COMPONENTS: READING ROOM
# ==========================================
class ReadingView(discord.ui.View):
    def __init__(self, bot, db, user_id=None, dynamic_persistent=False):
        super().__init__(timeout=None)
        self.bot = bot
        self.db = db
        self.user_id = user_id
        self.dynamic_persistent = dynamic_persistent

    async def _resolve_user_session(self, interaction: discord.Interaction):
        if not self.dynamic_persistent:
            return self.user_id
            
        # Fallback for persistent buttons after a bot restart
        session = await self.db.get_active_session_by_user(interaction.user.id)
        if session and session[0] == interaction.channel_id:
            return interaction.user.id
        return None

    @discord.ui.button(label="⏬ Load Next Content", style=discord.ButtonStyle.green, custom_id="novel_btn_next")
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        
        target_user_id = await self._resolve_user_session(interaction)
        if not target_user_id or interaction.user.id != target_user_id:
            await interaction.followup.send("⚠️ This is a private reading room. You cannot control this book.", ephemeral=True)
            return

        session = await self.db.get_active_session_by_user(target_user_id)
        if not session:
            await interaction.followup.send("❌ No active reading session found.", ephemeral=True)
            return

        channel_id, novel_id, current_chunk = session
        next_chunk_index = current_chunk + 1

        next_text = await self.db.fetch_chunk(novel_id, next_chunk_index)
        if next_text:
            # Note: Now requires novel_id to update the correct book in their inventory
            await self.db.update_session_progress(target_user_id, novel_id, next_chunk_index)
            
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass

            fresh_view = ReadingView(self.bot, self.db, user_id=target_user_id)
            await interaction.channel.send(content=next_text, view=fresh_view)
        else:
            await interaction.channel.send("🎉 **Wonderful! You have completely finished reading this volume!**")

    @discord.ui.button(label="🔖 Save & Close Room", style=discord.ButtonStyle.danger, custom_id="novel_btn_close")
    async def close_room(self, interaction: discord.Interaction, button: discord.ui.Button):
        target_user_id = await self._resolve_user_session(interaction)
        if not target_user_id or interaction.user.id != target_user_id:
            await interaction.response.send_message("⚠️ You do not have permissions to shut down this session.", ephemeral=True)
            return

        await interaction.response.send_message("🔒 Saving metrics and archiving channel...", ephemeral=True)
        await self.db.clear_active_channel(target_user_id)
        await interaction.channel.delete(reason="User executed close interface button manually.")


# ==========================================
# UI COMPONENTS: INVENTORY MANAGEMENT
# ==========================================
class InventorySelect(discord.ui.Select):
    def __init__(self, inventory_data):
        options = []
        for item in inventory_data[:25]: # Discord limits selects to 25 options max
            novel_id, title, author, current_chunk, total_chunks = item
            progress = round((current_chunk / total_chunks) * 100) if total_chunks > 0 else 0
            
            options.append(discord.SelectOption(
                label=title[:100], 
                description=f"Progress: {progress}% (Chunk {current_chunk}/{total_chunks})",
                value=novel_id,
                emoji="📖"
            ))
            
        super().__init__(placeholder="Select a saved novel to manage...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_novel_id = self.values[0]
        # Update the view buttons to target the selected novel
        self.view.selected_novel_id = selected_novel_id
        
        # Enable the buttons
        for child in self.view.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = False
                
        await interaction.response.edit_message(view=self.view)


class InventoryView(discord.ui.View):
    def __init__(self, bot, db, user_id, inventory_data):
        super().__init__(timeout=180)
        self.bot = bot
        self.db = db
        self.user_id = user_id
        self.selected_novel_id = None
        
        # Add the dropdown
        self.add_item(InventorySelect(inventory_data))

    @discord.ui.button(label="🚀 Continue Reading", style=discord.ButtonStyle.green, custom_id="inv_continue", disabled=True)
    async def continue_reading(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This is not your inventory!", ephemeral=True)
            
        await interaction.response.send_message("Initiating your reading room...", ephemeral=True)
        # We delegate the actual room creation to the bot's command logic internally
        cog = self.bot.get_cog("ReadingCog")
        await cog.spawn_reading_room(interaction, self.selected_novel_id)
        await interaction.message.delete() # Cleanup inventory message

    @discord.ui.button(label="🗑️ Drop Book", style=discord.ButtonStyle.danger, custom_id="inv_delete", disabled=True)
    async def drop_book(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This is not your inventory!", ephemeral=True)
            
        await self.db.remove_from_inventory(self.user_id, self.selected_novel_id)
        await interaction.response.send_message("🗑️ Book removed from your personal library.", ephemeral=True)
        await interaction.message.delete()


# ==========================================
# MAIN COMMAND COG
# ==========================================
class ReadingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db

    # --- ADMIN GROUP COMMANDS ---
    admin_group = app_commands.Group(name="noveladmin", description="Library management dashboard utilities.", default_permissions=discord.Permissions(administrator=True))

    @admin_group.command(name="delete", description="Permanently delete a junk upload from the master database.")
    async def admin_delete(self, interaction: discord.Interaction, novel_id: str):
        await interaction.response.defer(ephemeral=True)
        details = await self.db.get_novel_details(novel_id)
        
        if not details:
            await interaction.followup.send(f"❌ No novel found with ID: `{novel_id}`.")
            return
            
        await self.db.delete_novel_entirely(novel_id)
        await interaction.followup.send(f"🗑️ Successfully purged **{details[0]}** (`{novel_id}`) from the master database.")

    @admin_group.command(name="togglecleanup", description="Turns the 48-hour automated inactivity channel cleaner ON or OFF.")
    async def toggle_cleanup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild.id
        
        settings = await self.db.get_guild_settings(guild_id)
        if not settings:
            await interaction.followup.send("❌ Please execute the `/setup` configuration utility first.")
            return
            
        current_state = settings[3] # cleanup_disabled is index 3
        new_state = 1 if current_state == 0 else 0
        
        # Save inverted state back to DB
        await self.db.save_guild_settings(guild_id, settings[0], settings[1], settings[2])
        # (save_guild_settings resets it to 0, so we do a targeted raw update for this toggle)
        async with await self.db._get_connection() as conn:
            await conn.execute("UPDATE guild_settings SET cleanup_disabled = ? WHERE guild_id = ?", (new_state, guild_id))
            await conn.commit()

        status_text = "❌ **DISABLED** (Rooms remain open indefinitely)." if new_state == 1 else "✅ **ENABLED** (Idle channels purged after 48 hours)."
        
        embed = discord.Embed(
            title="⚙️ System Automation Toggled",
            description=f"The background automatic inactivity sweeping loop has been changed:\n\n{status_text}",
            color=discord.Color.orange()
        )
        await interaction.followup.send(embed=embed)

    # --- STANDARD COMMANDS ---
    @app_commands.command(name="setup", description="Deploys the dedicated reading category hub structure.")
    @app_commands.checks.has_permissions(administrator=True)
    async def server_setup(self, interaction: discord.Interaction, custom_name: str = None):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        if custom_name:
            try:
                await guild.me.edit(nick=custom_name)
            except Exception:
                pass

        category = await guild.create_category("📚 READING CORNER")
        search_ch = await guild.create_text_channel("┃novel-search", category=category)
        lounge_ch = await guild.create_text_channel("┃reading-lounge", category=category)

        await self.db.save_guild_settings(guild.id, category.id, search_ch.id, lounge_ch.id)

        welcome_embed = discord.Embed(
            title="🎯 Welcome to the Search Hub",
            description="Use `/search` to find entries, `/upload` to submit novel text assets, or `/read` to begin your session.",
            color=discord.Color.blue()
        )
        await search_ch.send(embed=welcome_embed)
        await interaction.followup.send("✅ Server deployment sequence completed successfully!")

    @app_commands.command(name="upload", description="Submit a raw .txt file directly into the server database library.")
    async def upload_novel(self, interaction: discord.Interaction, file: discord.Attachment, title: str, author: str):
        await interaction.response.defer(ephemeral=True)

        if not file.filename.endswith('.txt'):
            await interaction.followup.send("❌ Format validation failure! Please provide standard `.txt` files.")
            return

        file_bytes = await file.read()
        raw_text = file_bytes.decode('utf-8', errors='ignore')

        novel_id = re.sub(r'[^a-zA-Z0-9]', '_', title.lower().strip())
        total_chunks = await self.db.import_text_novel(novel_id, title, raw_text, author=author)

        embed = discord.Embed(
            title="💾 Book Successfully Ingested",
            description=f"**{title}** by *{author}* has been processed.",
            color=discord.Color.green()
        )
        embed.add_field(name="Registry Key", value=f"`{novel_id}`", inline=True)
        embed.add_field(name="Pages Created", value=f"`{total_chunks}` blocks", inline=True)
        
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="search", description="Lookup registered books across database index tables.")
    async def search_novels(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        results = await self.db.search_novels(query)

        if not results:
            await interaction.followup.send("🔍 No database matches found. Try an alternate phrase.")
            return

        embed = discord.Embed(title=f"🔎 Search Indexes for: '{query}'", color=discord.Color.blurple())
        for novel_id, title, author in results:
            embed.add_field(
                name=f"📚 {title}", 
                value=f"**Author:** {author}\n**Launch Key:** `/read novel_id: {novel_id}`", 
                inline=False
            )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="inventory", description="Open your personal saved library to continue reading.")
    async def user_inventory(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        inventory_data = await self.db.get_user_inventory(interaction.user.id)
        if not inventory_data:
            await interaction.followup.send("📭 Your inventory is empty! Start a book using `/read`.")
            return
            
        embed = discord.Embed(
            title="🎒 Your Reading Inventory",
            description="Select a book from the dropdown below to manage it or resume reading.",
            color=discord.Color.dark_purple()
        )
        
        view = InventoryView(self.bot, self.db, interaction.user.id, inventory_data)
        await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="read", description="Launch an isolated user-focused text frame workspace.")
    async def read_novel(self, interaction: discord.Interaction, novel_id: str):
        await interaction.response.defer(ephemeral=True)
        await self.spawn_reading_room(interaction, novel_id)

    async def spawn_reading_room(self, interaction: discord.Interaction, novel_id: str):
        """Helper function to build the channel. Used by both /read and the Inventory Continue button."""
        guild = interaction.guild
        novel_data = await self.db.get_novel_details(novel_id)
        
        if not novel_data:
            await interaction.followup.send("❌ Specified catalog key not found. Query using `/search` first.", ephemeral=True)
            return

        title, author, total_chunks = novel_data

        # Enforce one active channel per user at a time
        existing = await self.db.get_active_session_by_user(interaction.user.id)
        if existing and existing[0]:
            await interaction.followup.send(f"⚠️ You already have an active reading room open! Tap here: <#{existing[0]}>", ephemeral=True)
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True)
        }

        settings = await self.db.get_guild_settings(guild.id)
        category_id = settings[0] if settings else None
        category = discord.utils.get(guild.categories, id=category_id)

        room_name = f"📖-{interaction.user.name}-{novel_id[:10]}"
        reading_channel = await guild.create_text_channel(name=room_name, category=category, overwrites=overwrites)

        current_chunk = await self.db.register_or_get_session(interaction.user.id, reading_channel.id, novel_id)

        text_block = await self.db.fetch_chunk(novel_id, current_chunk)
        if not text_block:
            text_block = "📂 *No text chunks discovered inside storage frameworks.*"

        interactive_view = ReadingView(self.bot, self.db, user_id=interaction.user.id)
        
        header_embed = discord.Embed(
            title=f"📖 Reading: {title}",
            description=f"By **{author}** • Block `{current_chunk + 1}/{total_chunks}`\n*Move naturally lower down the interface as content drops via click controls.*",
            color=discord.Color.dark_teal()
        )
        
        await reading_channel.send(embed=header_embed)
        await reading_channel.send(content=text_block, view=interactive_view)
        await interaction.followup.send(f"🚀 Canvas environment operational. Head over to: {reading_channel.mention}", ephemeral=True)


async def setup(bot):
    await bot.add_cog(ReadingCog(bot))
