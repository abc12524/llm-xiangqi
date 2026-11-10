#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对照中国象棋规则，逐条验证 xiangqi_server.py 的走法生成与合法性判定。"""
import json
import os
import sys
import tempfile
import time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))

import xiangqi_server as X

RED, BLACK = X.RED, X.BLACK
passed, failed = [], []

def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("PASS " if cond else "FAIL ") + name + (("  " + extra) if extra else ""))

def empty():
    return [[None] * 9 for _ in range(10)]

def put(b, x, y, color, t):
    b[y][x] = (color, t)

def legal(board, color):
    return set(X.move_to_str(m) for m in X.gen_legal_moves(board, color))

def new_game(timeout=60, log_path=None):
    """开局并返回 (game, {side: token})"""
    g = X.Game(timeout=timeout, log_path=log_path)
    t1, s1, _ = g.join()
    t2, s2, _ = g.join()
    return g, {s1: t1, s2: t2}

def tokens(g):
    return {side: tok for tok, side in g.players.items()}

# ---------- 1. 初始局面走法数：标准答案 44 ----------
g = X.Game()
red_moves = X.gen_legal_moves(g.board, RED)
check("初始局面红方合法走法 = 44", len(red_moves) == 44, f"实际 {len(red_moves)}")
check("初始局面黑方合法走法 = 44", len(X.gen_legal_moves(g.board, BLACK)) == 44)

# ---------- 2. 各兵种初始走法数（逐子核对） ----------
def piece_moves(board, x, y):
    p = board[y][x]
    return set(X.move_to_str(m) for m in X.GENERATORS[p[1]](board, p[0], x, y))

b = X.build_initial_board()
check("左车 (a0) 2 步", len(piece_moves(b, 0, 0)) == 2, str(sorted(piece_moves(b, 0, 0))))
check("左马 (b0) 2 步", len(piece_moves(b, 1, 0)) == 2, str(sorted(piece_moves(b, 1, 0))))
check("左相 (c0) 2 步", len(piece_moves(b, 2, 0)) == 2, str(sorted(piece_moves(b, 2, 0))))
check("左士 (d0) 1 步(仕角)", len(piece_moves(b, 3, 0)) == 1, str(sorted(piece_moves(b, 3, 0))))
check("帅 (e0) 1 步", len(piece_moves(b, 4, 0)) == 1, str(sorted(piece_moves(b, 4, 0))))
check("左炮 (b2) 12 步", len(piece_moves(b, 1, 2)) == 12, f"实际 {len(piece_moves(b, 1, 2))}")
check("兵 (a3) 1 步", len(piece_moves(b, 0, 3)) == 1)

# ---------- 3. 马：蹩马腿 ----------
b = empty(); put(b, 4, 0, RED, 'K'); put(b, 4, 9, BLACK, 'K')
put(b, 4, 4, RED, 'N')
m = piece_moves(b, 4, 4)
check("空盘马 (e4) 8 步", len(m) == 8, str(len(m)))
b2 = [r[:] for r in b]; put(b2, 4, 5, RED, 'P')          # 蹩上方马腿
m2 = piece_moves(b2, 4, 4)
check("蹩马腿：上方两方向被封", len(m2) == 6, str(sorted(m)))
check("蹩马腿后不含 e4f6/e4d6", "e4f6" not in m2 and "e4d6" not in m2)

# ---------- 4. 相/象：塞象眼 与 不过河 ----------
b = empty(); put(b, 4, 0, RED, 'K'); put(b, 4, 9, BLACK, 'K')
put(b, 2, 0, RED, 'B')
check("相 (c0) 2 步: a2/e2", piece_moves(b, 2, 0) == {"c0a2", "c0e2"}, str(sorted(piece_moves(b, 2, 0))))
b2 = [r[:] for r in b]; put(b2, 1, 1, RED, 'P')          # 塞左象眼(x+dx//2, y+dy//2)=(1,1)
check("塞象眼：c0a2 被封", "c0a2" not in piece_moves(b2, 2, 0))
check("塞左眼不影响右路 c0e2", "c0e2" in piece_moves(b2, 2, 0))
b3 = empty(); put(b3, 4, 0, RED, 'K'); put(b3, 4, 9, BLACK, 'K'); put(b3, 4, 4, RED, 'B')
nm = piece_moves(b3, 4, 4)
check("相在河界(e4)不能过河", all(int(s[2:4][1]) <= 4 for s in nm), str(sorted(nm)))

