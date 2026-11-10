#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证人机模式：本地引擎对手（0 token），三档强度，可持久化。"""
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import xiangqi_server as X
import xiangqi_engine as E

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

def post(port, path, data):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)

def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=30) as r:
        return json.load(r)

tmp = tempfile.mkdtemp(prefix="xq_eng_")

print("=" * 56)
print("A. 人机开局")
print("=" * 56)

g = X.Game(timeout=60, state_path=os.path.join(tmp, "e1.json"))
srv, port = start(g)
try:
    st, j = post(port, "/join", {"name": "我", "vs": "engine", "level": "greedy"})
    check("A1 一次 join 直接开局", j["status"] == "playing", j["status"])
    check("A1 挑战者执红, 机器执黑",
          j["side"] == "red" and g.auto_side == "black",
          f"me={j['side']} engine={g.auto_side}")
    check("A1 难度已设置", g.engine_level == "greedy", g.engine_level)
    check("A1 返回 resumed=false", j["resumed"] is False)

    h = get(port, "/history")
    check("A2 红先行: 机器执黑不抢走", len(h) == 0, str(h))
    check("A2 首手轮到挑战者", j["turn"] == "red", str(j["turn"]))

    # 玩家走一步, 机器必须立刻应手(取当前合法步, 不假设自己是哪一方)
    legal = get(port, "/state")["legal_moves"].split()
    st, s = post(port, "/move", {"player": j["player"], "move": legal[0]})
    check("A3 玩家走子成功", st == 200, f"HTTP {st} {s}")
    h2 = get(port, "/history")
    check("A3 机器自动应手(记录 +2)", len(h2) == len(h) + 2,
          f"{len(h)} -> {len(h2)}")
    check("A3 机器那手是对方走的", h2[-1]["by"] == g.auto_side, str(h2[-1]))

    # 机器走的一定是合法步(能落盘就说明通过了 gen_legal_moves 校验)
    check("A4 机器走法全部合法", all(r["move"] for r in h2), "有空走法")

    # 逐步走 8 手, 看机器不会卡死
    ok = True
    for i in range(8):
        s = get(port, "/state")
        if s["status"] != "playing":
            break
        mv = s["legal_moves"].split()[0]
        st, _ = post(port, "/move", {"player": j["player"], "move": mv})
        if st != 200:
            ok = False
            break
    check("A5 连续 8 手对弈不卡死", ok and get(port, "/state")["status"] in
          ("playing", "red_win", "black_win"), str(ok))

    # 人机模式下第三人仍被拒
    st, _ = post(port, "/join", {"name": "路人"})
    check("A6 人机局第三人 409", st == 409, str(st))
finally:
    srv.shutdown()

print()
print("=" * 56)
print("B. 三档强度")
print("=" * 56)

for lv in ("random", "greedy", "depth2"):
    g = X.Game(timeout=60, state_path=os.path.join(tmp, f"e_{lv}.json"))
    srv, port = start(g)
    try:
        st, j = post(port, "/join", {"name": "我", "vs": "engine", "level": lv})
        h = get(port, "/history")
        check(f"B {lv:7s} 能开局并走棋", j["status"] == "playing" and
              (len(h) >= 1 or g.auto_side == "black"), f"{j['status']} h={len(h)}")
    finally:
        srv.shutdown()

print()
print("=" * 56)
print("C. 引擎单元")
print("=" * 56)

g = X.Game(timeout=0, state_path=None)
moves = X.gen_legal_moves(g.board, "r")
check("C1 开局 44 步", len(moves) == 44, str(len(moves)))
for lv in ("random", "greedy", "depth2"):
    mv = E.choose_move(g.board, "r", moves, lv, legal_fn=X.gen_legal_moves)
    check(f"C1 {lv:7s} 返回合法走法", mv in moves, str(mv))

# 空走法列表
check("C2 无合法走法返回 None", E.choose_move(g.board, "r", [], "greedy") is None)

# 吃子机会必须抓住: 造一个红车能白吃黑马的局面
b = [[None] * 9 for _ in range(10)]
b[0][4] = ('r', 'K'); b[9][3] = ('b', 'K')      # 黑将放 d9, 避免与红帅照面
b[0][0] = ('r', 'R')
b[9][0] = ('b', 'N')          # 红车 a0 正对黑马 a9, 中间无子
ms = X.gen_legal_moves(b, 'r')
mv = E.choose_move(b, 'r', ms, 'greedy')
check("C3 greedy 会吃掉白送的马", mv == (0, 0, 0, 9), X.move_to_str(mv))

