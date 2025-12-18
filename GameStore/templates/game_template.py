#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

"""templates.game_template

Reusable template for GameStore games.

A game script can run in two modes:
- --server <port>: act as a TCP game server, accept clients, and run the match.
- --client --connect <ip> <port>: connect to the game server and provide UI/IO.

The lobby server launches game scripts with --server.
The player client launches the same script with --client.
"""


import argparse
import json
import socket
import struct
from dataclasses import dataclass
from typing import Any, Dict, List


# =========================
# Protocol: length-prefixed JSON
# =========================

def send_json(sock: socket.socket, obj: Dict[str, Any]) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sock.sendall(struct.pack("!I", len(data)) + data)

def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf

def recv_json(sock: socket.socket) -> Dict[str, Any]:
    (length,) = struct.unpack("!I", recv_exact(sock, 4))
    data = recv_exact(sock, length)
    return json.loads(data.decode("utf-8"))


# =========================
# Game configuration (edit these constants)
# =========================

MAX_PLAYERS = 2
GAME_NAME = "Rock Paper Scissors"


@dataclass
class PlayerConn:
    sock: socket.socket
    addr: Any
    pid: int  # 0..MAX_PLAYERS-1


# =========================
# Game logic: Rock-Paper-Scissors (single round)
# =========================

VALID_MOVES = {"r": "rock", "p": "paper", "s": "scissors"}
BEATS = {
    "rock": "scissors",
    "paper": "rock",
    "scissors": "paper",
}

class GameLogic:
    def __init__(self, n_players: int):
        self.n = n_players
        self.moves: Dict[int, str] = {}  # pid -> move
        self.finished = False

    def apply_action(self, pid: int, action: Dict[str, Any]) -> Dict[str, Any]:
        if self.finished:
            return {"type": "ignored", "reason": "already_finished"}

        if action.get("type") != "move":
            return {"type": "error", "message": "unknown action"}

        raw = str(action.get("value", "")).strip().lower()
        if raw not in VALID_MOVES:
            return {"type": "error", "message": "invalid move (use r/p/s)"}

        if pid in self.moves:
            return {"type": "ignored", "reason": "already_moved"}

        self.moves[pid] = VALID_MOVES[raw]

        if len(self.moves) < self.n:
            return {
                "type": "state",
                "received": len(self.moves),
                "waiting": self.n - len(self.moves),
            }

        # Settlement (single round)
        self.finished = True
        p0 = self.moves.get(0)
        p1 = self.moves.get(1)

        if p0 == p1:
            winner = -1  # tie
            reason = "tie"
        elif BEATS[p0] == p1:
            winner = 0
            reason = f"{p0} beats {p1}"
        else:
            winner = 1
            reason = f"{p1} beats {p0}"

        # Do not reveal opponents' moves during the round; only reveal them in the final result.
        return {
            "type": "result",
            "winner": winner,
            "moves": self.moves,
            "reason": reason,
        }


# =========================
# Server mode
# =========================

def run_server(port: int) -> None:
    print(f"[{GAME_NAME}] Server starting on port {port}, waiting for {MAX_PLAYERS} players...")

    logic = GameLogic(MAX_PLAYERS)
    players: List[PlayerConn] = []

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", port))
    listener.listen()

    try:
        # 1) 接滿玩家
        while len(players) < MAX_PLAYERS:
            sock, addr = listener.accept()
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            pid = len(players)
            players.append(PlayerConn(sock=sock, addr=addr, pid=pid))
            print(f"[Server] Player {pid} connected from {addr}")

        # 2) 通知開始（分配 pid）
        for p in players:
            send_json(p.sock, {"type": "start", "pid": p.pid, "n_players": MAX_PLAYERS})

        # 3) 主迴圈：收 action -> 更新 -> 廣播
        while not logic.finished:
            for p in players:
                try:
                    p.sock.settimeout(0.2)
                    msg = recv_json(p.sock)
                except socket.timeout:
                    continue
                except Exception:
                    print("[Server] player disconnected, ending game.")
                    logic.finished = True
                    break

                if msg.get("type") != "action":
                    continue

                event = logic.apply_action(p.pid, msg.get("action", {}))

                # 廣播 event
                for q in players:
                    try:
                        send_json(q.sock, {"type": "event", "event": event})
                    except Exception:
                        pass

                # 如果出了 result，就結束
                if event.get("type") == "result":
                    logic.finished = True
                    break

        # 4) 收尾：送 end，讓 client 正常 exit 回到房間
        for p in players:
            try:
                send_json(p.sock, {"type": "end"})
            except Exception:
                pass

    finally:
        try:
            listener.close()
        except Exception:
            pass
        for p in players:
            try:
                p.sock.close()
            except Exception:
                pass
        print(f"[{GAME_NAME}] Server stopped.")


# =========================
# Client mode
# =========================

def run_client(ip: str, port: int) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.connect((ip, port))

    try:
        start = recv_json(sock)
        if start.get("type") != "start":
            print("[Client] Protocol error.")
            return

        pid = int(start["pid"])
        n_players = int(start["n_players"])
        print(f"[{GAME_NAME}] Connected! You are Player {pid} / {n_players - 1}")

        moved = False
        saw_result = False

        while True:

            if not moved and not saw_result:
                print("Choose your move: r=rock, p=paper, s=scissors")
                while True:
                    mv = input("> ").strip().lower()
                    if mv in VALID_MOVES:
                        break
                    print("Invalid. Please input r / p / s.")
                send_json(sock, {"type": "action", "action": {"type": "move", "value": mv}})
                moved = True
                print("[Client] Move sent. Waiting for result...")

            msg = recv_json(sock)
            t = msg.get("type")

            if t == "event":
                ev = msg.get("event", {})
                et = ev.get("type")

                if et == "state":
                    print(f"[State] received={ev.get('received')} waiting={ev.get('waiting')}")

                elif et == "error":
                    print("[Error]", ev.get("message"))
                    moved = False

                elif et == "result":
                    moves = ev.get("moves", {})
                    winner = ev.get("winner", -1)
                    reason = ev.get("reason", "")

                    m0 = moves.get(0, moves.get("0"))
                    m1 = moves.get(1, moves.get("1"))

                    print("\n========== RESULT ==========")
                    print(f"P0 = {m0}")
                    print(f"P1 = {m1}")
                    print(f"Reason: {reason}")
                    if winner == -1:
                        print("=> Tie!")
                    else:
                        print(f"=> Winner: Player {winner}")
                    print("============================\n")

                    saw_result = True


                    input("按 Enter 回到房間...")

                    break

            elif t == "end":
                break

    except Exception as e:
        print("[Client] Disconnected:", type(e).__name__)
    finally:
        try:
            sock.close()
        except Exception:
            pass


# =========================
# Entry
# =========================

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", type=int, help="Run as game server on port")
    ap.add_argument("--client", action="store_true", help="Run as game client")
    ap.add_argument("--connect", nargs=2, metavar=("IP", "PORT"), help="Connect to server (client mode)")
    args = ap.parse_args()

    if args.server is not None:
        run_server(args.server)
        return

    if args.client and args.connect:
        ip = args.connect[0]
        port = int(args.connect[1])
        run_client(ip, port)
        return

    print("Usage:")
    print("  python rps_template.py --server <port>")
    print("  python rps_template.py --client --connect <ip> <port>")


if __name__ == "__main__":
    main()
