from __future__ import annotations

"""client_player.app

CLI player client for the GameStore project.

Features
- Register / login as a player.
- Browse the store (list games, download or update local copies).
- Create / join rooms and receive real-time room notifications.
- Launch the downloaded game script as a separate Python process when a match starts.

Networking
- Uses a single TCP control connection to the lobby server (JSON messages).
- Uses a background reader thread (SocketRouter) to separate responses vs. notifications.
"""


# Allow running as a script: add the project root to sys.path so imports work
# when executing `python client_player/app.py` directly.
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import json
import os
import queue
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List

from common.net import send_message, recv_message, recv_raw_exact, pick_python

SERVER_IP = "140.113.17.11"
SERVER_PORT = 12088


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def read_line(prompt: str = "") -> str:
    if prompt:
        print(prompt, end="", flush=True)
    return sys.stdin.readline().rstrip("\n")


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def user_dir(user: str) -> Path:
    return Path("client_player/downloads") / user


def local_version(user: str, game_name: str) -> str:
    vp = user_dir(user) / f"{game_name}.ver"
    if vp.exists():
        try:
            return vp.read_text(encoding="utf-8").strip() or "0.0"
        except Exception:
            return "0.0"
    return "0.0"


def save_version(user: str, game_name: str, ver: str) -> None:
    vp = user_dir(user) / f"{game_name}.ver"
    ensure_dir(vp.parent)
    vp.write_text(str(ver), encoding="utf-8")


class SocketRouter(threading.Thread):
    """Background socket reader.

    The lobby server sends two kinds of JSON messages:
    - Responses: contain a "status" field (reply to an RPC request)
    - Notifications: do not contain "status" (async pushes like game_start)

    This thread continuously reads from the socket and routes messages into
    the appropriate queue so the main thread can stay synchronous.
    """

    def __init__(self, sock: socket.socket, resp_q: "queue.Queue[Dict[str, Any]]", notif_q: "queue.Queue[Dict[str, Any]]"):
        super().__init__(daemon=True)
        self.sock = sock
        self.resp_q = resp_q
        self.notif_q = notif_q
        self.alive = True

    def run(self) -> None:
        while self.alive:
            try:
                msg = json.loads(recv_message(self.sock))
            except Exception:
                self.notif_q.put({"action": "_disconnected"})
                break

            if "status" in msg:
                self.resp_q.put(msg)
            else:
                self.notif_q.put(msg)


def rpc(sock: socket.socket, resp_q: "queue.Queue[Dict[str, Any]]", req: Dict[str, Any]) -> Dict[str, Any]:
    '''
    Send one request and wait for its matching response.
    Design note: this client keeps requests sequential (one in flight),
    so the first response popped from resp_q belongs to the last request.
    '''
    send_message(sock, json.dumps(req, ensure_ascii=False))
    return resp_q.get()  # sequential: one outstanding request at a time


def download_game(sock: socket.socket, resp_q: "queue.Queue[Dict[str, Any]]", server_ip: str, user: str, game_name: str, server_ver: str = "") -> bool:
    res = rpc(sock, resp_q, {"action": "download_request", "gamename": game_name})
    if res.get("status") != "ok":
        print("[Error] Download failed:", res.get("message", "Unknown"))
        return False

    port = int(res["port"])
    filesize = int(res["filesize"])
    filename = str(res["filename"])
    ver = str(res.get("version", server_ver or ""))

    ensure_dir(user_dir(user))
    save_path = user_dir(user) / filename

    data_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        data_sock.connect((server_ip, port))
    except Exception:
        print("[Error] Data connection failed")
        return False

    received = 0
    try:
        with data_sock:
            with open(save_path, "wb") as f:
                while received < filesize:
                    n = min(4096, filesize - received)
                    buf = recv_raw_exact(data_sock, n)
                    f.write(buf)
                    received += n
                    pct = int(received * 100 / filesize) if filesize else 100
                    print(f"\rProgress: {pct}%", end="", flush=True)
        print()
    except Exception:
        print("\n[Error] Download interrupted")
        return False

    if received == filesize:
        print("[Success] Game downloaded")
        if ver:
            save_version(user, game_name, ver)
        return True
    return False


def launch_game_client(server_ip: str, port: int, user: str, filename: str) -> None:
    game_path = user_dir(user) / filename
    if not game_path.exists():
        print(f"[Error] Local game file missing: {game_path}")
        return

    py = pick_python()
    cmd = [py, str(game_path), "--client", "--connect", server_ip, str(port)]
    print("[System] Launching game:", " ".join(cmd))
    try:
        subprocess.run(cmd)
    except Exception as e:
        print("[Error] Failed to start game:", e)


