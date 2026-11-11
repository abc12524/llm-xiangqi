#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国象棋对战服务端（LLM 友好接口）

规则: 红先行; 双人局红黑随机分配, 人机局电脑固定执黑(挑战者执红先行);
      只有执棋方可走子; 每步限时(双人默认 60s / 人机 600s)超时判负
接口:
  POST /join   {"name"?,"player"?}        → {"player","side","resumed",...}  带 player/同名 = 重连恢复
  GET  /wait?player=p1&since=<ply>&timeout=50 → {"event",...}  长轮询: 轮到你/对手走了/快超时/终局
  POST /move   {"player","move":"h2e2"}   → 状态快照                     仅执棋方可用
  GET  /state?player=p1                   → 状态快照
  GET  /history[?game=<gid>]              → 走棋记录(浏览器=HTML 回放页; 程序=JSON 数组)
  GET  /games                             → 历史对局列表
  GET  /watch?since=<ply>&st=<status>&timeout=25 → 回放页自动刷新用的长轮询(有变化才返回)
  POST /resign {"player"}                 → 认输, 对方胜
  POST /draw   {"player"}                 → 求和; 对手 {"player","accept":true|false} 同意/反对
  POST /reset  {"player"?,"clear"?}       → 重新开局
坐标: 列 a-i 左→右, 行 0-9 下→上(红方视角); 走法 'h2e2' = h2 走到 e2
棋子: 大写=红 K帅 A仕 B相 N马 R车 C炮 P兵; 小写=黑 k将 a士 b象 n马 r车 c炮 p卒
"""
import argparse
import datetime
import hashlib
import json
import os
import random
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import xiangqi_engine
import pikafish_uci

# ============================================================
# 常量与初始布局
# ============================================================
RED, BLACK = 'r', 'b'

# 人机模式固定席位: 电脑执黑, 挑战者执红(红先行)
ENGINE_SIDE, HUMAN_SIDE = 'black', 'red'
REASON_CHECKMATE, REASON_STALEMATE, REASON_TIMEOUT = 'checkmate', 'stalemate', 'timeout'
REASON_RESIGN = 'resign'
REASON_PERPETUAL = 'perpetual_check'   # 长将判负
REASON_AGREEMENT = 'agreement'         # 双方同意和棋
PERPETUAL_REPEAT = 3                   # 同一局面出现第 3 次即判长将

# 机器一方可选强度: 本地纯 Python 引擎(random/greedy/depth2) + Pikafish 档位(easy..max)
ALL_LEVELS = tuple(xiangqi_engine.LEVELS) + tuple(pikafish_uci.LEVELS)
DEFAULT_LEVEL = 'hard'

EV_YOUR_TURN = 'your_turn'          # 轮到你了
EV_OPPONENT_MOVED = 'opponent_moved'  # 对手落子
EV_TIMEOUT_WARNING = 'timeout_warning'  # 快超时, 该走了
EV_GAME_OVER = 'game_over'          # 终局
EV_DRAW_OFFER = 'draw_offer'        # 对手求和, 等你同意/反对
EV_IDLE = 'idle'                    # 本轮无变化

def build_initial_board():
    board = [[None] * 9 for _ in range(10)]
    def put(x, y, color, t):
        board[y][x] = (color, t)
    # 红方 (y=0 底线)
    for x, t in enumerate("RNBAKABNR"):
        put(x, 0, RED, t)
    put(1, 2, RED, 'C'); put(7, 2, RED, 'C')
    for x in (0, 2, 4, 6, 8):
        put(x, 3, RED, 'P')
    # 黑方 (y=9 底线)
    for x, t in enumerate("RNBAKABNR"):
        put(x, 9, BLACK, t)
    put(1, 7, BLACK, 'C'); put(7, 7, BLACK, 'C')
    for x in (0, 2, 4, 6, 8):
        put(x, 6, BLACK, 'P')
    return board

# ============================================================
# 坐标与走法字符串
# ============================================================
def coord_to_str(x, y):
    return chr(ord('a') + x) + str(y)

def str_to_coord(s):
    if len(s) != 2:
        raise ValueError(f"bad coord: {s!r}")
    x = ord(s[0]) - ord('a')
    y = int(s[1])
    if not (0 <= x < 9 and 0 <= y < 10):
        raise ValueError(f"out of board: {s!r}")
    return x, y

def move_to_str(m):
    x1, y1, x2, y2 = m
    return coord_to_str(x1, y1) + coord_to_str(x2, y2)

def str_to_move(s):
    # 容忍 LLM 常见写法: 大小写 / 空格 / 'h2-e2' / 'h2>e2'
    t = str(s).strip().lower()
    for ch in ('-', '>', '→', ' '):
        t = t.replace(ch, '')
    if len(t) != 4:
        raise ValueError(f"bad move: {s!r} (需要 4 字符, 如 'h2e2')")
    x1, y1 = str_to_coord(t[0:2])
    x2, y2 = str_to_coord(t[2:4])
    return (x1, y1, x2, y2)

def turn_code(turn):
    return RED if turn == 'red' else BLACK

def opp(color):
    return BLACK if color == RED else RED

def opp_side(turn):
    """回合名字取反: 'red' <-> 'black' (注意不是颜色码, 不要和 opp() 混用)"""
    return 'black' if turn == 'red' else 'red'

# ============================================================
# 中文记谱 (炮二平五 / 马8进7)
# ============================================================
CN_NUM = "零一二三四五六七八九"
CN_PIECE = {
    RED:   {'R': '车', 'N': '马', 'B': '相', 'A': '仕', 'K': '帅', 'C': '炮', 'P': '兵'},
    BLACK: {'R': '车', 'N': '马', 'B': '象', 'A': '士', 'K': '将', 'C': '炮', 'P': '卒'},
}
# 斜行棋子: 进退后面写目标列号, 其余写步数
DIAG_PIECES = ('N', 'B', 'A')

def _num(n, color):
    """记谱数字: 红方用汉字, 黑方用阿拉伯数字(通行写法)"""
    return CN_NUM[n] if color == RED else str(n)

def file_no(x, color):
    """列号: 红方从红右手边数 1-9, 黑方从黑右手边数 1-9"""
    return 9 - x if color == RED else x + 1

def move_notation(board, m):
    """把走法转成中文记谱(炮二平五/马8进7); 起点无子或无法判定时返回 None

    board 必须是走子之前的局面。
    """
    x1, y1, x2, y2 = m
    if not in_bounds(x1, y1):
        return None
    p = board[y1][x1]
    if p is None:
        return None
    color, kind = p
    name = CN_PIECE[color][kind]

    # 同一列有多个同种棋子 → 用 前/中/后 代替列号 (红方 y 大的靠前)
    same = [yy for yy in range(10) if board[yy][x1] == (color, kind)]
    if len(same) >= 2:
        same.sort(reverse=(color == RED))
        i = same.index(y1)
        if len(same) == 2:
            head = ('前', '后')[i] + name
        elif len(same) == 3:
            head = ('前', '中', '后')[i] + name
        else:
            head = name + _num(file_no(x1, color), color)   # 罕见, 退回列号
    else:
        head = name + _num(file_no(x1, color), color)

    if y1 == y2:
        return head + '平' + _num(file_no(x2, color), color)

    forward = (y2 > y1) if color == RED else (y2 < y1)
    tail = '进' if forward else '退'
    if kind in DIAG_PIECES:
        return head + tail + _num(file_no(x2, color), color)
    return head + tail + _num(abs(y2 - y1), color)

BOARD_LEGEND = ("棋盘: 红方大写 R车 N马 B相 A仕 K帅 C炮 P兵, 黑方小写 r车 n马 b象 a士 k将 c炮 p卒; "
                "列 a-i 从左到右, 行 0=红方底线(屏幕下方), 行 9=黑方底线(上方). "
                "坐标写法 <列><行><列><行>, 如 h2e2 = 炮二平五.")

def annotate_history(history):
    """补全老棋谱缺的中文记谱/将军标记(按走法重放); 返回新列表, 不改原对象"""
    board = build_initial_board()
    turn = 'red'
    out = []
    for rec in history:
        r = dict(rec)
        try:
            m = str_to_move(r.get('move') or '')
        except (ValueError, TypeError):
            out.append(r)
            continue
        if not r.get('notation'):
            r['notation'] = move_notation(board, m)
        board = make_move(board, *m)
        if 'check' not in r and in_check(board, opp(turn_code(turn))):
            r['check'] = True
        turn = opp_side(turn)
        out.append(r)
    return out


# ============================================================
# 走法生成 (伪合法)
# ============================================================
def in_bounds(x, y):
    return 0 <= x < 9 and 0 <= y < 10

def own(board, color, x, y):
    p = board[y][x]
    return p is not None and p[0] == color

def in_palace(color, x, y):
    if color == RED:
        return 3 <= x <= 5 and 0 <= y <= 2
    return 3 <= x <= 5 and 7 <= y <= 9

def gen_rook(board, color, x, y):
    moves = []
    for dx, dy in ((1,0),(-1,0),(0,1),(0,-1)):
        nx, ny = x + dx, y + dy
        while in_bounds(nx, ny):
            if board[ny][nx] is None:
                moves.append((x, y, nx, ny))
            else:
                if board[ny][nx][0] != color:
                    moves.append((x, y, nx, ny))
                break
            nx += dx; ny += dy
    return moves

def gen_knight(board, color, x, y):
    moves = []
    for dx, dy, lx, ly in (
        (1,2,0,1),(-1,2,0,1),(1,-2,0,-1),(-1,-2,0,-1),
        (2,1,1,0),(2,-1,1,0),(-2,1,-1,0),(-2,-1,-1,0),
    ):
        nx, ny = x + dx, y + dy
        if not in_bounds(nx, ny):
            continue
        if board[y + ly][x + lx] is not None:   # 蹩马腿
            continue
        if own(board, color, nx, ny):
            continue
        moves.append((x, y, nx, ny))
    return moves

def gen_bishop(board, color, x, y):
    moves = []
    for dx, dy in ((2,2),(2,-2),(-2,2),(-2,-2)):
        nx, ny = x + dx, y + dy
        if not in_bounds(nx, ny):
            continue
        if color == RED and ny > 4:       # 红相不过河
            continue
        if color == BLACK and ny < 5:     # 黑象不过河
            continue
        if board[y + dy // 2][x + dx // 2] is not None:  # 塞象眼
            continue
        if own(board, color, nx, ny):
            continue
        moves.append((x, y, nx, ny))
    return moves

def gen_advisor(board, color, x, y):
    moves = []
    for dx, dy in ((1,1),(1,-1),(-1,1),(-1,-1)):
        nx, ny = x + dx, y + dy
        if not in_palace(color, nx, ny) or own(board, color, nx, ny):
            continue
        moves.append((x, y, nx, ny))
    return moves

def gen_king(board, color, x, y):
    moves = []
    for dx, dy in ((1,0),(-1,0),(0,1),(0,-1)):
        nx, ny = x + dx, y + dy
        if not in_palace(color, nx, ny) or own(board, color, nx, ny):
            continue
        moves.append((x, y, nx, ny))
    return moves

def gen_cannon(board, color, x, y):
    moves = []
    for dx, dy in ((1,0),(-1,0),(0,1),(0,-1)):
        nx, ny = x + dx, y + dy
        # 空格段
        while in_bounds(nx, ny) and board[ny][nx] is None:
            moves.append((x, y, nx, ny))
            nx += dx; ny += dy
        # 跳过炮架，找第一个子
        if in_bounds(nx, ny):
            nx += dx; ny += dy
            while in_bounds(nx, ny):
                if board[ny][nx] is not None:
                    if board[ny][nx][0] != color:
                        moves.append((x, y, nx, ny))
                    break
                nx += dx; ny += dy
    return moves

def gen_pawn(board, color, x, y):
    moves = []
    forward = 1 if color == RED else -1
    ny = y + forward
    if in_bounds(x, ny) and not own(board, color, x, ny):
        moves.append((x, y, x, ny))
    # 过河后可横走
    crossed = (color == RED and y >= 5) or (color == BLACK and y <= 4)
    if crossed:
        for dx in (-1, 1):
            nx = x + dx
            if in_bounds(nx, y) and not own(board, color, nx, y):
                moves.append((x, y, nx, y))
    return moves

GENERATORS = {
    'R': gen_rook, 'N': gen_knight, 'B': gen_bishop,
    'A': gen_advisor, 'K': gen_king, 'C': gen_cannon, 'P': gen_pawn,
}

def gen_moves(board, color):
    moves = []
    for y in range(10):
        for x in range(9):
            p = board[y][x]
            if p is None or p[0] != color:
                continue
            moves.extend(GENERATORS[p[1]](board, color, x, y))
    return moves

# ============================================================
# 合法性校验
# ============================================================
def make_move(board, x1, y1, x2, y2):
    nb = [row[:] for row in board]
    nb[y2][x2] = nb[y1][x1]
    nb[y1][x1] = None
    return nb

def find_king(board, color):
    for y in range(10):
        for x in range(9):
            if board[y][x] == (color, 'K'):
                return x, y
    return None, None

def kings_face(board):
    rx, ry = find_king(board, RED)
    bx, by = find_king(board, BLACK)
    if rx is None or bx is None or rx != bx:
        return False
    for y in range(min(ry, by) + 1, max(ry, by)):
        if board[y][rx] is not None:
            return False
    return True

def is_attacked(board, by_color, x, y):
    for m in gen_moves(board, by_color):
        if m[2] == x and m[3] == y:
            return True
    return False

def in_check(board, color):
    kx, ky = find_king(board, color)
    if kx is None:
        return True
    return is_attacked(board, opp(color), kx, ky)

def gen_legal_moves(board, color):
    result = []
    for m in gen_moves(board, color):
        x1, y1, x2, y2 = m
        nb = make_move(board, x1, y1, x2, y2)
        if kings_face(nb):
            continue
        kx, ky = find_king(nb, color)
        if kx is None:
            continue
        if is_attacked(nb, opp(color), kx, ky):
            continue
        result.append(m)
    return result

def render_board(board, flip=False):
    """flip=True 给黑方看: 行列都反转(180°), 但行列标签仍是真实坐标"""
    rows = range(10) if flip else range(9, -1, -1)
    cols = range(8, -1, -1) if flip else range(9)
    lines = ["   " + " ".join("abcdefghi"[x] for x in cols)]
    for y in rows:
        cells = []
        for x in cols:
            p = board[y][x]
            if p is None:
                cells.append(".")
            elif p[0] == RED:
                cells.append(p[1])
            else:
                cells.append(p[1].lower())
        lines.append(f"{y:2d} " + " ".join(cells))
    return "\n".join(lines)

# ============================================================
# 根路径文档 (Markdown)
# ============================================================
RULES_MD = """# 中国象棋 · 对战服务

