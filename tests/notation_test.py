#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中文记谱 + 合法走法归属 + last_move + 长将判负 + 棋盘图例。"""
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

def mv(s):
    return X.str_to_move(s)

tmp = tempfile.mkdtemp(prefix="xq_nt_")

print("=" * 56)
print("A. 中文记谱")
print("=" * 56)

b0 = X.build_initial_board()
cases = [
    ('h2e2', '炮二平五'), ('b0c2', '马八进七'), ('h0g2', '马二进三'),
    ('i0h0', '车一平二'), ('a0b0', '车九平八'), ('b7b4', '炮2进3'),
    ('h9g7', '马8进7'), ('c3c4', '兵七进一'), ('c6c5', '卒3进1'),
    ('e0e1', '帅五进一'), ('e9e8', '将5进1'),
]
bad = [(s, X.move_notation(b0, mv(s)), want) for s, want in cases
       if X.move_notation(b0, mv(s)) != want]
check("A1 开局常见走法记谱正确", not bad, bad[:3])

# 斜行棋子(马/相/士): 进退后写目标列号
check("A2 马走目标列号", X.move_notation(b0, mv('b0c2')) == '马八进七')
check("A3 相走田字", X.move_notation(b0, mv('c0e2')) == '相七进五',
      X.move_notation(b0, mv('c0e2')))
check("A4 士上九宫", X.move_notation(b0, mv('d0e1')) == '仕六进五',
      X.move_notation(b0, mv('d0e1')))

# 同列两个同种棋子 → 前/后 代替列号
b2 = X.build_initial_board()
b2[4][0] = (X.RED, 'R')          # a4 再放一车
check("A5 同列双车用前/后", X.move_notation(b2, mv('a4a6')) == '前车进二',
      X.move_notation(b2, mv('a4a6')))
check("A6 后面那个车", X.move_notation(b2, mv('a0a1')) == '后车进一',
      X.move_notation(b2, mv('a0a1')))

# 三个同列兵 → 前/中/后
b3 = [[None] * 9 for _ in range(10)]
for y in (3, 4, 5):
    b3[y][2] = (X.RED, 'P')
check("A7 同列三兵用前/中/后",
      [X.move_notation(b3, mv(s)) for s in ('c5c6', 'c4c5', 'c3c4')]
      == ['前兵进一', '中兵进一', '后兵进一'],
      [X.move_notation(b3, mv(s)) for s in ('c5c6', 'c4c5', 'c3c4')])

check("A8 起点无子返回 None",
      X.move_notation(b0, mv('a5a6')) is None)

print()
print("=" * 56)
print("B. 合法走法归属（回归：以前给的是对手的着法）")
print("=" * 56)

g = X.Game(timeout=600, state_path=None, engine_level='random')
srv, port = start(g)
st, j = req(port, "/join", {"name": "挑战者", "vs": "engine", "level": "random"})
tok, side = j["player"], j["side"]
check("B0 人机局挑战者执红", side == "red", side)

st, s1 = req(port, f"/state?player={tok}")
check("B1 轮到我 → 有 legal_moves", st == 200 and 'legal_moves' in s1, str(s1)[:120])
check("B2 moves_for 指向我", s1.get("moves_for") == "red", s1.get("moves_for"))
red_set = set(X.move_to_str(m) for m in X.gen_legal_moves(g.board, 'r'))
check("B3 列表就是红方合法集", set(s1.get("legal_moves", "").split()) == red_set,
      len(s1.get("legal_moves", "").split()))

# 走一手(不等), 机器立刻应手 → 又轮到我; 中途抓一次"没轮到我"
before = req(port, f"/state?player={tok}")[1]["legal_moves"].split()[0]
st, ack = req(port, "/move", {"player": tok, "move": before})
check("B4 不带 wait 的回执带 last_move",
      ack.get("last_move", {}).get("by") == "black", str(ack)[:160])
check("B5 last_move 有中文记谱",
      bool(ack.get("last_move", {}).get("notation")), str(ack.get("last_move")))

# 观战(不带 player): 给当前回合方 + moves_for
st, s2 = req(port, "/state")
check("B6 观战给当前回合方并标注",
      s2.get("moves_for") == s2.get("turn") and 'legal_moves' in s2, str(s2)[:120])
turn_set = set(X.move_to_str(m) for m in X.gen_legal_moves(g.board, X.turn_code(g.turn)))
check("B7 观战列表属于 turn 方",
      set(s2.get("legal_moves", "").split()) == turn_set)

# 双人局: 没轮到我的那次查询不能给走法
g2 = X.Game(timeout=600, state_path=None)
srv2, port2 = start(g2)
_, ja = req(port2, "/join", {"name": "甲"})
_, jb = req(port2, "/join", {"name": "乙"})
cur_tok = ja["player"] if g2.turn == ja["side"] else jb["player"]
other_tok = jb["player"] if cur_tok == ja["player"] else ja["player"]
st, sm = req(port2, f"/state?player={other_tok}")
check("B8 没轮到我 → 不给 legal_moves", 'legal_moves' not in sm, str(sm)[:140])
check("B9 you.is_turn 为 false", sm.get("you", {}).get("is_turn") is False, str(sm.get("you")))
st, sc = req(port2, f"/state?player={cur_tok}")
check("B10 轮到我就给", 'legal_moves' in sc and sc.get("moves_for") == g2.turn,
      str(sc)[:140])