def fetch_games(sock: socket.socket, resp_q: "queue.Queue[Dict[str, Any]]") -> List[Dict[str, Any]]:
    res = rpc(sock, resp_q, {"action": "list_games"})
    return list(res.get("data", [])) if res.get("status") == "ok" else []


def show_store(sock: socket.socket, resp_q: "queue.Queue[Dict[str, Any]]", server_ip: str, user: str) -> None:
    games = fetch_games(sock, resp_q)
    while True:
        clear_screen()
        print("=== Game Store ===")
        for i, g in enumerate(games, 1):
            name = str(g.get("name", ""))
            s_ver = str(g.get("version", "1.0"))
            l_ver = local_version(user, name)
            if l_ver == "0.0":
                tag = "[New]"
            elif l_ver != s_ver:
                tag = "[Update Available]"
            else:
                tag = "[Installed]"
            print(f"{i}. {name} {tag} (Rating: {float(g.get('avg_rating',0)):.1f} | DL: {g.get('downloads',0)})")
        print("0. Back")

        sel = read_line("Select: ").strip()
        if sel == "0":
            return
        try:
            idx = int(sel)
            g = games[idx - 1]
        except Exception:
            continue

        name = str(g.get("name", ""))
        while True:
            clear_screen()
            print(f"=== {name} ===")
            print("Author:", g.get("dev", "Unknown"))
            print("Type:", g.get("game_type", "CLI"))
            print("Max Players:", g.get("max_players", 2))
            print("Version:", g.get("version", "1.0"))
            print("Description:", g.get("description", ""))
            print("\n--- Ratings & Comments ---")
            comments = g.get("comments") or []
            if comments:
                for c in comments:
                    print(f"{c.get('user')}: {c.get('score')}/5 - {c.get('content')}")
            else:
                print("(No comments yet)")

            print("\nActions:")
            print("1. Download / Update")
            print("2. Rate this Game")
            print("3. Back")
            a = read_line("Select: ").strip()
            if a == "3":
                break
            if a == "1":
                download_game(sock, resp_q, server_ip, user, name, server_ver=str(g.get("version", "1.0")))
                read_line("Press Enter...")
            elif a == "2":
                score_s = read_line("Rating (1-5 integer): ").strip()
                if not score_s.isdigit() or not (1 <= int(score_s) <= 5):
                    read_line("Invalid. Press Enter...")
                    continue
                content = read_line("Comment (Enter to skip): ").strip() or "No comment"
                res = rpc(sock, resp_q, {"action": "add_comment", "game_name": name, "score": int(score_s), "content": content})
                print(res.get("message", res.get("status")))
                games = fetch_games(sock, resp_q)
                g = next((x for x in games if x.get("name") == name), g)
                read_line("Press Enter...")


def print_rooms(sock: socket.socket, resp_q: "queue.Queue[Dict[str, Any]]") -> None:
    res = rpc(sock, resp_q, {"action": "list_rooms"})
    rooms = res.get("data", []) if res.get("status") == "ok" else []
    print("\n=== Rooms ===")
    if not rooms:
        print("empty")
    for r in rooms:
        print(f"[{r.get('id')}] {r.get('name')} (Game: {r.get('game')}) - {r.get('status')} {r.get('players')}/{r.get('max_players')}")


def print_players(sock: socket.socket, resp_q: "queue.Queue[Dict[str, Any]]") -> None:
    res = rpc(sock, resp_q, {"action": "list_players"})
    players = res.get("data", []) if res.get("status") == "ok" else []
    print("\n=== Online Players ===")
    if not players:
        print("empty")
    for p in players:
        print("-", p)


