import aiosqlite
import re
from datetime import datetime

class NovelDatabase:
    def __init__(self, db_name="novel_library.db"):
        self.db_name = db_name

    async def _get_connection(self):
        """Creates and returns an asynchronous connection to the SQLite database."""
        return await aiosqlite.connect(self.db_name)

    async def initialize_tables(self):
        """Creates the database schema if it doesn't already exist. Must be awaited."""
        async with await self._get_connection() as conn:
            # 1. Server Configuration Table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    category_id INTEGER,
                    search_channel_id INTEGER,
                    lounge_channel_id INTEGER,
                    cleanup_disabled INTEGER DEFAULT 0
                )
            """)

            # 2. Master Novel Index Table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS novels (
                    novel_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    author TEXT DEFAULT 'Unknown',
                    total_chunks INTEGER DEFAULT 0
                )
            """)

            # 3. Parsed Content Storage Table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS novel_chunks (
                    novel_id TEXT,
                    chunk_index INTEGER,
                    content TEXT NOT NULL,
                    PRIMARY KEY (novel_id, chunk_index),
                    FOREIGN KEY (novel_id) REFERENCES novels(novel_id) ON DELETE CASCADE
                )
            """)

            # 4. User Inventory & Progress Tracker
            # Replaced single-session table with an inventory system allowing multiple saved books.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_inventory (
                    user_id INTEGER,
                    novel_id TEXT,
                    current_chunk INTEGER DEFAULT 0,
                    channel_id INTEGER,
                    last_interacted TEXT,
                    PRIMARY KEY (user_id, novel_id),
                    FOREIGN KEY (novel_id) REFERENCES novels(novel_id) ON DELETE CASCADE
                )
            """)
            await conn.commit()

    # --- GUILD SETTINGS METHODS ---
    async def save_guild_settings(self, guild_id, category_id, search_channel_id, lounge_channel_id):
        async with await self._get_connection() as conn:
            await conn.execute("""
                INSERT INTO guild_settings (guild_id, category_id, search_channel_id, lounge_channel_id, cleanup_disabled)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(guild_id) DO UPDATE SET
                    category_id=excluded.category_id,
                    search_channel_id=excluded.search_channel_id,
                    lounge_channel_id=excluded.lounge_channel_id
            """, (guild_id, category_id, search_channel_id, lounge_channel_id))
            await conn.commit()

    async def get_guild_settings(self, guild_id):
        async with await self._get_connection() as conn:
            async with conn.execute("SELECT category_id, search_channel_id, lounge_channel_id, cleanup_disabled FROM guild_settings WHERE guild_id = ?", (guild_id,)) as cursor:
                return await cursor.fetchone()

    # --- NOVEL INGESTION & LIBRARY MANAGEMENT ---
    async def import_text_novel(self, novel_id, title, raw_text, author="Unknown", max_chunk_size=1500):
        """Splits text synchronously (it's fast enough in memory) but writes to DB asynchronously."""
        chunks = []
        current_chunk = ""
        paragraphs = raw_text.split("\n")
        
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
                
            if len(current_chunk) + len(para) + 1 <= max_chunk_size:
                current_chunk += para + "\n\n"
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                if len(para) > max_chunk_size:
                    words = para.split(" ")
                    sub_chunk = ""
                    for word in words:
                        if len(sub_chunk) + len(word) + 1 <= max_chunk_size:
                            sub_chunk += word + " "
                        else:
                            chunks.append(sub_chunk.strip())
                            sub_chunk = word + " "
                    current_chunk = sub_chunk + "\n\n"
                else:
                    current_chunk = para + "\n\n"
                    
        if current_chunk:
            chunks.append(current_chunk.strip())

        async with await self._get_connection() as conn:
            await conn.execute("""
                INSERT INTO novels (novel_id, title, author, total_chunks)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(novel_id) DO UPDATE SET total_chunks=excluded.total_chunks
            """, (novel_id, title, author, len(chunks)))

            # Clear old chunks if overwriting
            await conn.execute("DELETE FROM novel_chunks WHERE novel_id = ?", (novel_id,))
            
            # Fast bulk insert using executemany
            chunk_data = [(novel_id, idx, text) for idx, text in enumerate(chunks)]
            await conn.executemany("""
                INSERT INTO novel_chunks (novel_id, chunk_index, content)
                VALUES (?, ?, ?)
            """, chunk_data)
            await conn.commit()
            
        return len(chunks)

    async def delete_novel_entirely(self, novel_id):
        """Admin function to purge a junk upload from the database completely."""
        async with await self._get_connection() as conn:
            # Foreign keys ON DELETE CASCADE will automatically wipe associated chunks and inventory items
            await conn.execute("DELETE FROM novels WHERE novel_id = ?", (novel_id,))
            await conn.commit()

    async def search_novels(self, query):
        async with await self._get_connection() as conn:
            async with conn.execute("SELECT novel_id, title, author FROM novels WHERE title LIKE ? OR author LIKE ? LIMIT 10", (f"%{query}%", f"%{query}%")) as cursor:
                return await cursor.fetchall()

    async def get_novel_details(self, novel_id):
        async with await self._get_connection() as conn:
            async with conn.execute("SELECT title, author, total_chunks FROM novels WHERE novel_id = ?", (novel_id,)) as cursor:
                return await cursor.fetchone()

    async def fetch_chunk(self, novel_id, chunk_index):
        async with await self._get_connection() as conn:
            async with conn.execute("SELECT content FROM novel_chunks WHERE novel_id = ? AND chunk_index = ?", (novel_id, chunk_index)) as cursor:
                res = await cursor.fetchone()
                return res[0] if res else None

    # --- INVENTORY & SESSION MANAGEMENT ---
    async def get_user_inventory(self, user_id):
        """Fetches all books the user has saved, joining with the novels table for titles."""
        async with await self._get_connection() as conn:
            query = """
                SELECT i.novel_id, n.title, n.author, i.current_chunk, n.total_chunks 
                FROM user_inventory i
                JOIN novels n ON i.novel_id = n.novel_id
                WHERE i.user_id = ?
                ORDER BY i.last_interacted DESC
            """
            async with conn.execute(query, (user_id,)) as cursor:
                return await cursor.fetchall()

    async def remove_from_inventory(self, user_id, novel_id):
        """Deletes a specific book from a user's library."""
        async with await self._get_connection() as conn:
            await conn.execute("DELETE FROM user_inventory WHERE user_id = ? AND novel_id = ?", (user_id, novel_id))
            await conn.commit()

    async def register_or_get_session(self, user_id, channel_id, novel_id):
        """Adds a book to inventory if new, or updates the active channel if it exists."""
        now_str = datetime.utcnow().isoformat()
        async with await self._get_connection() as conn:
            # Check if this specific book is already in their inventory
            async with conn.execute("SELECT current_chunk FROM user_inventory WHERE user_id = ? AND novel_id = ?", (user_id, novel_id)) as cursor:
                existing = await cursor.fetchone()

            if existing:
                await conn.execute("""
                    UPDATE user_inventory 
                    SET channel_id = ?, last_interacted = ? 
                    WHERE user_id = ? AND novel_id = ?
                """, (channel_id, now_str, user_id, novel_id))
                await conn.commit()
                return existing[0]
            else:
                await conn.execute("""
                    INSERT INTO user_inventory (user_id, novel_id, current_chunk, channel_id, last_interacted)
                    VALUES (?, ?, 0, ?, ?)
                """, (user_id, novel_id, channel_id, now_str))
                await conn.commit()
                return 0

    async def update_session_progress(self, user_id, novel_id, chunk_index):
        now_str = datetime.utcnow().isoformat()
        async with await self._get_connection() as conn:
            await conn.execute("""
                UPDATE user_inventory 
                SET current_chunk = ?, last_interacted = ? 
                WHERE user_id = ? AND novel_id = ?
            """, (chunk_index, now_str, user_id, novel_id))
            await conn.commit()

    async def get_active_session_by_user(self, user_id):
        """Finds whichever book currently has an active Discord channel open."""
        async with await self._get_connection() as conn:
            async with conn.execute("SELECT channel_id, novel_id, current_chunk FROM user_inventory WHERE user_id = ? AND channel_id IS NOT NULL", (user_id,)) as cursor:
                return await cursor.fetchone()

    async def get_all_active_sessions(self):
        """Used by the 48-hour auto-cleanup loop in main.py"""
        async with await self._get_connection() as conn:
            async with conn.execute("SELECT user_id, channel_id, last_interacted FROM user_inventory WHERE channel_id IS NOT NULL") as cursor:
                return await cursor.fetchall()

    async def clear_active_channel(self, user_id):
        """Wipes the active channel binding for the user to close the reading room safely."""
        async with await self._get_connection() as conn:
            await conn.execute("UPDATE user_inventory SET channel_id = NULL WHERE user_id = ?", (user_id,))
            await conn.commit()