红先走。只有执棋方可以走子，超时判负。红黑分配：

- **双人对战**：双方红黑**随机分配**（每局重开都重新随机）
- **人机模式**：电脑**固定执黑**，挑战者**固定执红**、先行

限时：

- **双人对战**：每步限时 **%TOKEN%** 秒，剩余 %WARN% 秒时推送 `timeout_warning`
- **人机模式**：每步限时 **%ENGINE%** 分钟，剩余 %ENGINE_WARN% 秒时推送提醒

## 一、棋盘与坐标

棋盘 9 列 × 10 行，中间是楚河汉界。

- 列：`a`–`i`，从左到右（红方视角）
- 行：`0`–`9`，从下到上；**红方底线为 `0`、黑方底线为 `9`**
- 红方位于行 0–4，黑方位于行 5–9

**棋子大小写**（看棋盘先认这一点）：**大写是红方**（`K`帅 `A`仕 `B`相 `N`马 `R`车 `C`炮 `P`兵），
**小写是黑方**（`k`将 `a`士 `b`象 `n`马 `r`车 `c`炮 `p`卒），`.` 是空点。
红方视角下最下面一行 `0` 全是大写、最上面一行 `9` 全是小写；
**行号 `0` 永远是红方底线、`9` 永远是黑方底线**，别记反。

**视角**：`board` 按请求方身份给朝向 —— 黑方 token 拿到的是 180° 旋转（行 `0` 在上、`i` 列在左），
行列标签仍是真实坐标，走法不需要换算；无身份或红方请求保持红方视角（行 `0` 在下、`a` 列在左）。

走法写成 4 个字符：`起点列行 + 终点列行`，例如 `h2e2` 表示 h2 的棋子走到 e2。
大小写、空格、`h2-e2`、`h2>e2` 都会被自动归一化。

服务仍在 `POST /join` 的返回里带一条 `board_legend`，内容同上，方便开局时对齐认知。

## 二、棋子与记法

| 红 | 黑 | 名称 | 数量 |
|----|----|------|------|
| K | k | 帅 / 将 | 1 |
| A | a | 仕 / 士 | 2 |
| B | b | 相 / 象 | 2 |
| N | n | 马 | 2 |
| R | r | 车 | 2 |
| C | c | 炮 | 2 |
| P | p | 兵 / 卒 | 5 |

棋盘快照中 **大写为红方、小写为黑方**，空格用 `.` 表示，例如：

```
   a b c d e f g h i
 9 r n b a k a b n r
 8 . . . . . . . . .
 7 . c . . . . . c .
 6 p . p . p . p . p
 5 . . . . . . . . .
 4 . . . . . . . . .
 3 P . P . P . P . P
 2 . C . . . . . C .
 1 . . . . . . . . .
 0 R N B A K A B N R
```

## 三、走子规则

| 棋子 | 走法 | 限制 |
|------|------|------|
| 车 R | 直线任意格数 | 不能越过任何棋子 |
| 马 N | 走「日」字（先直一格再斜一格） | **蹩马腿**：直行方向相邻那格有子则走不过去 |
| 相/象 B | 走「田」字（斜走两格） | **塞象眼**：田字中心有子则不能走；**不能过河** |
| 仕/士 A | 九宫内斜走一格 | 不能出九宫 |
| 帅/将 K | 九宫内直走一格 | 不能出九宫 |
| 炮 C | 不吃子时同车（直线任意格） | 吃子必须**隔着且仅隔一个**棋子（炮架） |
| 兵/卒 P | 向前一格 | **过河后**可以左右横走一格；**永远不能后退** |

补充两条关键规则：

1. **将帅不能照面**：帅与将不能在同一条直线上直接相对（中间无子），任何导致照面的走法都非法。
2. **九宫**：红方为 `d0`–`f2`，黑方为 `d7`–`f9`，帅/将与仕/士不得离开。

## 四、胜负

- **将死**：一方被将军且无任何合法走法解将 → 该方负
- **困毙**：一方未被将军但无任何合法走法 → 该方负（中国象棋规则中困毙同样判负）
- **长将**：同一局面第 3 次出现、且重复循环里某一方**每一手都在将军** → 该方负（长将判负）
- **超时**：轮到自己走棋，超过限时（双人 %TOKEN% 秒 / 人机 %ENGINE% 分钟）未提交走法 → 该方负
- **认输**：`POST /resign` → 认输方负
- **和棋**：`POST /draw` 求和，对手 `{"accept": true}` 同意 → `status` 为 `draw`、`end_reason` 为 `agreement`

`status` 取值：`waiting`（等人）/ `playing` / `red_win` / `black_win` / `draw`（和棋）；
终局时 `end_reason` 说明原因：`checkmate` / `stalemate` / `timeout` / `resign` / `perpetual_check` / `agreement`，
同时给出 `timed_out` 或 `resigned`（哪一方超时/认输），长将时给 `perpetual_check`（哪一方长将）。

## 五、接口

### 1. 加入对局 / 断线重连

```
POST /join
{"name": "昵称", "player": "p1"}
```

- 新玩家：不传 `player`，也不与已有玩家同名 → 分配新编号；满 2 人后新玩家再加入返回 409
- **重连**：传回原来的 `player`（如 `"p1"`），或传与之前**相同的 `name`**，即恢复原来的角色和棋盘，`resumed` 为 `true`
- 返回 `{"player": "p1", "side": "red"|"black", "resumed": bool, ...状态快照}`
- 双人对战：第 1 人随机拿红或黑，第 2 人自动拿另一方（**两人到齐才开局**，计时从红方第一步开始）
- 人机对战：见下节，席位固定

### 1b. 人机模式（本地引擎，0 token）

```
POST /join
{"name": "我", "vs": "engine", "level": "hard"}
```

带上 `"vs": "engine"`，机器会**自动坐到你的对面并立即开局**，不需要第二个人。
人机局的席位**固定**：电脑**执黑**，挑战者（你）**执红**、先行 —— 不用猜自己是什么颜色。
机器走棋由服务端本地的 **Pikafish（皮卡鱼）** 引擎算，**不调用任何大模型，不消耗 token**。
人机对局的每步限时是 **%ENGINE% 分钟**（双人对战仍是 %TOKEN% 秒），可以从容思考。
人机开局会占用整张桌子：上一局若已结束，直接 `vs: "engine"` 会自动清桌开新局；
对局进行中想换人机，先 `POST /reset {"clear": true}`。

`level` 档位（默认 `hard`）：

| `level` | 说明 |
|---|---|
| `easy` | 只看一层，并在几个候补里随机挑，会漏子。适合新手 |
| `normal` | 三层搜索，能看穿简单兑子 |
| `hard` | 限时 0.4 秒，业余高手也难赢（默认） |
| `max` | 限时 1.5 秒，基本没有机会 |
| `random` | 完全随机，最弱（内置引擎，不需要 Pikafish） |
| `greedy` | 一层贪心（内置引擎） |
| `depth2` | 两层极小极大（内置引擎） |

Pikafish 不可用时，`easy/normal/hard/max` 会自动回落成内置引擎的 `depth2`，对局不会中断。

机器应手是同步完成的，你 `POST /move` 之后返回的局面里，机器那一步已经走好了，
`GET /history` 里能看到它每手走了什么。人机局同样支持断线重连。

### 2. 等待通知（长轮询）★

```
GET /wait?player=p1&since=3&timeout=50
```

这是**你走完一步之后应该阻塞在这里**的接口。服务端会挂起请求，直到下列事件之一发生：

| `event` | 含义 | 建议动作 |
|---|---|---|
| `your_turn` | 轮到你走了（**最主要的事件**） | 立刻从 `legal_moves` 里选一步 `POST /move` |
| `timeout_warning` | 轮到的一方剩余不足 %WARN% 秒 | 该走的人必须马上落子，另一方可留意对手将超时 |
| `game_over` | 对局结束 | 看 `status` / `end_reason` / `timed_out` |
| `opponent_moved` | 检测到 `ply` 变化，但当前还没轮到你 | 刷新棋盘，继续等 |
| `idle` | 等到 `timeout` 秒仍无变化 | 重新发一次 `/wait` |

> 象棋是双方轮流走，所以**对手落子后基本都已经轮到你**，此时服务端直接返回 `your_turn` 而不是 `opponent_moved`。
> 也就是说：你走完一步、挂上 `/wait`，醒来时要么是你该走，要么是对局结束 —— 这正是"通知对方"的实现方式。

参数：`since` 传你已知的手数（`ply`），用于判断对手是否落子；省略则只等"轮到我/终局/预警"。
`timeout` 是本次最长阻塞秒数，默认 50，上限 110。

**请不要用轮询 `/state` 的方式等待** —— `/wait` 会把请求挂住，有事件立刻返回，既省调用又不会错过提醒。

### 3. 查看局面

```
GET /state?player=p1
```

`player` 可省略；带上时额外返回 `you` 字段。返回内容见「六、状态字段」。

### 4. 走棋（推荐带 `wait`，一步一请求）

```
POST /move
{"player": "p1", "move": "h2e2", "wait": 60}
```

带 `wait` 时：服务端先落子，然后**挂起请求直到对手也走完**，直接返回你下一次该走的新局面。
这样一整局每步只需 **1 次请求**，响应也最短 —— 这是省 token 的推荐用法。

不带 `wait` 时只返回走棋确认，不给棋盘（棋盘用下一次 `/wait` 或 `/state` 取）：

```
{"ok": 1, "ply": 5, "turn": "black"}
```

- 只有**当前回合方**能走，否则 403 `not your turn`
- 对局已结束（终局后）再走子返回 409 `game is not in progress`，优先于回合校验
- 走法非法返回 400，棋盘不变
- 身份也可放在 `X-Player` 请求头或 `?player=` 查询参数
- **幂等**：同一玩家重发同一手、且局面没变（典型是没收到响应就重试），直接返回上次的回执并带 `"duplicate": true`，**不会报错**。人机模式下尤其重要 —— 你的棋早已生效，重发只会被告知"已收到过"。