# ---------- 5. 士/帅：不出九宫 ----------
b = empty(); put(b, 4, 0, RED, 'K'); put(b, 4, 9, BLACK, 'K'); put(b, 4, 1, RED, 'A')
check("士 (e1) 4 个斜角", len(piece_moves(b, 4, 1)) == 4, str(sorted(piece_moves(b, 4, 1))))
b = empty(); put(b, 3, 2, RED, 'K'); put(b, 4, 9, BLACK, 'K')
km = piece_moves(b, 3, 2)
check("帅在九宫角(d2)只在宫内", km == {"d2e2", "d2d1"}, str(sorted(km)))
check("帅不能出九宫(d2d3 不存在)", "d2d3" not in km)

# ---------- 6. 炮：隔一子吃 / 不隔子不能吃 ----------
b = empty(); put(b, 4, 0, RED, 'K'); put(b, 4, 9, BLACK, 'K')
put(b, 0, 5, RED, 'C'); put(b, 0, 7, BLACK, 'P')       # 直接相邻遮挡 -> 不能吃
check("炮紧邻目标不能吃", "a5a7" not in piece_moves(b, 0, 5))
b2 = [r[:] for r in b]; put(b2, 0, 6, BLACK, 'P')       # 中间加炮架
check("炮隔一子可吃 a5a7", "a5a7" in piece_moves(b2, 0, 5))
b3 = [r[:] for r in b]
put(b3, 0, 6, BLACK, 'P'); put(b3, 0, 7, BLACK, 'P'); put(b3, 0, 8, BLACK, 'P')
check("炮隔一子(a6)可吃最近的 a7", "a5a7" in piece_moves(b3, 0, 5))
check("炮不能越子打 a8(隔了两个子)", "a5a8" not in piece_moves(b3, 0, 5))

# ---------- 7. 兵/卒：过河横走 ----------
b = empty(); put(b, 4, 0, RED, 'K'); put(b, 4, 9, BLACK, 'K'); put(b, 0, 4, RED, 'P')
check("兵未过河(a4)只能前进", piece_moves(b, 0, 4) == {"a4a5"}, str(sorted(piece_moves(b, 0, 4))))
b = empty(); put(b, 4, 0, RED, 'K'); put(b, 4, 9, BLACK, 'K'); put(b, 0, 5, RED, 'P')
check("兵过河(a5)可横走+前进", piece_moves(b, 0, 5) == {"a5a6", "a5b5"}, str(sorted(piece_moves(b, 0, 5))))
b = empty(); put(b, 4, 0, RED, 'K'); put(b, 4, 9, BLACK, 'K'); put(b, 0, 5, BLACK, 'P')
check("黑卒未过河(a5)只能前进", piece_moves(b, 0, 5) == {"a5a4"}, str(sorted(piece_moves(b, 0, 5))))
b = empty(); put(b, 4, 0, RED, 'K'); put(b, 4, 9, BLACK, 'K'); put(b, 0, 4, BLACK, 'P')
check("黑卒过河(a4)可横走+前进", piece_moves(b, 0, 4) == {"a4a3", "a4b4"}, str(sorted(piece_moves(b, 0, 4))))
b = empty(); put(b, 4, 0, RED, 'K'); put(b, 4, 9, BLACK, 'K'); put(b, 0, 3, BLACK, 'P')
check("黑卒在 a3(已过河)可横走", piece_moves(b, 0, 3) == {"a3a2", "a3b3"}, str(sorted(piece_moves(b, 0, 3))))
b = empty(); put(b, 4, 0, RED, 'K'); put(b, 4, 9, BLACK, 'K'); put(b, 0, 0, BLACK, 'P')
check("卒到红方底线(a0)不能再前进, 只能横走", piece_moves(b, 0, 0) == {"a0b0"}, str(sorted(piece_moves(b, 0, 0))))

