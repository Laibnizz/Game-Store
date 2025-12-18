from __future__ import annotations

"""server.db

Thread-safe JSON database wrapper.

This module persists:
- users (username/password/role + play history)
- games (metadata, file name, download history, and comments)

It intentionally uses a coarse lock to keep the JSON file consistent for this homework-scale project.
"""


import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional


def _calc_rating(comments: List[Dict[str, Any]]) -> float:
    """Compute average rating from a list of comment dicts."""
    if not comments:
        return 0.0
    return sum(int(c.get("score", 0)) for c in comments) / len(comments)


class Database:
    """Simple JSON-file database with a coarse lock for thread safety."""
    def __init__(self, db_file: str | Path = "database.json"):
        self.db_file = Path(db_file)
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {"users": [], "games": []}
        self._load()

    def _load(self) -> None:
        if not self.db_file.exists():
            self._save()
            return
        try:
            self._data = json.loads(self.db_file.read_text(encoding="utf-8"))
        except Exception:
            self._data = {}
        self._data.setdefault("users", [])
        self._data.setdefault("games", [])
        self._save()

    def _save(self) -> None:
        self.db_file.write_text(json.dumps(self._data, ensure_ascii=False, indent=4), encoding="utf-8")

    # ---------------- users ----------------
    def register_user(self, username: str, password: str, role: str) -> bool:
        with self._lock:
            for u in self._data["users"]:
                if u.get("username") == username and u.get("role") == role:
                    return False
            self._data["users"].append({"username": username, "password": password, "role": role})
            self._save()
            return True

    def login_user(self, username: str, password: str, role_hint: Optional[str] = None) -> Optional[str]:
        with self._lock:
            for u in self._data["users"]:
                if u.get("username") == username and u.get("password") == password:
                    role = str(u.get("role", "player"))
                    if role_hint and role != role_hint:
                        continue
                    return role
        return None

    def record_play_history(self, username: str, game_name: str) -> None:
        with self._lock:
            for u in self._data["users"]:
                if u.get("username") == username:
                    ph = u.setdefault("play_history", [])
                    if game_name not in ph:
                        ph.append(game_name)
                        self._save()
                    return

    def has_played(self, username: str, game_name: str) -> bool:
        with self._lock:
            for u in self._data["users"]:
                if u.get("username") == username:
                    return game_name in u.get("play_history", [])
        return False

    # ---------------- games ----------------
    def get_game_owner(self, game_name: str) -> str:
        with self._lock:
            for g in self._data["games"]:
                if g.get("name") == game_name:
                    return str(g.get("dev", ""))
        return ""

    def get_game_filename(self, game_name: str) -> str:
        with self._lock:
            for g in self._data["games"]:
                if g.get("name") == game_name:
                    return str(g.get("filename", ""))
        return ""

    def get_game_version(self, game_name: str) -> str:
        with self._lock:
            for g in self._data["games"]:
                if g.get("name") == game_name:
                    return str(g.get("version", "1.0"))
        return "1.0"

    def get_game_max_players(self, game_name: str) -> int:
        with self._lock:
            for g in self._data["games"]:
                if g.get("name") == game_name:
                    return int(g.get("max_players", 2))
        return 2

    def upsert_game(
        self,
        dev_name: str,
        game_name: str,
        desc: str,
        filename: str,
        version: str,
        game_type: str,
        max_players: int,
    ) -> None:
        """Insert a new game entry or update an existing one owned by the developer."""
        with self._lock:
            for g in self._data["games"]:
                if g.get("name") == game_name and g.get("dev") == dev_name:
                    g["description"] = desc
                    g["filename"] = filename
                    g["version"] = version
                    g["game_type"] = game_type
                    g["max_players"] = max_players
                    self._save()
                    return

            self._data["games"].append(
                {
                    "name": game_name,
                    "dev": dev_name,
                    "description": desc,
                    "filename": filename,
                    "version": version,
                    "game_type": game_type,
                    "max_players": max_players,
                    "downloaded_by": [],
                    "comments": [],
                }
            )
            self._save()

    def delete_game(self, dev_name: str, game_name: str) -> str:
        with self._lock:
            games = self._data["games"]
            for i, g in enumerate(list(games)):
                if g.get("name") == game_name and g.get("dev") == dev_name:
                    filename = str(g.get("filename", ""))
                    games.pop(i)
                    self._save()
                    return filename
        return ""

    def record_download(self, game_name: str, username: str) -> None:
        with self._lock:
            for g in self._data["games"]:
                if g.get("name") == game_name:
                    dl = g.setdefault("downloaded_by", [])
                    if username not in dl:
                        dl.append(username)
                        self._save()
                    return

    def add_comment(self, game_name: str, user: str, score: int, content: str) -> bool:
        with self._lock:
            for g in self._data["games"]:
                if g.get("name") == game_name:
                    comments = g.setdefault("comments", [])
                    if any(c.get("user") == user for c in comments):
                        return False
                    comments.append({"user": user, "score": int(score), "content": content})
                    self._save()
                    return True
        return False

    def get_games(self) -> List[Dict[str, Any]]:
        with self._lock:
            out: List[Dict[str, Any]] = []
            for g in self._data["games"]:
                item = dict(g)
                comments = item.get("comments") or []
                item["avg_rating"] = _calc_rating(comments)
                item["comment_count"] = len(comments)

                downloaded_by = item.get("downloaded_by") or []
                item["downloads"] = len(downloaded_by)
                item.pop("downloaded_by", None)
                out.append(item)
            return out
