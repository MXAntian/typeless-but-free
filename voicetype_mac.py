#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Typeless (macOS) - 语音输入工具 · macOS 原生移植版
按住右 ⌘ Command 录音,松开后转写并自动输入到当前光标位置。

技术栈(全部本地 / 免 API key,呼应 "but-free"):
  - 录音: sounddevice (PortAudio)
  - ASR : mlx-whisper (Apple Silicon 原生,本地推理,无需联网/API key)
  - 热键: pynput (全局监听 右⌘ Command,可配置)
  - 注入: 剪贴板 + Cmd+V (中文可靠,优于逐字模拟键盘)
  - 托盘: rumps (macOS 状态栏常驻)

原 Windows 版 voicetype.py 依赖 keyboard/win32*/win10toast,在 macOS 不可用;
本文件是对等功能的 macOS 重写。

入口:
  python voicetype_mac.py            # 托盘 App(默认,打包目标)
  python voicetype_mac.py --cli      # 纯命令行(无托盘,便于调试)
  python voicetype_mac.py --selftest # 自检:用 `say` 合成语音 → 转写 → 验证 ASR 闭环(免权限)
  python voicetype_mac.py --mictest  # 录 4s 真麦克风 → 转写(只需麦克风权限,验录音+ASR)
"""

import sys
import os
import json
import queue
import tempfile
import threading
import subprocess
import time

import numpy as np

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
SAMPLE_RATE = 16000          # whisper 要求 16kHz
CHANNELS = 1                 # 单声道

DEFAULT_CONFIG = {
    # mlx-community 上的 whisper 模型(small=速度/中文准确度均衡;medium 更准但更慢更大)
    "model": "mlx-community/whisper-small-mlx",
    "language": "zh",        # 识别语言;设为 null 则自动检测
    "hotkey": "cmd_r",       # 录音热键:pynput Key 名。右⌘(Mac 一定有);可改 alt_r/shift_r/ctrl_r 等
    "sample_rate": SAMPLE_RATE,
    "min_seconds": 0.3,      # 短于此时长的录音视为误触,丢弃
    "max_record_seconds": 30,  # 看门狗:录音超过此秒数强制停止(防 release 丢失致麦克风无限常开)
    "simplified": True,      # whisper 倾向输出繁体;True 则用 opencc t2s 转简体
    "vocabulary": [],        # 常用词/专名(如 CLAUDE.md / agent / MCP)→ 喂 whisper initial_prompt 做热词偏置
    "corrections_file": "corrections.json",  # 学到的「误听→正确」替换表(纠错飞轮,落用户数据目录)
}

# 用户可写数据目录:打包成 .app 后 bundle 内只读且写入会破坏 ad-hoc 签名,
# 故 config / corrections 等运行期数据统一落这里。
DATA_DIR = os.path.expanduser("~/Library/Application Support/Typeless")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception as e:
        print(f"[data] 创建数据目录失败: {e}", file=sys.stderr)
    return DATA_DIR


def _user_config_path():
    return os.path.join(_data_dir(), "config.json")


def load_config():
    """读配置,缺失项用默认值补全。优先用户数据目录,回退脚本目录(源码调试)。"""
    cfg = dict(DEFAULT_CONFIG)
    for path in (os.path.join(DATA_DIR, "config.json"),
                 os.path.join(SCRIPT_DIR, "config.json")):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    cfg.update(json.load(f))
            except Exception as e:
                print(f"[config] 读取 {path} 失败,使用默认配置: {e}", file=sys.stderr)
            break
    return cfg


# ---------------------------------------------------------------------------
# 录音
# ---------------------------------------------------------------------------
class Recorder:
    """按住开始 / 松开停止的录音器。采集 float32 单声道 16kHz 音频。"""

    def __init__(self, sample_rate=SAMPLE_RATE):
        self.sample_rate = sample_rate
        self._q = queue.Queue()
        self._stream = None
        self._recording = False

    def _callback(self, indata, frames, time_info, status):
        if status:
            # 溢出/欠载等,打印但不中断
            print(f"[rec] {status}", file=sys.stderr)
        if self._recording:
            self._q.put(indata.copy())

    def start(self):
        import sounddevice as sd
        if self._recording:
            return
        # 清空残留
        while not self._q.empty():
            self._q.get_nowait()
        self._recording = True
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=CHANNELS,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()

    def stop(self):
        """停止并返回拼接后的 float32 numpy 一维数组(空则返回长度 0 数组)。"""
        if not self._recording:
            return np.zeros(0, dtype=np.float32)
        self._recording = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        chunks = []
        while not self._q.empty():
            chunks.append(self._q.get_nowait())
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(chunks, axis=0).reshape(-1).astype(np.float32)
        return audio


# ---------------------------------------------------------------------------
# 转写 (mlx-whisper)
# ---------------------------------------------------------------------------
_CC = None          # opencc t2s 转换器,懒加载
_CC_WARNED = False  # 转换器不可用只告警一次


def to_simplified(text):
    """繁体→简体(opencc t2s)。opencc 不可用时保留原文并告警(不静默吞)。"""
    global _CC, _CC_WARNED
    if not text:
        return text
    try:
        if _CC is None:
            from opencc import OpenCC
            _CC = OpenCC("t2s")
        return _CC.convert(text)
    except Exception as e:
        if not _CC_WARNED:
            print(f"[opencc] 简繁转换不可用,输出保留原文(可能为繁体): {e}", file=sys.stderr)
            _CC_WARNED = True
        return text


# ---------------------------------------------------------------------------
# 纠错飞轮(本地学习「误听→正确」)+ 常用词热词偏置
#   逻辑对齐 Windows 版 voicetype.py(load/apply/save_correction / add_vocab_term /
#   difflib 字符级 diff 学习),交互改用 mac 原生托盘菜单 + rumps.Window(见 TypelessApp)。
# ---------------------------------------------------------------------------
def _corrections_path(cfg):
    p = cfg.get("corrections_file", "corrections.json")
    return p if os.path.isabs(p) else os.path.join(_data_dir(), p)


def load_corrections(cfg):
    """读本地替换表 [{from,to}] → [(from,to)](长 from 优先,避免子串先替换)。"""
    try:
        p = _corrections_path(cfg)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            rules = [(d["from"], d["to"]) for d in data if d.get("from") and d.get("to")]
            return sorted(rules, key=lambda r: -len(r[0]))
    except Exception as e:
        print(f"[corrections] 读取失败: {e}", file=sys.stderr)
    return []


def apply_corrections(text, rules):
    for frm, to in rules:
        if frm and frm in text:
            text = text.replace(frm, to)
    return text


def save_correction(cfg, frm, to):
    """学一条「误听 frm → 正确 to」;from 已存在则更新。返回是否写入。"""
    frm, to = (frm or "").strip(), (to or "").strip()
    if not frm or not to or frm == to or len(frm) > 30:
        return False
    try:
        p = _corrections_path(cfg)
        data = []
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        for d in data:
            if d.get("from") == frm:
                d["to"] = to
                break
        else:
            data.append({"from": frm, "to": to})
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[corrections] 保存失败: {e}", file=sys.stderr)
        return False


def add_vocab_term(cfg, term):
    """把专名加进 vocabulary 并落盘 config(喂下次 initial_prompt)。返回是否新增。"""
    term = (term or "").strip()
    if not term or len(term) > 30:
        return False
    vocab = list(cfg.get("vocabulary") or [])
    if term in vocab:
        return False
    vocab.append(term)
    cfg["vocabulary"] = vocab
    try:
        with open(_user_config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[vocab] 配置写回失败: {e}", file=sys.stderr)
    return True


def learn_from_edit(cfg, original, edited):
    """字符级 diff(difflib)从用户更正中抽「听错→正确」规则,存本地 + 收进常用词。返回新学条数。
    字符级对音译/专名比词级更准(对齐 Windows 版 _learn_from_edit)。"""
    if not edited or not edited.strip() or edited == original:
        return 0
    try:
        import difflib
        n = 0
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(
                None, original, edited, autojunk=False).get_opcodes():
            if op == "replace":
                frm = original[i1:i2].strip()
                to = edited[j1:j2].strip()
                if frm and to and frm != to and len(frm) <= 30:
                    if save_correction(cfg, frm, to):
                        n += 1
                    add_vocab_term(cfg, to)
        return n
    except Exception as e:
        print(f"[learn] {e}", file=sys.stderr)
        return 0


class Transcriber:
    def __init__(self, model, language=None, simplified=True, vocabulary=None):
        self.model = model
        self.language = language
        self.simplified = simplified
        self.initial_prompt = self._make_prompt(vocabulary)
        self._warmed = False

    @staticmethod
    def _make_prompt(vocabulary):
        """常用词拼成 whisper initial_prompt(热词偏置:提升专名/术语识别敏感度)。空则 None。"""
        terms = [t.strip() for t in (vocabulary or []) if t and t.strip()]
        return "、".join(terms) if terms else None

    def set_vocabulary(self, vocabulary):
        self.initial_prompt = self._make_prompt(vocabulary)

    def warmup(self):
        """提前触发模型下载/加载(首次会从 HuggingFace 拉取权重)。"""
        if self._warmed:
            return
        import mlx_whisper  # 延迟 import,加快非转写路径启动
        silent = np.zeros(SAMPLE_RATE, dtype=np.float32)  # 1s 静音
        try:
            mlx_whisper.transcribe(
                silent, path_or_hf_repo=self.model,
                language=self.language, fp16=True, verbose=False,
            )
        except Exception as e:
            print(f"[asr] warmup 警告: {e}", file=sys.stderr)
        self._warmed = True

    def transcribe(self, audio):
        """audio: float32 16kHz 一维数组,返回识别文本(strip 后)。"""
        if audio is None or len(audio) == 0:
            return ""
        import mlx_whisper
        kwargs = dict(
            path_or_hf_repo=self.model,
            language=self.language, fp16=True, verbose=False,
        )
        if self.initial_prompt:
            kwargs["initial_prompt"] = self.initial_prompt
        result = mlx_whisper.transcribe(audio, **kwargs)
        text = (result.get("text") or "").strip()
        if self.simplified:
            text = to_simplified(text)
        return text


# ---------------------------------------------------------------------------
# 文本注入(剪贴板 + Cmd+V)
# ---------------------------------------------------------------------------
def _pbcopy(text):
    p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
    p.communicate(text.encode("utf-8"))


def _pbpaste():
    try:
        return subprocess.check_output(["pbpaste"]).decode("utf-8")
    except Exception:
        return ""


def inject_text(text, restore_clipboard=True):
    """把 text 写入剪贴板并模拟 Cmd+V 粘贴到当前光标处。
    需要『辅助功能(Accessibility)』权限才能模拟按键。
    """
    if not text:
        return
    from pynput.keyboard import Controller, Key
    prev = _pbpaste() if restore_clipboard else None
    _pbcopy(text)
    time.sleep(0.05)
    kb = Controller()
    kb.press(Key.cmd)
    kb.press("v")
    kb.release("v")
    kb.release(Key.cmd)
    if restore_clipboard and prev is not None:
        time.sleep(0.2)
        _pbcopy(prev)


# ---------------------------------------------------------------------------
# 热键监听 + 录音转写流程
# ---------------------------------------------------------------------------
class VoiceTypeEngine:
    """串联 热键 → 录音 → 转写 → 注入 的核心引擎。"""

    def __init__(self, cfg, on_status=None):
        self.cfg = cfg
        self.recorder = Recorder(cfg["sample_rate"])
        self.transcriber = Transcriber(cfg["model"], cfg.get("language"),
                                       cfg.get("simplified", True), cfg.get("vocabulary"))
        self.on_status = on_status or (lambda s: None)
        self._busy = False
        self._listener = None
        self._t0 = 0.0
        self._watchdog = None                     # 录音看门狗(防 release 丢失时麦克风无限常开)
        self.max_record_seconds = cfg.get("max_record_seconds", 30)
        self.corrections = load_corrections(cfg)  # 学到的「误听→正确」替换表
        self.last_text = ""                       # 上一次插入的文本(供「纠正上一句」学习)
        self.on_learned = None                    # 学到新规则后回调(刷新状态等)

    # 修饰键松开时,pynput@macOS 常把 cmd_r 退化成通用 cmd(或事件丢失)→ 严格 ==cmd_r 匹配不上
    _GENERIC = {"cmd_r": "cmd", "cmd_l": "cmd", "alt_r": "alt", "alt_l": "alt",
                "shift_r": "shift", "shift_l": "shift", "ctrl_r": "ctrl", "ctrl_l": "ctrl"}

    def _hotkey(self):
        from pynput.keyboard import Key
        name = self.cfg.get("hotkey", "cmd_r")
        return getattr(Key, name, Key.cmd_r)

    def _release_match(self, key):
        """松开匹配:精确热键 或 其通用修饰键形式(macOS 松开退化兜底)。"""
        from pynput.keyboard import Key
        name = self.cfg.get("hotkey", "cmd_r")
        if key == getattr(Key, name, None):
            return True
        g = self._GENERIC.get(name)
        return bool(g and key == getattr(Key, g, None))

    def _log_key(self, action, key):
        """诊断日志:记修饰键的真实键名到 DATA_DIR/keylog.txt(用于定位 release 退化/丢失)。"""
        try:
            with open(os.path.join(_data_dir(), "keylog.txt"), "a", encoding="utf-8") as f:
                f.write(f"{action}\t{key!r}\n")
        except Exception:
            pass

    def _start_watchdog(self):
        self._cancel_watchdog()
        self._watchdog = threading.Timer(self.max_record_seconds, self._on_watchdog)
        self._watchdog.daemon = True
        self._watchdog.start()

    def _cancel_watchdog(self):
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    def _on_watchdog(self):
        if self.recorder._recording:
            self._log_key("watchdog-forcestop", self._hotkey())
            self._stop_and_process()

    def _stop_and_process(self):
        """停止录音 → 校验时长 → 丢 worker 线程转写+注入。可被 release/toggle/watchdog 复用。"""
        self._cancel_watchdog()
        if not self.recorder._recording:
            return
        dur = time.time() - self._t0
        audio = self.recorder.stop()
        if dur < self.cfg.get("min_seconds", 0.3) or len(audio) == 0:
            self.on_status("⌨︎ 就绪(录音过短,已忽略)")
            return
        threading.Thread(target=self._process, args=(audio,), daemon=True).start()

    def _on_press(self, key):
        if key != self._hotkey():
            return
        self._log_key("press", key)
        if self.recorder._recording:
            # 兜底:上次松开事件丢失导致还在录 → 这次按下当作"停止"(toggle)
            self._stop_and_process()
            return
        if not self._busy:
            self._t0 = time.time()
            self.on_status("● 录音中…")
            self.recorder.start()
            self._start_watchdog()

    def _on_release(self, key):
        # 诊断:记录所有修饰键家族的松开真实键名(看 cmd_r 退化成了什么)
        if self._release_match(key) or any(m in repr(key) for m in ("cmd", "alt", "ctrl", "shift")):
            self._log_key("release", key)
        if self._release_match(key) and self.recorder._recording:
            self._stop_and_process()

    def _process(self, audio):
        self._busy = True
        try:
            self.on_status("✎ 转写中…")
            text = self.transcriber.transcribe(audio)
            text = apply_corrections(text, self.corrections)  # 应用学到的纠错规则
            if text:
                self.last_text = text
                self.on_status(f"→ {text[:30]}")
                inject_text(text)
            else:
                self.on_status("⌨︎ 就绪(未识别到语音)")
        except Exception as e:
            self.on_status(f"⚠︎ 出错: {e}")
            print(f"[engine] {e}", file=sys.stderr)
        finally:
            self._busy = False
            time.sleep(1.2)
            self.on_status("⌨︎ 就绪")

    def correct_last(self, edited):
        """用户给出上一句的更正版 → 字符级 diff 学习「误听→正确」+ 即时生效。返回新学条数。"""
        n = learn_from_edit(self.cfg, self.last_text, edited)
        if n:
            self.corrections = load_corrections(self.cfg)            # 重载替换表
            self.transcriber.set_vocabulary(self.cfg.get("vocabulary"))  # 刷新热词
            if self.on_learned:
                self.on_learned(n)
        return n

    def start(self):
        from pynput import keyboard
        self._listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        self._listener.start()

    def stop(self):
        if self._listener:
            self._listener.stop()


# ---------------------------------------------------------------------------
# 入口:托盘 / CLI / 自检
# ---------------------------------------------------------------------------
def run_tray():
    import rumps
    cfg = load_config()

    class TypelessApp(rumps.App):
        def __init__(self):
            super().__init__("⌨︎", quit_button="退出 Typeless")
            self._status_item = rumps.MenuItem("状态: 加载模型中…")
            # 纠错飞轮入口:点一下用 rumps 原生输入框改上一句 → 系统学会「误听→正确」并自动应用
            self._correct_item = rumps.MenuItem("纠正上一句…", callback=self._correct_last)
            self.menu = [self._status_item, None, self._correct_item]
            self.engine = VoiceTypeEngine(cfg, on_status=self._set_status)
            threading.Thread(target=self._boot, daemon=True).start()

        def _set_status(self, s):
            # rumps 回调可能来自 worker 线程,改标题是线程安全的属性写
            self._status_item.title = s
            self.title = "●" if s.startswith("●") else "⌨︎"

        def _correct_last(self, _):
            # 菜单回调跑在主线程(AppKit run loop)→ rumps.Window 在此安全弹出
            last = self.engine.last_text
            if not last:
                rumps.alert("Typeless", "还没有可纠正的句子,先按住右 ⌘ 说一句话。")
                return
            win = rumps.Window(
                title="纠正上一句",
                message="改成正确的文字;保存后系统会记住「听错→正确」并自动应用到以后:",
                default_text=last, ok="学习", cancel="取消",
                dimensions=(360, 90),
            )
            resp = win.run()
            if not (resp.clicked and resp.text.strip()):
                return
            n = self.engine.correct_last(resp.text.strip())
            if n:
                self._set_status(f"⌨︎ 就绪 · 已学会 {n} 条纠错")
                try:
                    rumps.notification("Typeless", "已学会",
                                       f"新增 {n} 条「误听→正确」规则,已自动应用到以后")
                except Exception:
                    pass  # 非 bundle 运行时 notification 可能不可用,不影响学习
            else:
                self._set_status("⌨︎ 就绪(无改动)")

        def _boot(self):
            # 先起热键监听 —— 否则下模型期间(首次 ~500MB)按任何键都没反应
            self.engine.start()
            self._set_status("⌨︎ 模型加载中…(已可按右 ⌘,首次转写稍等)")
            self.engine.transcriber.warmup()
            self._set_status("⌨︎ 就绪 · 按住右 ⌘ 说话")

    print("Typeless (macOS) 托盘已启动。按住右 ⌘ Command 录音,松开输入。")
    print("首次运行需在『系统设置 → 隐私与安全性』授予『麦克风』+『辅助功能』权限。")
    TypelessApp().run()


def run_cli():
    cfg = load_config()
    print("Typeless (macOS) CLI 模式。加载模型中(首次会下载)…")
    engine = VoiceTypeEngine(cfg, on_status=lambda s: print(f"\r{s}".ljust(60), end="", flush=True))
    engine.transcriber.warmup()
    print(f"\r就绪。按住【{cfg.get('hotkey','cmd_r')}】键说话,松开出字。退出:关闭此终端窗口。".ljust(60))
    engine.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        engine.stop()
        print("\n已退出。")


def run_selftest():
    """免权限自检:用 macOS `say` 合成一段中文语音 → mlx-whisper 转写 → 比对。
    验证『录音之外』的整条 ASR 链路(模型下载/加载/推理/中文输出)。
    """
    cfg = load_config()
    phrase = "今天天气很好我想去公园散步"
    print(f"[selftest] 合成语音: 「{phrase}」")
    tmpd = tempfile.mkdtemp()
    aiff = os.path.join(tmpd, "t.aiff")
    wav = os.path.join(tmpd, "t.wav")
    subprocess.check_call(["say", "-v", "Tingting", "-o", aiff, phrase])
    # 转成 16k 单声道 wav(ffmpeg),再让 whisper 直接读路径
    subprocess.check_call(
        ["ffmpeg", "-y", "-i", aiff, "-ar", str(SAMPLE_RATE), "-ac", "1", wav],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(f"[selftest] 加载模型 {cfg['model']} 并转写(首次会下载权重)…")
    import mlx_whisper
    t0 = time.time()
    result = mlx_whisper.transcribe(
        wav, path_or_hf_repo=cfg["model"], language=cfg.get("language"),
        fp16=True, verbose=False,
    )
    raw = (result.get("text") or "").strip()
    text = to_simplified(raw) if cfg.get("simplified", True) else raw
    dt = time.time() - t0
    if text != raw:
        print(f"[selftest] 原始(繁): 「{raw}」")
    print(f"[selftest] 识别结果: 「{text}」  (耗时 {dt:.1f}s)")
    # 宽松判定:识别文本含关键词即算通过(whisper 可能加标点/同音字)
    hit = sum(k in text for k in ["天气", "公园", "散步", "今天"])
    # 简体校验:开启 simplified 时不应残留繁体字
    no_trad = not any(t in text for t in ["氣", "園", "風", "點", "體"])
    ok = hit >= 2 and len(text) >= 4 and (no_trad or not cfg.get("simplified", True))
    print(f"[selftest] 关键词命中 {hit}/4 · 无繁体残留={no_trad}  →  {'PASS ✅' if ok else 'FAIL ❌'}")
    return 0 if ok else 1


def run_mictest(seconds=4):
    """录一段真麦克风音频 → 转写 → 打印。只需『麦克风』权限(无需辅助功能),
    用于在跑全链路前先验证 录音 + ASR 是否通。"""
    cfg = load_config()
    rec = Recorder(cfg["sample_rate"])
    tr = Transcriber(cfg["model"], cfg.get("language"), cfg.get("simplified", True))
    print(f"[mictest] 加载模型中(首次会下载)…")
    tr.warmup()
    print(f"[mictest] ● 开始录音 {seconds}s,请现在说话…")
    rec.start()
    time.sleep(seconds)
    audio = rec.stop()
    print(f"[mictest] 录到 {len(audio)/cfg['sample_rate']:.1f}s 音频,转写中…")
    if len(audio) == 0:
        print("[mictest] ✗ 没录到音频 —— 检查『系统设置→隐私与安全性→麦克风』是否已授权终端/App")
        return 1
    text = tr.transcribe(audio)
    print(f"[mictest] 识别结果: 「{text}」")
    print("[mictest] " + ("PASS ✅ 录音+ASR 链路通" if text else "✗ 未识别到语音(说话声音太小?或非中文?)"))
    return 0 if text else 1


def run_keytest():
    """诊断:打印每个按下/松开的键名,验证 pynput 能否收到全局键盘事件。
    - 按键【完全无输出】= 当前运行的程序(终端/App)没拿到『辅助功能』权限。
    - 按右 Ctrl 输出 `Key.ctrl_r` = 键名+权限都 OK,问题在主程序逻辑。
    - 输出别的名 = 你按的键不是 ctrl_r,把 config 的 hotkey 改成打印出来的名。
    """
    from pynput import keyboard
    cfg = load_config()
    print(f"[keytest] 当前 config 热键 = {cfg.get('hotkey')}")
    print("[keytest] 监听键盘中… 按键应打印键名;按右 Ctrl 看是否输出 Key.ctrl_r;按 Esc 退出。")
    print("[keytest] 若按键【完全没有输出】→ 当前程序没『辅助功能』权限(去系统设置授予后重跑)。")

    def on_press(key):
        print(f"  ↓ 按下: {key!r}", flush=True)

    def on_release(key):
        print(f"  ↑ 松开: {key!r}", flush=True)
        if key == keyboard.Key.esc:
            print("[keytest] 已退出。", flush=True)
            return False

    with keyboard.Listener(on_press=on_press, on_release=on_release) as l:
        l.join()
    return 0


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        sys.exit(run_selftest())
    elif "--mictest" in args:
        sys.exit(run_mictest())
    elif "--keytest" in args:
        sys.exit(run_keytest())
    elif "--cli" in args:
        run_cli()
    else:
        run_tray()


if __name__ == "__main__":
    # PyInstaller 打包后,某些依赖(如 hf-xet 下载器)会 spawn 子进程;
    # 不调 freeze_support(),子进程会从 main() 重新执行 → 误起多个托盘实例。
    import multiprocessing
    multiprocessing.freeze_support()
    main()