# 三档在真实开局都能选出行棋
for lv in ("greedy", "depth2"):
    mv = E.choose_move(g.board, 'r', moves, lv, legal_fn=X.gen_legal_moves)
    check(f"C4 {lv:7s} 开局不乱走(在合法集内)", mv in moves)

print()
print("=" * 56)
print("D. 人机模式可持久化")
print("=" * 56)

sp = os.path.join(tmp, "e_persist.json")
g = X.Game(timeout=60, state_path=sp, engine_level="depth2")
g.join("我", None, "engine", "depth2")
check("D1 人机局已落盘", os.path.exists(sp))
g2 = X.Game(timeout=60, state_path=sp)          # 模拟重启
check("D1 重启后机器仍在座", g2.auto_side == g.auto_side,
      f"{g2.auto_side} vs {g.auto_side}")
check("D1 重启后难度保留", g2.engine_level == "depth2", g2.engine_level)

print()
print("=" * 56)
print("E. 固定席位: 重开 / 占座 / 老存档")
print("=" * 56)

g = X.Game(timeout=60, state_path=None, engine_level="greedy")
tok, side, _ = g.join("我", None, "engine", "greedy")
check("E1 开局挑战者执红", side == "red" and g.auto_side == "black",
      f"{side}/{g.auto_side}")
g.reset(keep_players=True)
check("E2 重开后仍是挑战者执红",
      g.players.get(tok) == "red" and g.auto_side == "black",
      f"{g.players.get(tok)}/{g.auto_side}")
check("E3 重开后红先行", g.turn == "red", g.turn)

# 桌上已有真人时, vs=engine 不能占座(否则一人一机挤一人, 席位冲突)
g2 = X.Game(timeout=60, state_path=None)
g2.join("甲", None, None, None)
try:
    g2.join("乙", None, "engine", "greedy")
    ok = False
except X.TableFull:
    ok = True
check("E4 有人占座时 vs=engine 被拒", ok)

# 老存档可能是机器执红(改设定前的), 重启后要纠正成机器执黑
sp2 = os.path.join(tmp, "e_legacy.json")
legacy = {
    "players": {"p1": "black"}, "names": {"p1": "旧玩家"}, "pid": 1,
    "board": X._board_to_json(X.build_initial_board()), "turn": "black",
    "status": "playing", "history": [], "auto_side": "red",
    "engine_level": "greedy",
}
with open(sp2, "w", encoding="utf-8") as f:
    json.dump(legacy, f, ensure_ascii=False)
g3 = X.Game(timeout=60, state_path=sp2)
check("E5 进行中的老存档纠正为机器执黑/挑战者执红",
      g3.auto_side == "black" and g3.players == {"p1": "red"},
      f"{g3.auto_side} {g3.players}")

# 已结束的老存档保持原样(否则棋谱里的红黑会被说反)
legacy["status"] = "black_win"
legacy["end_reason"] = "timeout"
with open(sp2, "w", encoding="utf-8") as f:
    json.dump(legacy, f, ensure_ascii=False)
g4 = X.Game(timeout=60, state_path=sp2)
check("E6 已结束的老对局不改席位", g4.auto_side == "red" and
      g4.players == {"p1": "black"}, f"{g4.auto_side} {g4.players}")

# 上一局已结束 → 直接 vs=engine 自动清桌开新局, 不用手动 reset
tok4, side4, _ = g4.join("新人", None, "engine", "greedy")
check("E7 旧局结束后可直接开人机新局",
      side4 == "red" and g4.auto_side == "black" and g4.status == "playing"
      and g4.turn == "red", f"{side4}/{g4.auto_side}/{g4.status}/{g4.turn}")
check("E8 新局棋盘已清空", len(g4.history) == 0, str(len(g4.history)))

# 已有人机局时, 不带 vs 的人也只能坐挑战者位(红); 红已有人 → 拒绝
try:
    g4.join("插队", None, None, None)
    ok = False
except X.TableFull:
    ok = True
check("E9 人机局红位已占时拒绝新玩家", ok)

# clear 重开把机器席位一起清掉, 回到双人 waiting
g4.reset(keep_players=False)
check("E10 clear 后机器离座", g4.auto_side is None and not g4.players
      and g4.status == "waiting", f"{g4.auto_side} {g4.players} {g4.status}")
tok5, side5, _ = g4.join("甲", None, None, None)
g4.join("乙", None, None, None)
check("E11 清桌后恢复正常双人随机分边",
      side5 in ("red", "black") and set(g4.players.values()) == {"red", "black"}
      and g4.status == "playing", f"{g4.players} {g4.status}")

print()
print("=" * 56)
print(f"结果: {len(passed)} 通过, {len(failed)} 失败")
if failed:
    print("失败项: " + ", ".join(failed))
sys.exit(1 if failed else 0)