### 5. 重开一局

```
POST /reset
{"player": "p1"}
```

保留双方玩家，**重新分配红黑**（双人局随机；人机局仍是挑战者执红、电脑执黑），红方重新先行，计时重置。
传 `{"clear": true}` 则清空所有玩家（**连机器席位一起清掉**），回到 `waiting` 状态等新人入座。

### 6. 读取走棋记录

```
GET /history
GET /history?game=<gid>        # 某一局历史棋谱
GET /games                     # 历史对局列表
```

返回本局全部走棋记录（**JSON 数组**），字段见「七、走棋记录」。`/state` 不包含记录。

同一地址按 Accept 协商：**浏览器打开是棋盘回放页**（可播放、逐步回看、点棋谱跳转、
下拉框切换历史对局），**程序/LLM 请求仍是上面的 JSON 数组**，行为不变
（curl、requests 默认 `Accept: */*`，走 JSON）。需要强制时加参数：`?format=json` 或 `?format=html`。

每局结束（或重开）时整局棋谱落到 `xiangqi_games/<gid>.json`，`GET /games` 返回索引
（`id` / `started_at` / `red` / `black` / `status` / `end_reason` / `plies`），新的在前。
回放页顶部的下拉框就是它，选一局即可复现。

### 6b. 认输

```
POST /resign
{"player": "p1"}
```

认输方直接判负，对方获胜，`status` 变成 `red_win` / `black_win`，
`end_reason` 为 `resign`，同时给出 `resigned`（哪一方认输）。棋谱照样保留可回放。

### 6c. 求和

```
POST /draw    {"player": "p1"}                    # 提议和棋
POST /draw    {"player": "p2", "accept": true}    # 同意 → 和棋
POST /draw    {"player": "p2", "accept": false}   # 反对 → 继续下
```

- 提议方 `{"player"}` 只带身份，返回 `{"draw": "offered", "by": "red", ...状态快照}`
- 对手回应带 `accept`：`true` → `status` 变 `draw`、`end_reason` 为 `agreement`；`false` → 提议作废继续下
- 对方等待时 `/wait` 会收到 `"event": "draw_offer"`，快照里也有 `draw_offer` 指出是谁在求和
- **一落子提议即作废**（和棋的意思只在当前局面有效），所以提议后别再走子
- 人机模式下机器不接和棋，直接返回 `{"draw": "declined", "reason": "engine_declines"}`
- 已经提议过又提一次 → `{"draw": "pending"}`；别人没提议却去 `accept` → 400

## 六、状态字段

接口只返回棋局本身，不含玩家列表、对局历史等附加字段。

| 字段 | 含义 |
|------|------|
| `status` | `waiting` / `playing` / `red_win` / `black_win` / `draw` |
| `turn` | 当前该走的一方：`red` / `black` |
| `in_check` | 当前回合方是否被将军 |
| `board` | 棋盘文本快照（唯一的棋盘表示） |
| `legal_moves` | 合法走法，**空格分隔的单字符串**，如 `"a0a1 a0a2 b0c2"`（`.split()` 即得数组）；终局为空串。**只属于 `moves_for` 那一方**，见下方说明 |
| `moves_for` | `legal_moves` 属于哪一方：`red` / `black`。等于 `turn` |
| `last_move` | 上一步：`{"ply":5,"by":"black","move":"h7e7","notation":"炮8平5","captured":null,"check":true}`，`captured`/`check` 按需出现 |
| `remaining_ms` | 当前回合方还剩多少毫秒；非 `playing` 时为 0 |
| `end_reason` | 仅终局出现：`checkmate` / `stalemate` / `timeout` / `resign` / `perpetual_check` / `agreement` |
| `draw_offer` | 仅有人求和时出现：`red` / `black`，表示谁提的和。用 `POST /draw {"accept": ...}` 回应 |
| `timed_out` | 仅超时终局出现：`red` / `black`，表示是哪一方超时 |
| `resigned` | 仅认输终局出现：`red` / `black`，表示是哪一方认输 |
| `perpetual_check` | 仅长将终局出现：`red` / `black`，表示是哪一方长将判负 |
| `you` | 仅当请求带 `player` 时出现：`{"side": "red", "is_turn": true}` |

**关于 `legal_moves` 的归属**（重要，以前这里是错的）：

- 带 `player` 查询且**轮到你** → 给你的走法，可以直接当"可选着集合"用
- 带 `player` 查询但**没轮到你** → **不返回 `legal_moves`**（此时棋盘不是你该走的，给出来只会误导）
- 不带 `player`（纯观战）→ 给当前回合方的走法，并明确标注 `moves_for`

所以判断能不能走，只看两件事：`you.is_turn` 为 `true`，且 `legal_moves` 存在。
不要用别的回合的列表推理自己的棋 —— 走子本身仍以服务端校验为准，非法会返回 400 并附上当前合法走法。

`/join` 额外返回 `player`（你的编号）、`side`（你执红还是黑）与 `board_legend`（棋盘图例）。
**`player` 请务必保存**：断线后可用它原样重连，恢复同一角色和同一盘棋。

## 七、走棋记录

每手记一条，内存保留全量，并追加写入 `xiangqi_history.jsonl`（可用 `--log` 改路径）。

| 字段 | 含义 |
|------|------|
| `ply` | 第几手，从 1 开始 |
| `by` | 走子方：`red` / `black` |
| `move` | 走法，如 `h2e2` |
| `notation` | 中文记谱，如 `炮二平五`（红方用汉字数字，黑方用阿拉伯数字，如 `炮8平5`） |
| `captured` | 被吃棋子，未吃子为 `null` |
| `check` | 仅这手将军时出现，值为 `true` |
| `at` | 落子时间，ISO 8601 带时区 |
| `elapsed_ms` | 本手用时（毫秒，从轮到该方起算） |

```
[{"ply": 1, "by": "red", "move": "h2e2", "notation": "炮二平五", "captured": null,
  "at": "2026-11-10T12:00:00+08:00", "elapsed_ms": 3120}]
```

## 八、一局完整示例（省 token 写法）

```
# 甲加入
POST /join  {"name":"甲"}          → player=p1, side=black, resumed=false
# 乙加入
POST /join  {"name":"乙"}          → player=p2, side=red,   resumed=false

# 红方(乙)先走: 落子 + 挂起等黑方, 一次请求拿回下一手
POST /move  {"player":"p2","move":"h2e2","wait":60}
            → {"event":"your_turn", "board":"...", "legal_moves":"a6a5 c6c5 ...", ...}
# 继续走, 如此循环, 每步 1 个请求
POST /move  {"player":"p2","move":"b2e2","wait":60}

# 黑方(甲)第一次接手
GET  /wait?player=p1&timeout=50    → event=your_turn, 拿到棋盘后同样用 move+wait

# 程序中断后重来
POST /join  {"player":"p2"}        → resumed=true, 棋盘和角色都在
```

## 九、省 token 的关键

1. **每步只用 1 个请求**：`POST /move {... "wait":60}`。走完直接挂起等对手，返回就是你的下一手局面，
   比 `move` + `wait` 两次请求省一半。
2. **不要轮询 `/state`**：`/wait` 会挂住，有事件才返回，不烧 token。缓存失效时才用 `/state` 重新同步。
3. **不要把历史响应留在上下文里**：每步只需要「当前棋盘 + `legal_moves`」，上一步的 JSON 已经作废。
   把历史全留着会让 prompt 随步数线性膨胀，那才是真正的 token 黑洞。
4. **精简事件不占带宽**：`idle` / `timeout_warning` / `game_over` 的响应只有 `status`/`turn`/`remaining_ms`，
   不含棋盘和走法，约 96 字节。

