# skill: xiangqi
# description: 中国象棋对战（nuc5 服务端）：加入/重连、看局面、走子并等对手、棋谱与历史对局、认输、求和
# status: enable
# usage:
#   action = state（默认）| join | move | wait | history | games | resign | draw | reset
#     join: name / vs="engine"（人机: 你固定执红先行, 电脑执黑）/ level=hard|max|normal|easy
#           （Pikafish 四档）或 random|greedy|depth2 / step=每步限时秒数（默认 300, 30-3600,
#           0=不限；空桌第一人设定, 后来者加入即同意）；上一局已结束会自动清桌开新局
#     move: move="h2e2"；wait=秒（默认 55，走完直接挂起等对手，一次调用拿到下一手；0=不等）
#     history: last=N（末尾 N 手）；game=<gid> 看历史某局；games: limit=N（默认 20）
#     resign: 认输；draw: accept=true 同意对方求和 / false 反对（不带 accept = 自己提议）
#     reset: clear=true
#   配置: config.json 的 XIANGQI_URL（默认 http://192.168.30.181:5040）
#   示例: {"action":"move","move":"h2e2"} / {"action":"join","vs":"engine","level":"hard","step":600}
"""中国象棋服务端（xiangqi_server.py）的客户端技能。

服务端: POST /join /move /reset, GET /state /wait /history（每步默认 300s, join 的 step 可改）
棋盘: 列 a-i, 行 0-9; 红方视角行 0 在下; 黑方 token 拿到的 board 是 180° 旋转
      (i 列在左、行 0 在上), 两种视角的标签都是真实坐标, 走法 'h2e2' = h2 走到 e2 不用换算
子: 大写=红 K帅 A仕 B相 N马 R车 C炮 P兵; 小写=黑 k将 a士 b象 n马 r车 c炮 p卒

输出只保留决策需要的东西（省 token）:
  {"status","turn","left"(秒),"board","moves","last","check","draw_note","event","end"}
  board 是唯一棋盘表示; moves 是空格分隔的合法走法串, **只在轮到自己时给**
  (服务端按 player 判定归属, 不会把对手的着法当成你的); last 是上一步的
  "方 坐标 中文记谱 [吃子] [将军]"; draw_note 表示有人在求和。
  idle 事件只回 {"event","status","turn","left"}——轮不到自己时不必给全盘。

席位: join 成功后 token 存本目录 session.json（不返回给模型），后续 move/wait/reset 自动带上；
      丢了就带 name 重新 join（服务端按昵称恢复原席位）。
"""
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

try:  # android-agent / kelivo-agent
    from _envelope import ok, error
except ImportError:  # shell-tool
    from core.tools.envelope import ok, error

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_PATH = os.path.join(HERE, "session.json")
DEFAULT_URL = "http://192.168.30.181:5040"
MAX_EVENT_WAIT = 110.0
DEFAULT_EVENT_WAIT = 55.0

CH = {"K": "帅", "A": "仕", "B": "相", "N": "马", "R": "车", "C": "炮", "P": "兵",
      "k": "将", "a": "士", "b": "象", "n": "马", "r": "车", "c": "炮", "p": "卒"}

# 本机常驻 HTTP_PROXY，内网请求必须绕开代理，否则拿到 404/超时
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class XqError(Exception):
    def __init__(self, message, code="error", extra=None):
        super().__init__(message)
        self.code = code
        self.extra = extra or {}


# ---------- 配置与席位 ----------
def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _base_url():
    env = os.environ.get("XIANGQI_URL")
    if env:
        return env.rstrip("/")
    cfg = _read_json(os.path.join(HERE, "config.json"))
    if isinstance(cfg, dict) and cfg.get("XIANGQI_URL"):
        return str(cfg["XIANGQI_URL"]).rstrip("/")
    return DEFAULT_URL


def _session():
    d = _read_json(SESSION_PATH)
    return d if isinstance(d, dict) else {}


def _save_session(data):
    try:
        tmp = SESSION_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SESSION_PATH)
    except OSError:
        pass


def _token(sess):
    tok = sess.get("player")
    if not tok:
        raise XqError('还没入座: 先 {"action":"join","name":"你的名字"}', code="not_joined")
    return tok


# ---------- HTTP ----------
def _to_error(raw, status):
    try:
        msg = json.loads(raw).get("error") or ""
    except (ValueError, AttributeError):
        msg = ""
    msg = msg or (raw or "")[:150] or ("HTTP %s" % status)
    low = msg.lower()
    for needle, code in (("not joined", "not_joined"), ("not your turn", "not_your_turn"),
                         ("illegal move", "illegal_move"), ("table full", "table_full"),
                         ("not in progress", "game_over"), ("bad move format", "illegal_move")):
        if needle in low:
            return XqError(msg, code=code)
    return XqError(msg, code="http_%s" % status)


