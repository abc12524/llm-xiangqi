#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回放页自动刷新: /watch 长轮询(有变化立即返回, 无变化按 timeout 返回) + 页面引用检查。"""
import json
import os
import sys
import threading
import time
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

def req(port, path, data=None, timeout=60):
    hdr = {"Content-Type": "application/json", "Accept": "application/json"}
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                               data=None if data is None else json.dumps(data).encode(),
                               headers=hdr)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read())

print("=" * 56)
print("A. /watch 长轮询")
print("=" * 56)

g = X.Game(timeout=600, state_path=None)
srv, port = start(g)
a = req(port, "/join", {"name": "甲"})
b = req(port, "/join", {"name": "乙"})
mover = a if g.turn == a["side"] else b

# 1) 有变化: 立刻返回
box = {}
def watch(payload=None):
    t0 = time.monotonic()
    try:
        box["r"] = req(port, "/watch?since=0&st=playing&timeout=8")
    except Exception as e:                      # noqa: BLE001
        box["err"] = str(e)
    box["dt"] = time.monotonic() - t0

th = threading.Thread(target=watch)
th.start()
time.sleep(0.4)
mv = g.snapshot(you=mover["player"])["legal_moves"].split()[0]
req(port, "/move", {"player": mover["player"], "move": mv})
th.join(10)
check("A1 落子后立即唤醒", box.get("r", {}).get("changed") is True, box)
check("A2 唤醒延迟 < 1.5s", box.get("dt", 99) < 1.5, "%.2fs" % box.get("dt", 99))
check("A3 返回最新手数", box.get("r", {}).get("ply") == 1, box.get("r"))

# 2) 无变化: 按 timeout 返回
t0 = time.monotonic()
r = req(port, "/watch?since=1&st=playing&timeout=1.5")
dt = time.monotonic() - t0
check("A4 无变化时超时返回", r.get("changed") is False and 1.2 <= dt <= 3.0,
      f"{r} {dt:.2f}s")

# 3) 终局也算变化
box2 = {}
def watch2():
    t0 = time.monotonic()
    box2["r"] = req(port, "/watch?since=1&st=playing&timeout=8")
    box2["dt"] = time.monotonic() - t0

th2 = threading.Thread(target=watch2)
th2.start()
time.sleep(0.3)
req(port, "/resign", {"player": mover["player"]})
th2.join(10)
check("A5 认输(状态变化)也唤醒", box2.get("r", {}).get("changed") is True, box2)
check("A6 返回终局状态", box2.get("r", {}).get("status") in ("red_win", "black_win"),
      box2.get("r"))

print()
print("=" * 56)
print("B. 回放页脚本")
print("=" * 56)

meta = {"status": "playing", "turn": "red", "remaining_ms": 60000, "end_reason": None,
        "players": [{"side": "red", "name": "甲"}, {"side": "black", "name": "乙"}],
        "auto_side": None, "engine_level": "hard", "step_s": 60, "engine_step_s": 600,
        "gid": "x", "is_current": True, "games": []}
html = X.build_history_html([], meta)
check("B1 页面用 /watch 驱动", "/watch?since=" in html)
check("B2 不再固定 4 秒轮询", "setInterval(poll" not in html)
check("B3 保留兜底轮询", "setInterval(sync" in html)
check("B4 历史对局不跟随", "is_current" in html and "if (!M.is_current) return;" in html)
check("B5 切回前台补一次同步", "visibilitychange" in html)

print()
print("=" * 56)
print(f"结果: {len(passed)} 通过, {len(failed)} 失败")
if failed:
    print("失败项: " + ", ".join(failed))
sys.exit(1 if failed else 0)
