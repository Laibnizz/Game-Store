from __future__ import annotations

"""client_dev.app

CLI developer client for the GameStore project.

Features
- Register / login as a developer.
- Upload new games or update existing games (script files).
- (Optionally) list games and manage uploads.

Networking
- Control channel uses JSON messages over a single TCP connection.
- Upload uses a separate short-lived TCP connection to transfer raw bytes.
"""


# Allow running as a script: add the project root to sys.path so imports work
# when executing `python client_dev/app.py` directly.
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))


import json
import os
import socket
import sys
from pathlib import Path

from common.net import send_message, recv_message, send_raw, pick_python

SERVER_IP = "140.113.17.11"
SERVER_PORT = 12088

def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def read_line(prompt: str = "") -> str:
    if prompt:
        print(prompt, end="", flush=True)
    return sys.stdin.readline().rstrip("\n")


def is_nonempty(s: str) -> bool:
    return bool(s.strip())


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except Exception:
        return -1


def connect_server(ip: str, port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((ip, port))
    return s


def send_file(ip: str, port: int, src: Path, total: int) -> bool:
    '''
    Upload a file to the server's data-channel port.

    The lobby server tells us which ephemeral port to connect to. We then
    stream the file as raw bytes (no JSON framing) until EOF.
    '''
    data_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        data_sock.connect((ip, port))
    except Exception:
        return False

    sent = 0
    try:
        with data_sock:
            with open(src, "rb") as f:
                while True:
                    buf = f.read(4096)
                    if not buf:
                        break
                    send_raw(data_sock, buf)
                    sent += len(buf)
                    if total > 0:
                        pct = int(sent * 100 / total)
                        print(f"\r[System] Uploading: {pct}%", end="", flush=True)
        print()
        return sent == total
    except Exception:
        return False


def fetch_my_games(sock: socket.socket, username: str):
    send_message(sock, json.dumps({"action": "list_games"}, ensure_ascii=False))
    res = json.loads(recv_message(sock))
    if res.get("status") != "ok":
        return []
    return [g for g in res.get("data", []) if g.get("dev") == username]


def prompt_required(label: str) -> str:
    while True:
        v = read_line(label)
        if is_nonempty(v):
            return v.strip()
        print("[Warning] 不能留空，請再輸入一次。")


def prompt_choice(label: str, allowed: set[str]) -> str:
    allowed_up = {x.upper() for x in allowed}
    while True:
        v = prompt_required(label).upper()
        if v in allowed_up:
            return v
        print(f"[Warning] 請輸入：{', '.join(sorted(allowed_up))}")


def prompt_positive_int(label: str) -> int:
    while True:
        v = prompt_required(label)
        try:
            n = int(v)
            if n > 0:
                return n
        except Exception:
            pass
        print("[Warning] 請輸入 > 0 的整數。")


def do_upload(sock: socket.socket, server_ip: str, username: str, is_new: bool) -> None:
    clear_screen()
    print("=== Upload" + (" New" if is_new else " Update") + " Game ===")

    if is_new:
        game_name = prompt_required("Game Name: ")
    else:
        my_games = fetch_my_games(sock, username)
        if not my_games:
            print("[Info] 你沒有可更新的遊戲。\nPress Enter...")
            read_line()
            return
        for i, g in enumerate(my_games, 1):
            print(f"{i}. {g.get('name')} (Ver: {g.get('version','1.0')})")
        print("0. Cancel")
        sel = read_line("Select: ")
        if sel.strip() == "0" or not sel.strip():
            return
        try:
            idx = int(sel)
            game_name = my_games[idx - 1]["name"]
        except Exception:
            print("Invalid input.\nPress Enter...")
            read_line()
            return

    version = prompt_required("Version (e.g., 1.0): ")
    game_type = prompt_choice("Game Type (CLI/GUI): ", {"CLI", "GUI"})
    max_players = prompt_positive_int("Max Players (e.g., 2): ")
    desc = prompt_required("Description: ")

    while True:
        p = read_line("File Path: ").strip()
        if p.lower() == "cancel":
            return
        if not p:
            print("[Warning] 檔案路徑不能空白。")
            continue
        src = Path(p)
        size = file_size(src)
        if size >= 0:
            filename = src.name
            break
        print(f"[Error] 找不到檔案：{p}")

    # First, ask the lobby server to reserve an upload slot and return
    # a temporary TCP port for the raw file transfer.
    req = {
        "action": "upload_request",
        "is_new_game": is_new,
        "gamename": game_name,
        "version": version,
        "description": desc,
        "game_type": game_type,
        "max_players": max_players,
        "filename": filename,
        "filesize": size,
    }
    send_message(sock, json.dumps(req, ensure_ascii=False))
    res = json.loads(recv_message(sock))
    if res.get("status") != "ok":
        print("[Error] " + res.get("message", "Unknown"))
        print("Press Enter...")
        read_line()
        return

    port = int(res["port"])
    print(f"[System] Connecting to data channel port {port}...")
    ok = send_file(server_ip, port, src, size)
    if ok:
        print("[Success] Upload OK!")
    else:
        print("[Error] File transfer failed.")
    print("Press Enter...")
    read_line()


def do_remove(sock: socket.socket, username: str) -> None:
    clear_screen()
    print("=== Remove Game ===")
    my_games = fetch_my_games(sock, username)
    if not my_games:
        print("[Info] 你沒有可下架的遊戲。\nPress Enter...")
        read_line()
        return

    for i, g in enumerate(my_games, 1):
        print(f"{i}. {g.get('name')} (Ver: {g.get('version','1.0')})")
    print("0. Cancel")
    sel = read_line("Select: ")
    if sel.strip() == "0" or not sel.strip():
        return
    try:
        idx = int(sel)
        gamename = my_games[idx - 1]["name"]
    except Exception:
        return

    confirm = read_line(f"Type 'yes' to confirm remove '{gamename}': ")
    if confirm.strip().lower() != "yes":
        return

    send_message(sock, json.dumps({"action": "delete_game", "gamename": gamename}, ensure_ascii=False))
    res = json.loads(recv_message(sock))
    if res.get("status") == "ok":
        print("[Success] " + res.get("message", ""))
    else:
        print("[Error] " + res.get("message", "Unknown"))
    print("Press Enter...")
    read_line()


def do_list_my(sock: socket.socket, username: str) -> None:
    clear_screen()
    print("=== My Published Games ===")
    my_games = fetch_my_games(sock, username)
    if not my_games:
        print("(No games)")
    for g in my_games:
        print(f"Name: {g.get('name')}\nVersion: {g.get('version','1.0')}\nFile: {g.get('filename','')}\nDesc: {g.get('description','')}\n--------------------")
    print("Press Enter...")
    read_line()


def main() -> None:
    # Hardcoded server address for this deployment
    server_ip = SERVER_IP
    server_port = SERVER_PORT

    sock = connect_server(server_ip, server_port)
    username = ""

    try:
        while True:
            if not username:
                clear_screen()
                print("=== Developer Client ===")
                print("1. Register")
                print("2. Login")
                print("0. Exit")
                sel = read_line("Select: ")
                if sel == "0":
                    return
                if sel == "1":
                    u = prompt_required("Username: ")
                    p = prompt_required("Password: ")
                    send_message(sock, json.dumps({"action": "register", "username": u, "password": p, "role": "developer"}, ensure_ascii=False))
                    res = json.loads(recv_message(sock))
                    print(res.get("message", res.get("status")))
                    read_line("Press Enter...")
                elif sel == "2":
                    u = prompt_required("Username: ")
                    p = prompt_required("Password: ")
                    send_message(sock, json.dumps({"action": "login", "username": u, "password": p, "role": "developer"}, ensure_ascii=False))
                    res = json.loads(recv_message(sock))
                    if res.get("status") == "ok" and res.get("role") == "developer":
                        username = u
                    else:
                        print("[Error] " + res.get("message", "Login failed or not a developer"))
                        read_line("Press Enter...")
                continue

            clear_screen()
            print(f"=== Developer Menu ({username}) ===")
            print("1. My Games")
            print("2. Upload New Game")
            print("3. Update Existing Game")
            print("4. Remove Game")
            print("5. Logout")
            print("0. Exit")
            sel = read_line("Select: ")

            if sel == "1":
                do_list_my(sock, username)
            elif sel == "2":
                do_upload(sock, server_ip, username, is_new=True)
            elif sel == "3":
                do_upload(sock, server_ip, username, is_new=False)
            elif sel == "4":
                do_remove(sock, username)
            elif sel == "5":
                send_message(sock, json.dumps({"action": "logout"}, ensure_ascii=False))
                _ = recv_message(sock)
                username = ""
            elif sel == "0":
                return

    finally:
        try:
            sock.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
