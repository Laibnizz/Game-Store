from __future__ import annotations

"""server.app

Lobby server for the GameStore project.

Responsibilities
- Accept TCP clients (players and developers).
- Handle JSON-based request/response actions (register/login/list/upload/download/rooms).
- Push asynchronous room notifications (player_joined, game_start, room_reset, etc.).
- Spawn a game server process when the room host starts a match.
- Store and serve uploaded game scripts from server/uploaded_games/.

Transport
- Control channel: length-prefixed UTF-8 JSON (see common.net).
- Data channel (upload/download): separate TCP connection sending raw file bytes.
"""


# Allow running as a script: add the project root to sys.path so imports work
# when executing `python server/app.py` directly from the repo.
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import json
import os
import socket
import selectors
import threading
import subprocess
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Any

from common.net import send_message, recv_message, send_raw, recv_raw_exact, pick_python
from server.db import Database
from server.room import RoomManager

SERVER_PORT = 12088  # Lobby server TCP port (control channel)
UPLOAD_DIR = Path("server/uploaded_games")  # Where uploaded game scripts are stored on the server

def _safe_token(s: str) -> str:
    """Sanitize an arbitrary string so it is safe to use in filenames."""
    s = (s or "").strip()
    s = re.sub(r"[^A-Za-z0-9_.-]", "_", s)
    return s[:80] or "x"

def make_server_filename(game_name: str, dev_name: str, version: str, client_filename: str) -> str:
    """Generate a unique, safe filename for server storage."""
    base = Path(str(client_filename)).name  # drop any directories
    ext = Path(base).suffix or ".py"
    return f"{_safe_token(game_name)}__{_safe_token(dev_name)}__v{_safe_token(version)}{ext}"


@dataclass
class ClientInfo:
    sock: socket.socket
    state: str = "CONNECTED"  # CONNECTED|LOGGED_IN|IN_ROOM
    username: str = ""
    role: str = ""
    room_id: int = -1


