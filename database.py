import sqlite3
import re
from datetime import datetime

class NovelDatabase:
    def __init__(self, db_name="novel_library.db"):
        self.db_name = db_name

    def _get_connection(self):
        """Creates and returns a raw connection to the SQLite database file."""
        return sqlite3.connect(self.db_name)

    def initialize_tables(self):
        """Creates the database schema if it doesn't already exist."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # 1. Server Configuration Table
            # Added cleanup_disabled to natively support the toggle feature
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    category_id INTEGER,
                    search_channel_id INTEGER,
                    lounge_channel_id INTEGER,
                    cleanup_disabled INTEGER DEFAULT 0
                )
            """)

            # 2. Master Novel Index Table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS novels (
                    novel_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    author TEXT DEFAULT 'Unknown',
                    total_chunks INTEGER DEFAULT 0
                )
            """)

            # 3. Parsed Content Storage Table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS novel_chunks (
                    novel_id TEXT,
                    chunk_index INTEGER,
                    content TEXT NOT NULL,
                    PRIMARY KEY (novel_id, chunk_index),
                    FOREIGN KEY (novel_id) REFERENCES novels(novel_id) ON DELETE CASCADE
                )
            """)

            # 4. User Reading Progress Session Tracker
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS reading_sessions (
                    user_id INTEGER PRIMARY KEY,
                    channel_id INTEGER UNIQUE,
                    novel_id TEXT,
                    current_chunk INTEGER DEFAULT 0,
                    last_interacted TEXT,
                    FOREIGN KEY (novel_id) REFERENCES novels(novel_id)
                )
            """)
            conn.commit()

    # --- GUILD SETTINGS METHODS ---
    def save_guild_settings(self, guild_id, category_id, search_channel_id, lounge_channel_id):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO guild_settings (guild_id, category_id, search_channel_id, lounge_channel_id, cleanup_disabled)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(guild_id) DO UPDATE SET
                    category_id=excluded.category_id,
                    search_channel_id=excluded.search_channel_id,
                    lounge_channel_id=excluded.lounge_channel_id
            """, (guild_id, category_id, search_channel_id, lounge_channel_id))
            conn.commit()

    def get_guild_settings(self, guild_id):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT category_id, search_channel_id, lounge_channel_id, cleanup_disabled FROM guild_settings WHERE guild_id = ?", (guild_id,))
            return cursor.fetchone()

    # --- NOVEL INGESTION & PARSING ENGINE ---
    def import_text_novel(self, novel_id, title, raw_text, author="Unknown", max_chunk_size=1500):
        """
        Splits a raw book string into distinct, sentence-safe narrative chunks,
        indexes it under the master listing, and saves all chunks.
        """
        chunks = []
        current_chunk = ""
        
        # Split by paragraph cleanings first
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
                # Handle single paragraphs that exceed character constraints natively
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

        # Write calculations into storage
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO novels (novel_id, title, author, total_chunks)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(novel_id) DO UPDATE SET total_chunks=excluded.total_chunks
            """, (novel_id, title, author, len(chunks)))

            # Clear any old structural variations before rewriting
            cursor.execute("DELETE FROM novel_chunks WHERE novel_id = ?", (novel_id,))
            
            for idx, text_block in enumerate(chunks):
                cursor.execute("""
                    INSERT INTO novel_chunks (novel_id, chunk_index, content)
                    VALUES (?, ?, ?)
                """, (novel_id, idx, text_block))
            conn.commit()
            
        return len(chunks)

    def search_novels(self, query):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT novel_id, title, author FROM novels WHERE title LIKE ? OR author LIKE ? LIMIT 10", (f"%{query}%", f"%{query}%"))
            return cursor.fetchall()

    def get_novel_details(self, novel_id):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT title, author, total_chunks FROM novels WHERE novel_id = ?", (novel_id,))
            return cursor.fetchone()

    def fetch_chunk(self, novel_id, chunk_index):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT content FROM novel_chunks WHERE novel_id = ? AND chunk_index = ?", (novel_id, chunk_index))
            res = cursor.fetchone()
            return res[0] if res else None

    # --- SESSION MANAGEMENT METHODS ---
    def register_or_get_session(self, user_id, channel_id, novel_id):
        """Prepares or returns an active channel state tracking record."""
        now_str = datetime.utcnow().isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Look up existing progress
            cursor.execute("SELECT current_chunk FROM reading_sessions WHERE user_id = ?", (user_id,))
            existing = cursor.fetchone()

            if existing:
                cursor.execute("""
                    UPDATE reading_sessions 
                    SET channel_id = ?, novel_id = ?, last_interacted = ? 
                    WHERE user_id = ?
                """, (channel_id, novel_id, now_str, user_id))
                conn.commit()
                return existing[0]
            else:
                cursor.execute("""
                    INSERT INTO reading_sessions (user_id, channel_id, novel_id, current_chunk, last_interacted)
                    VALUES (?, ?, ?, 0, ?)
                """, (user_id, channel_id, novel_id, now_str))
                conn.commit()
                return 0

    def update_session_progress(self, user_id, chunk_index):
        now_str = datetime.utcnow().isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE reading_sessions 
                SET current_chunk = ?, last_interacted = ? 
                WHERE user_id = ?
            """, (chunk_index, now_str, user_id))
            conn.commit()

    def get_active_session_by_user(self, user_id):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT channel_id, novel_id, current_chunk FROM reading_sessions WHERE user_id = ?", (user_id,))
            return cursor.fetchone()

    def get_all_active_sessions(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, channel_id, last_interacted FROM reading_sessions WHERE channel_id IS NOT NULL")
            return cursor.fetchall()

    def clear_active_channel(self, user_id):
        """Wipes the dynamic execution space binding while leaving saved milestones clean."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE reading_sessions SET channel_id = NULL WHERE user_id = ?", (user_id,))
            conn.commit()
