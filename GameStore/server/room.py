from __future__ import annotations

"""server.room

In-memory room state management.

A Room is a simple container tracking:
- host, players, chosen game
- idle/playing state and the port of the running game server (if any)

RoomManager exposes helper methods used by the lobby server.
"""


from dataclasses import dataclass, field
import threading
from typing import Dict, List, Optional


@dataclass
class Room:
    """Represents a single lobby room (not the running game process)."""
    id: int
    name: str
    host_user: str
    game_name: str
    status: str = "idle"  # idle|playing
    game_port: int = 0
    max_players: int = 2
    players: List[str] = field(default_factory=list)


class RoomManager:
    """Thread-safe manager for creating/joining/leaving rooms and tracking game state."""
    def __init__(self):
        self._rooms: Dict[int, Room] = {}
        self._lock = threading.Lock()

    def create_room(self, name: str, host: str, game_name: str, max_players: int) -> int:
        with self._lock:
            rid = 1
            while rid in self._rooms:
                rid += 1
            r = Room(id=rid, name=name, host_user=host, game_name=game_name, max_players=max_players, players=[host])
            self._rooms[rid] = r
            return rid

    def join_room(self, room_id: int, user: str) -> bool:
        with self._lock:
            r = self._rooms.get(room_id)
            if not r:
                return False
            if r.status != "idle":
                return False
            if user in r.players:
                return False
            if len(r.players) >= r.max_players:
                return False
            r.players.append(user)
            return True

    def is_room_full(self, room_id: int) -> bool:
        with self._lock:
            r = self._rooms.get(room_id)
            return bool(r and len(r.players) == r.max_players)

    def leave_room(self, room_id: int, user: str) -> int:
        """Return 1 if room disbanded, 0 if left, -1 if noop."""
        with self._lock:
            r = self._rooms.get(room_id)
            if not r:
                return -1
            if r.host_user == user:
                self._rooms.pop(room_id, None)
                return 1
            if user in r.players:
                r.players.remove(user)
                if not r.players:
                    self._rooms.pop(room_id, None)
                    return 1
                return 0
            return -1

    def list_rooms(self):
        with self._lock:
            return [
                {
                    "id": r.id,
                    "name": r.name,
                    "game": r.game_name,
                    "status": r.status,
                    "players": len(r.players),
                    "max_players": r.max_players,
                }
                for r in self._rooms.values()
            ]

    def get_room_info(self, room_id: int):
        with self._lock:
            r = self._rooms.get(room_id)
            if not r:
                return None
            return {
                "id": r.id,
                "name": r.name,
                "host": r.host_user,
                "game": r.game_name,
                "status": r.status,
                "players": list(r.players),
                "max_players": r.max_players,
                "game_port": r.game_port,
            }

    def start_game(self, room_id: int, port: int) -> bool:
        with self._lock:
            r = self._rooms.get(room_id)
            if not r:
                return False
            r.status = "playing"
            r.game_port = int(port)
            return True

    def finish_game(self, room_id: int) -> bool:
        with self._lock:
            r = self._rooms.get(room_id)
            if not r:
                return False
            r.status = "idle"
            r.game_port = 0
            return True

    def is_game_active(self, game_name: str) -> bool:
        with self._lock:
            return any(r.game_name == game_name for r in self._rooms.values())