def _call(path, payload=None, timeout=20.0):
    url = _base_url() + path
    headers = {"Accept": "application/json"}       # 别让 /history 协商成 HTML 回放页
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise _to_error(exc.read().decode("utf-8", "replace"), exc.code)
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        raise XqError("连不上象棋服务 %s (%s)" % (_base_url(), exc), code="unreachable")
    try:
        return json.loads(raw)
    except ValueError:
        raise XqError("服务返回了非 JSON 数据", code="bad_response")


def _state_of(token):
    q = ("?player=" + urllib.parse.quote(token, safe="")) if token else ""
    return _call("/state" + q)


# ---------- 返回裁剪 ----------
def _brief(snap, event=None, my_side=None):
    """把服务端快照压成模型真正需要的字段（省 token）。"""
    out = {"status": snap.get("status"), "turn": snap.get("turn")}
    board = snap.get("board")
    if board:
        out["board"] = board
        if "legal_moves" in snap:          # 服务端只在轮到你(或观战)时给
            out["moves"] = snap["legal_moves"]
    if snap.get("status") == "playing" and snap.get("remaining_ms") is not None:
        out["left"] = int(round(snap["remaining_ms"] / 1000.0))
    if snap.get("in_check"):
        out["check"] = True
    lm = snap.get("last_move")
    if lm:
        seg = "%s %s" % (lm.get("by"), lm.get("move"))
        if lm.get("notation"):
            seg += " " + lm["notation"]
        if lm.get("captured"):
            seg += " 吃" + CH.get(lm["captured"], lm["captured"])
        if lm.get("check"):
            seg += " 将军"
        out["last"] = seg
    offer = snap.get("draw_offer")
    if offer:
        out["draw_note"] = ("对手求和, 用 draw accept=true/false 回应"
                            if my_side and offer != my_side
                            else "你已求和, 等对手回应")
    if event:
        out["event"] = event
    if snap.get("end_reason"):
        out["end"] = snap["end_reason"]
    if snap.get("timed_out"):
        out["timed_out"] = snap["timed_out"]
    if snap.get("perpetual_check"):
        out["perpetual_check"] = snap["perpetual_check"]
    return out


def _settle(resp, token):
    """统一 move/wait 的返回: 保证轮到自己时一定有棋盘, 其余事件保持精简。"""
    side = _session().get("side")
    event = resp.get("event")
    if not event:                                   # move 未带 wait: 只回 {ok,ply,turn}
        brief = _brief(_state_of(token), my_side=side)
        if resp.get("ply"):
            brief["ply"] = resp["ply"]
        return brief
    if event == "idle":                             # 轮不到自己: 不给棋盘
        return _brief(resp, event, side)
    if not resp.get("board"):                       # 终局/预警/求和等精简快照: 补一次全盘
        merged = dict(resp)
        merged.update(_state_of(token))
        resp = merged
    return _brief(resp, event, side)


# ---------- actions ----------
def _act_state(a, sess):
    return _brief(_state_of(sess.get("player")), my_side=sess.get("side"))


def _act_join(a, sess):
    payload = {}
    name = a.get("name") or sess.get("name")
    if name:
        payload["name"] = str(name)[:24]
    token = a.get("player") or sess.get("player")
    if token:
        payload["player"] = token
    if a.get("vs"):
        payload["vs"] = a["vs"]
    if a.get("level"):
        payload["level"] = a["level"]
    if a.get("step") is not None:
        payload["step"] = a["step"]        # 每步限时(秒), 0 = 不限; 后来者传了也会被忽略
    r = _call("/join", payload)
    _save_session({"player": r.get("player"), "side": r.get("side"),
                   "name": payload.get("name", "")})
    out = {"side": r.get("side"), "resumed": bool(r.get("resumed")),
           "step": r.get("step_s")}
    out.update(_brief(r, my_side=r.get("side")))
    return out


def _event_wait(value, default=DEFAULT_EVENT_WAIT):
    try:
        sec = float(value)
    except (TypeError, ValueError):
        sec = default
    return min(max(sec, 1.0), MAX_EVENT_WAIT)


def _act_move(a, sess):
    token = _token(sess)
    move = str(a.get("move") or "").strip()
    if not move:
        raise XqError('缺少 move（如 "h2e2"）', code="bad_move")
    try:
        wait = float(a.get("wait", DEFAULT_EVENT_WAIT))
    except (TypeError, ValueError):
        wait = DEFAULT_EVENT_WAIT
    payload = {"player": token, "move": move}
    if wait > 0:
        payload["wait"] = _event_wait(wait)
    try:
        resp = _call("/move", payload, timeout=(_event_wait(wait) if wait > 0 else 0) + 15)
    except XqError as exc:
        # 走错时直接把当前合法走法带上, 省掉一轮 /state
        if exc.code in ("illegal_move", "not_your_turn"):
            try:
                st = _state_of(token)
                exc.extra["turn"] = st.get("turn")
                if exc.code == "illegal_move":
                    exc.extra["moves"] = st.get("legal_moves", "")
                else:
                    exc.extra["left"] = int((st.get("remaining_ms") or 0) / 1000)
            except XqError:
                pass
        raise
    return _settle(resp, token)