# ---------- 8. 将帅照面（飞将） ----------
b = empty(); put(b, 4, 0, RED, 'K'); put(b, 4, 9, BLACK, 'K')
check("帅将同列中间无子 -> kings_face True", X.kings_face(b))
check("帅不能走到照面位置(e0e1 也不合法)", "e0e1" not in legal(b, RED))
b2 = empty(); put(b, 4, 0, RED, 'K'); put(b, 4, 9, BLACK, 'K'); put(b, 4, 5, RED, 'P')
check("中间有子 -> kings_face False", not X.kings_face(b2))

# ---------- 9. 送将不合法（真正的牵制：黑车与红帅同列） ----------
b = empty(); put(b, 4, 9, BLACK, 'K')                  # 黑王 e9，与红帅同列但被黑车隔开
put(b, 4, 0, RED, 'K'); put(b, 4, 5, BLACK, 'R')       # 红帅 e0 与黑车 e5 同列
lm = legal(b, RED)
check("被牵制：帅不能走进黑车控制列", "e0e1" not in lm, str(sorted(lm)))
check("被牵制：帅可横移至 f0", "e0f0" in lm, str(sorted(lm)))
check("被牵制：帅可左移至 d0", "e0d0" in lm, str(sorted(lm)))

# ---------- 10. 将死判定 ----------
b = empty()
put(b, 4, 0, RED, 'K')
put(b, 4, 9, BLACK, 'K'); put(b, 3, 9, BLACK, 'R'); put(b, 5, 9, BLACK, 'R')
check("双车绝杀: 红方无合法走法", len(X.gen_legal_moves(b, RED)) == 0)

# ---------- 11. 一轮实际对局 ----------
g, tok = new_game()
seq = ["c3c4", "c6c5", "b2b9", "a9a8", "b0c2"]  # 含吃子
ok = True
for s in seq:
    try:
        g.apply_move(tok[g.turn], s)
    except X.GameError as e:
        ok = False; print("  move failed:", s, e); break
check("走子序列可执行", ok)
if ok:
    check("吃子记录正确(b2b9 吃黑马)", any(h["captured"] == 'n' for h in g.history),
          str([h["captured"] for h in g.history]))
    check("轮次正确切换", g.turn == 'black', g.turn)

# ---------- 12. 非法走法拦截 ----------
g, tok = new_game()
bad_list = ["a0a4", "e0e2", "i0i3", "b0b1", "zzzz", "a0", ""]
rejected = 0
for bad in bad_list:
    try:
        g.apply_move(tok['red'], bad)
    except X.GameError:
        rejected += 1
check("非法/畸形走法全部被拒", rejected == len(bad_list), f"{rejected}/{len(bad_list)}")

# ---------- 13. 红黑随机分配 / 红先行 ----------
sides_seen = set()
for _ in range(60):
    g2 = X.Game()
    t1, s1, _ = g2.join()
    t2, s2, _ = g2.join()
    sides_seen.add(s1)
    if s1 == s2:
        break
check("红黑随机分配(两种执红情况都出现)", sides_seen == {'red', 'black'}, str(sides_seen))
check("两人到齐即开局", g2.status == 'playing', g2.status)
check("开局红先行", g2.turn == 'red', g2.turn)

# ---------- 14. 只有执棋方能走 ----------
g3, tk = new_game()
try:
    g3.apply_move(tk['black'], "h7e7")
    check("非执棋方走子被拒", False, "竟然走成了")
except X.NotYourTurn:
    check("非执棋方走子被拒", True)
