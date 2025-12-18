#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tic-Tac-Toe (3x3) GUI 2P - Tkinter
Protocol: length-prefixed JSON over TCP

Run:
  python tictactoe_gui_2p.py --server <port>
  python tictactoe_gui_2p.py --client --connect <ip> <port>
"""

from __future__ import annotations
import argparse, json, queue, socket, struct, threading, time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import tkinter as tk
from tkinter import messagebox

GAME_NAME = "Tic-Tac-Toe (GUI 2P)"
MAX_PLAYERS = 2

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
# Game logic
# -------------------------
WIN_LINES = [
    (0,1,2),(3,4,5),(6,7,8),
    (0,3,6),(1,4,7),(2,5,8),
    (0,4,8),(2,4,6),
]

class GameLogic:
    def __init__(self):
        self.board = [""] * 9  # "" | "X" | "O"
        self.turn = 0          # pid 0 or 1
        self.finished = False
        self.winner: Optional[int] = None  # None means draw or not finished
        self.last_move: Optional[Dict[str, Any]] = None

    def _check_winner_symbol(self) -> Optional[str]:
        for a,b,c in WIN_LINES:
            if self.board[a] and self.board[a] == self.board[b] == self.board[c]:
                return self.board[a]
        return None

    def _is_full(self) -> bool:
        return all(cell != "" for cell in self.board)

    def state_event(self) -> Dict[str, Any]:
        return {
            "type": "state",
            "board": self.board,
            "turn": self.turn,
            "finished": self.finished,
            "winner": self.winner,     # pid index (0/1) or None (draw / unfinished)
            "last_move": self.last_move
        }

    def apply_action(self, pid: int, action: Dict[str, Any]) -> Dict[str, Any]:
        if self.finished:
            return {"type": "ignored", "reason": "already_finished"}

        if pid != self.turn:
            return {"type": "error", "message": "not your turn", "turn": self.turn}

        if action.get("type") != "place":
            return {"type": "error", "message": "unknown action"}

        try:
            idx = int(action.get("index"))
        except Exception:
            return {"type": "error", "message": "invalid index"}

        if not (0 <= idx <= 8):
            return {"type": "error", "message": "index out of range"}

        if self.board[idx] != "":
            return {"type": "error", "message": "cell already occupied"}

        sym = "X" if pid == 0 else "O"
        self.board[idx] = sym
        self.last_move = {"pid": pid, "symbol": sym, "index": idx}

        ws = self._check_winner_symbol()
        if ws is not None:
            self.finished = True
            self.winner = 0 if ws == "X" else 1
        elif self._is_full():
            self.finished = True
            self.winner = None  # draw
        else:
            self.turn = 1 - self.turn

        return self.state_event()

# -------------------------
# Server
# -------------------------
@dataclass
class PlayerConn:
    sock: socket.socket
    addr: Any
    pid: int

def run_server(port: int) -> None:
    print(f"[{GAME_NAME}] Server on port {port}, waiting for 2 players...")

    logic = GameLogic()
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
        while len(players) < MAX_PLAYERS:
            sock, addr = listener.accept()
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            pid = len(players)
            players.append(PlayerConn(sock=sock, addr=addr, pid=pid))
            print(f"[Server] Player {pid} connected from {addr}")

        for p in players:
            send_json(p.sock, {"type": "start", "pid": p.pid, "n_players": 2})

        broadcast({"type": "event", "event": logic.state_event()})

        while not logic.finished:
            progressed = False
            for p in players:
                try:
                    p.sock.settimeout(0.2)
                    msg = recv_json(p.sock)
                except socket.timeout:
                    continue
                except Exception:
                    logic.finished = True
                    logic.winner = None
                    break

                if msg.get("type") != "action":
                    continue

                ev = logic.apply_action(p.pid, msg.get("action", {}))

                if ev.get("type") == "error":
                    try:
                        send_json(p.sock, {"type": "event", "event": ev})
                    except Exception:
                        pass
                    continue

                broadcast({"type": "event", "event": ev})
                progressed = True
                if ev.get("finished"):
                    break

            if not progressed:
                time.sleep(0.05)

        broadcast({"type": "end"})

    finally:
        try: listener.close()
        except Exception: pass
        for p in players:
            try: p.sock.close()
            except Exception: pass
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

        self.q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.running = True
        self.state = {
            "board": [""]*9,
            "turn": 0,
            "finished": False,
            "winner": None,
            "last_move": None
        }

        self.root = tk.Tk()
        self.root.title(f"{GAME_NAME} - Player {self.pid}")

        tk.Label(self.root, text=GAME_NAME, font=("Arial", 16, "bold")).pack(pady=8)
        self.lbl_info = tk.Label(self.root, text=f"You are Player {self.pid} ({'X' if self.pid==0 else 'O'})")
        self.lbl_info.pack()

        self.lbl_turn = tk.Label(self.root, text="Turn: Player 0", font=("Arial", 12))
        self.lbl_turn.pack(pady=6)

        self.lbl_last = tk.Label(self.root, text="Last move: -")
        self.lbl_last.pack(pady=2)

        board_frame = tk.Frame(self.root)
        board_frame.pack(pady=8)

        self.btns: List[tk.Button] = []
        for r in range(3):
            for c in range(3):
                idx = r*3 + c
                b = tk.Button(board_frame, text="", width=6, height=3,
                              command=lambda i=idx: self.place(i),
                              font=("Arial", 14, "bold"))
                b.grid(row=r, column=c, padx=4, pady=4)
                self.btns.append(b)

        self.lbl_status = tk.Label(self.root, text="", fg="blue")
        self.lbl_status.pack(pady=6)

        tk.Label(self.root, text="遊戲結束後按 Enter 回到房間。").pack(pady=4)

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

    def place(self, idx: int) -> None:
        if self.state.get("finished"):
            return
        if int(self.state.get("turn", 0)) != self.pid:
            return
        if self.state["board"][idx] != "":
            return
        try:
            send_json(self.sock, {"type": "action", "action": {"type": "place", "index": idx}})
        except Exception:
            self.lbl_status.config(text="Send failed (disconnected).")

    def ui_tick(self) -> None:
        while True:
            try:
                msg = self.q.get_nowait()
            except queue.Empty:
                break

            if msg.get("type") == "event":
                ev = msg.get("event", {})
                if ev.get("type") == "state":
                    self.state.update(ev)
                    self.render()
                elif ev.get("type") == "error":
                    self.lbl_status.config(text=f"[Error] {ev.get('message')}")
            elif msg.get("type") == "disconnect":
                self.lbl_status.config(text="Disconnected.")
                self.state["finished"] = True
                self.render()

        if self.running:
            self.root.after(50, self.ui_tick)

    def render(self) -> None:
        board = self.state["board"]
        for i in range(9):
            self.btns[i].config(text=board[i])

        turn = int(self.state.get("turn", 0))
        self.lbl_turn.config(text=f"Turn: Player {turn} ({'X' if turn==0 else 'O'})")

        last = self.state.get("last_move")
        if last:
            self.lbl_last.config(text=f"Last move: P{last['pid']} {last['symbol']} @ {last['index']}")
        else:
            self.lbl_last.config(text="Last move: -")

        finished = bool(self.state.get("finished"))
        if finished:
            w = self.state.get("winner")
            if w is None:
                text = "Draw! Press Enter to return."
            elif int(w) == self.pid:
                text = "🎉 You win! Press Enter to return."
            else:
                text = f"You lose. Winner: Player {w}. Press Enter to return."
            self.lbl_status.config(text=text)
            if not getattr(self, "_shown_over", False):
                self._shown_over = True
                try:
                    messagebox.showinfo("Game Over", text)
                except Exception:
                    pass
        else:
            self.lbl_status.config(text="")

        # 只有自己回合且未結束時可按
        my_turn = (turn == self.pid) and (not finished)
        for i in range(9):
            st = "normal" if (my_turn and board[i] == "") else "disabled"
            self.btns[i].config(state=st)

    def on_return(self) -> None:
        if self.state.get("finished"):
            self.on_close()

    def on_close(self) -> None:
        self.running = False
        try: self.sock.close()
        except Exception: pass
        try: self.root.destroy()
        except Exception: pass

    def mainloop(self) -> None:
        self.root.mainloop()

def run_client(ip: str, port: int) -> None:
    GuiClient(ip, port).mainloop()

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
    print("  python tictactoe_gui_2p.py --server <port>")
    print("  python tictactoe_gui_2p.py --client --connect <ip> <port>")

if __name__ == "__main__":
    main()
