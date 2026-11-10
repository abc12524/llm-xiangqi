#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国象棋本地引擎 —— 纯 Python、零第三方依赖、不调用任何大模型（0 token）

三档强度:
  random   完全随机走一步合法棋
  greedy   一层贪心: 走完哪步子力最优就走哪步(会吃子、尽量不白送)
  depth2   两层极小极大: 假设对手也会挑最优应对, 避免"吃了子反被吃"

用法:
  from xiangqi_engine import choose_move
  mv = choose_move(board, color, legal_moves, level='greedy', legal_fn=gen_legal_moves)
  # board[y][x] = (color, type) 或 None; color 为 'r'/'b'
"""
import random

# 子力价值(参考常见中象估值); 帅/将极大, 便于识别将死
PIECE_VALUE = {'K': 6000, 'R': 600, 'N': 300, 'C': 320, 'A': 120, 'B': 120, 'P': 70}
PAWN_CROSSED_BONUS = 40      # 兵/卒过河后加值
MATE = 10 ** 6

LEVELS = ('random', 'greedy', 'depth2')


def _apply(board, m):
    """走一步, 返回新棋盘(不修改原盘)"""
    x1, y1, x2, y2 = m
    nb = [row[:] for row in board]
    nb[y2][x2] = nb[y1][x1]
    nb[y1][x1] = None
    return nb


def evaluate(board, color):
    """从 color 视角算子力分差(正数对自己有利)"""
    score = 0
    for y in range(10):
        row = board[y]
        for x in range(9):
            p = row[x]
            if p is None:
                continue
            v = PIECE_VALUE[p[1]]
            if p[1] == 'P' and ((p[0] == 'r' and y >= 5) or (p[0] == 'b' and y <= 4)):
                v += PAWN_CROSSED_BONUS
            score += v if p[0] == 'r' else -v
    return score if color == 'r' else -score


def _greedy(board, color, moves):
    """一层: 直接看走完后的子力"""
    best, best_v = None, None
    for m in moves:
        v = evaluate(_apply(board, m), color)
        if best_v is None or v > best_v:
            best, best_v = m, v
    return best


def _search2(board, color, moves, legal_fn):
    """两层: 我走一步, 对手挑最损我的一步应对"""
    opponent = 'b' if color == 'r' else 'r'
    best, best_v = None, None
    for m in moves:
        nb = _apply(board, m)
        replies = legal_fn(nb, opponent)
        if not replies:                  # 对手无棋可走 → 我赢, 直接走这步
            return m
        worst = None
        for r in replies:
            v = evaluate(_apply(nb, r), color)
            if worst is None or v < worst:
                worst = v
                if best_v is not None and worst <= best_v:
                    break                # 这步不会更好, 提前放弃
        if best_v is None or worst > best_v:
            best, best_v = m, worst
    return best


def choose_move(board, color, moves, level='greedy', legal_fn=None):
    """挑一步棋。moves 为合法走法列表; depth2 需要传 legal_fn 生成对方应手。"""
    if not moves:
        return None
    if level == 'random':
        return random.choice(moves)
    if level == 'depth2' and legal_fn is not None:
        return _search2(board, color, moves, legal_fn)
    return _greedy(board, color, moves)
