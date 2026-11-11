#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证本轮新增能力：
  1. 长轮询 /wait —— 轮到你 / 对手落子 / 快超时 / 终局
  2. 超时预警 —— 双方都能收到 timeout_warning
  3. 断线重连 —— 凭 player token 或同名恢复角色与棋盘
  4. 进程重启恢复 —— 状态落盘, 重启后棋盘/玩家在, 且当前回合重新计时
"""
import json
import os
import sys
import tempfile
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

# ------------------------------------------------------------
# HTTP 小工具
# ------------------------------------------------------------
def start_server(game, port=0):
    X.Handler.game = game
    srv = ThreadingHTTPServer(('127.0.0.1', port), X.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]

def get(port, path, timeout=30):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return json.load(r)

def post(port, path, data, timeout=30):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)

class Waiting(threading.Thread):
    """后台发一次 /wait, 记录结果与耗时"""
    def __init__(self, port, who, since=None, timeout=30):
        super().__init__(daemon=True)
        self.port, self.who, self.since, self.waittime = port, who, since, timeout
        self.result = None
        self.elapsed = None
        self.t0 = None
    def run(self):
        q = f"?player={self.who}&timeout={self.waittime}"
        if self.since is not None:
            q += f"&since={self.since}"
        self.t0 = time.monotonic()
        try:
            self.result = get(self.port, "/wait" + q, timeout=self.waittime + 10)
        except Exception as e:
            self.result = {"error": str(e)}
        self.elapsed = time.monotonic() - self.t0

def wait_for_playing(port, tries=40):
    for _ in range(tries):
        s = get(port, "/state")
        if s["status"] != "waiting":
            return s
        time.sleep(0.05)
    return get(port, "/state")

print("=" * 60)
print("A. 长轮询事件")
print("=" * 60)

tmpdir = tempfile.mkdtemp(prefix="xq_")
g = X.Game(timeout=60, state_path=os.path.join(tmpdir, "s1.json"))
srv, port = start_server(g)
try:
    st, p1 = post(port, "/join", {"name": "甲"})
    st, p2 = post(port, "/join", {"name": "乙"})
    red = p1["player"] if p1["side"] == "red" else p2["player"]
    blk = p2["player"] if red == p1["player"] else p1["player"]
    check("两人到齐即开局", p2["status"] == "playing", p2["status"])
    check("join 返回 resumed=false", p1["resumed"] is False and p2["resumed"] is False)
    check("join 带 you.side", p1.get("you", {}).get("side") == p1["side"], str(p1.get("you")))

    # A1. 轮到我 → 立即返回 your_turn
    w = Waiting(port, red, since=0, timeout=20)
    w.start(); w.join(10)
    check("A1 轮到红方 -> 立即 your_turn",
          w.result and w.result.get("event") == "your_turn",
          f"{w.result.get('event') if w.result else None} / {w.elapsed:.2f}s")
    check("A1 立即返回(不干等)", w.elapsed is not None and w.elapsed < 1.0,
          f"{w.elapsed:.2f}s")

    # A2. 非回合方挂起, 对手走子后唤醒
    w = Waiting(port, blk, since=0, timeout=25)
    w.start()
    time.sleep(0.6)
    alive = w.is_alive()
    post(port, "/move", {"player": red, "move": "h2e2"})
    w.join(10)
    check("A2 非回合方被挂起", alive)
    check("A2 对手落子 -> opponent_moved",
          w.result and w.result.get("event") in ("opponent_moved", "your_turn"),
          str(w.result.get("event") if w.result else None))
    check("A2 唤醒延迟 < 1.5s", w.elapsed is not None and w.elapsed < 1.5,
          f"{w.elapsed:.2f}s")

    # A3. 无事件时到点返回 idle（此时轮到黑方，让红方来等）
    w = Waiting(port, red, since=None, timeout=2)
    w.start(); w.join(6)
    check("A3 无变化 -> idle", w.result and w.result.get("event") == "idle",
          str(w.result.get("event") if w.result else None))
    check("A3 idle 按时返回(约2s)", 1.5 <= (w.elapsed or 0) <= 3.5,
          f"{w.elapsed:.2f}s")

    # A4. 未加入者 /wait
    st, body = post(port, "/move", {"player": "p99", "move": "h2e2"})
    check("A4 未加入走子 -> 403", st == 403, str(body))
    try:
        get(port, "/wait?player=p99")
        code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    check("A4 未加入 /wait -> 403", code == 403, str(code))

    # A5. /state 带 player 返回 you
    s = get(port, f"/state?player={blk}")
    check("A5 /state?player 返回 you", s.get("you") == {"side": g.players[blk],
                                                       "is_turn": g.players[blk] == g.turn},
          str(s.get("you")))
    s2 = get(port, "/state")
    check("A5 /state 不带 player 无 you 字段", "you" not in s2, str(sorted(s2)))
    check("A5 /state 含 remaining_ms", isinstance(s2.get("remaining_ms"), int),
          str(s2.get("remaining_ms")))
finally:
    srv.shutdown()

print()
print("=" * 60)
print("B. 超时预警（两方都能收到）")
print("=" * 60)

g = X.Game(timeout=6, warn_before=2, state_path=os.path.join(tmpdir, "s2.json"))
srv, port = start_server(g)
try:
    st, p1 = post(port, "/join", {"name": "甲"})
    st, p2 = post(port, "/join", {"name": "乙"})
    red = p1["player"] if p1["side"] == "red" else p2["player"]
    blk = p2["player"] if red == p1["player"] else p1["player"]

    # 红方不走; 黑方挂起等, 应在剩余 2s 时收到 warning
    w = Waiting(port, blk, since=0, timeout=20)
    w.start(); w.join(20)
    ev = (w.result or {}).get("event")
    rem = (w.result or {}).get("remaining_ms")
    check("B1 旁观方收到 timeout_warning", ev == "timeout_warning", f"event={ev} rem={rem}")
    check("B1 预警时剩余 <= 2s", isinstance(rem, int) and 0 < rem <= 2000, str(rem))
    check("B1 预警用时约 4s", 3.2 <= (w.elapsed or 0) <= 5.5, f"{w.elapsed:.2f}s")

    # 此刻红方仍在走, 再挂一次不应重复报警(等成 game_over)
    w2 = Waiting(port, blk, since=0, timeout=10)
    w2.start(); w2.join(15)
    ev2 = (w2.result or {}).get("event")
    check("B2 同一回合不重复预警", ev2 != "timeout_warning", f"event={ev2}")
    check("B2 超时后为 game_over", ev2 == "game_over", f"event={ev2}")
    check("B2 终局 black_win", (w2.result or {}).get("status") == "black_win",
          str((w2.result or {}).get("status")))
    check("B2 标明超时方为 red", (w2.result or {}).get("timed_out") == "red",
          str((w2.result or {}).get("timed_out")))
    check("B2 end_reason=timeout", (w2.result or {}).get("end_reason") == "timeout",
          str((w2.result or {}).get("end_reason")))

    # 轮到自己的人也应知道剩余时间
    st, rs = post(port, "/reset", {"clear": True})
    st, q1 = post(port, "/join", {"name": "甲"})
    st, q2 = post(port, "/join", {"name": "乙"})
    red2 = q1["player"] if q1["side"] == "red" else q2["player"]
    w3 = Waiting(port, red2, since=0, timeout=20)
    w3.start(); w3.join(15)
    check("B3 回合方 your_turn 且带 remaining_ms",
          (w3.result or {}).get("event") == "your_turn"
          and isinstance((w3.result or {}).get("remaining_ms"), int),
          str((w3.result or {}).get("remaining_ms")))
finally:
    srv.shutdown()

print()
print("=" * 60)
print("C. 断线重连")
print("=" * 60)

g = X.Game(timeout=60, state_path=os.path.join(tmpdir, "s3.json"))
srv, port = start_server(g)
try:
    st, p1 = post(port, "/join", {"name": "甲"})
    st, p2 = post(port, "/join", {"name": "乙"})
    red = p1["player"] if p1["side"] == "red" else p2["player"]
    blk = p2["player"] if red == p1["player"] else p1["player"]
    side_of = {p1["player"]: p1["side"], p2["player"]: p2["side"]}

    post(port, "/move", {"player": red, "move": "h2e2"})
    board_after = get(port, "/state")["board"]

    # C1. 凭 token 重连
    st, r1 = post(port, "/join", {"player": red})
    check("C1 凭 token 重连 resumed=true", r1.get("resumed") is True, str(r1.get("resumed")))
    check("C1 token 不变", r1.get("player") == red, str(r1.get("player")))
    check("C1 角色不变", r1.get("side") == side_of[red], str(r1.get("side")))
    check("C1 棋盘保持", r1.get("board") == board_after,
          f"期望与 /state 一致, 实际 {'一致' if r1.get('board') == board_after else '不一致'}")
    check("C1 手数保持", len(get(port, "/history")) == 1,
          str(len(get(port, "/history"))))

    # C2. 凭同名重连（LLM 丢了 token）
    st, r2 = post(port, "/join", {"name": "甲"})
    check("C2 同名重连 resumed=true", r2.get("resumed") is True, str(r2.get("resumed")))
    check("C2 拿回原 token", r2.get("player") in (p1["player"], p2["player"]),
          str(r2.get("player")))

    # C3. 重连后可继续走棋
    st, r3 = post(port, "/move", {"player": blk, "move": "h7e7"})
    check("C3 重连后正常走子", st == 200 and len(get(port, "/history")) == 2, str(st))

    # C4. 第三人加入仍 409
    st, r4 = post(port, "/join", {"name": "丙"})
    check("C4 新人仍被拒 409", st == 409, str(st))

    # C5. 用错误 token 不算重连
    st, r5 = post(port, "/join", {"player": "p99", "name": "丁"})
    check("C5 陌生 token 不劫持已有座位", st == 409, str(st))

    # C6. board 按执方视角: 黑方 180° 旋转, 但行列标签仍是真实坐标
    vr = get(port, f"/state?player={red}")["board"]
    vb = get(port, f"/state?player={blk}")["board"]
    vn = get(port, "/state")["board"]
    check("C6 红方视角首行 a..i", vr.splitlines()[0].split() == list("abcdefghi"),
          vr.splitlines()[0])
    check("C6 黑方视角首行 i..a", vb.splitlines()[0].split() == list("ihgfedcba"),
          vb.splitlines()[0])
    check("C6 黑方底线行在最下", vb.splitlines()[-1].startswith(" 9 r n b a k a b n r"),
          vb.splitlines()[-1])
    check("C6 无身份默认红方视角", vn == vr)
    check("C6 红黑文本不同", vb != vr)
finally:
    srv.shutdown()

print()
print("=" * 60)
print("D. 进程重启恢复")
print("=" * 60)

state_path = os.path.join(tmpdir, "s4.json")
g = X.Game(timeout=60, state_path=state_path)
g.join("甲"); g.join("乙")
red = [t for t, s in g.players.items() if s == 'red'][0]
g.apply_move(red, "h2e2")
before = {
    "players": dict(g.players),
    "turn": g.turn,
    "board": X.render_board(g.board),
    "ply": len(g.history),
}
check("D0 状态文件已落盘", os.path.exists(state_path), state_path)

g2 = X.Game(timeout=60, state_path=state_path)      # 模拟进程重启
check("D1 重启后 loaded=True", g2.restored is True, str(g2.restored))
check("D1 玩家与角色恢复", g2.players == before["players"], f"{g2.players} vs {before['players']}")
check("D1 轮次恢复", g2.turn == before["turn"], g2.turn)
check("D1 棋盘恢复", X.render_board(g2.board) == before["board"], "board 不一致")
check("D1 手数恢复", len(g2.history) == before["ply"], str(len(g2.history)))
check("D1 当前回合重新计时", g2.remaining_ms() > 55000, str(g2.remaining_ms()))
blk_tok = [t for t, s in g2.players.items() if s == 'black'][0]
g2.apply_move(blk_tok, "h7e7")
check("D1 仍可继续走棋", len(g2.history) == 2, str(len(g2.history)))

# D2. 终局状态也能恢复
g3 = X.Game(timeout=6, state_path=os.path.join(tmpdir, "s5.json"))
g3.join("甲"); g3.join("乙")
g3.deadline = time.monotonic() - 1
g3.tick()
g4 = X.Game(timeout=6, state_path=os.path.join(tmpdir, "s5.json"))
check("D2 终局状态可恢复", g4.status == g3.status and g4.end_reason == "timeout",
      f"{g4.status}/{g4.end_reason}")
check("D2 终局恢复后不重新计时", g4.remaining_ms() == 0, str(g4.remaining_ms()))

print()
print("=" * 60)
print("E. 每步限时设定 (step)")
print("=" * 60)

gd = X.Game(state_path=None)
check("E1 默认每步 300s", gd.current_timeout() == 300.0, str(gd.current_timeout()))
check("E1 meta 两个 step 都是 300",
      gd.meta()["step_s"] == 300 and gd.meta()["engine_step_s"] == 300,
      f"{gd.meta()['step_s']}/{gd.meta()['engine_step_s']}")

gd.join("甲", step=600)
check("E2 空桌第一人 step=600 生效", gd.timeout == 600.0, str(gd.timeout))
gd.join("乙", step=1200)
check("E3 第二人传 step 被忽略(加入即同意)", gd.timeout == 600.0, str(gd.timeout))

gd.reset(keep_players=True, save=False)
check("E4 保留玩家重开时沿用 600", gd.timeout == 600.0, str(gd.timeout))
gd.reset(keep_players=False, save=False, reset_step=True)
check("E5 清桌回到默认 300", gd.timeout == 300.0, str(gd.timeout))

ge = X.Game(state_path=None)
ge.join("挑战者", vs="engine", step=90)
check("E6 人机局 step 生效", ge.engine_timeout == 90.0, str(ge.engine_timeout))
check("E6 人机 current_timeout", ge.current_timeout() == 90.0, str(ge.current_timeout()))

for bad, label in ((5, "越界"), ("abc", "非法")):
    try:
        gd.join("丙", step=bad)
        code = 200
    except X.GameError as e:
        code = e.code
    check(f"E7 step {label} 返回 400", code == 400, str(code))

print()
print("=" * 60)
print(f"结果: {len(passed)} 通过, {len(failed)} 失败")
if failed:
    print("失败项: " + ", ".join(failed))
sys.exit(1 if failed else 0)
