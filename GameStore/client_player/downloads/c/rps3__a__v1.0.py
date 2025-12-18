#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rock-Paper-Scissors (3 Players) GUI - Single Round
Protocol: length-prefixed JSON over TCP

Run:
  python rps_gui_3p_one_round.py --server <port>
  python rps_gui_3p_one_round.py --client --connect <ip> <port>
"""

from __future__ import annotations

import argparse
import json
import queue
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List

import tkinter as tk
from tkinter import messagebox

GAME_NAME = "RPS (GUI 3P, 1 Round)"
MAX_PLAYERS = 3

MOVES = ["rock", "paper", "scissors"]
BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}


# -------------------------
# JSON framing
# -------------------------
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


# -------------------------
# Game logic (single round)
# -------------------------
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

        mv = str(action.get("value", "")).strip().lower()
        if mv not in MOVES:
            return {"type": "error", "message": "invalid move (rock/paper/scissors)"}

        if pid in self.moves:
            return {"type": "ignored", "reason": "already_moved"}

        self.moves[pid] = mv

        # Do NOT reveal other players' moves before result.
        if len(self.moves) < self.n:
            return {"type": "state", "received": len(self.moves), "waiting": self.n - len(self.moves)}

        # Settlement (single round)
        self.finished = True
        kinds = set(self.moves.values())

        if len(kinds) == 1:
            winners: List[int] = []
            reason = "Tie (all same)"
        elif len(kinds) == 3:
            winners = []
            reason = "Tie (all three kinds)"
        else:
            a, b = list(kinds)
            win_move = a if BEATS[a] == b else b
            winners = [p for p, m in self.moves.items() if m == win_move]
            reason = f"{win_move} wins"

        return {
            "type": "result",
            "moves": self.moves,
            "winners": winners,
            "reason": reason,
        }


# -------------------------
# Server
# -------------------------
@dataclass
class PlayerConn:
    sock: socket.socket
    addr: Any
    pid: int


def run_server(port: int) -> None:
    print(f"[{GAME_NAME}] Server on port {port}, waiting for {MAX_PLAYERS} players...")

    logic = GameLogic(MAX_PLAYERS)
    players: List[PlayerConn] = []

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", port))
    listener.listen()

    def broadcast(obj: Dict[str, Any]) -> None:
        for p in players:
            try:
                send_json(p.sock, obj)
            except Exception:
                pass

    try:
        # 1) accept players
        while len(players) < MAX_PLAYERS:
            sock, addr = listener.accept()
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            pid = len(players)
            players.append(PlayerConn(sock=sock, addr=addr, pid=pid))
            print(f"[Server] Player {pid} connected from {addr}")

        # 2) start
        for p in players:
            send_json(p.sock, {"type": "start", "pid": p.pid, "n_players": MAX_PLAYERS, "game": GAME_NAME})

        # 3) loop until result
        while not logic.finished:
            progressed = False
            for p in players:
                try:
                    p.sock.settimeout(0.2)
                    msg = recv_json(p.sock)
                except socket.timeout:
                    continue
                except Exception:
                    # someone disconnected -> end as tie
                    logic.finished = True
                    broadcast({"type": "event", "event": {"type": "result", "moves": logic.moves, "winners": [], "reason": "Disconnected"}})
                    break

                if msg.get("type") != "action":
                    continue

                ev = logic.apply_action(p.pid, msg.get("action", {}))

                # error: only send to that client
                if ev.get("type") == "error":
                    try:
                        send_json(p.sock, {"type": "event", "event": ev})
                    except Exception:
                        pass
                    continue

                broadcast({"type": "event", "event": ev})
                progressed = True

                if ev.get("type") == "result":
                    break

            if not progressed:
                time.sleep(0.05)

        # 4) end
        broadcast({"type": "end"})

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


# -------------------------
# Client GUI
# -------------------------
class GuiClient:
    def __init__(self, ip: str, port: int):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.connect((ip, port))

        start = recv_json(self.sock)
        if start.get("type") != "start":
            raise RuntimeError("Protocol error: missing start")
        self.pid = int(start["pid"])
        self.n_players = int(start.get("n_players", MAX_PLAYERS))

        self.q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.running = True

        self.state = {
            "phase": "choose",   # choose | waiting | result
            "received": 0,
            "waiting": self.n_players,
            "result": None,
            "end_received": False,
        }

        # Tk must be created in main thread
        self.root = tk.Tk()
        self.root.title(f"{GAME_NAME} - Player {self.pid}")

        tk.Label(self.root, text=GAME_NAME, font=("Arial", 16, "bold")).pack(pady=8)
        self.lbl_you = tk.Label(self.root, text=f"You are Player {self.pid}", font=("Arial", 12))
        self.lbl_you.pack()

        self.lbl_info = tk.Label(self.root, text="Choose one move (single round).", font=("Arial", 12))
        self.lbl_info.pack(pady=6)

        btn_frame = tk.Frame(self.root)
        btn_frame.pack(pady=8)

        self.btns: Dict[str, tk.Button] = {}
        for mv in MOVES:
            b = tk.Button(btn_frame, text=mv, width=12, height=2,
                          command=lambda m=mv: self.send_move(m),
                          font=("Arial", 12, "bold"))
            b.pack(side="left", padx=6)
            self.btns[mv] = b

        self.lbl_status = tk.Label(self.root, text="", fg="blue", font=("Arial", 11))
        self.lbl_status.pack(pady=8)

        tk.Label(self.root, text="結果出來後按 Enter 回到房間。").pack(pady=4)

        self.root.bind("<Return>", lambda _e: self.on_return())
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        threading.Thread(target=self.recv_loop, daemon=True).start()
        self.root.after(50, self.ui_tick)
        self.render()

    def recv_loop(self) -> None:
        try:
            while self.running:
                msg = recv_json(self.sock)
                self.q.put(msg)
                if msg.get("type") == "end":
                    break
        except Exception:
            self.q.put({"type": "disconnect"})
        finally:
            self.running = False

    def send_move(self, mv: str) -> None:
        if self.state["phase"] != "choose":
            return
        try:
            send_json(self.sock, {"type": "action", "action": {"type": "move", "value": mv}})
            self.state["phase"] = "waiting"
            self.lbl_status.config(text=f"Sent: {mv}. Waiting others...")
            for b in self.btns.values():
                b.config(state="disabled")
        except Exception:
            self.lbl_status.config(text="Send failed (disconnected).")
            self.state["phase"] = "result"

    def ui_tick(self) -> None:
        while True:
            try:
                msg = self.q.get_nowait()
            except queue.Empty:
                break

            t = msg.get("type")

            if t == "event":
                ev = msg.get("event", {})
                et = ev.get("type")

                if et == "state":
                    self.state["received"] = int(ev.get("received", 0))
                    self.state["waiting"] = int(ev.get("waiting", 0))
                    self.render()

                elif et == "error":
                    self.lbl_status.config(text=f"[Error] {ev.get('message')}")
                    # allow re-choose if error
                    self.state["phase"] = "choose"
                    for b in self.btns.values():
                        b.config(state="normal")

                elif et == "result":
                    self.state["phase"] = "result"
                    self.state["result"] = ev
                    self.render()
                    if not getattr(self, "_shown_over", False):
                        self._shown_over = True
                        try:
                            messagebox.showinfo("Game Over", self._result_text(ev))
                        except Exception:
                            pass

            elif t == "end":
                self.state["end_received"] = True
                # do not auto-close; wait Enter
                self.render()

            elif t == "disconnect":
                self.state["phase"] = "result"
                self.state["result"] = {"type": "result", "moves": {}, "winners": [], "reason": "Disconnected"}
                self.render()

        if self.running:
            self.root.after(50, self.ui_tick)

    def _result_text(self, ev: Dict[str, Any]) -> str:
        moves = ev.get("moves", {})
        winners = ev.get("winners", [])
        reason = ev.get("reason", "")

        lines = ["========== RESULT =========="]
        for p in range(self.n_players):
            mv = moves.get(str(p), moves.get(p, None))
            lines.append(f"P{p}: {mv if mv is not None else '-'}")
        lines.append(f"Reason: {reason}")
        if winners:
            lines.append("Winners: " + ", ".join([f"P{int(w)}" for w in winners]))
        else:
            lines.append("Tie")
        lines.append("============================")
        if self.pid in [int(x) for x in winners]:
            lines.append("🎉 You win!")
        elif winners:
            lines.append("❌ You lose.")
        else:
            lines.append("🤝 Tie.")
        return "\n".join(lines)

    def render(self) -> None:
        if self.state["phase"] == "choose":
            self.lbl_info.config(text="Choose one move (single round).")
            self.lbl_status.config(text="")
            for b in self.btns.values():
                b.config(state="normal")

        elif self.state["phase"] == "waiting":
            rec = self.state.get("received", 0)
            wait = self.state.get("waiting", self.n_players)
            self.lbl_info.config(text=f"Waiting... received={rec}, waiting={wait}")
            # buttons already disabled

        elif self.state["phase"] == "result":
            ev = self.state.get("result")
            if ev:
                self.lbl_info.config(text="Game Over (single round).")
                self.lbl_status.config(text=self._result_text(ev))
            else:
                self.lbl_info.config(text="Game Over.")
                self.lbl_status.config(text="No result.")
            for b in self.btns.values():
                b.config(state="disabled")

    def on_return(self) -> None:
        if self.state["phase"] == "result":
            self.on_close()

    def on_close(self) -> None:
        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def mainloop(self) -> None:
        self.root.mainloop()


def run_client(ip: str, port: int) -> None:
    GuiClient(ip, port).mainloop()


# -------------------------
# Entry
# -------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", type=int, help="Run as game server on port")
    ap.add_argument("--client", action="store_true", help="Run as game client (GUI)")
    ap.add_argument("--connect", nargs=2, metavar=("IP", "PORT"), help="Connect to server")
    args = ap.parse_args()

    if args.server is not None:
        run_server(args.server)
        return

    if args.client and args.connect:
        run_client(args.connect[0], int(args.connect[1]))
        return

    print("Usage:")
    print("  python rps_gui_3p_one_round.py --server <port>")
    print("  python rps_gui_3p_one_round.py --client --connect <ip> <port>")


if __name__ == "__main__":
    main()
