#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

"""client_dev.games.rps

Single-round Rock-Paper-Scissors example game.

This is a simple reference implementation used during development.
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
# 遊戲設定（開發者可改）
# =========================

MAX_PLAYERS = 2
GAME_NAME = "Rock Paper Scissors"


@dataclass
class PlayerConn:
    sock: socket.socket
    addr: Any
    pid: int  # 0..MAX_PLAYERS-1


# =========================
# 遊戲邏輯：猜拳（單局）
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

        # 結算（單局）
        self.finished = True
        p0 = self.moves.get(0)
        p1 = self.moves.get(1)

        '''
        if p0 == p1:
            winner = -1  # tie
            reason = "tie"
        elif BEATS[p0] == p1:
            winner = 0
            reason = f"{p0} beats {p1}"
        else:
            winner = 1
            reason = f"{p1} beats {p0}"
        '''

        if p0 == p1:
            winner = 0  # tie
            reason = f"{p0} beats {p1}"
        elif BEATS[p0] == p1:
            winner = 0
            reason = f"{p0} beats {p1}"
        else:
            winner = 1
            reason = f"{p1} beats {p0}"


        # 這裡「不直接把對方出的拳顯示在過程中」，只在結果公布
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
        # 1) Accept connections until the room is full
        while len(players) < MAX_PLAYERS:
            sock, addr = listener.accept()
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            pid = len(players)
            players.append(PlayerConn(sock=sock, addr=addr, pid=pid))
            print(f"[Server] Player {pid} connected from {addr}")

        # 2) Broadcast a 'start' message (assign each player a pid)
        for p in players:
            send_json(p.sock, {"type": "start", "pid": p.pid, "n_players": MAX_PLAYERS})

        # 3) Main loop: receive actions -> update game state -> broadcast events
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

                # Broadcast the event to every connected client
                for q in players:
                    try:
                        send_json(q.sock, {"type": "event", "event": event})
                    except Exception:
                        pass

                # If we produced a final result, end the match loop
                if event.get("type") == "result":
                    logic.finished = True
                    break

        # 4) Cleanup: send 'end' so clients can exit cleanly back to the lobby
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

                    # Key UX: wait for Enter so the result screen stays visible before returning to lobby
                    input("按 Enter 回到房間...")

                    # Simplest: break out and let the lobby/client process return to the room UI
                    break

            elif t == "end":
                # 如果 server 有送 end，也可以讓它自然結束
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
