"""Mock NetEase catalog provider for testing multi-module integration."""
from __future__ import annotations

from agent_tools.netease import NeteaseCatalogProvider, Track


class MockNeteaseCatalog:
    """本地 mock，用于测试多模块集成，无需真实网易云 API。"""

    def __init__(self):
        self.catalog = [
            Track("1", "Take Five", "Dave Brubeck", "Time Out"),
            Track("2", "So What", "Miles Davis", "Kind of Blue"),
            Track("3", "Blue in Green", "Bill Evans", "Kind of Blue"),
            Track("4", "Autumn Leaves", "Cannonball Adderley", "Somethin' Else"),
            Track("5", "Round Midnight", "Thelonious Monk", "Genius of Modern Music"),
            Track("6", "My Favorite Things", "John Coltrane", "My Favorite Things"),
            Track("7", "Summertime", "Ella Fitzgerald", "Porgy and Bess"),
            Track("8", "Fly Me to the Moon", "Frank Sinatra", "It Might as Well Be Swing"),
        ]

    def search_tracks(self, query: str, *, limit: int) -> list[Track]:
        """简单的关键词匹配，返回匹配的歌曲。"""
        query_lower = query.lower()
        results = [
            track for track in self.catalog
            if query_lower in track.title.lower()
            or query_lower in track.artist.lower()
            or query_lower in track.album.lower()
        ]
        return results[:limit]
