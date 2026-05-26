"""
Standalone app for exposing the local SongZip SQLite store from a PC.
"""

from spotdl.utils.songzip_store_remote import create_songzip_store_bridge_app

app = create_songzip_store_bridge_app()
