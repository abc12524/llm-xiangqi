#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""求和（提议 / 同意 / 反对）。"""
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import xiangqi_server as X

passed, failed = [], []

def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    tail = ("  " + str(extra)) if (extra and not cond) else ""
    print(("PASS " if cond else "FAIL ") + name + tail)

def start(game):
    X.Handler.game = game
    srv = ThreadingHTTPServer(('127.0.0.1', 0), X.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]

def req(port, path, data=None):
    hdr = {"Content-Type": "application/json", "Accept": "application/json"}
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                               data=None if data is None else json.dumps(data).encode(),
                               headers=hdr)
    try:
        with urllib.request.urlopen(r, timeout=90) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, {}

def two_humans():
    g = X.Game(timeout=600, state_path=None)
    srv, port = start(g)
    _, a = req(port, "/join", {"name": "甲"})
    _, b = req(port, "/join", {"name": "乙"})
    return g, srv, port, a, b

print("=" * 56)
print("A. 双人局求和")
print("=" * 56)

g, srv, port, a, b = two_humans()
mover = a if g.turn == a["side"] else b          # 该走的一方
waiter = b if mover is a else a
check("A0 两人已开局", g.status == "playing", g.status)

st, r = req(port, "/draw", {"player": mover["player"]})
check("A1 提议求和", st == 200 and r.get("draw") == "offered", str(r)[:120])
check("A2 返回提议方", r.get("by") == mover["side"], r.get("by"))
check("A3 快照带 draw_offer", r.get("draw_offer") == mover["side"], r.get("draw_offer"))

st, s = req(port, f"/state?player={waiter['player']}")
check("A4 对手能看到求和", s.get("draw_offer") == mover["side"], str(s)[:120])

st, w = req(port, f"/wait?player={waiter['player']}&timeout=3")
check("A5 /wait 收到 draw_offer", w.get("event") == "draw_offer", str(w)[:140])

st, r2 = req(port, "/draw", {"player": mover["player"]})
check("A6 重复提议 → pending", r2.get("draw") == "pending", str(r2)[:100])

st, r3 = req(port, "/draw", {"player": mover["player"], "accept": True})
check("A7 提议方不能自己同意(400)", st == 400, f"{st} {r3}")

st, r4 = req(port, "/draw", {"player": waiter["player"], "accept": False})
check("A8 反对", st == 200 and r4.get("draw") == "declined", str(r4)[:120])
check("A9 反对后对局继续", r4.get("status") == "playing", r4.get("status"))
check("A10 反对后 draw_offer 清空", "draw_offer" not in r4, str(r4)[:120])

st, r5 = req(port, "/draw", {"player": waiter["player"], "accept": True})
check("A11 没人提议时 accept → 400", st == 400, f"{st} {r5}")

# 提议后有人落子 → 提议作废
req(port, "/draw", {"player": mover["player"]})
check("A12 提议已挂起", g.draw_offer == mover["side"], g.draw_offer)
mv = g.snapshot(you=mover["player"])["legal_moves"].split()[0]
req(port, "/move", {"player": mover["player"], "move": mv})
check("A13 一落子提议作废", g.draw_offer is None, g.draw_offer)

print()
print("=" * 56)
print("B. 同意和棋")
print("=" * 56)

g2, srv2, port2, a2, b2 = two_humans()
req(port2, "/draw", {"player": a2["player"]})
who2 = b2                                           # a2 提议, 由 b2 回应
st, r = req(port2, "/draw", {"player": who2["player"], "accept": True})
check("B1 同意后和棋", r.get("draw") == "agreed", str(r)[:120])
check("B2 status=draw", r.get("status") == "draw", r.get("status"))
check("B3 end_reason=agreement", r.get("end_reason") == "agreement", r.get("end_reason"))
st, s = req(port2, "/state")
check("B4 和棋后不能再走", s.get("status") == "draw", s.get("status"))
st, r = req(port2, "/move", {"player": a2["player"], "move": "h2e2"})
check("B5 和棋后走子 409", st == 409, f"{st} {r}")
st, r = req(port2, "/draw", {"player": a2["player"]})
check("B6 和棋后再求和 409", st == 409, f"{st} {r}")

print()
print("=" * 56)
print("C. 人机局 / 终局 / 存档")
print("=" * 56)

g3 = X.Game(timeout=600, state_path=None, engine_level="random")
srv3, port3 = start(g3)
_, j = req(port3, "/join", {"name": "挑战者", "vs": "engine", "level": "random"})
st, r = req(port3, "/draw", {"player": j["player"]})
check("C1 机器不接和棋", r.get("draw") == "declined" and r.get("reason") == "engine_declines",
      str(r)[:140])
check("C2 人机局仍在进行", r.get("status") == "playing", r.get("status"))
check("C3 没有留下悬着的提议", g3.draw_offer is None, g3.draw_offer)

g4, srv4, port4, a4, b4 = two_humans()
req(port4, "/resign", {"player": a4["player"]})
st, r = req(port4, "/draw", {"player": b4["player"]})
check("C4 终局后求和 409", st == 409, f"{st} {r}")

import tempfile
tmp = tempfile.mkdtemp(prefix="xq_draw_")
sp = os.path.join(tmp, "s.json")
g5 = X.Game(timeout=600, state_path=sp)
srv5, port5 = start(g5)
_, a5 = req(port5, "/join", {"name": "甲"})
_, b5 = req(port5, "/join", {"name": "乙"})
req(port5, "/draw", {"player": a5["player"]})
off_side = g5.draw_offer
g5b = X.Game(timeout=600, state_path=sp)          # 重启
check("C5 重启保留求和提议", g5b.draw_offer == off_side, g5b.draw_offer)
check("C6 重启后仍是进行中", g5b.status == "playing", g5b.status)

print()
print("=" * 56)
print(f"结果: {len(passed)} 通过, {len(failed)} 失败")
if failed:
    print("失败项: " + ", ".join(failed))
sys.exit(1 if failed else 0)
