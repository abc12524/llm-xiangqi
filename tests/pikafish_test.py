#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pikafish 接入层测试：FEN 正确性、引擎缺失时的回落、难度切换。"""
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
import pikafish_uci as P

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
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)

print("=" * 56)
print("A. 棋盘 → FEN")
print("=" * 56)

b = X.build_initial_board()
fen = P.board_to_fen(b, 'red')
want = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"
check("A1 初始局面 FEN 标准", fen == want, fen)
check("A2 黑先走时 w→b", P.board_to_fen(b, 'black').endswith("b - - 0 1"))

# 炮二平五后
m = X.str_to_move('h2e2')
b2 = X.make_move(b, *m)
fen2 = P.board_to_fen(b2, 'black')
check("A3 走子后 FEN 反映落点", fen2 == "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C2C4/9/RNBAKABNR b - - 0 1", fen2)
check("A4 十行齐全", fen2.split(' ')[0].count('/') == 9, fen2)
check("A5 每行 9 列", all(sum(int(c) if c.isdigit() else 1 for c in row) == 9
                          for row in fen2.split(' ')[0].split('/')))
# 空行用 9 表示
check("A6 空行压缩成 9", '/9/' in fen)

print("=" * 56)
print("B. 引擎不可用时回落")
print("=" * 56)

tmp = tempfile.mkdtemp(prefix="xq_pf_")
ok = P.configure('/nonexistent/pikafish-binary')
check("B1 二进制不存在 → configure False", ok is False)
check("B2 is_available False", P.is_available() is False)
check("B3 best_move 返回 None 不抛", P.best_move(b, 'red', 'hard') is None)

g = X.Game(timeout=60, state_path=os.path.join(tmp, "g1.json"),
           engine_level='hard')
check("B4 人机档位被接受", g.engine_level == 'hard', g.engine_level)
srv, port = start(g)
with g.cond:
    tok, side, _ = g.join('甲', vs='engine', level='hard')
    g.auto_play()
    check("B5 无引擎也能开局", g.status == 'playing', g.status)
    first = g.snapshot(you=tok)['legal_moves'].split()[0]
# 锁必须放掉, 否则 HTTP 线程拿不到 cond 会超时
st, _ = post(port, "/move", {"player": tok, "move": first})
check("B6 玩家落子成功", st == 200, st)
check("B7 最后一手是机器走的", g.history and g.history[-1]['by'] == g.auto_side,
      g.history[-1] if g.history else None)
last = g.history[-1]
replay, ok = X.build_initial_board(), True
for r in g.history:
    mv = X.str_to_move(r['move'])
    if mv not in X.gen_legal_moves(replay, X.turn_code(r['by'])):
        ok = False
        break
    replay = X.make_move(replay, *mv)
check("B8 整局走法逐手合法", ok and replay == g.board, len(g.history))
srv.shutdown()

print("=" * 56)
print("C. 难度切换")
print("=" * 56)

g2 = X.Game(timeout=60, state_path=os.path.join(tmp, "g2.json"), engine_level='greedy')
srv2, port2 = start(g2)
with g2.cond:
    g2.join('乙', vs='engine', level='normal')
    check("C1 join 指定 normal", g2.engine_level == 'normal', g2.engine_level)
    g2.join('乙', vs='engine', level='max')
    check("C2 同局再 join 能换难度", g2.engine_level == 'max', g2.engine_level)
    g2.join('乙', vs='engine', level='不存在的档')
    check("C3 非法档位不改动", g2.engine_level == 'max', g2.engine_level)
    g2.reset(keep_players=True, save=False)
    g2.join('乙', vs='engine', level='easy')
    check("C4 reset 后换难度仍生效", g2.engine_level == 'easy', g2.engine_level)
srv2.shutdown()

print("=" * 56)
print("D. 档位定义")
print("=" * 56)

check("D1 Pikafish 四档齐全", P.LEVELS == ('easy', 'normal', 'hard', 'max'), P.LEVELS)
check("D2 server 全部档位合并", set(X.ALL_LEVELS) == set(P.LEVELS) | set(X.xiangqi_engine.LEVELS),
      X.ALL_LEVELS)
check("D3 默认档在合法集合里", X.DEFAULT_LEVEL in X.ALL_LEVELS, X.DEFAULT_LEVEL)
check("D4 每档都有参数", all(lv in P.DIFFICULTY for lv in P.LEVELS))
check("D5 max 比 hard 慢",
      P.DIFFICULTY['max'][1] > P.DIFFICULTY['hard'][1], P.DIFFICULTY)

print()
print("=" * 56)
print("结果: %d 通过, %d 失败" % (len(passed), len(failed)))
if failed:
    print("失败项:", failed)
sys.exit(1 if failed else 0)
