#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""认输 + 历史棋谱存档 + 对局列表复现。"""
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

def req(port, path, data=None, accept=None):
    hdr = {"Content-Type": "application/json"}
    if accept:
        hdr["Accept"] = accept
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                               data=None if data is None else json.dumps(data).encode(),
                               headers=hdr)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            body = resp.read()
            ct = resp.headers.get('Content-Type', '')
            if 'json' in ct:
                return resp.status, json.loads(body), ct
            return resp.status, body.decode('utf-8'), ct
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body), e.headers.get('Content-Type', '')
        except ValueError:
            return e.code, body.decode('utf-8', 'replace'), e.headers.get('Content-Type', '')

tmp = tempfile.mkdtemp(prefix="xq_games_")
gdir = os.path.join(tmp, "games")

print("=" * 56)
print("A. 认输")
print("=" * 56)

g = X.Game(timeout=60, state_path=os.path.join(tmp, "s.json"),
           games_dir=gdir, engine_level='greedy')
srv, port = start(g)
with g.cond:
    tok, side, _ = g.join('甲', vs='engine', level='greedy')
    g.auto_play()
    first = g.snapshot(you=tok)['legal_moves'].split()[0]
st, _, _ = req(port, "/move", {"player": tok, "move": first})
check("A1 开局走子成功", st == 200, st)
plies = len(g.history)

st, snap, _ = req(port, "/resign", {"player": tok})
check("A2 认输返回 200", st == 200, st)
check("A3 认输方判负", snap["status"] == ('black_win' if side == 'red' else 'red_win'),
      snap["status"])
check("A4 end_reason=resign", snap.get("end_reason") == "resign", snap.get("end_reason"))
check("A5 标明谁认输", snap.get("resigned") == side, snap.get("resigned"))
check("A6 棋谱保留", len(g.history) == plies and plies >= 2, len(g.history))

st, j, _ = req(port, "/resign", {"player": tok})
check("A7 终局后再认输报错", st in (400, 409), st)
st, j, _ = req(port, "/resign", {"player": "p999"})
check("A8 未加入者认输报错", st in (400, 403), st)

print("=" * 56)
print("B. 历史棋谱存档")
print("=" * 56)

gid1 = g.gid
f1 = os.path.join(gdir, gid1 + ".json")
check("B1 认输后生成存档文件", os.path.isfile(f1), gid1)
rec = json.load(open(f1, encoding='utf-8')) if os.path.isfile(f1) else {}
check("B2 存档含走法", len(rec.get("history") or []) == plies, len(rec.get("history") or []))
check("B3 存档含双方名字", bool(rec.get("red")) or bool(rec.get("black")),
      (rec.get("red"), rec.get("black")))
check("B4 存档记录终局原因", rec.get("end_reason") == "resign", rec.get("end_reason"))
check("B5 存档不含 token", not any(k in rec for k in ("players", "names")),
      list(rec.keys()))
check("B6 存档带 id/started_at", rec.get("id") == gid1 and rec.get("started_at"),
      (rec.get("id"), rec.get("started_at")))

st, lst, _ = req(port, "/games")
check("B7 /games 返回列表", st == 200 and isinstance(lst, list), type(lst).__name__)
check("B8 列表含本局", any(x.get("id") == gid1 for x in lst), [x.get("id") for x in lst])
one = next((x for x in lst if x.get("id") == gid1), {})
check("B9 列表带手数与结果", one.get("plies") == plies and one.get("status"),
      one)
check("B10 列表不含走法(省流量)", "history" not in one, list(one.keys()))

# 重开一局: 旧局必须还在
with g.cond:
    g.reset(keep_players=True)
    g.auto_play()
    first2 = g.snapshot(you=tok)['legal_moves'].split()[0]
req(port, "/move", {"player": tok, "move": first2})
gid2 = g.gid
st, lst2, _ = req(port, "/games")
check("B11 新局 gid 与旧局不同", gid1 != gid2, f"{gid1} vs {gid2}")
check("B12 旧局仍留在列表", any(x.get("id") == gid1 for x in lst2),
      [x.get("id") for x in lst2])

g3 = X.Game(timeout=60, state_path=os.path.join(tmp, "s3.json"), games_dir=gdir)
check("B13 空局不落盘", not os.path.isfile(os.path.join(gdir, g3.gid + ".json")), g3.gid)

print("=" * 56)
print("C. 棋谱复现")
print("=" * 56)

st, hist, ct = req(port, f"/history?game={gid1}")
check("C1 取历史局走法", st == 200 and isinstance(hist, list) and len(hist) == plies,
      f"{st} {type(hist).__name__}")
check("C2 与存档一致", hist == rec.get("history"), "不一致")
check("C3 默认(程序)仍返回 JSON", 'json' in ct, ct)

st, page, ct = req(port, f"/history?game={gid1}", accept="text/html,application/xhtml+xml")
check("C4 浏览器打开返回 HTML", st == 200 and 'html' in ct, f"{st} {ct}")
check("C5 页面内嵌该局数据", gid1 in page and str(plies) in page, "缺数据")
check("C6 页面含对局下拉框", 'id="gsel"' in page, "没有 gsel")
check("C7 页面不含 token", '"player": "p' not in page and 'p1"' not in page, "可能有 token")

st, j, _ = req(port, "/history?game=20000101-000000-zzzz")
check("C8 不存在的对局 404", st == 404, st)
st, j, _ = req(port, "/history?game=../../etc/passwd")
check("C9 路径穿越被挡", st == 404, st)

st, page, _ = req(port, "/history", accept="text/html")
check("C10 当前局页面也能切换", 'id="gsel"' in page and gid2 in page, "缺当前局 id")

print()
print("=" * 56)
print("结果: %d 通过, %d 失败" % (len(passed), len(failed)))
if failed:
    print("失败项:", failed)
sys.exit(1 if failed else 0)