实测（80 手一局）：每步约 604 字节 ≈ **159 token**，整局约 **12.7k token**。
"""


# ============================================================
# /history 的 HTML 回放页（仅供浏览器看; 程序访问仍是 JSON）
# ============================================================
HISTORY_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>中国象棋 · 对局回放</title>
<style>
:root{--bg:#f4efe4;--card:#fffdf7;--ink:#2a2620;--dim:#8b8175;--line:#e6ddcc;
 --red:#b3271e;--black:#232a31;--accent:#8a6b3f;--hl:rgba(179,39,30,.55)}
@media (prefers-color-scheme:dark){:root{--bg:#14161a;--card:#1c1f25;--ink:#eae7e0;
 --dim:#8d939c;--line:#2b3038;--red:#e2574b;--black:#c9d2dd;--accent:#c9a76a;
 --hl:rgba(226,87,75,.6)}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);min-height:100vh;padding:26px 18px 44px;
 display:flex;justify-content:center;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.app{display:grid;grid-template-columns:minmax(300px,620px) 336px;gap:20px;width:100%;
 max-width:1000px;align-items:start}
@media (max-width:900px){.app{grid-template-columns:1fr}body{padding:16px 12px 32px}}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;
 box-shadow:0 1px 2px rgba(0,0,0,.05),0 14px 30px -22px rgba(0,0,0,.45)}
.boardcard{padding:14px 14px 8px}
.bhead{display:flex;align-items:center;gap:10px;padding:2px 4px 12px}
.badge{font-size:13px;font-weight:600;padding:5px 11px;border-radius:999px;
 background:#efe9dc;color:var(--dim);letter-spacing:.01em}
.badge.live{background:rgba(45,140,80,.13);color:#2d8c50}
.badge.over{background:rgba(179,39,30,.13);color:var(--red)}
@media (prefers-color-scheme:dark){.badge{background:#252a32}
 .badge.live{background:rgba(60,180,110,.16);color:#5ec98d}
 .badge.over{background:rgba(226,87,75,.16);color:#e2574b}}
.sub{font-size:12.5px;color:var(--dim);margin-left:auto;font-variant-numeric:tabular-nums}
.gsel{appearance:none;font:inherit;font-size:12px;color:var(--dim);cursor:pointer;
 max-width:250px;padding:5px 10px;border-radius:999px;border:1px solid var(--line);
 background:#efe9dc;outline:none;transition:border-color .13s,color .13s}
.gsel:hover{color:var(--ink);border-color:var(--accent)}
@media (prefers-color-scheme:dark){.gsel{background:#252a32;color:#b9c0cb}}
.board{position:relative;width:100%;aspect-ratio:920/1020}
.board svg{display:block;width:100%;height:100%;border-radius:12px;
 filter:drop-shadow(0 12px 20px rgba(90,60,20,.28))}
.pc{transition:transform .30s cubic-bezier(.34,.02,.2,1)}
.pc text{font-family:"KaiTi","STKaiti","Kaiti SC","楷体",Georgia,serif;font-size:52px;
 font-weight:700;text-anchor:middle;dominant-baseline:central;pointer-events:none;
 user-select:none}
.pc.red text{fill:#b3271e}
.pc.black text{fill:#20262c}
.controls{display:flex;align-items:center;gap:8px;padding:12px 4px 6px}
.controls button{appearance:none;border:1px solid var(--line);background:transparent;
 color:var(--ink);height:34px;min-width:36px;border-radius:9px;font-size:13px;
 cursor:pointer;transition:background .15s,opacity .15s;font-family:inherit}
.controls button:hover:not(:disabled){background:rgba(127,127,127,.13)}
.controls button:disabled{opacity:.3;cursor:default}
.controls button.primary{background:var(--accent);border-color:transparent;color:#fff;
 font-weight:600;padding:0 14px}
#seek{flex:1;min-width:70px;height:4px;accent-color:var(--accent);cursor:pointer}
.plynum{font-size:12px;color:var(--dim);font-variant-numeric:tabular-nums;
 min-width:52px;text-align:right;white-space:nowrap}
.side{display:flex;flex-direction:column;gap:16px;position:sticky;top:26px}
@media (max-width:900px){.side{position:static}}
.card h2{margin:0;padding:14px 16px 10px;font-size:12px;font-weight:600;
 letter-spacing:.08em;color:var(--dim)}
.card h2 .hint{font-weight:400;letter-spacing:0;color:var(--dim);opacity:.75;float:right}
.pl{display:flex;align-items:center;gap:10px;padding:10px 16px;
 border-top:1px solid var(--line);transition:background .2s}
.pl .dot{width:21px;height:21px;border-radius:50%;display:grid;place-items:center;
 font-family:KaiTi,serif;font-size:13px;font-weight:700;border:1.5px solid currentColor;
 background:rgba(255,255,255,.5)}
@media (prefers-color-scheme:dark){.pl .dot{background:rgba(255,255,255,.08)}}
.pl.red{color:var(--red)}.pl.black{color:var(--black)}
.pl .nm{color:var(--ink);font-size:14px;font-weight:500}
.pl .tag{margin-left:auto;font-size:11px;color:var(--dim);border:1px solid var(--line);
 padding:2px 7px;border-radius:999px;white-space:nowrap}
.pl.on{background:rgba(127,127,127,.1)}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line);
 border-top:1px solid var(--line);border-radius:0 0 16px 16px;overflow:hidden}
.stats div{background:var(--card);padding:10px 16px}
.stats b{display:block;font-size:15px;font-weight:600;font-variant-numeric:tabular-nums}
.stats span{font-size:11.5px;color:var(--dim)}
.moves{max-height:min(52vh,400px);overflow:auto;padding:2px 10px 12px;
 border-top:1px solid var(--line);border-radius:0 0 16px 16px;scrollbar-width:thin}
.mrow{display:grid;grid-template-columns:32px 1fr 1fr;align-items:center;gap:5px;
 border-radius:8px}
.mrow:nth-child(odd){background:rgba(127,127,127,.05)}
.num{color:var(--dim);font-size:11.5px;text-align:right;padding-right:2px;
 font-variant-numeric:tabular-nums}
.mv{appearance:none;border:0;background:transparent;color:var(--ink);font:inherit;
 font-size:13px;cursor:pointer;text-align:left;padding:6px 8px;border-radius:7px;
 display:flex;align-items:center;gap:6px;transition:background .13s,color .13s}
.mv:hover{background:rgba(127,127,127,.14)}
.mv.on{background:var(--accent);color:#fff}
.mv.on .cap,.mv.on .tm{color:rgba(255,255,255,.82)}
.mv .cap{font-size:11px;color:var(--dim)}
.mv .cn{white-space:nowrap;letter-spacing:.02em}
.mv .chk{font-style:normal;font-size:10px;font-weight:700;color:#d9453d;margin-left:2px;
 vertical-align:super}
.mv.on .chk{color:#ffd9d4}
.mv .tm{margin-left:auto;font-size:10.5px;color:var(--dim);font-variant-numeric:tabular-nums}
.mv.none{cursor:default;color:var(--dim);opacity:.45}
.mv.none:hover{background:transparent}
.empty{padding:22px 16px;color:var(--dim);font-size:13px;text-align:center}
</style>
</head>
<body>
<div class="app">
  <div>
    <div class="card boardcard">
      <div class="bhead">
        <span class="badge" id="status">加载中</span>
        <span class="sub" id="clock"></span>
        <select class="gsel" id="gsel" title="切换历史对局"></select>
      </div>
      <div class="board">
        <svg id="svg" viewBox="0 0 920 1020" xmlns="http://www.w3.org/2000/svg">
          <defs>
            <linearGradient id="wood" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0" stop-color="#f2ddb0"/>
              <stop offset=".45" stop-color="#e9cd97"/>
              <stop offset="1" stop-color="#d9b478"/>
            </linearGradient>
            <radialGradient id="pcfill" cx="34%" cy="28%" r="78%">
              <stop offset="0" stop-color="#fffaea"/>
              <stop offset=".62" stop-color="#f2e2bf"/>
              <stop offset="1" stop-color="#dcc38f"/>
            </radialGradient>
          </defs>
          <rect x="0" y="0" width="920" height="1020" rx="14" fill="url(#wood)"/>
          <rect x="22" y="22" width="876" height="976" rx="8" fill="none"
                stroke="#a9803f" stroke-width="4" opacity=".85"/>
          <g id="grid" stroke="#8a6b3f" stroke-width="2.4" stroke-linecap="round"></g>
          <g id="river" font-family="KaiTi,STKaiti,serif" font-size="44" fill="#8a6b3f"
             opacity=".62" text-anchor="middle">
            <text x="300" y="527">楚 河</text>
            <text x="620" y="527">汉 界</text>
          </g>
          <g id="hl"></g>
          <g id="pieces"></g>
        </svg>
      </div>
      <div class="controls">
        <button id="first" title="开局 (Home)">⏮</button>
        <button id="prev" title="上一步 (←)">◀</button>
        <button id="play" class="primary" title="播放 / 暂停 (空格)">▶ 播放</button>
        <button id="next" title="下一步 (→)">▶</button>
        <button id="last" title="末手 (End)">⏭</button>
        <input type="range" id="seek" min="0" max="0" value="0">
        <span class="plynum" id="plynum">0 / 0</span>
      </div>
    </div>
  </div>
  <aside class="side">
    <div class="card" style="overflow:hidden">
      <h2>对局</h2>
      <div id="players"></div>
      <div class="stats" id="stats"></div>
    </div>
    <div class="card" style="overflow:hidden">
      <h2>棋谱<span class="hint">点任意一手回看</span></h2>
      <div class="moves" id="moves"><div class="empty">本局还没有走子</div></div>
    </div>
  </aside>
</div>
<script>
const D = %DATA%;
let H = D.history || [];
const M = D.meta || {};
const NS = 'http://www.w3.org/2000/svg';
const CH = {K:'帅',A:'仕',B:'相',N:'马',R:'车',C:'炮',P:'兵',
            k:'将',a:'士',b:'象',n:'马',r:'车',c:'炮',p:'卒'};
const X = x => 60 + x * 100, Y = y => 60 + (9 - y) * 100;

/* ---------- 历史对局切换 ---------- */
(function(){
  const sel = document.getElementById('gsel');
  if (!sel) return;
  const games = M.games || [];
  if (!games.length && !M.gid) { sel.style.display = 'none'; return; }
  const res = {red_win:'红胜', black_win:'黑胜'};
  const add = (v, t) => {
    const o = document.createElement('option');
    o.value = v; o.textContent = t; sel.appendChild(o);
  };
  const label = g => {
    const t = (g.started_at || g.updated_at || '').slice(5, 16).replace('T', ' ');
    return t + ' · ' + (g.red || '空') + ' vs ' + (g.black || '空') +
           ' · ' + g.plies + '手 · ' + (res[g.status] || '进行中');
  };
  add('', M.is_current ? '当前对局' : '← 回到当前局');
  games.forEach(g => add(g.id, label(g)));
  sel.value = M.is_current ? '' : (M.gid || '');
  sel.onchange = () => {
    location.href = location.pathname +
      (sel.value ? '?game=' + encodeURIComponent(sel.value) : '');
  };
})();

/* ---------- 棋盘线 ---------- */
const grid = document.getElementById('grid');
function ln(x1, y1, x2, y2) {
  const e = document.createElementNS(NS, 'line');
  e.setAttribute('x1', x1); e.setAttribute('y1', y1);
  e.setAttribute('x2', x2); e.setAttribute('y2', y2);
  grid.appendChild(e);
}
for (let i = 0; i < 10; i++) ln(60, 60 + i * 100, 860, 60 + i * 100);
for (let x = 0; x < 9; x++) {
  const vx = 60 + x * 100;
  if (x === 0 || x === 8) ln(vx, 60, vx, 960);
  else { ln(vx, 60, vx, 460); ln(vx, 560, vx, 960); }
}
ln(360, 60, 560, 260); ln(560, 60, 360, 260);
ln(360, 760, 560, 960); ln(560, 760, 360, 960);

/* ---------- 初始局面 ---------- */
const BACK = 'RNBAKABNR', INIT = [];
(function () {
  let id = 0;
  for (let x = 0; x < 9; x++) INIT.push({id: id++, x, y: 0, t: BACK[x]});
  [1, 7].forEach(x => INIT.push({id: id++, x, y: 2, t: 'C'}));
  [0, 2, 4, 6, 8].forEach(x => INIT.push({id: id++, x, y: 3, t: 'P'}));
  for (let x = 0; x < 9; x++) INIT.push({id: id++, x, y: 9, t: BACK[x].toLowerCase()});
  [1, 7].forEach(x => INIT.push({id: id++, x, y: 7, t: 'c'}));
  [0, 2, 4, 6, 8].forEach(x => INIT.push({id: id++, x, y: 6, t: 'p'}));
})();

function parseMove(mv) {
  return [mv.charCodeAt(0) - 97, +mv[1], mv.charCodeAt(2) - 97, +mv[3]];
}

function stateAt(n) {
  const m = new Map();
  INIT.forEach(p => m.set(p.x + ',' + p.y, {id: p.id, x: p.x, y: p.y, t: p.t}));
  for (let i = 0; i < n && i < H.length; i++) {
    const [fx, fy, tx, ty] = parseMove(H[i].move);
    const p = m.get(fx + ',' + fy);
    m.delete(fx + ',' + fy); m.delete(tx + ',' + ty);
    if (p) { p.x = tx; p.y = ty; m.set(tx + ',' + ty, p); }
  }
  return [...m.values()];
}

/* ---------- 棋子 ---------- */
const layer = document.getElementById('pieces'), els = new Map();
const isRed = t => t === t.toUpperCase();

function makePiece(p) {
  const g = document.createElementNS(NS, 'g');
  g.setAttribute('class', 'pc ' + (isRed(p.t) ? 'red' : 'black'));
  const c1 = document.createElementNS(NS, 'circle');
  c1.setAttribute('r', 44); c1.setAttribute('fill', 'url(#pcfill)');
  c1.setAttribute('stroke', '#a9803f'); c1.setAttribute('stroke-width', 3);
  const c2 = document.createElementNS(NS, 'circle');
  c2.setAttribute('r', 35); c2.setAttribute('fill', 'none'); c2.setAttribute('stroke', '#bf9a5d');
  c2.setAttribute('stroke-width', 1.4); c2.setAttribute('opacity', .8);
  const tx = document.createElementNS(NS, 'text');
  tx.textContent = CH[p.t] || '?';
  g.append(c1, c2, tx);
  return g;
}

/* ---------- 高亮上一步 ---------- */
const hl = document.getElementById('hl');
const hlBox = document.createElementNS(NS, 'rect');
hlBox.setAttribute('width', 94); hlBox.setAttribute('height', 94);
hlBox.setAttribute('rx', 12); hlBox.setAttribute('fill', 'none');
hlBox.setAttribute('stroke-width', 3); hlBox.setAttribute('stroke-dasharray', '8 7');
const hlDot = document.createElementNS(NS, 'circle');
hlDot.setAttribute('r', 47); hlDot.setAttribute('fill', 'none');
hlDot.setAttribute('stroke-width', 3.4);
hl.append(hlBox, hlDot);

function markLast(n) {
  if (!n) { hlBox.style.display = hlDot.style.display = 'none'; return; }
  const mv = H[n - 1], [fx, fy, tx, ty] = parseMove(mv.move);
  const col = mv.by === 'red' ? 'rgba(179,39,30,.6)' : 'rgba(40,48,56,.55)';
  hlBox.style.display = hlDot.style.display = '';
  hlBox.setAttribute('x', X(fx) - 47); hlBox.setAttribute('y', Y(fy) - 47);
  hlBox.setAttribute('stroke', col);
  hlDot.setAttribute('cx', X(tx)); hlDot.setAttribute('cy', Y(ty));
  hlDot.setAttribute('stroke', col);
}

/* ---------- 渲染 ---------- */
let cur = H.length;
let lastStatus = M.status || '';

function render(n) {
  const st = stateAt(n), alive = new Set();
  for (const p of st) {
    alive.add(p.id);
    let el = els.get(p.id);
    if (!el) {
      el = makePiece(p); els.set(p.id, el);
      el.style.transition = 'none';
      el.style.transform = 'translate(' + X(p.x) + 'px,' + Y(p.y) + 'px)';
      layer.appendChild(el);
      requestAnimationFrame(() => { el.style.transition = ''; });
    } else {
      el.style.transform = 'translate(' + X(p.x) + 'px,' + Y(p.y) + 'px)';
    }
  }
  for (const [id, el] of [...els]) if (!alive.has(id)) { el.remove(); els.delete(id); }
  markLast(n);
}

/* ---------- 棋谱 ---------- */
const movesBox = document.getElementById('moves');

function buildMoves() {
  movesBox.innerHTML = '';
  if (!H.length) {
    movesBox.innerHTML = '<div class="empty">本局还没有走子</div>';
    return;
  }
  for (let i = 0; i < H.length; i += 2) {
    const row = document.createElement('div');
    row.className = 'mrow';
    const num = document.createElement('span');
    num.className = 'num'; num.textContent = (i / 2 + 1) + '.';
    row.appendChild(num);
    for (const j of [i, i + 1]) {
      if (j >= H.length) {
        const s = document.createElement('span');
        s.className = 'mv none'; s.textContent = '·';
        row.appendChild(s); continue;
      }
      const r = H[j], b = document.createElement('button');
      b.className = 'mv';
      b.dataset.ply = r.ply;
      const [fx, fy, tx, ty] = parseMove(r.move);
      const c = String.fromCharCode(97 + fx) + fy, d = String.fromCharCode(97 + tx) + ty;
      b.title = '第 ' + r.ply + ' 手 · ' + (r.by === 'red' ? '红' : '黑') + ' ' +
        c + '→' + d + (r.check ? ' · 将军' : '');
      b.innerHTML = '<span class="cn">' + (r.notation || (c + '→' + d)) +
        (r.check ? '<i class="chk">将</i>' : '') + '</span>' +
        (r.captured ? '<span class="cap">吃' + (CH[r.captured] || r.captured) + '</span>' : '') +
        '<span class="tm">' + (r.elapsed_ms / 1000).toFixed(1) + 's</span>';
      b.addEventListener('click', () => { stop(); go(r.ply); });
      row.appendChild(b);
    }
    movesBox.appendChild(row);
  }
}

/* ---------- 状态 ---------- */
const badge = document.getElementById('status');
const clock = document.getElementById('clock');
const playersBox = document.getElementById('players');
const statsBox = document.getElementById('stats');

function fmtMs(ms) {
  const s = Math.round(ms / 1000);
  return (s >= 60 ? Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0') : s + 's');
}

function setStatus(s) {
  const st = s.status, t = s.turn;
  let txt, cls = 'badge';
  if (st === 'playing') { txt = '进行中 · 轮到' + (t === 'red' ? '红方' : '黑方'); cls += ' live'; }
  else if (st === 'waiting') { txt = '等待入座'; cls += ' live'; }
  else if (st === 'draw') { txt = '和棋 · 双方同意'; cls += ' over'; }
  else {
    const why = {timeout: '超时', checkmate: '将死', stalemate: '困毙',
                 resign: '认输', perpetual_check: '长将判负'}[s.end_reason] || '';
    txt = (st === 'red_win' ? '红方胜' : '黑方胜') + (why ? ' · ' + why : '');
    cls += ' over';
  }
  badge.textContent = txt; badge.className = cls;
  clock.textContent = st === 'playing' ? '剩余 ' + fmtMs(s.remaining_ms || 0)
                                     : (st === 'waiting' ? '等待' : '终局');
  if (s.draw_offer) {
    clock.textContent += ' · 求和待回应(' +
      (s.draw_offer === 'red' ? '红' : '黑') + ')';
  }
  document.querySelectorAll('.pl').forEach(el => {
    el.classList.toggle('on', st === 'playing' && el.dataset.side === t);
  });
}

function paintMeta(s) {
  const ps = M.players || [];                 // 席位来自 /meta, 与页面初载同源
  const aiSide = M.auto_side;
  const aiTag = 'AI · ' + (M.engine_level || 'hard');
  const nameOf = side => {
    const p = ps.find(x => x.side === side);
    if (p && p.name) return p.name;
    return side === aiSide ? '电脑' : '空位';
  };
  playersBox.innerHTML = '';
  for (const side of ['red', 'black']) {
    const d = document.createElement('div');
    d.className = 'pl ' + side; d.dataset.side = side;
    d.innerHTML = '<span class="dot">' + (side === 'red' ? '帅' : '将') + '</span>' +
      '<span class="nm">' + nameOf(side) + '</span>' +
      '<span class="tag">' + (aiSide === side ? aiTag : (side === 'red' ? '红' : '黑')) + '</span>';
    playersBox.appendChild(d);
  }
  const total = H.reduce((a, r) => a + (r.elapsed_ms || 0), 0);
  const last = H.length ? H[H.length - 1].at.slice(11, 19) : '—';
  const limit = M.auto_side ? M.engine_step_s : M.step_s;   // 人机局用 10 分钟那一档
  statsBox.innerHTML =
    '<div><b>' + H.length + '</b><span>手数</span></div>' +
    '<div><b>' + (total >= 1000 ? fmtMs(total) : '—') + '</b><span>总用时</span></div>' +
    '<div><b>' + last + '</b><span>最后一手</span></div>' +
    '<div><b>' + (limit ? limit + 's' : '—') + '</b><span>每步限时</span></div>';
  setStatus(s);                               // 席位重建后再标当前回合, 否则高亮丢失
}

/* ---------- 控制 ---------- */
let timer = null;
const seek = document.getElementById('seek');
const btnPlay = document.getElementById('play');
const btnFirst = document.getElementById('first'), btnPrev = document.getElementById('prev');
const btnNext = document.getElementById('next'), btnLast = document.getElementById('last');

function stop() {
  if (timer) { clearInterval(timer); timer = null; }
  btnPlay.textContent = '▶ 播放';
}

function play() {
  if (timer) { stop(); return; }
  if (cur >= H.length) go(0);
  btnPlay.textContent = '⏸ 暂停';
  timer = setInterval(() => {
    if (cur >= H.length) { stop(); return; }
    go(cur + 1);
  }, 2000);
}

function go(n) {
  cur = Math.max(0, Math.min(H.length, n));
  seek.max = Math.max(1, H.length);
  render(cur);
  seek.value = cur;
  document.getElementById('plynum').textContent = cur + ' / ' + H.length;
  btnFirst.disabled = btnPrev.disabled = cur === 0;
  btnNext.disabled = btnLast.disabled = cur >= H.length;
  document.querySelectorAll('.mv.on').forEach(e => e.classList.remove('on'));
  const el = document.querySelector('.mv[data-ply="' + cur + '"]');
  if (el) { el.classList.add('on'); el.scrollIntoView({block: 'nearest'}); }
}

seek.addEventListener('input', () => { stop(); go(+seek.value); });
btnFirst.onclick = () => { stop(); go(0); };
btnPrev.onclick = () => { stop(); go(cur - 1); };
btnNext.onclick = () => { stop(); go(cur + 1); };
btnLast.onclick = () => { stop(); go(H.length); };
btnPlay.onclick = play;
document.addEventListener('keydown', e => {
  if (e.key === 'ArrowLeft') { stop(); go(cur - 1); e.preventDefault(); }
  else if (e.key === 'ArrowRight') { stop(); go(cur + 1); e.preventDefault(); }
  else if (e.key === 'Home') { stop(); go(0); }
  else if (e.key === 'End') { stop(); go(H.length); }
  else if (e.code === 'Space') { play(); e.preventDefault(); }
});

/* ---------- 实时跟随 ---------- */
async function sync() {
  if (!M.is_current) return;            // 在看历史对局, 不跟着当前局变
  try {
    const [rm, rs, rh] = await Promise.all([
      fetch('/meta?t=' + Date.now(), {cache: 'no-store'})
        .then(r => r.ok ? r.json() : null).catch(() => null),
      fetch('/state?t=' + Date.now(), {cache: 'no-store'})
        .then(r => r.ok ? r.json() : null).catch(() => null),
      fetch(location.pathname + '?format=json&t=' + Date.now(), {cache: 'no-store'})
        .then(r => r.ok ? r.json() : null).catch(() => null)
    ]);
    if (Array.isArray(rh) && rh.length !== H.length) {
      const follow = cur >= H.length;
      H = rh; buildMoves();
      go(follow ? H.length : Math.min(cur, H.length));
    }
    if (rm) Object.assign(M, rm);
    if (rs) {
      Object.assign(M, rs);
      M.draw_offer = rs.draw_offer || null;    // /state 没该字段 = 没人求和
      lastStatus = rs.status || lastStatus;
    }
    paintMeta(M);
  } catch (e) {}
}

/* 事件驱动: /watch 挂起直到手数或状态变化, 一落子立刻刷新 */
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function watchLoop() {
  if (!M.is_current) return;
  let fails = 0;
  while (true) {
    try {
      const r = await fetch('/watch?since=' + H.length +
                            '&st=' + encodeURIComponent(lastStatus) +
                            '&timeout=25&t=' + Date.now(), {cache: 'no-store'});
      if (!r.ok) throw new Error('watch ' + r.status);
      const j = await r.json();
      fails = 0;
      if (j.changed) { lastStatus = j.status || lastStatus; await sync(); }
    } catch (e) {
      if (++fails >= 3) {               // 服务端不支持 /watch: 退回轮询
        setInterval(sync, 2000);
        return;
      }
      await sleep(800);
    }
  }
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) sync(); });
watchLoop();

/* ---------- 启动 ---------- */
buildMoves();
paintMeta(Object.assign({}, M, {players: M.players}));
go(H.length);
</script>
</body>
</html>
"""