class LobbyServer:
    def __init__(self, host: str = "0.0.0.0", port: int = SERVER_PORT):
        self.host = host
        self.port = port
        self.sel = selectors.DefaultSelector()
        self.clients: Dict[socket.socket, ClientInfo] = {}
        self.db = Database("database.json")
        self.rooms = RoomManager()
        self.sessions: Dict[str, ClientInfo] = {}

    def start(self) -> None:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

        # Create the listening socket for the lobby control channel (JSON messages).
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen()
        listener.setblocking(False)

        self.sel.register(listener, selectors.EVENT_READ, data=None)
        print(f"Lobby Server (Python) running on {self.port}")

        try:
            while True:
                # selectors lets us multiplex many client sockets in a single thread.
                for key, _ in self.sel.select(timeout=None):
                    if key.data is None:
                        self._accept(key.fileobj)
                    else:
                        self._service(key)
        except KeyboardInterrupt:
            print("\n[System] Server shutting down...")
        finally:
            self.sel.close()

    def _accept(self, listener: socket.socket) -> None:
        conn, addr = listener.accept()
        conn.setblocking(True)  # we do blocking reads inside handler
        info = ClientInfo(sock=conn)
        self.clients[conn] = info
        self.sel.register(conn, selectors.EVENT_READ, data=info)
        print(f"New connection from {addr} (fd={conn.fileno()})")

    def _disconnect(self, info: ClientInfo) -> None:
        sock = info.sock
        uname = info.username or "Guest"
        print(f"[System] Disconnected: {uname} (fd={sock.fileno()})")

        if info.username and info.role and self.sessions.get(f"{info.role}:{info.username}") is info:
            self.sessions.pop(f"{info.role}:{info.username}", None)

        # leave room and notify
        if info.room_id != -1 and info.username:
            rid = info.room_id
            ret = self.rooms.leave_room(rid, info.username)

            if ret in (0, 1):
                if ret == 1:
                    notify = {"action": "room_disbanded"}
                else:
                    notify = {"action": "player_left", "username": info.username, "data": self.rooms.get_room_info(rid)}

                for other in list(self.clients.values()):
                    if other.sock is sock:
                        continue
                    if other.room_id == rid:
                        try:
                            send_message(other.sock, json.dumps(notify, ensure_ascii=False))
                        except Exception:
                            pass
                        if ret == 1:
                            other.state = "LOGGED_IN"
                            other.room_id = -1

        try:
            self.sel.unregister(sock)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
        self.clients.pop(sock, None)

    def _service(self, key: selectors.SelectorKey) -> None:
        info: ClientInfo = key.data
        sock = info.sock

        try:
            req_str = recv_message(sock)
        except Exception:
            self._disconnect(info)
            return

        try:
            req = json.loads(req_str)
        except Exception:
            return

        action = str(req.get("action", ""))
        who = info.username or "Guest"
        print(f"[Req] {who}: {action}")

        # Dispatch to the corresponding action handler, e.g. action='login' -> _act_login().
        handler = getattr(self, f"_act_{action}", None)
        if not handler:
            self._send(sock, {"status": "error", "message": f"Unknown action: {action}"})
            return
        try:
            handler(info, req)
        except Exception as e:
            self._send(sock, {"status": "error", "message": f"Server exception: {type(e).__name__}"})

    def _send(self, sock: socket.socket, obj: Dict[str, Any]) -> None:
        send_message(sock, json.dumps(obj, ensure_ascii=False))

    # ===================== Actions =====================
    def _act_register(self, c: ClientInfo, req: Dict[str, Any]) -> None:
        role = str(req.get("role", "player"))
        ok = self.db.register_user(str(req.get("username", "")), str(req.get("password", "")), role)
        if ok:
            self._send(c.sock, {"status": "ok", "message": "Registration successful"})
        else:
            self._send(c.sock, {"status": "error", "message": "Username already exists"})

    def _act_login(self, c: ClientInfo, req: Dict[str, Any]) -> None:
        target = str(req.get("username", "")).strip()
        password = str(req.get("password", ""))

        if not target:
            self._send(c.sock, {"status": "error", "message": "Username required"})
            return

        role_hint = str(req.get("role", "")).strip() or None
        role = self.db.login_user(target, password, role_hint)
        if not role:
            self._send(c.sock, {"status": "error", "message": "Invalid username or password"})
            return

        # If this connection was logged in as someone else, clean it first.
        if c.username and c.role and self.sessions.get(f"{c.role}:{c.username}") is c:
            self.sessions.pop(f"{c.role}:{c.username}", None)

        # Same account single-session rule:
        # We keep a sessions map so a username+role can only be online once at a time.
        # For this project, we choose "deny login if already online".
        # (So players/devs cannot log into an account that is currently online.)
        session_key = f"{role}:{target}"
        old = self.sessions.get(session_key)
        if old and old.sock is not c.sock:
            self._send(c.sock, {"status": "error", "message": "This account is already online."})
            return

        c.state = "LOGGED_IN"
        c.username = target
        c.role = role
        c.room_id = -1
        self.sessions[session_key] = c
        self._send(c.sock, {"status": "ok", "role": role})


    def _act_list_games(self, c: ClientInfo, req: Dict[str, Any]) -> None:
        self._send(c.sock, {"status": "ok", "data": self.db.get_games()})

    def _act_list_rooms(self, c: ClientInfo, req: Dict[str, Any]) -> None:
        self._send(c.sock, {"status": "ok", "data": self.rooms.list_rooms()})

    def _act_list_players(self, c: ClientInfo, req: Dict[str, Any]) -> None:
        players = [ci.username for ci in self.clients.values() if ci.username and ci.role == "player"]
        self._send(c.sock, {"status": "ok", "data": players})

    def _act_upload_request(self, c: ClientInfo, req: Dict[str, Any]) -> None:
        game_name = str(req.get("gamename", ""))
        is_new = bool(req.get("is_new_game", False))

        owner = self.db.get_game_owner(game_name)
        if is_new:
            if owner:
                if owner == c.username:
                    msg = f"Failed: You already have a game named '{game_name}'. Please use 'Update Game'."
                else:
                    msg = f"Failed: Game name '{game_name}' is already taken by another developer."
                self._send(c.sock, {"status": "error", "message": msg})
                return
        else:
            if not owner:
                self._send(c.sock, {"status": "error", "message": f"Failed: Game '{game_name}' does not exist."})
                return
            if owner != c.username:
                self._send(c.sock, {"status": "error", "message": "Failed: Permission Denied. You do not own this game."})
                return

        client_filename = str(req.get("filename", ""))
        filesize = int(req.get("filesize", 0))
        ver = str(req.get("version", "1.0"))
        game_type = str(req.get("game_type", "CLI"))
        max_players = int(req.get("max_players", 2))
        filename = make_server_filename(game_name, c.username, ver, client_filename)

        # Create a temporary data-channel listener for the raw file upload.
        # The client will connect to this port and stream exactly `filesize` bytes.
        # Create a temporary data-channel listener for the raw file download.
        # The client connects to this port to receive the file bytes.
        transfer_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        transfer_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        transfer_sock.bind(("0.0.0.0", 0))
        transfer_sock.listen(1)
        port = transfer_sock.getsockname()[1]

        save_path = UPLOAD_DIR / Path(filename).name

        threading.Thread(
            target=self._file_upload_thread,
            args=(transfer_sock, save_path, filesize),
            daemon=True,
        ).start()

        # update DB immediately (same behavior as original)
        self.db.upsert_game(c.username, game_name, str(req.get("description", "")), filename, ver, game_type, max_players)

        self._send(c.sock, {"status": "ok", "port": port})

    def _file_upload_thread(self, transfer_sock: socket.socket, save_path: Path, filesize: int) -> None:
        transfer_sock.settimeout(10)
        try:
            data_sock, _ = transfer_sock.accept()
        except Exception:
            try:
                transfer_sock.close()
            finally:
                return

        try:
            with data_sock:
                data_sock.settimeout(10)
                save_path.parent.mkdir(parents=True, exist_ok=True)
                with open(save_path, "wb") as f:
                    remaining = filesize
                    chunk = 4096
                    while remaining > 0:
                        n = chunk if remaining > chunk else remaining
                        try:
                            buf = recv_raw_exact(data_sock, n)
                        except Exception:
                            break
                        f.write(buf)
                        remaining -= n
        finally:
            try:
                transfer_sock.close()
            except Exception:
                pass
        print(f"[System] File saved: {save_path}")

    def _act_download_request(self, c: ClientInfo, req: Dict[str, Any]) -> None:
        gamename = str(req.get("gamename", ""))
        filename = self.db.get_game_filename(gamename)

        if not filename:
            self._send(c.sock, {"status": "error", "message": "Game not found in DB"})
            return

        filepath = UPLOAD_DIR / Path(filename).name
        if not filepath.exists():
            self._send(c.sock, {"status": "error", "message": "File missing on server"})
            return

        self.db.record_download(gamename, c.username)

        fsize = filepath.stat().st_size
        transfer_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        transfer_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        transfer_sock.bind(("0.0.0.0", 0))
        transfer_sock.listen(1)
        port = transfer_sock.getsockname()[1]

        threading.Thread(
            target=self._file_download_thread,
            args=(transfer_sock, filepath),
            daemon=True,
        ).start()

        self._send(c.sock, {"status": "ok", "port": port, "filesize": fsize, "filename": filename, "version": self.db.get_game_version(gamename)})

    def _file_download_thread(self, transfer_sock: socket.socket, filepath: Path) -> None:
        transfer_sock.settimeout(10)
        try:
            data_sock, _ = transfer_sock.accept()
        except Exception:
            try:
                transfer_sock.close()
            finally:
                return

        try:
            with data_sock:
                data_sock.settimeout(10)
                with open(filepath, "rb") as f:
                    while True:
                        buf = f.read(4096)
                        if not buf:
                            break
                        try:
                            send_raw(data_sock, buf)
                        except Exception:
                            break
        finally:
            try:
                transfer_sock.close()
            except Exception:
                pass
        print(f"[System] File sent: {filepath}")

    def _act_delete_game(self, c: ClientInfo, req: Dict[str, Any]) -> None:
        game_name = str(req.get("gamename", ""))
        if self.rooms.is_game_active(game_name):
            self._send(c.sock, {"status": "error", "message": "Failed: Game is currently active in a room. Please wait for matches to finish."})
            return

        filename = self.db.delete_game(c.username, game_name)
        if not filename:
            self._send(c.sock, {"status": "error", "message": "Permission Denied: You do not own this game or it does not exist."})
            return

        filepath = UPLOAD_DIR / Path(filename).name
        try:
            filepath.unlink(missing_ok=True)
        except Exception:
            pass
        self._send(c.sock, {"status": "ok", "message": "Game deleted successfully"})

    def _act_create_room(self, c: ClientInfo, req: Dict[str, Any]) -> None:
        rname = str(req.get("room_name", ""))
        gname = str(req.get("game_name", ""))
        if not self.db.get_game_filename(gname):
            self._send(c.sock, {"status": "error", "message": "Game not found"})
            return

        rid = self.rooms.create_room(rname, c.username, gname, self.db.get_game_max_players(gname))
        c.state = "IN_ROOM"
        c.room_id = rid
        self._send(c.sock, {"status": "ok", "room_id": rid, "data": self.rooms.get_room_info(rid)})

    def _act_join_room(self, c: ClientInfo, req: Dict[str, Any]) -> None:
        rid = int(req.get("room_id", -1))
        if self.rooms.join_room(rid, c.username):
            c.state = "IN_ROOM"
            c.room_id = rid
            res = {"status": "ok", "message": "Joined", "data": self.rooms.get_room_info(rid)}
            self._send(c.sock, res)

            notify = {"action": "player_joined", "username": c.username, "data": self.rooms.get_room_info(rid)}
            for other in self.clients.values():
                if other.room_id == rid and other.sock is not c.sock:
                    try:
                        send_message(other.sock, json.dumps(notify, ensure_ascii=False))
                    except Exception:
                        pass
        else:
            self._send(c.sock, {"status": "error", "message": "Cannot join (Room full or playing)"})

    def _act_leave_room(self, c: ClientInfo, req: Dict[str, Any]) -> None:
        if c.room_id == -1:
            self._send(c.sock, {"status": "ok"})
            return

        rid = c.room_id
        ret = self.rooms.leave_room(rid, c.username)

        if ret == 1:
            notify = {"action": "room_disbanded"}
        else:
            notify = {"action": "player_left", "username": c.username, "data": self.rooms.get_room_info(rid)}

        for other in self.clients.values():
            if other.room_id == rid and other.sock is not c.sock:
                try:
                    send_message(other.sock, json.dumps(notify, ensure_ascii=False))
                except Exception:
                    pass
                if ret == 1:
                    other.state = "LOGGED_IN"
                    other.room_id = -1

        c.state = "LOGGED_IN"
        c.room_id = -1
        self._send(c.sock, {"status": "ok"})

    def _act_start_game(self, c: ClientInfo, req: Dict[str, Any]) -> None:
        if c.room_id == -1:
            self._send(c.sock, {"status": "error", "message": "Not in a room"})
            return
        info = self.rooms.get_room_info(c.room_id)
        if not info:
            self._send(c.sock, {"status": "error", "message": "Room not found"})
            return
        if info.get("host") != c.username:
            self._send(c.sock, {"status": "error", "message": "Host only"})
            return
        if not self.rooms.is_room_full(c.room_id):
            self._send(c.sock, {"status": "error", "message": "Cannot start: Room is not full yet."})
            return

        filename = self.db.get_game_filename(str(info.get("game")))
        game_port = 14010 + int(c.room_id)
        game_path = str((UPLOAD_DIR / Path(filename).name).resolve())

        # Start the uploaded game script as a separate process in server mode.
        # Each room uses a deterministic port so players know where to connect.
        py = pick_python()
        try:
            subprocess.Popen([py, game_path, "--server", str(game_port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            subprocess.Popen([py, game_path, "--server", str(game_port)])

        self.rooms.start_game(c.room_id, game_port)

        broadcast = {"action": "game_start", "game_port": game_port, "filename": filename}
        for other in self.clients.values():
            if other.room_id == c.room_id:
                try:
                    send_message(other.sock, json.dumps(broadcast, ensure_ascii=False))
                except Exception:
                    pass

        # IMPORTANT: also respond to the requester so clients doing RPC won't block
        self._send(c.sock, {"status": "ok", "message": "Game started"})

    def _act_finish_game(self, c: ClientInfo, req: Dict[str, Any]) -> None:
        # IMPORTANT: always respond to avoid client RPC deadlock.
        if c.room_id == -1:
            self._send(c.sock, {"status": "error", "message": "Not in a room"})
            return
        info = self.rooms.get_room_info(c.room_id)
        if not info:
            self._send(c.sock, {"status": "error", "message": "Room not found"})
            return
        if info.get("host") != c.username:
            self._send(c.sock, {"status": "error", "message": "Host only"})
            return

        self.rooms.finish_game(c.room_id)
        gname = str(info.get("game"))
        for p in info.get("players", []):
            self.db.record_play_history(str(p), gname)

        notify = {"action": "room_reset", "data": self.rooms.get_room_info(c.room_id)}
        for other in self.clients.values():
            if other.room_id == c.room_id:
                try:
                    send_message(other.sock, json.dumps(notify, ensure_ascii=False))
                except Exception:
                    pass

        self._send(c.sock, {"status": "ok", "message": "Game finished"})

    def _act_add_comment(self, c: ClientInfo, req: Dict[str, Any]) -> None:
        gname = str(req.get("game_name", ""))
        score = int(req.get("score", 0))
        content = str(req.get("content", ""))

        if not self.db.has_played(c.username, gname):
            self._send(c.sock, {"status": "error", "message": "You must play this game before rating it!"})
            return

        ok = self.db.add_comment(gname, c.username, score, content)
        if ok:
            self._send(c.sock, {"status": "ok", "message": "Comment added successfully"})
        else:
            self._send(c.sock, {"status": "error", "message": "You have already rated this game or game not found."})

    def _act_logout(self, c: ClientInfo, req: Dict[str, Any]) -> None:
        if c.username and c.role and self.sessions.get(f"{c.role}:{c.username}") is c:
            self.sessions.pop(f"{c.role}:{c.username}", None)

        if c.room_id != -1 and c.username:
            self.rooms.leave_room(c.room_id, c.username)

        c.state = "CONNECTED"
        c.username = ""
        c.role = ""
        c.room_id = -1
        self._send(c.sock, {"status": "ok"})




if __name__ == "__main__":
    LobbyServer().start()