def _act_wait(a, sess):
    token = _token(sess)
    sec = _event_wait(a.get("wait", DEFAULT_EVENT_WAIT))
    path = "/wait?player=%s&since=-1&timeout=%s" % (
        urllib.parse.quote(token, safe=""), ("%.1f" % sec).rstrip("0").rstrip("."))
    return _settle(_call(path, timeout=sec + 15), token)


def _act_history(a, sess):
    gid = str(a.get("game") or "").strip()
    path = "/history" + ("?game=" + urllib.parse.quote(gid, safe="") if gid else "")
    rows = _call(path)
    if not isinstance(rows, list):
        raise XqError("服务返回的棋谱不是数组", code="bad_response")
    total = len(rows)
    try:
        last = int(a.get("last") or 0)
    except (TypeError, ValueError):
        last = 0
    show = rows[-last:] if last > 0 else rows
    lines = []
    if last > 0 and total > last:
        lines.append("共 %d 手，只列最后 %d 手" % (total, last))
    for r in show:
        seg = "%s. %s %s" % (r.get("ply"), "红" if r.get("by") == "red" else "黑",
                             r.get("notation") or r.get("move"))
        cap = r.get("captured")
        if cap:
            seg += " 吃" + CH.get(cap, cap)
        seg += " %.1fs" % ((r.get("elapsed_ms") or 0) / 1000.0)
        lines.append(seg)
    return {"count": total, "text": "\n".join(lines) if lines else "(尚无走子)"}


RESULT_CN = {"red_win": "红胜", "black_win": "黑胜", "draw": "和棋"}


def _act_games(a, sess):
    rows = _call("/games")
    if not isinstance(rows, list):
        raise XqError("服务返回的对局列表不是数组", code="bad_response")
    try:
        limit = int(a.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    show = rows[:limit] if limit > 0 else rows
    lines = []
    for g in show:
        t = (g.get("started_at") or g.get("updated_at") or "").replace("T", " ")[5:16]
        res = RESULT_CN.get(g.get("status"), "进行中")
        why = {"timeout": "超时", "resign": "认输", "checkmate": "将死",
               "stalemate": "困毙", "perpetual_check": "长将",
               "agreement": "双方同意"}.get(g.get("end_reason"))
        seg = "%s  %s  %s(红) vs %s(黑)  %d手  %s%s" % (
            g.get("id"), t, g.get("red") or "空", g.get("black") or "空",
            g.get("plies") or 0, res, ("·" + why) if why else "")
        lines.append(seg)
    return {"count": len(rows), "text": "\n".join(lines) if lines else "(还没有历史对局)"}


def _act_resign(a, sess):
    payload = {}
    if sess.get("player"):
        payload["player"] = sess["player"]
    return _brief(_call("/resign", payload))


def _act_draw(a, sess):
    payload = {}
    if sess.get("player"):
        payload["player"] = sess["player"]
    if a.get("accept") is not None:
        payload["accept"] = bool(a.get("accept"))
    r = _call("/draw", payload)
    out = {"draw": r.get("draw")}
    if r.get("reason"):
        out["reason"] = r["reason"]
    if r.get("by"):
        out["by"] = r["by"]
    out.update(_brief(r, my_side=sess.get("side")))
    return out


def _act_reset(a, sess):
    payload = {}
    if sess.get("player"):
        payload["player"] = sess["player"]
    if a.get("clear"):
        payload["clear"] = True
    return _brief(_call("/reset", payload))


_ACTIONS = {"state": _act_state, "join": _act_join, "move": _act_move,
            "wait": _act_wait, "history": _act_history, "games": _act_games,
            "resign": _act_resign, "draw": _act_draw, "reset": _act_reset}


def run(arguments):
    a = arguments or {}
    action = str(a.get("action") or "state").strip().lower()
    if action not in _ACTIONS:
        return error("未知 action '%s'，可用: %s" % (action, " / ".join(sorted(_ACTIONS))),
                     code="unknown_action")
    try:
        return ok(_ACTIONS[action](a, _session()))
    except XqError as exc:
        return error(str(exc), code=exc.code, **exc.extra)
    except Exception as exc:  # noqa: BLE001
        return error("%s: %s" % (type(exc).__name__, exc), code="internal")


if __name__ == "__main__":
    print(run(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}))