def _meta_from_archive(rec, game):
    """历史对局的 meta, 字段与当前局保持一致, 页面代码不用分叉"""
    players = []
    for side in ('red', 'black'):
        nm = rec.get(side)
        if nm:
            players.append({"side": side, "name": nm})
    return {
        "status": rec.get("status"),
        "turn": "red",
        "remaining_ms": 0,
        "end_reason": rec.get("end_reason"),
        "timed_out": rec.get("timed_out"),
        "resigned": rec.get("resigned"),
        "players": players,
        "auto_side": rec.get("auto_side"),
        "engine_level": rec.get("engine_level"),
        "step_s": int(game.timeout) if game.timeout > 0 else 0,
        "engine_step_s": int(game.engine_timeout) if game.engine_timeout > 0 else 0,
    }


def build_history_html(history, meta):
    """把走棋记录包成人类可看的回放页; 数据与 JSON 接口同源"""
    payload = json.dumps({"meta": meta, "history": history}, ensure_ascii=False)
    payload = payload.replace('</', '<\\/')          # 防止数据里出现 </script>
    return HISTORY_HTML.replace('%DATA%', payload)


# ============================================================
# 对局异常
# ============================================================
class GameError(Exception):
    code = 400

class TableFull(GameError):
    code = 409

class NotJoined(GameError):
    code = 403

class NotYourTurn(GameError):
    code = 403

class GameOver(GameError):
    code = 409

# ============================================================
# 对局状态
# ============================================================
def _board_to_json(board):
    return [[None if c is None else [c[0], c[1]] for c in row] for row in board]

def _board_from_json(data):
    return [[None if c is None else (c[0], c[1]) for c in row] for row in data]

