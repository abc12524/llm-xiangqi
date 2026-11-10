#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""/history 内容协商与回放页自检（本机临时端口，不碰线上）"""
import json
import os
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))

import xiangqi_server as xs
import xiangqi_engine as eng

PORT = 0
OK = [0, 0]


def check(name, cond, extra=''):
    OK[0] += 1
    if cond:
        OK[1] += 1
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {extra}")


# ---------- 造一局 ----------
g = xs.Game(timeout=60, warn_before=20, engine_level='greedy')
t1, s1, _ = g.join('红方玩家')
t2, s2, _ = g.join('黑方玩家')
red_token = t1 if s1 == 'red' else t2
black_token = t2 if s1 == 'red' else t1

for _ in range(40):
    if g.status != 'playing':
        break
    color = xs.turn_code(g.turn)
    legal = xs.gen_legal_moves(g.board, color)
    mv = eng.choose_move(g.board, color, legal, 'greedy', xs.gen_legal_moves)
    who = red_token if g.turn == 'red' else black_token
    g.apply_move(who, xs.move_to_str(mv))

print(f"demo: {len(g.history)} 手, status={g.status}, turn={g.turn}")

xs.Handler.game = g
srv = ThreadingHTTPServer(('127.0.0.1', 0), xs.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f'http://127.0.0.1:{PORT}'


def get(url, accept=None):
    req = urllib.request.Request(url)
    if accept:
        req.add_header('Accept', accept)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.headers.get('Content-Type', ''), r.read()


BROWSER = 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'

print('\n[1] 内容协商')
st, ct, body = get(base + '/history')
check('无 Accept → JSON', 'application/json' in ct, ct)
check('JSON 是记录数组', isinstance(json.loads(body), list), body[:60])
check('JSON 手数一致', len(json.loads(body)) == len(g.history))

st, ct, body = get(base + '/history', BROWSER)
check('浏览器 Accept → HTML', 'text/html' in ct, ct)
html = body.decode()

st, ct, body = get(base + '/history?format=json', BROWSER)
check('?format=json 覆盖 Accept', 'application/json' in ct, ct)

st, ct, body = get(base + '/history?format=html')
check('?format=html 强制页面', 'text/html' in ct, ct)

st, ct, body = get(base + '/history.html')
check('/history.html 别名', 'text/html' in ct, ct)

print('\n[2] 页面内容')
check('占位符已替换', '%DATA%' not in html)
check('无 token 泄漏', red_token not in html and black_token not in html)
check('含玩家名', '红方玩家' in html and '黑方玩家' in html)

i = html.index('const D = ') + len('const D = ')
j = html.index(';\nlet H = ', i)
data = json.loads(html[i:j])
check('内嵌数据可解析', isinstance(data.get('history'), list))
check('内嵌手数一致', len(data['history']) == len(g.history))
check('meta 含席位', len(data['meta']['players']) == 2)

# 每一手的走法都要能还原到棋盘上（复算一遍前端算法）
block = {}
for p in [{'x': x, 'y': 0, 't': c} for x, c in enumerate('RNBAKABNR')]:
    block[(p['x'], p['y'])] = p['t']
for x in (1, 7):
    block[(x, 2)] = 'C'
for x in (0, 2, 4, 6, 8):
    block[(x, 3)] = 'P'
for x, c in enumerate('RNBAKABNR'):
    block[(x, 9)] = c.lower()
for x in (1, 7):
    block[(x, 7)] = 'c'
for x in (0, 2, 4, 6, 8):
    block[(x, 6)] = 'p'

bad = []
for rec in data['history']:
    mv = rec['move']
    f = (ord(mv[0]) - 97, int(mv[1]))
    t = (ord(mv[2]) - 97, int(mv[3]))
    piece = block.get(f)
    if piece is None:
        bad.append((rec['ply'], 'from 无子', mv))
        continue
    cap = block.get(t)
    if rec['captured'] and cap != rec['captured']:
        bad.append((rec['ply'], f"吃子不符 {cap} vs {rec['captured']}", mv))
    block.pop(f)
    block.pop(t, None)
    block[t] = piece
check('前端口径能重放全部走法', not bad, bad[:3])

print('\n[3] JSON 接口未变')
st, ct, body = get(base + '/state')
snap = json.loads(body)
check('/state 正常', snap['status'] == g.status and 'board' in snap)
check('/state 不含 history', 'history' not in snap)

open('replay_preview.html', 'w', encoding='utf-8').write(html)
print(f"\n{OK[1]}/{OK[0]} 通过; 预览写入 replay_preview.html ({len(html)} 字节)")
srv.shutdown()