print()
print("=" * 56)
print("C. 棋盘图例")
print("=" * 56)

g6 = X.Game(timeout=600, state_path=None)
srv6, port6 = start(g6)
st, j2 = req(port6, "/join", {"name": "丙"})
legend = j2.get("board_legend", "")
check("C1 join 返回图例", bool(legend), str(j2)[:120])
check("C2 图例说明大小写", "大写" in legend and "小写" in legend, legend[:60])
check("C3 图例说明行号方向", "行 0" in legend and "下方" in legend, legend[:80])
check("C4 图例给出坐标示例", "h2e2" in legend, legend[:80])

print()
print("=" * 56)
print("D. 长将判负")
print("=" * 56)

def bare():
    b = [[None] * 9 for _ in range(10)]
    b[0][4] = (X.RED, 'K')      # 红帅 e0
    b[9][3] = (X.BLACK, 'K')    # 黑将 d9
    b[5][4] = (X.BLACK, 'R')    # 黑车 e5, 一直照帅
    return b

def fresh(**kw):
    gg = X.Game(timeout=600, state_path=None, **kw)
    gg.board = bare()
    gg.turn = 'red'
    gg.status = 'playing'
    gg.history = []
    gg.pos_history = [gg._pos_key()]
    return gg

gd = fresh()
loop = ['e0f0', 'e5f5', 'f0e0', 'f5e5']
for k in range(2):
    for m in loop:
        if gd.status != 'playing':
            break
        gd._do_move(m)
check("D1 第三次重复 → 长将判负", gd.status == 'red_win', gd.status)
check("D2 end_reason=perpetual_check", gd.end_reason == 'perpetual_check', gd.end_reason)
check("D3 判的是长将的一方(黑)", gd.perpetual == 'black', gd.perpetual)
check("D4 长将方确实每手将军",
      all(r.get('check') for r in gd.history if r['by'] == 'black'), str(gd.history[-1]))
check("D5 守方没被误判",
      all(not r.get('check') for r in gd.history if r['by'] == 'red'))

snap = gd.snapshot()
check("D6 快照给出 perpetual_check", snap.get("perpetual_check") == 'black', str(snap)[:120])

# 重复但都不将军 → 不判
gd2 = X.Game(timeout=600, state_path=None)
bb = [[None] * 9 for _ in range(10)]
bb[0][4] = (X.RED, 'K')
bb[9][3] = (X.BLACK, 'K')
bb[5][0] = (X.BLACK, 'R')      # 黑车在 a5, 来回挪但不将军
gd2.board = bb
gd2.turn = 'red'
gd2.status = 'playing'
gd2.history = []
gd2.pos_history = [gd2._pos_key()]
for m in ['e0f0', 'a5a4', 'f0e0', 'a4a5'] * 2:
    gd2._do_move(m)
check("D7 单纯重复(无将军)不判负", gd2.status == 'playing', gd2.status)

# 重启后仍能判(局面历史按棋谱重建)
sp = os.path.join(tmp, "perp.json")
gd3 = X.Game(timeout=600, state_path=sp)
gd3.board = bare()
gd3.turn = 'red'
gd3.status = 'playing'
gd3.history = []
gd3.pos_history = [gd3._pos_key()]
for m in loop:
    gd3._do_move(m)
g4 = X.Game(timeout=600, state_path=sp)          # 重启
check("D8 重启恢复局面历史", len(g4.pos_history) == 5, len(g4.pos_history))
for m in loop:
    if g4.status != 'playing':
        break
    g4._do_move(m)
check("D9 重启后长将仍能判", g4.end_reason == 'perpetual_check', g4.end_reason)

print()
print("=" * 56)
print("E. 棋谱中文记谱")
print("=" * 56)

g5 = X.Game(timeout=600, state_path=None)
srv5, port5 = start(g5)
_, j5a = req(port5, "/join", {"name": "甲"})
_, j5b = req(port5, "/join", {"name": "乙"})
tok5 = j5a["player"] if g5.turn == j5a["side"] else j5b["player"]
mv5 = req(port5, f"/state?player={tok5}")[1]["legal_moves"].split()[0]
req(port5, "/move", {"player": tok5, "move": mv5})
h = req(port5, "/history")[1]
check("E1 新记录带 notation", bool(h[0].get("notation")), str(h[0]))
check("E2 新记录带 check 字段时是 true", 'check' not in h[0] or h[0]['check'] is True, str(h[0]))

# 老记录(没有 notation/check) → 输出时补全
for r in g5.history:
    r.pop('notation', None)
    r.pop('check', None)
h2 = req(port5, "/history")[1]
check("E3 老棋谱输出时补记谱", bool(h2[0].get("notation")), str(h2[0]))
check("E4 补的记谱与原走法一致",
      h2[0]["notation"] == X.move_notation(X.build_initial_board(), X.str_to_move(h2[0]["move"])),
      h2[0].get("notation"))

print()
print("=" * 56)
print(f"结果: {len(passed)} 通过, {len(failed)} 失败")
if failed:
    print("失败项: " + ", ".join(failed))
sys.exit(1 if failed else 0)