try:
    X.Game().apply_move("pX", "h2e2")
    check("未加入者走子被拒", False)
except X.NotJoined:
    check("未加入者走子被拒", True)

# ---------- 15. 满员 ----------
g4, _tk4 = new_game()
try:
    g4.join()
    check("第三人加入被拒", False)
except X.TableFull:
    check("第三人加入被拒", True)

# ---------- 16. 超时判负 ----------
g5 = X.Game(timeout=0.15)
g5.join(); g5.join()
time.sleep(0.25)
snap = g5.snapshot()
check("超时判负", snap.get('end_reason') == 'timeout', str(snap.get('end_reason')))
check("红方未走判负=黑胜", snap['status'] == 'black_win', snap['status'])

g6, tk6 = new_game(timeout=60)
g6.apply_move(tk6['red'], "h2e2")
check("走子后计时重置给下一方", g6.remaining_ms() > 55000, f"{g6.remaining_ms()}ms")
check("正常走子不误判超时", g6.status == 'playing', g6.status)

# ---------- 17. 快照只含棋局 ----------
keys = set(g6.snapshot().keys())
allowed = {'status', 'turn', 'in_check', 'board', 'legal_moves', 'moves_for',
           'last_move', 'remaining_ms', 'end_reason', 'timed_out',
           'resigned', 'perpetual_check'}
check("快照只含棋局字段", keys <= allowed, str(sorted(keys)))
check("快照必含 remaining_ms", 'remaining_ms' in keys, str(sorted(keys)))
check("快照不含 fen/players/clock/history",
      not (keys & {'fen', 'players', 'clock', 'history', 'you', 'ply', 'legend'}),
      str(sorted(keys)))

# ---------- 18. 走棋记录含时间 ----------
rec = g6.history[0]
check("记录含 move/by/ply", (rec['ply'], rec['by'], rec['move']) == (1, 'red', 'h2e2'), str(rec))
check("记录含 at 时间戳", isinstance(rec.get('at'), str) and 'T' in rec['at'], rec.get('at'))
check("记录含 elapsed_ms", isinstance(rec.get('elapsed_ms'), int), str(rec.get('elapsed_ms')))
check("多手记录累积", len(g6.history) == 1, str(len(g6.history)))

# ---------- 19. 记录落盘 ----------
fp = os.path.join(tempfile.gettempdir(), 'xq_log_test.jsonl')
if os.path.exists(fp):
    os.remove(fp)
g7, tk7 = new_game(timeout=60, log_path=fp)
g7.apply_move(tk7['red'], "h2e2")
g7.apply_move(tk7['black'], "h7e7")
lines = 0
if os.path.exists(fp):
    with open(fp, encoding='utf-8') as f:
        lines = len([l for l in f if l.strip()])
check("每手追加一行 JSONL", lines == 2, f"{lines} 行")
if lines:
    with open(fp, encoding='utf-8') as f:
        first = json.loads(f.readline())
    need = {'ply', 'by', 'move', 'notation', 'captured', 'at', 'elapsed_ms'}
    check("落盘记录字段完整",
          need <= set(first) and set(first) <= need | {'check'}, str(sorted(first)))

# ---------- 20. reset 重开 ----------
g8, _tk8 = new_game()
g8.apply_move(tokens(g8)['red'], "h2e2")
g8.reset()
check("reset 后红先行", g8.turn == 'red', g8.turn)
check("reset 后记录清空", g8.history == [], str(len(g8.history)))
check("reset 后保留两名玩家", len(g8.players) == 2, str(len(g8.players)))
check("reset 后重新开局", g8.status == 'playing', g8.status)
check("reset 后双方颜色仍不重复", set(g8.players.values()) == {'red', 'black'}, str(set(g8.players.values())))

print()
print(f"结果: {len(passed)} 通过, {len(failed)} 失败")
if failed:
    print("失败项: " + ", ".join(failed))
    sys.exit(1)