class Game:
    def __init__(self, auto_side=None, timeout=60.0, log_path=None,
                 state_path=None, warn_before=20.0, engine_level=DEFAULT_LEVEL,
                 engine_timeout=600.0, engine_warn_before=60.0, games_dir=None):
        # 人机局固定席位: 电脑永远执黑, 挑战者执红先行
        self.auto_side = ENGINE_SIDE if auto_side else None
        self.games_dir = games_dir or None    # 每局一个 <gid>.json, 供列表与复现
        self.gid = None
        self.resigned = None
        self.engine_level = (engine_level if engine_level in ALL_LEVELS
                             else DEFAULT_LEVEL)
        self.timeout = float(timeout)                 # 双人对战: 每步限时
        self.engine_timeout = float(engine_timeout)   # 人机模式: 每步限时(默认 10 分钟)
        self.engine_warn_ms = int(max(0.0, engine_warn_before) * 1000)
        self.log_path = log_path
        self.state_path = state_path
        self.warn_ms = int(max(0.0, warn_before) * 1000)
        self.cond = threading.Condition()

        self.draw_offer = None     # 求和提议方: 'red' | 'black' | None
        self.players = {}          # token -> 'red' | 'black'
        self.names = {}
        self._pid = 0
        self._warned = set()       # 本回合已提醒过的 token
        self._acks = {}            # token -> 上一次成功走子的回执(用于幂等重发)
        self._turn_started = time.monotonic()
        self.reset(keep_players=False, save=False)   # 构造时不落盘, 否则会覆盖待恢复的存档

        self.restored = False
        self.restored_clock_reset = False
        if state_path:
            self.restored = self._load()
            if self.restored:
                self._archive()      # 重启后补一次归档, 免得列表里缺这一局

    # ---------- 持久化 ----------
    @staticmethod
    def _new_gid():
        ts = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        tail = ''.join(random.choice('abcdefghjkmnpqrstuvwxyz23456789')
                       for _ in range(4))
        return ts + '-' + tail

    def _names_by_side(self):
        out = {'red': None, 'black': None}
        for tok, side in self.players.items():
            out[side] = self.names.get(tok) or tok
        if self.auto_side and not out.get(self.auto_side):
            out[self.auto_side] = '电脑'      # 机器那侧没有玩家 token, 给个可读名字
        return out

    def archive_data(self):
        """一局的完整存档内容(列表页只取元信息, 复现页取 history)"""
        nm = self._names_by_side()
        return {
            "id": self.gid,
            "started_at": getattr(self, 'started_at', None),
            "updated_at": datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
            "status": self.status,
            "end_reason": self.end_reason,
            "timed_out": getattr(self, 'timed_out', None),
            "resigned": self.resigned,
            "auto_side": self.auto_side,
            "engine_level": self.engine_level,
            "red": nm['red'],
            "black": nm['black'],
            "history": self.history,
        }

    def _archive(self):
        """把当前局写到 games/<gid>.json; 空局不落盘"""
        if not self.games_dir or not self.gid or not self.history:
            return
        try:
            os.makedirs(self.games_dir, exist_ok=True)
            p = os.path.join(self.games_dir, self.gid + '.json')
            tmp = p + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.archive_data(), f, ensure_ascii=False)
            os.replace(tmp, p)
        except OSError:
            pass

    def games_list(self):
        """历史对局索引(不含走法), 新的在前"""
        if not self.games_dir or not os.path.isdir(self.games_dir):
            return []
        out = []
        for fn in sorted(os.listdir(self.games_dir)):
            if not fn.endswith('.json'):
                continue
            try:
                with open(os.path.join(self.games_dir, fn), encoding='utf-8') as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            hist = d.get('history') or []
            out.append({
                "id": d.get("id") or fn[:-5],
                "started_at": d.get("started_at"),
                "updated_at": d.get("updated_at"),
                "red": d.get("red"),
                "black": d.get("black"),
                "status": d.get("status"),
                "end_reason": d.get("end_reason"),
                "resigned": d.get("resigned"),
                "plies": len(hist),
                "engine_level": d.get("engine_level"),
                "auto_side": d.get("auto_side"),
                "last_move": hist[-1]["move"] if hist else None,
            })
        out.sort(key=lambda x: x.get("started_at") or x.get("updated_at") or '',
                 reverse=True)
        return out

    def load_game(self, gid):
        """取某局完整记录; 当前局优先内存(可能比磁盘新)"""
        gid = os.path.basename(str(gid))
        if not gid or gid in ('.', '..'):
            return None
        if self.gid == gid and self.history:
            return self.archive_data()
        if not self.games_dir:
            return None
        try:
            with open(os.path.join(self.games_dir, gid + '.json'),
                      encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _save(self):
        """原子写入当前对局, 供进程重启后恢复; 失败不影响对局"""
        self._archive()                 # 每步都刷新归档, 断电也只丢最后一步
        if not self.state_path:
            return
        data = {
            "players": self.players,
            "names": self.names,
            "pid": self._pid,
            "board": _board_to_json(self.board),
            "turn": self.turn,
            "status": self.status,
            "end_reason": self.end_reason,
            "timed_out": getattr(self, 'timed_out', None),
            "history": self.history,
            "auto_side": self.auto_side,
            "engine_level": self.engine_level,
            "gid": self.gid,
            "started_at": getattr(self, 'started_at', None),
            "resigned": self.resigned,
            "perpetual": self.perpetual,
            "draw_offer": self.draw_offer,
            "pos_history": self.pos_history,     # 长将检测用, 重启后接着判
            "saved_at": datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        }
        tmp = self.state_path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def _load(self):
        """启动时恢复上一次的对局; 恢复后当前回合重新计时(中断不算超时)"""
        try:
            with open(self.state_path, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        try:
            self.players = {str(k): str(v) for k, v in data["players"].items()}
            self.names = {str(k): str(v) for k, v in data.get("names", {}).items()}
            self._pid = int(data.get("pid", len(self.players)))
            self.board = _board_from_json(data["board"])
            self.turn = data["turn"]
            self.status = data["status"]
            self.end_reason = data.get("end_reason")
            self.timed_out = data.get("timed_out")
            self.history = data.get("history", [])
            if data.get("auto_side") and self.status in ('waiting', 'playing'):
                # 进行中的老存档可能是机器执红/挑战者执黑, 统一成人机固定席位
                self.auto_side = ENGINE_SIDE
                for tok in self.players:
                    self.players[tok] = HUMAN_SIDE
            elif data.get("auto_side"):
                # 已结束的旧局保持原样(换了席位会把棋谱里的红黑说反)
                self.auto_side = data.get("auto_side")
            lv = data.get("engine_level")
            if lv in ALL_LEVELS:
                self.engine_level = lv
            self.gid = data.get("gid") or self.gid
            self.started_at = data.get("started_at") or getattr(self, 'started_at', None)
            self.resigned = data.get("resigned")
            self.perpetual = data.get("perpetual")
            self.draw_offer = data.get("draw_offer")
            saved_pos = data.get("pos_history")
            if isinstance(saved_pos, list) and len(saved_pos) == len(self.history) + 1:
                self.pos_history = [str(k) for k in saved_pos]
            else:                                # 老存档没有 → 按棋谱重放
                self.pos_history = self._rebuild_pos_history(self.history)
        except (KeyError, TypeError, ValueError):
            return False
        # 对局进行中 → 重置当前回合计时, 避免"重启期间的时间"把玩家判负
        if self.status == 'playing':
            self._arm_clock()
            self.restored_clock_reset = True
        else:
            self.deadline = None
        return True

    # ---------- 玩家 ----------
    def _seated(self):
        s = set(self.players.values())
        if self.auto_side:
            s.add(self.auto_side)
        return s

    def _maybe_start(self):
        if self.status == 'waiting' and self._seated() >= {'red', 'black'}:
            self.status = 'playing'
            self._arm_clock()

    def _arm_clock(self):
        self._turn_started = time.monotonic()
        t = self.current_timeout()
        self.deadline = (self._turn_started + t) if t > 0 else None
        self._warned = set()          # 新回合, 预警重新计数

    def current_timeout(self):
        """当前对局的每步限时: 人机 10 分钟, 双人 60 秒"""
        return self.engine_timeout if self.auto_side else self.timeout

    def current_warn_ms(self):
        """当前对局的预警阈值: 人机最后 60 秒, 双人最后 20 秒"""
        if self.auto_side:
            return self.engine_warn_ms
        return self.warn_ms

    def join(self, name=None, token=None, vs=None, level=None):
        """返回 (token, side, resumed)。带有效 token 或同名 = 重连恢复。
        vs='engine' 时落座并让机器执黑开局(人机模式: 挑战者执红先行)。
        """
        nm = str(name)[:24] if name else None

        # 换难度走哪条路径都要生效(重连/同名恢复都会提前 return)
        if vs == 'engine' and level in ALL_LEVELS:
            self.engine_level = level

        # 1) 按 token 恢复
        if token and token in self.players:
            if nm:
                self.names[token] = nm
            return token, self.players[token], True

        # 2) 按昵称恢复(LLM 丢了 token 时)
        if not token and nm:
            for tok, saved in self.names.items():
                if saved == nm and tok in self.players:
                    return tok, self.players[tok], True

        # 3) 新玩家
        if vs == 'engine':
            # 人机固定席位: 电脑执黑, 挑战者执红先行
            if self._seated() & {HUMAN_SIDE, ENGINE_SIDE}:
                if self.status in ('red_win', 'black_win'):
                    self.reset(keep_players=False, save=False)   # 上一局已结束, 自动清桌
                else:
                    raise TableFull(
                        "engine game needs an empty table: challenger=red, engine=black; "
                        "POST /reset {\"clear\": true} first")
            self.auto_side = ENGINE_SIDE
            side = HUMAN_SIDE
        else:
            if self._seated() >= {'red', 'black'}:
                raise TableFull("table full (2 players already seated)")
            seated = self._seated()
            if self.auto_side:
                # 已是人机局: 真人只能是挑战者席位(红)
                if HUMAN_SIDE in seated:
                    raise TableFull(
                        "engine game already has its challenger (red); POST /reset first")
                side = HUMAN_SIDE
            elif not seated:
                side = random.choice(['red', 'black'])  # 第一人随机分边
            else:
                side = 'black' if 'red' in seated else 'red'
        self._pid += 1
        token = f"p{self._pid}"
        self.players[token] = side
        self.names[token] = nm or token
        self._maybe_start()
        self._save()
        return token, side, False

    def _reassign_sides(self):
        if not self.players:
            return
        if self.auto_side:
            self.auto_side = ENGINE_SIDE
            for tok in self.players:
                self.players[tok] = HUMAN_SIDE     # 人机: 挑战者固定执红
        else:
            sides = ['red', 'black']
            random.shuffle(sides)                 # 每局重新随机红黑
            for tok, side in zip(self.players, sides):
                self.players[tok] = side

    # ---------- 局面 ----------
    def reset(self, keep_players=True, save=True):
        self._archive()                            # 上一局先落盘再清空
        self.gid = self._new_gid()
        self.started_at = datetime.datetime.now().astimezone().isoformat(timespec='seconds')
        self.resigned = None
        self.board = build_initial_board()
        self.turn = 'red'                          # 红先行
        self.history = []
        self.draw_offer = None
        self.pos_history = [self._pos_key()]        # 局面重复检测(长将判负用)
        self.perpetual = None                       # 长将判负的一方
        self.end_reason = None
        self.timed_out = None
        self.deadline = None
        self._warned = set()
        if not keep_players:
            self._acks = {}
        self._turn_started = time.monotonic()
        self.status = 'waiting'
        if not keep_players:
            self.players.clear()
            self.names.clear()
            self.auto_side = None       # 清桌连机器席位一起清, 回到双人 waiting
        self._reassign_sides()
        self._maybe_start()
        if save:
            self._save()

    def tick(self):
        """惰性结算超时: 轮到某人走而超过 deadline, 该方判负"""
        if self.status != 'playing' or self.deadline is None:
            return False
        if time.monotonic() >= self.deadline:
            self.timed_out = self.turn
            self.status = 'black_win' if self.turn == 'red' else 'red_win'
            self.end_reason = REASON_TIMEOUT
            self.deadline = None
            self._save()
            return True
        return False

    def remaining_ms(self):
        if self.status != 'playing' or self.deadline is None:
            return 0
        return max(0, int((self.deadline - time.monotonic()) * 1000))

    def _do_move(self, move_str):
        """执行走法(不含身份校验)"""
        if self.status != 'playing':
            raise GameOver(f"game is not in progress: {self.status}")
        color = turn_code(self.turn)
        try:
            m = str_to_move(move_str)
        except ValueError as e:
            raise GameError(f"bad move format: {e}")
        if m not in gen_legal_moves(self.board, color):
            raise GameError(f"illegal move: {move_str}")
        x1, y1, x2, y2 = m
        captured = self.board[y2][x2]
        notation = move_notation(self.board, m)      # 必须用走子之前的局面
        self.board = make_move(self.board, x1, y1, x2, y2)
        now = time.monotonic()
        check = in_check(self.board, turn_code(opp_side(self.turn)))
        rec = {
            "ply": len(self.history) + 1,
            "by": self.turn,
            "move": move_to_str(m),
            "notation": notation,
            "captured": None if captured is None else (
                captured[1] if captured[0] == RED else captured[1].lower()
            ),
            "at": datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
            "elapsed_ms": max(0, int((now - self._turn_started) * 1000)),
        }
        if check:
            rec["check"] = True
        self.history.append(rec)
        self._append_log(rec)
        self.draw_offer = None          # 有人落子 → 之前的求和提议作废
        self.turn = 'black' if self.turn == 'red' else 'red'
        self.pos_history.append(self._pos_key())
        # 对方无合法走法 → 将死/困毙
        if not gen_legal_moves(self.board, turn_code(self.turn)):
            self.status = 'red_win' if self.turn == 'black' else 'black_win'
            self.end_reason = (REASON_CHECKMATE
                               if in_check(self.board, turn_code(self.turn))
                               else REASON_STALEMATE)
            self.deadline = None
        else:
            loser = self._perpetual_loser()          # 长将判负
            if loser:
                self.perpetual = loser
                self.status = 'black_win' if loser == 'red' else 'red_win'
                self.end_reason = REASON_PERPETUAL
                self.deadline = None
            else:
                self._arm_clock()
        self._save()

    def apply_move(self, player, move_str):
        if player not in self.players:
            raise NotJoined("not joined; POST /join first (or reconnect with your player id)")
        if self.status != 'playing':            # 终局优先报 409, 而不是 not your turn
            raise GameOver(f"game is not in progress: {self.status}")
        if self.players[player] != self.turn:
            raise NotYourTurn(
                f"not your turn: you are {self.players[player]}, "
                f"turn is {self.turn}")
        self._do_move(move_str)

    def resign(self, player):
        """认输: 该方判负, 对方获胜"""
        if player not in self.players:
            raise NotJoined("not joined; POST /join first (or reconnect with your player id)")
        if self.status != 'playing':
            raise GameOver(f"game is not in progress: {self.status}")
        side = self.players[player]
        self.resigned = side
        self.status = 'black_win' if side == 'red' else 'red_win'
        self.end_reason = REASON_RESIGN
        self.deadline = None
        self.draw_offer = None
        self._save()
        return self.snapshot(you=player)

    # ---------- 求和 ----------
    def propose_draw(self, player):
        """求和: 记下是谁提的, 等对手同意/反对; 机器一方不接和棋"""
        if player not in self.players:
            raise NotJoined("not joined; POST /join first (or reconnect with your player id)")
        if self.status != 'playing':
            raise GameOver(f"game is not in progress: {self.status}")
        side = self.players[player]
        if self.draw_offer == side:
            return {"draw": "pending", "by": side, **self.snapshot(you=player)}
        if self.auto_side and self.auto_side != side:
            # 机器不会回应, 直接按"反对"处理, 免得提议永远悬着
            self.draw_offer = None
            self._save()
            self.cond.notify_all()
            return {"draw": "declined", "by": side, "reason": "engine_declines",
                    "status": self.status, "turn": self.turn}
        self.draw_offer = side
        self._save()
        return {"draw": "offered", "by": side, **self.snapshot(you=player)}

    def answer_draw(self, player, accept):
        """回应求和: accept=True 和棋, False 继续下"""
        if player not in self.players:
            raise NotJoined("not joined; POST /join first (or reconnect with your player id)")
        if self.status != 'playing':
            raise GameOver(f"game is not in progress: {self.status}")
        side = self.players[player]
        if not self.draw_offer:
            raise GameError("no draw offer pending")
        if self.draw_offer == side:
            raise GameError("you offered the draw; wait for the opponent to answer")
        if accept:
            self.status = 'draw'
            self.end_reason = REASON_AGREEMENT
            self.deadline = None
            self.draw_offer = None
            self._save()
            self.cond.notify_all()
            return {"draw": "agreed", **self.snapshot(you=player)}
        self.draw_offer = None
        self._save()
        self.cond.notify_all()
        return {"draw": "declined", **self.snapshot(you=player)}

    # ---------- 幂等: 重发同一手不报错 ----------
    # ---------- 长将判负 ----------
    def _pos_key(self):
        """局面指纹(棋盘+轮次, 取哈希省空间), 不含 status —— 长将按"局面重复"判"""
        raw = render_board(self.board) + "|" + self.turn
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    @staticmethod
    def _rebuild_pos_history(history=None):
        """按棋谱重放出每步后的局面指纹(老存档没有该字段时用)"""
        keys = [hashlib.sha1((render_board(build_initial_board()) + "|red").encode())
                .hexdigest()[:16]]
        board = build_initial_board()
        turn = 'red'
        for r in (history or []):
            try:
                m = str_to_move(r["move"])
            except (KeyError, ValueError, TypeError):
                break
            board = make_move(board, *m)
            turn = opp_side(turn)
            keys.append(hashlib.sha1((render_board(board) + "|" + turn).encode())
                        .hexdigest()[:16])
        return keys

    def _perpetual_loser(self):
        """同一局面出现第 3 次时, 判断循环内谁在长将; 返回该方('red'/'black')或 None

        规则: 一个循环里某一方每一手都在将军、另一方没有 → 该方长将判负。
        双方都长将(或都没将军)不判, 留给对局继续。
        """
        if not self.pos_history:
            return None
        key = self.pos_history[-1]
        idx = [i for i, k in enumerate(self.pos_history) if k == key]
        if len(idx) < PERPETUAL_REPEAT:
            return None
        start = idx[-2]                     # 上一个循环的起点
        cyc = self.history[start:]          # 循环内的走法
        if not cyc:
            return None
        flags = {'red': [], 'black': []}
        for r in cyc:
            flags.setdefault(r.get('by'), []).append(bool(r.get('check')))
        checking = [s for s, f in flags.items() if f and all(f)]
        quiet = [s for s, f in flags.items() if f and not all(f)]
        if len(checking) == 1 and quiet:
            return checking[0]
        return None

    def _board_key(self):
        """局面指纹(棋盘+回合+状态); 变了就说明不是同一时刻的重发"""
        return render_board(self.board) + "|" + self.turn + "|" + self.status

    @staticmethod
    def _norm_move(move_str):
        """归一化走法(容忍大小写/连字符/空格); 非法返回 None"""
        try:
            return move_to_str(str_to_move(move_str))
        except (ValueError, TypeError):
            return None

    def ack_of(self, player, move_str):
        """同一玩家 + 同一手 + 局面未变 → 判定为重发, 返回上次回执"""
        rec = self._acks.get(player)
        if not rec:
            return None
        nm = self._norm_move(move_str)
        if nm is None or rec.get("move") != nm:
            return None
        if rec.get("key") != self._board_key():
            return None
        return rec.get("payload")

    def remember_ack(self, player, move_str, payload):
        """登记某玩家上一次成功走子的回执"""
        nm = self._norm_move(move_str)
        if nm is None:
            return
        self._acks[player] = {"move": nm, "key": self._board_key(),
                              "payload": payload}

    def _engine_move(self, color, moves):
        """机器选一步: 优先 Pikafish(强), 不可用或给出非法步时回落本地引擎"""
        lv = self.engine_level
        if lv in pikafish_uci.LEVELS:
            s = pikafish_uci.best_move(self.board, self.turn, lv)
            if s:
                try:
                    mv = str_to_move(s)
                except ValueError:
                    mv = None
                if mv is not None and mv in moves:
                    return mv
        weak = lv if lv in xiangqi_engine.LEVELS else 'depth2'
        return xiangqi_engine.choose_move(self.board, color, moves, weak,
                                          legal_fn=gen_legal_moves)

    def auto_play(self):
        """机器一方走棋: 本地引擎/Pikafish 选步(0 token)"""
        guard = 0
        while (self.status == 'playing' and self.auto_side == self.turn
               and guard < 200):
            guard += 1
            color = turn_code(self.turn)
            moves = gen_legal_moves(self.board, color)
            if not moves:
                break
            mv = self._engine_move(color, moves)
            if mv is None:
                break
            try:
                self._do_move(move_to_str(mv))
            except GameError:
                self._do_move(move_to_str(random.choice(moves)))

    def _append_log(self, rec):
        """走棋记录落盘 (JSONL, 追加); 记录失败不影响对局"""
        if not self.log_path:
            return
        try:
            with open(self.log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        except OSError:
            pass

    # ---------- 事件(供 /wait) ----------
    def event_for(self, who, since=None):
        """返回该玩家此刻应收到的事件, 无事件返回 None"""
        if who not in self.players:
            raise NotJoined("not joined; POST /join first")
        if self.status != 'playing':
            return EV_GAME_OVER
        # 轮到自己最优先
        if self.players[who] == self.turn:
            return EV_YOUR_TURN
        # 对手求和, 等我表态
        if self.draw_offer and self.draw_offer != self.players[who]:
            return EV_DRAW_OFFER
        # 对手落子
        if since is not None and since >= 0 and len(self.history) != since:
            return EV_OPPONENT_MOVED
        # 快超时提醒(每人每回合只提醒一次)
        rem = self.remaining_ms()
        warn = self.current_warn_ms()
        if warn and 0 < rem <= warn and who not in self._warned:
            self._warned.add(who)
            return EV_TIMEOUT_WARNING
        return None

    # ---------- 快照（只含棋局本身） ----------
    def last_move(self):
        """上一步(谁走的/坐标/中文记谱/是否吃子将军), 省得调用方自己 diff 棋盘"""
        if not self.history:
            return None
        r = self.history[-1]
        out = {"ply": r.get("ply"), "by": r.get("by"), "move": r.get("move")}
        if r.get("notation"):
            out["notation"] = r["notation"]
        if r.get("captured"):
            out["captured"] = r["captured"]
        if r.get("check"):
            out["check"] = True
        return out

    def snapshot(self, you=None, event=None):
        """event 为 None 时给完整局面(主动查询); 是事件时按事件裁剪, 省 token。

        your_turn  -> 完整: 要决策, 必须给棋盘和合法走法
        其他事件   -> 精简: 只给 status/turn/remaining_ms, 省下 500+ B

        合法走法**只给该走棋的一方**: 已知 you 且没轮到你时不返回 legal_moves
        (以前固定按 turn 给, 导致查询方拿到的是对手的着法集合)。
        """
        self.tick()
        full = event is None or event == EV_YOUR_TURN
        turn_color = turn_code(self.turn)
        your_side = self.players.get(you) if you and you in self.players else None
        snap = {
            "status": self.status,
            "turn": self.turn,
            "remaining_ms": self.remaining_ms(),
        }
        if full:
            snap = {
                "status": self.status,
                "turn": self.turn,
                "board": render_board(self.board, flip=(your_side == 'black')),
                "in_check": in_check(self.board, turn_color),
            }
            # 走法归属: 观战(不知道你是谁)给该走的一方并标 moves_for;
            # 已知你是谁时, 只有轮到你才给, 否则干脆不给(避免拿对手着法当自己的)
            mine = your_side is not None and your_side == self.turn
            if self.status == 'playing' and (mine or your_side is None):
                snap["legal_moves"] = " ".join(
                    move_to_str(m) for m in gen_legal_moves(self.board, turn_color))
                snap["moves_for"] = self.turn
            lm = self.last_move()
            if lm:
                snap["last_move"] = lm
            snap["remaining_ms"] = self.remaining_ms()
        else:
            lm = self.last_move()
            if lm:
                snap["last_move"] = lm
        if self.draw_offer:
            snap["draw_offer"] = self.draw_offer      # 谁在求和(等你回应用 accept)
        if self.end_reason:
            snap["end_reason"] = self.end_reason
            if self.end_reason == REASON_TIMEOUT and self.timed_out:
                snap["timed_out"] = self.timed_out
            elif self.end_reason == REASON_RESIGN and self.resigned:
                snap["resigned"] = self.resigned
            elif self.end_reason == REASON_PERPETUAL and self.perpetual:
                snap["perpetual_check"] = self.perpetual
        if you and you in self.players:
            snap["you"] = {"side": self.players[you],
                           "is_turn": self.players[you] == self.turn}
        return snap

    def meta(self):
        """页面对局卡片用的元信息: 只给席位名, 不给 token(它是重连凭证)"""
        return {
            "status": self.status,
            "turn": self.turn,
            "remaining_ms": self.remaining_ms(),
            "end_reason": self.end_reason,
            "timed_out": getattr(self, 'timed_out', None),
            "resigned": self.resigned,
            "players": [{"side": s, "name": self.names.get(t) or t}
                        for t, s in self.players.items()],
            "auto_side": self.auto_side,
            "engine_level": self.engine_level,
            "step_s": int(self.timeout) if self.timeout > 0 else 0,
            "engine_step_s": int(self.engine_timeout) if self.engine_timeout > 0 else 0,
        }

    def rules_md(self):
        tpl = RULES_MD.replace("%TOKEN%",
                               str(int(self.timeout)) if self.timeout > 0 else "不限")
        tpl = tpl.replace("%WARN%", str(int(self.warn_ms / 1000)) if self.warn_ms else "0")
        tpl = tpl.replace("%ENGINE%",
                          str(int(self.engine_timeout / 60)) if self.engine_timeout > 0 else "不限")
        tpl = tpl.replace("%ENGINE_WARN%", str(int(self.engine_warn_ms / 1000)))
        return tpl

# ============================================================
# HTTP 服务
# ============================================================
class Handler(BaseHTTPRequestHandler):
    game = None

    def log_message(self, fmt, *args):
        pass  # 静默

    # ---------- 基础设施 ----------
    def _send(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self._raw(code, body, 'application/json; charset=utf-8')

    def _send_text(self, code, text, ctype='text/plain; charset=utf-8'):
        self._raw(code, text.encode('utf-8'), ctype)

    def _raw(self, code, body, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Player')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except (TypeError, ValueError):
            n = 0
        raw = self.rfile.read(n) if n > 0 else b''
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise GameError(f"invalid json: {e}")
        if not isinstance(data, dict):
            raise GameError("body must be a JSON object")
        return data

    def _query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _player(self, data):
        return (self._query().get('player', [None])[0]
                or (data.get('player') if isinstance(data, dict) else None)
                or self.headers.get('X-Player'))

    def _route(self):
        return urllib.parse.urlparse(self.path).path

    def _wants_html(self, q, path):
        """内容协商: 浏览器 → HTML 回放页; 程序/LLM → 原样 JSON

        判定顺序: ?format=json|html > 特殊路径(/history.html) > Accept 头。
        curl / requests 默认 Accept: */*, 不会命中 text/html, 所以自动走 JSON。
        """
        fmt = (q.get('format', [None])[0] or '').strip().lower()
        if fmt in ('json', 'raw'):
            return False
        if fmt in ('html', 'web', 'view'):
            return True
        if path != '/history':
            return True
        return 'text/html' in (self.headers.get('Accept') or '').lower()

    def _guard(self, fn):
        try:
            fn()
        except GameError as e:
            self._send(e.code, {"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass                               # 客户端已断开, 静默
        except Exception as e:                 # 兜底: 绝不让连接被静默掐断
            try:
                self._send(500, {"error": f"{type(e).__name__}: {e}"})
            except Exception:
                pass

    # ---------- 路由 ----------
    def do_GET(self):
        self._guard(self._get)

    def do_OPTIONS(self):
        self._send(200, {})

    def do_POST(self):
        self._guard(self._post)

    def _get(self):
        path = self._route()
        if path == '/':
            self._send_text(200, self.game.rules_md(), 'text/markdown; charset=utf-8')
            return
        if path in ('/history', '/history.html', '/replay'):
            q = self._query()
            want = (q.get('game', [None])[0] or '').strip()
            with self.game.cond:
                g = self.game
                g.tick()
                archived = g.load_game(want) if want and want != g.gid else None
                if want and want != g.gid and archived is None:
                    self._send(404, {"error": f"game not found: {want}"}); return
                if archived is not None:
                    hist = archived.get('history') or []
                    meta = _meta_from_archive(archived, g)
                else:
                    hist = list(g.history)
                    meta = g.meta()
                meta["gid"] = (archived or {}).get('id') if archived else g.gid
                meta["is_current"] = archived is None
                meta["games"] = g.games_list()[:60]
            hist = annotate_history(hist)      # 老棋谱补中文记谱/将军标记
            if self._wants_html(q, path):
                self._send_text(200, build_history_html(hist, meta),
                                'text/html; charset=utf-8')
            else:
                self._send(200, hist)
            return
        if path == '/games':
            with self.game.cond:
                self._send(200, self.game.games_list())
            return
        if path == '/meta':
            with self.game.cond:
                self.game.tick()
                self._send(200, self.game.meta())
            return
        if path == '/watch':
            self._watch(); return
        if path == '/wait':
            self._wait(); return
        if path != '/state':
            self._send(404, {"error": "not found"}); return
        who = self._player({})
        with self.game.cond:
            self.game.auto_play()
            self._send(200, self.game.snapshot(you=who))

    def _await_event(self, who, since, max_wait):
        """阻塞到 who 有事件, 返回 (event, payload)。调用方须已持有 g.cond。"""
        g = self.game
        deadline = time.monotonic() + max_wait
        while True:
            g.tick()
            ev = g.event_for(who, since if since >= 0 else None)
            if ev:
                g.cond.notify_all()
                return ev, {"event": ev, **g.snapshot(you=who, event=ev)}
            left = deadline - time.monotonic()
            if left <= 0:
                return EV_IDLE, {"event": EV_IDLE,
                                 **g.snapshot(you=who, event=EV_IDLE)}
            g.cond.wait(min(left, 0.5))

    def _watch(self):
        """看板用长轮询: 挂起直到手数/状态变化(最多 timeout 秒), 供回放页自动刷新"""
        q = self._query()
        try:
            since = int(q.get('since', ['-1'])[0])
        except (TypeError, ValueError):
            since = -1
        st = q.get('st', [''])[0]
        try:
            max_wait = float(q.get('timeout', ['25'])[0])
        except (TypeError, ValueError):
            max_wait = 25.0
        max_wait = min(max(max_wait, 1.0), 110.0)

        with self.game.cond:
            g = self.game
            deadline = time.monotonic() + max_wait
            while True:
                g.tick()
                if len(g.history) != since or g.status != st:
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                g.cond.wait(min(left, 0.5))
            self._send(200, {"changed": bool(len(g.history) != since or g.status != st),
                             "ply": len(g.history), "status": g.status, "turn": g.turn})

    def _wait(self):
        """长轮询: 挂起请求直到有该玩家关心的事件"""
        q = self._query()
        who = (q.get('player', [None])[0] or self.headers.get('X-Player'))
        if not who:
            raise GameError("missing 'player' (例如 ?player=p1)")
        try:
            since = int(q.get('since', ['-1'])[0])
        except (TypeError, ValueError):
            since = -1
        try:
            max_wait = float(q.get('timeout', ['50'])[0])
        except (TypeError, ValueError):
            max_wait = 50.0
        max_wait = min(max(max_wait, 1.0), 110.0)

        with self.game.cond:
            if who not in self.game.players:
                raise NotJoined("not joined; POST /join first (or reconnect with your player id)")
            ev, payload = self._await_event(who, since, max_wait)
            self._send(200, payload)

    def _post(self):
        path = self._route()
        if path not in ('/join', '/move', '/reset', '/resign', '/draw'):
            self._send(404, {"error": "not found"}); return
        data = self._read_json()
        with self.game.cond:
            g = self.game
            if path == '/join':
                token, side, resumed = g.join(data.get('name'), data.get('player'),
                                              data.get('vs'), data.get('level'))
                g.auto_play()
                g.cond.notify_all()
                self._send(200, {"player": token, "side": side,
                                 "resumed": resumed,
                                 "board_legend": BOARD_LEGEND,
                                 **g.snapshot(you=token)})
                return
            if path == '/resign':
                snap = g.resign(self._player(data))
                g.cond.notify_all()
                self._send(200, snap)
                return
            if path == '/draw':
                who = self._player(data)
                accept = data.get('accept')
                payload = (g.answer_draw(who, bool(accept)) if accept is not None
                           else g.propose_draw(who))
                g.cond.notify_all()
                self._send(200, payload)
                return
            if path == '/reset':
                who = self._player(data)
                if who and who not in g.players:
                    raise NotJoined("not joined; POST /join first")
                g.reset(keep_players=not bool(data.get('clear')))
                g.auto_play()
                g.cond.notify_all()
                self._send(200, g.snapshot(you=who))
                return
            who = self._player(data)
            move = data.get('move')
            if not isinstance(move, str):
                raise GameError("missing 'move' string")
            prev = g.ack_of(who, move)          # 幂等: 重发同一手直接回上次结果
            if prev is not None:
                self._send(200, {**prev, "duplicate": True})
                return
            g.apply_move(who, move)
            g.auto_play()
            g.cond.notify_all()
            ply = len(g.history)
            # 先登记, 挡住 wait 挂起期间的重发
            g.remember_ack(who, move, {"ok": 1, "ply": ply, "turn": g.turn,
                                       "last_move": g.last_move()})

            # wait=<秒>: 走完直接挂起等对手, 一步棋只需 1 次请求(省一半 token)
            wait_sec = data.get('wait')
            if wait_sec:
                try:
                    wait_sec = float(wait_sec)
                except (TypeError, ValueError):
                    raise GameError("'wait' must be a number of seconds")
                wait_sec = min(max(wait_sec, 1.0), 110.0)
                ev, payload = self._await_event(who, ply, wait_sec)
                g.remember_ack(who, move, payload)
                self._send(200, payload)
                return
            # 不挂起: 只回走棋确认 + 上一步(对手可能已应手), 不给棋盘
            self._send(200, {"ok": 1, "ply": ply, "turn": g.turn,
                             "last_move": g.last_move()})

# ============================================================
# 入口
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--timeout', type=float, default=60.0,
                    help='每步限时(秒), 超时判负; 0 = 不限时')
    ap.add_argument('--warn-before', type=float, default=20.0,
                    help='双人对战剩余多少秒时推送 timeout_warning (0 = 不提醒)')
    ap.add_argument('--engine-timeout', type=float, default=600.0,
                    help='人机模式每步限时(秒), 默认 600 (10 分钟)')
    ap.add_argument('--engine-warn-before', type=float, default=60.0,
                    help='人机模式剩余多少秒时推送预警, 默认 60')
    ap.add_argument('--auto-opponent', choices=['black', 'none'],
                    default='none',
                    help='启动即开人机局(机器固定执黑, 挑战者执红先行); 默认 none')
    ap.add_argument('--engine', choices=list(ALL_LEVELS),
                    default=DEFAULT_LEVEL,
                    help='机器一方强度: easy/normal/hard/max (Pikafish) '
                         '或 random/greedy/depth2 (内置纯 Python 引擎)')
    ap.add_argument('--pikafish', default='auto',
                    help="Pikafish 二进制路径; auto=自动探测, off=禁用(只用内置引擎)")
    ap.add_argument('--pikafish-threads', type=int, default=1,
                    help='Pikafish 线程数, 默认 1')
    ap.add_argument('--pikafish-hash', type=int, default=64,
                    help='Pikafish 置换表大小(MB), 默认 64')
    ap.add_argument('--log', default='xiangqi_history.jsonl',
                    help='走棋记录落盘路径(JSONL); 传空串关闭')
    ap.add_argument('--state-file', default='xiangqi_state.json',
                    help='对局状态文件(重启后自动恢复); 传空串关闭')
    ap.add_argument('--games-dir', default='xiangqi_games',
                    help='历史棋谱目录, 每局一个 <gid>.json; 传空串关闭存档')
    args = ap.parse_args()

    # 先起 Pikafish(加载 NNUE 要几秒), 失败就静默回落内置引擎
    pf_path = None
    if args.pikafish.lower() != 'off':
        if args.pikafish.lower() != 'auto':
            pf_path = args.pikafish
        if pikafish_uci.configure(pf_path, threads=args.pikafish_threads,
                                  hash_mb=args.pikafish_hash):
            print(f"[engine] Pikafish 就绪: {pikafish_uci.engine_path()}")
        elif args.engine in pikafish_uci.LEVELS:
            print(f"[engine] Pikafish 不可用, {args.engine} 将回落内置引擎")
    elif args.engine in pikafish_uci.LEVELS:
        print(f"[engine] 已禁用 Pikafish, {args.engine} 将回落内置引擎")

    game = Game(
        auto_side=None if args.auto_opponent == 'none' else ENGINE_SIDE,
        timeout=args.timeout,
        log_path=args.log or None,
        state_path=args.state_file or None,
        warn_before=args.warn_before,
        engine_level=args.engine,
        engine_timeout=args.engine_timeout,
        engine_warn_before=args.engine_warn_before,
        games_dir=args.games_dir or None,
    )
    Handler.game = game
    if game.restored:
        print(f"[restore] 已恢复对局: status={game.status} turn={game.turn} "
              f"players={game.players} ply={len(game.history)}"
              f"{' (当前回合计时已重置)' if game.restored_clock_reset else ''}")
    srv = ThreadingHTTPServer(('0.0.0.0', args.port), Handler)
    srv.daemon_threads = True
    print(f"xiangqi on :{args.port}  timeout={args.timeout}s  "
          f"warn={args.warn_before}s  auto={args.auto_opponent}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        pikafish_uci.close()

if __name__ == '__main__':
    main()
