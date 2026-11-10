"""验证 /move 幂等: 重发同一手不报错、不多走棋"""
import json
import os
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import xiangqi_server as X
from http.server import ThreadingHTTPServer

passed, failed = [], []


def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print(("PASS " if cond else "FAIL ") + name + (("  " + extra) if not cond and extra else ""))


def serve(game):
    X.Handler.game = game
    srv = ThreadingHTTPServer(('127.0.0.1', 0), X.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def post(B, path, data, timeout=60):
    req = urllib.request.Request(B + path, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def get(B, path):
    with urllib.request.urlopen(B + path, timeout=30) as r:
        return json.load(r)


# ============================================================
# A. 双人局(不挂起)
# ============================================================
print("=== A. 双人局不带 wait ===")
g = X.Game(timeout=0, state_path=None)
srv, B = serve(g)

a = post(B, "/join", {"name": "甲"})[1]
b = post(B, "/join", {"name": "乙"})[1]
red = a["player"] if a["side"] == "red" else b["player"]
blk = b["player"] if red == a["player"] else a["player"]

st1, r1 = post(B, "/move", {"player": red, "move": "h2e2"})
check("A1 首次提交 200", st1 == 200, str(r1))
ply1 = r1.get("ply")

st2, r2 = post(B, "/move", {"player": red, "move": "h2e2"})
check("A2 原样重发 200(不再 403)", st2 == 200, f"HTTP {st2} {r2}")
check("A3 重发被标记 duplicate", r2.get("duplicate") is True, str(r2))
check("A4 重发没有多走一手", r2.get("ply") == ply1, f"{ply1} -> {r2.get('ply')}")
check("A5 历史只有 1 手", len(get(B, "/history")) == 1, str(len(get(B, "/history"))))

st3, r3 = post(B, "/move", {"player": red, "move": "H2-E2"})
check("A6 换个写法(H2-E2)仍判为同手", st3 == 200 and r3.get("duplicate") is True, f"{st3} {r3}")

st4, r4 = post(B, "/move", {"player": blk, "move": "h7e7"})
check("A7 另一方正常走子不受影响", st4 == 200 and not r4.get("duplicate"), f"{st4} {r4}")

st5, r5 = post(B, "/move", {"player": red, "move": "b0c2"})
check("A8 走了别的子之后重发旧手不算 duplicate",
      st5 == 200 and not r5.get("duplicate"), f"{st5} {r5}")

st6, r6 = post(B, "/move", {"player": red, "move": "h2e2"})
check("A9 旧手在新局面下依正常校验(不该伪装成 duplicate)",
      not r6.get("duplicate"), f"{st6} {r6}")

st7, r7 = post(B, "/move", {"player": blk, "move": "h9g7"})     # 让回红方回合
check("A10 交回红方回合", st7 == 200 and not r7.get("duplicate"), f"{st7} {r7}")

st8, r8 = post(B, "/move", {"player": red, "move": "a0a5"})
check("A11 非法走法不缓存, 仍 400", st8 == 400, f"{st8} {r8}")
st9, r9 = post(B, "/move", {"player": red, "move": "a0a5"})
check("A12 非法走法重复提交仍 400", st9 == 400, f"{st9} {r9}")

g.reset(keep_players=True)
red_after = [t for t, s in g.players.items() if s == "red"][0]
st10, r10 = post(B, "/move", {"player": red_after, "move": "h2e2"})
check("A13 /reset 后重发不算 duplicate", st10 == 200 and not r10.get("duplicate"),
      f"{st10} {r10}")
srv.shutdown()

# ============================================================
# B. 人机局: 这正是 CodeArts 遇到的场景
# ============================================================
print("\n=== B. 人机局(固定席位: 我执红先行, 引擎执黑) ===")
g2 = X.Game(timeout=600, state_path=None)
srv2, B2 = serve(g2)
me = post(B2, "/join", {"name": "CodeArts", "vs": "engine"})[1]
who = me["player"]
check("B0 人机局挑战者固定执红", me["side"] == "red", str(me.get("side")))
check("B0 引擎执黑且红先行(还没人走)",
      g2.auto_side == "black" and me["turn"] == "red"
      and len(get(B2, "/history")) == 0,
      f"auto={g2.auto_side} turn={me.get('turn')}")

st, r = post(B2, "/move", {"player": who, "move": "h2e2"})
check("B1 首次走子 200", st == 200, f"{st} {r}")
ply_a = r.get("ply")
n_a = len(get(B2, "/history"))
check("B1 引擎已应手(记录 2 手)", n_a == 2, str(n_a))

st, r = post(B2, "/move", {"player": who, "move": "h2e2"})
check("B2 重发返回 200 而不是 400 illegal move", st == 200, f"HTTP {st} {r}")
check("B3 标记 duplicate", r.get("duplicate") is True, str(r))
check("B4 ply 没变(引擎没有再多走)", r.get("ply") == ply_a, f"{ply_a} -> {r.get('ply')}")
check("B5 历史手数没变", len(get(B2, "/history")) == n_a,
      f"{n_a} -> {len(get(B2, '/history'))}")

st, r = post(B2, "/move", {"player": who, "move": "h2e2", "wait": 20})
check("B6 带 wait 的重发也幂等", st == 200 and r.get("duplicate") is True, f"{st} {r}")

# 并发重发: 10 个线程同时提交同一手
g3 = X.Game(timeout=600, state_path=None)
srv3, B3 = serve(g3)
me3 = post(B3, "/join", {"name": "x", "vs": "engine"})[1]

res = []
lock = threading.Lock()


def fire():
    st, r = post(B3, "/move", {"player": me3["player"], "move": "h2e2"})
    with lock:
        res.append((st, r.get("duplicate"), r.get("error")))


ths = [threading.Thread(target=fire) for _ in range(10)]
[t.start() for t in ths]
[t.join(20) for t in ths]

ok200 = sum(1 for s, _d, _e in res if s == 200)
bad = [x for x in res if x[0] != 200]
check("C1 并发 10 次提交全部 200", ok200 == 10, str(bad))
check("C2 只有 1 次真走子, 其余 duplicate",
      sum(1 for _s, d, _e in res if not d) == 1,
      str([(s, d, e) for s, d, e in res]))
check("C3 历史 2 手(红 1 + 引擎应手 1)",
      len(get(B3, "/history")) == 2, str(len(get(B3, "/history"))))
srv3.shutdown()

print(f"\n结果: {len(passed)} 通过, {len(failed)} 失败")
if failed:
    print("失败项:", failed)
sys.exit(1 if failed else 0)