def main() -> None:
    # Hardcoded server address for this deployment (as requested).
    # If you need to change the lobby server address, update SERVER_IP/SERVER_PORT above.
    server_ip = SERVER_IP
    server_port = SERVER_PORT

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((server_ip, server_port))

    resp_q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
    notif_q: "queue.Queue[Dict[str, Any]]" = queue.Queue()

    router = SocketRouter(sock, resp_q, notif_q)
    router.start()

    user = ""
    state = "LOGIN"  # LOGIN|LOBBY|IN_ROOM
    room_data: Dict[str, Any] = {}

    def drain_notifications() -> None:
        nonlocal state, room_data
        while True:
            try:
                msg = notif_q.get_nowait()
            except queue.Empty:
                break

            action = msg.get("action", "")
            if action == "_disconnected":
                print("\n[System] Disconnected from server.")
                raise SystemExit(0)

            if action in {"player_joined", "player_left", "room_reset"}:
                if state == "IN_ROOM":
                    room_data = msg.get("data", room_data)
                    who = msg.get("username", "")
                    print(f"\n[Room Update] {action} {who}")
            elif action == "room_disbanded":
                print("\n[Info] Room disbanded by host.")
                state = "LOBBY"
                room_data = {}
            elif action == "game_start":
                port = int(msg.get("game_port", 0))
                filename = str(msg.get("filename", ""))
                print("\n[Info] Game starting...")
                launch_game_client(server_ip, port, user, filename)
                # Game client returns here after it ends.
                if state == "IN_ROOM" and str(room_data.get("host", "")) == user:
                    _ = rpc(sock, resp_q, {"action": "finish_game"})
                read_line("Press Enter to continue...")
            else:
                # ignore unknown pushes
                pass

    try:
        while True:
            drain_notifications()

            if not user:
                clear_screen()
                print("=== Player Client ===")
                print("1. Register")
                print("2. Login")
                print("0. Exit")
                sel = read_line("Select: ").strip()
                if sel == "0":
                    return
                if sel == "1":
                    u = read_line("Username: ")
                    p = read_line("Password: ")
                    res = rpc(sock, resp_q, {"action": "register", "username": u, "password": p, "role": "player"})
                    print(res.get("message", res.get("status")))
                    read_line("Press Enter...")
                elif sel == "2":
                    u = read_line("Username: ")
                    p = read_line("Password: ")
                    res = rpc(sock, resp_q, {"action": "login", "username": u, "password": p, "role": "player"})
                    if res.get("status") == "ok" and res.get("role") == "player":
                        user = u
                        state = "LOBBY"
                        ensure_dir(user_dir(user))
                    else:
                        print("[Error]", res.get("message", "Login failed"))
                        read_line("Press Enter...")
                continue

            if state == "LOBBY":
                clear_screen()
                print(f"=== Lobby ({user}) ===")
                print("1. Game Store")
                print("2. List Rooms")
                print("3. Create Room")
                print("4. Join Room")
                print("5. List Online Players")
                print("6. Logout")
                print("0. Exit")
                sel = read_line("Select: ").strip()

                if sel == "1":
                    show_store(sock, resp_q, server_ip, user)
                elif sel == "2":
                    clear_screen(); print_rooms(sock, resp_q); read_line("\nPress Enter...")
                elif sel == "5":
                    clear_screen(); print_players(sock, resp_q); read_line("\nPress Enter...")
                elif sel == "3":
                    rname = read_line("Room Name: ").strip() or "Room"
                    gname = read_line("Game Name: ").strip()
                    res = rpc(sock, resp_q, {"action": "create_room", "room_name": rname, "game_name": gname})
                    if res.get("status") == "ok":
                        room_data = res.get("data", {})
                        state = "IN_ROOM"
                        # auto download
                        download_game(sock, resp_q, server_ip, user, str(room_data.get("game", gname)))
                    else:
                        print("[Error]", res.get("message", ""))
                        read_line("Press Enter...")
                elif sel == "4":
                    rid = read_line("Room ID: ").strip()
                    if not rid.isdigit():
                        continue
                    res = rpc(sock, resp_q, {"action": "join_room", "room_id": int(rid)})
                    if res.get("status") == "ok":
                        room_data = res.get("data", {})
                        state = "IN_ROOM"
                        download_game(sock, resp_q, server_ip, user, str(room_data.get("game", "")))
                    else:
                        print("[Error]", res.get("message", ""))
                        read_line("Press Enter...")
                elif sel == "6":
                    _ = rpc(sock, resp_q, {"action": "logout"})
                    user = ""
                    state = "LOGIN"
                    room_data = {}
                elif sel == "0":
                    return

            elif state == "IN_ROOM":
                clear_screen()
                print("=== Room ===")
                print("ID:", room_data.get("id"))
                print("Name:", room_data.get("name"))
                print("Game:", room_data.get("game"))
                print("Status:", room_data.get("status"))
                print("Host:", room_data.get("host"))
                print("Players:", ", ".join(room_data.get("players", [])))
                print(f"({len(room_data.get('players', []))}/{room_data.get('max_players', 2)})")
                print("")
                print("Actions:")
                if str(room_data.get("host", "")) == user:
                    print("1. Start Game")
                else:
                    print("(Waiting for host to start...)")
                print("2. Leave Room")
                print("0. Refresh")
                sel = read_line("Select: ").strip()

                if sel == "1":
                    if str(room_data.get("host", "")) != user:
                        print("[Info] 只有房主可以開始遊戲。")
                        read_line("Press Enter...")
                        continue
                    res = rpc(sock, resp_q, {"action": "start_game"})
                    if res.get("status") == "error":
                        print("[Error]", res.get("message", ""))
                    else:
                        print(res.get("message", "ok"))
                    read_line("Press Enter...")
                elif sel == "2":
                    _ = rpc(sock, resp_q, {"action": "leave_room"})
                    state = "LOBBY"
                    room_data = {}
                elif sel == "0":
                    # Refresh only
                    continue
    finally:
        router.alive = False
        try:
            sock.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()