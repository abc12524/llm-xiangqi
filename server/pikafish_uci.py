#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
skill: pikafish_uci
description: Pikafish(皮卡鱼) UCI 适配层 —— 象棋最强开源引擎跑在本地子进程, 棋盘转 FEN 送进去, 取回 h2e2 形式走法, 0 token
status: stable
usage:
  import pikafish_uci
  pikafish_uci.configure()                      # 自动探测二进制, 起常驻进程
  mv = pikafish_uci.best_move(board, 'red', 'hard')   # 'h2e2' / None
  pikafish_uci.close()
  档位 easy / normal / hard / max; PIKAFISH 环境变量可指定二进制路径
"""
import os
import queue
import random
import subprocess
import sys
import threading

LEVELS = ('easy', 'normal', 'hard', 'max')

# 档位 → (限深/限时, 数值, MultiPV, 放水分数差 cp)
# easy: 只看 1 层, 在 6 个候选里挑分差 <=120cp 的随机走, 会漏子, 适合新手
# normal: 3 层, 已经能看穿简单的兑子
# hard: 0.4 秒, 业余高手够不着
# max: 1.5 秒, 人类没机会
DIFFICULTY = {
    'easy':   ('depth', 1, 6, 120),
    'normal': ('depth', 3, 1, 0),
    'hard':   ('movetime', 400, 1, 0),
    'max':    ('movetime', 1500, 1, 0),
}

BIN_NAME = ('Pikafish-Windows-x86-64-universal.exe'
            if sys.platform.startswith('win')
            else 'Pikafish-Linux-x86-64-universal')

_engine = None
_lock = threading.Lock()
_cooldown_until = 0.0
FAIL_COOLDOWN = 30.0          # 引擎连续失败后静默 30s, 期间让调用方回落本地引擎


def _default_path():
    env = os.environ.get('PIKAFISH')
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [
        env,
        '/opt/xiangqi/pikafish/' + BIN_NAME,
        os.path.join(here, 'pikafish', BIN_NAME),
        os.path.join(here, BIN_NAME),
        os.path.join(os.path.expanduser('~'), 'Downloads', 'pikafish', BIN_NAME),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


# ============================================================
# 棋盘 → 象棋 FEN
# ============================================================
def board_to_fen(board, turn_color):
    """board[y][x] = (color, type) 或 None; y=0 红方底线。返回标准象棋 FEN。"""
    rows = []
    for y in range(9, -1, -1):          # FEN 第一行是黑方底线
        row = board[y]
        seg, empty = [], 0
        for x in range(9):
            p = row[x]
            if p is None:
                empty += 1
                continue
            if empty:
                seg.append(str(empty))
                empty = 0
            seg.append(p[1].upper() if p[0] == 'r' else p[1].lower())
        if empty:
            seg.append(str(empty))
        rows.append(''.join(seg))
    side = 'w' if turn_color == 'red' else 'b'
    return '/'.join(rows) + ' ' + side + ' - - 0 1'


# ============================================================
# UCI 进程封装
# ============================================================
class _Engine:
    def __init__(self, path, threads=1, hash_mb=64):
        self.path = path
        self.cwd = os.path.dirname(os.path.abspath(path))
        self.threads = max(1, int(threads))
        self.hash_mb = max(1, int(hash_mb))
        self.proc = None
        self.q = queue.Queue()
        self.ready = False

    # ---------- 生命周期 ----------
    def start(self):
        self.stop()
        kw = {}
        if sys.platform.startswith('win'):
            kw['creationflags'] = 0x08000000          # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            [self.path], cwd=self.cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0, text=True,
            encoding='utf-8', errors='replace', **kw)
        self.q = queue.Queue()
        threading.Thread(target=self._reader, args=(self.proc, self.q),
                         daemon=True).start()
        self._send('uci')
        if not self._wait('uciok', 15):
            raise RuntimeError('pikafish: 没有 uciok')
        self._set('Threads', self.threads)
        self._set('Hash', self.hash_mb)
        # 权重默认取同目录 pikafish.nnue, 缺失时引擎会崩, 显式确认一下
        nnue = os.path.join(self.cwd, 'pikafish.nnue')
        if os.path.isfile(nnue):
            self._set('EvalFile', nnue)
        self._send('isready')
        if not self._wait('readyok', 30):
            raise RuntimeError('pikafish: 没有 readyok(权重加载失败?)')
        self.ready = True

    def _reader(self, proc, q):
        try:
            for line in proc.stdout:
                q.put(line.rstrip('\r\n'))
        except (OSError, ValueError):
            pass
        q.put(None)

    def stop(self):
        self.ready = False
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            if p.poll() is None:
                p.stdin.write('quit\n')
                p.stdin.flush()
                p.wait(timeout=3)
        except Exception:
            pass
        finally:
            try:
                if p.poll() is None:
                    p.kill()
            except Exception:
                pass
            for s in (p.stdin, p.stdout):
                try:
                    s.close()
                except Exception:
                    pass

    def alive(self):
        return self.proc is not None and self.proc.poll() is None and self.ready

    # ---------- 收发 ----------
    def _send(self, s):
        self.proc.stdin.write(s + '\n')
        self.proc.stdin.flush()

    def _wait(self, prefix, timeout):
        """阻塞等一行以 prefix 开头的输出; 超时返回 False"""
        deadline = _now() + timeout
        while True:
            left = deadline - _now()
            if left <= 0:
                return False
            try:
                line = self.q.get(timeout=left)
            except queue.Empty:
                return False
            if line is None:
                return False
            if line.startswith(prefix):
                return True

    def _set(self, name, value):
        self._send('setoption name %s value %s' % (name, value))

    def _drain(self):
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                return

    def go(self, fen, level):
        """算一步; 返回 (move, score_cp) 或 (None, None)"""
        mode, val, multipv, margin = DIFFICULTY.get(level, DIFFICULTY['normal'])
        self._drain()
        if multipv > 1:
            self._set('MultiPV', multipv)
        self._send('position fen ' + fen)
        cmd = 'go %s %d' % ('depth' if mode == 'depth' else 'movetime', val)
        self._send(cmd)

        budget = (val / 1000.0 if mode == 'movetime' else val * 0.8) + 10
        deadline = _now() + budget
        cands = []                      # [(score, move)]
        best = None
        while True:
            left = deadline - _now()
            if left <= 0:
                break
            try:
                line = self.q.get(timeout=left)
            except queue.Empty:
                break
            if line is None:
                break
            if line.startswith('info') and ' pv ' in line and ' score ' in line:
                sc, mv, mpv = _parse_info(line)
                if mv:
                    cands = [c for c in cands if c[2] != mpv] + [(sc, mv, mpv)]
            elif line.startswith('bestmove'):
                parts = line.split()
                best = parts[1] if len(parts) > 1 else None
                break
        if multipv > 1:
            self._set('MultiPV', 1)
        if best in (None, '(none)', '0000'):
            return None, None
        if margin <= 0 or not cands:
            return best, None
        # 放水: 在分差 margin 内的候选里随机挑
        cands.sort(key=lambda c: -c[0])
        top = cands[0][0]
        pool = [c for c in cands if top - c[0] <= margin] or cands[:1]
        return random.choice(pool)[1], top


def _parse_info(line):
    """info depth .. multipv N score cp X pv h2e2 ... → (score, first_move, multipv)"""
    tok = line.split()
    sc, mpv = 0, 1
    for i, t in enumerate(tok):
        if t == 'multipv' and i + 1 < len(tok):
            try:
                mpv = int(tok[i + 1])
            except ValueError:
                pass
        elif t == 'score' and i + 2 < len(tok):
            kind = tok[i + 1]
            try:
                v = int(tok[i + 2])
            except ValueError:
                continue
            if kind == 'cp':
                sc = v
            elif kind == 'mate':
                sc = (100000 - abs(v)) * (1 if v > 0 else -1)
    try:
        mv = tok[tok.index('pv') + 1]
    except (ValueError, IndexError):
        mv = None
    return sc, mv, mpv


def _now():
    import time
    return time.monotonic()


# ============================================================
# 对外接口
# ============================================================
def configure(path=None, threads=1, hash_mb=64):
    """启动(或重启)常驻引擎。找不到二进制返回 False。"""
    global _engine
    with _lock:
        p = path or (_engine.path if _engine else None) or _default_path()
        if not p or not os.path.isfile(p):
            _engine = None
            return False
        if _engine and _engine.path == p and _engine.alive():
            return True
        if _engine:
            _engine.stop()
        eng = _Engine(p, threads=threads, hash_mb=hash_mb)
        try:
            eng.start()
        except Exception:
            eng.stop()
            _engine = None
            return False
        _engine = eng
        return True


def is_available():
    return _engine is not None and _engine.alive()


def engine_path():
    return _engine.path if _engine else None


def best_move(board, turn_color, level='normal'):
    """棋盘(server 格式) + 该谁走 → 'h2e2' 或 None。引擎异常时返回 None, 绝不抛。"""
    global _cooldown_until
    with _lock:
        if _now() < _cooldown_until:
            return None
        eng = _engine
        if eng is None:
            return None
        try:
            if not eng.alive():
                eng.start()
            mv, _ = eng.go(board_to_fen(board, turn_color), level)
            if mv:
                _cooldown_until = 0.0
            return mv
        except Exception:
            try:
                eng.stop()
            except Exception:
                pass
            _cooldown_until = _now() + FAIL_COOLDOWN
            return None


def close():
    global _engine
    with _lock:
        if _engine:
            _engine.stop()
            _engine = None


def _selftest():
    ok = configure()
    print('engine:', _engine.path if _engine else None, 'ok=', ok)
    if not ok:
        return 1
    # 初始局面
    board = [[None] * 9 for _ in range(10)]
    back = ['R', 'N', 'B', 'A', 'K', 'A', 'B', 'N', 'R']
    for x in range(9):
        board[0][x] = ('r', back[x])
        board[9][x] = ('b', back[x])
    for x in (1, 7):
        board[2][x] = ('r', 'C')
        board[7][x] = ('b', 'C')
    for x in range(0, 9, 2):
        board[3][x] = ('r', 'P')
        board[6][x] = ('b', 'P')
    print('fen:', board_to_fen(board, 'red'))
    for lv in LEVELS:
        print('%-7s →' % lv, best_move(board, 'red', lv))
    close()
    return 0


if __name__ == '__main__':
    sys.exit(_selftest())
