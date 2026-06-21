#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VoiceType — 本地语音听写 + AI 改写（Typeless 风格，全本地 STT）。

流程：按住热键说话 → 松开停止 → faster-whisper 本地转写（CUDA）
     → DeepSeek 润色去口水词 → 弹窗确认/编辑 → 插入回原焦点窗口。

依赖见 requirements.txt。配置见 config.json（首次运行自动生成）。
"""
import os
# hf-xet 后端在镜像/部分网络下会把小文件（config/tokenizer/vocab）下成 0 字节，
# 禁掉它退回普通 HTTP 下载。建议同时 pip uninstall hf-xet（见 setup.ps1）。
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import sys
# Windows 控制台默认 GBK，非 ASCII 字符（✓ / emoji）会抛 UnicodeEncodeError。
# 强制 stdout/stderr 走 UTF-8（errors=replace 兜底），避免 print 崩溃。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import json
import time
import queue
import ctypes
import threading
import math
from pathlib import Path

import numpy as np
import sounddevice as sd
import pyperclip
import requests
from pynput import keyboard

APP_NAME = "Typeless but Free"
# 打包(PyInstaller frozen)后用 exe 所在目录，便携：exe + config.json + models/ 同处
if getattr(sys, "frozen", False):
    HERE = Path(sys.executable).resolve().parent
else:
    HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
# 粘贴用的修饰键：macOS 是 Cmd+V，其它平台 Ctrl+V
PASTE_KEY = keyboard.Key.cmd if sys.platform == "darwin" else keyboard.Key.ctrl

DEFAULTS = {
    "hotkey": "ctrl_r",                   # 触发键：ctrl_r / alt_r / caps_lock / f8 / 单字符…
    "hotkey_mode": "hold",                # hold=按住说话松开停 ; double_tap=双击开始/再双击停
    "suppress_hotkey": False,             # 拦截该键系统默认行为（caps_lock 建议自动开）
    "sample_rate": 16000,                 # Whisper 原生 16k，别改
    "whisper_model": "large-v3",          # large-v3 质量最好；想更快用 "large-v3-turbo" 或 "medium"
    "whisper_device": "cuda",             # cuda | cpu | auto（auto=cuda 失败自动退 cpu）
    "whisper_compute_type": "float16",    # cuda 用 float16；cpu 会自动改 int8
    "language": None,                     # None=自动检测；想锁中文填 "zh"
    "cleanup_enabled": True,              # AI 润色开关；关掉则直接用原始转写（也无需 key）
    "streaming_partial": True,            # 录音时实时显示转写（边说边看）
    "llm_base_url": "https://api.deepseek.com",   # 任意 OpenAI 兼容端点（DeepSeek/OpenAI/本地…）
    "llm_model": "deepseek-v4-flash",             # 该端点的模型名
    "llm_api_key": "",                            # 填你自己的 key（留空且无 env → 跳过润色）
    "llm_api_key_file": "",                       # 可选：从某个 .env 文件读 *_API_KEY
    "min_record_seconds": 0.3,            # 短于此忽略（防误触）
    "beam_size": 1,                       # 1=最快；想更准可调 5（慢一点）
    "confirm_before_insert": False,       # False=直接插入；True=弹窗确认再插
    "self_download": True,                # 用 requests 直连下载模型（绕开 hf hub 的 Xet 0 字节 bug）
    "models_dir": None,                   # 模型存放目录；None=<脚本目录>/models
    "hf_endpoint": "https://hf-mirror.com",   # 国内镜像；海外直连可改 "https://huggingface.co"
    "model_use_proxy": False,                 # False=下模型绕过系统代理(Clash)，镜像直连
}

CLEANUP_SYSTEM_PROMPT = (
    "你是一个语音听写后处理器。用户给你一段语音转写的原始文字，"
    "它可能有口水词（呃、那个、就是）、口误、重复、缺标点。"
    "你的任务：清理成像认真打字打出来的成稿——去掉口水词和重复、"
    "修正明显口误、补齐标点、该分点的分点、保持原意和原语言不变。"
    "只输出清理后的正文，不要任何解释、前后缀、引号包裹。"
)


# ────────────────────────── 配置 ──────────────────────────
def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update(user)
        except Exception as e:
            print(f"[warn] config.json 解析失败，用默认值: {e}")
    else:
        CONFIG_PATH.write_text(
            json.dumps(DEFAULTS, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[init] 已生成默认配置: {CONFIG_PATH}")
    return cfg


def load_api_key(cfg):
    """key 解析顺序：config.llm_api_key → 环境变量 → config.llm_api_key_file 里的 *_API_KEY。"""
    KEYS = ("LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")
    k = (cfg.get("llm_api_key") or "").strip()
    if k:
        return k
    for var in KEYS:
        if os.environ.get(var):
            return os.environ[var].strip()
    kf = (cfg.get("llm_api_key_file") or "").strip()
    if kf and Path(kf).exists():
        for line in Path(kf).read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):].strip()
            for var in KEYS:
                if line.startswith(var + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


# ────────────────────── 模型直连下载（绕开 hf hub）──────────────────────
# HF 把仓库迁到 Xet 存储后，huggingface_hub 对 Xet 文件会下成 0 字节 → 加载报 JSON 错。
# 这里用 requests 直连 /resolve/main/ 拉文件到本地，彻底绕开那套逻辑。
MODEL_REPOS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}
# 超集；不同模型仓文件名略有差异（vocabulary.json vs .txt），缺的 404 跳过
MODEL_FILES = [
    "config.json", "model.bin", "tokenizer.json",
    "vocabulary.json", "vocabulary.txt", "preprocessor_config.json",
]


def ensure_model(name, models_dir, hf_endpoint="https://hf-mirror.com", use_proxy=False):
    """把模型文件直连下载到 <models_dir>/<name>/，返回该目录。

    已完整 → 直接返回不碰网络（warm start 离线可用、不受网络波动影响）。
    必需文件下载失败才报错；可选文件（如某些仓没有的 vocabulary.txt）失败仅跳过。
    use_proxy=False → 忽略系统代理（Clash 会 RST 到 HF 的连接），国内镜像直连。
    """
    repo = MODEL_REPOS.get(name, f"Systran/faster-whisper-{name}")
    dest = Path(models_dir) / name
    dest.mkdir(parents=True, exist_ok=True)
    sess = requests.Session()
    sess.trust_env = bool(use_proxy)  # False → 不读 HTTP(S)_PROXY 环境变量

    def have(f):
        p = dest / f
        return p.exists() and p.stat().st_size > 0

    ESSENTIAL = ("config.json", "model.bin", "tokenizer.json")
    if all(have(f) for f in ESSENTIAL) and (have("vocabulary.json") or have("vocabulary.txt")):
        return str(dest)  # 已齐，零网络

    base_url = f"{hf_endpoint.rstrip('/')}/{repo}/resolve/main"
    for fname in MODEL_FILES:
        if have(fname):
            continue
        target = dest / fname
        tmp = target.with_name(target.name + ".part")
        try:
            with sess.get(f"{base_url}/{fname}", stream=True, timeout=180, allow_redirects=True) as r:
                if r.status_code == 404:
                    continue  # 该仓没这个文件
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                done = 0
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
                        done += len(chunk)
                        if total > 5 << 20:  # 大文件打印进度
                            pct = done * 100 // total if total else 0
                            print(f"\r[model] {fname} {done >> 20}/{total >> 20} MB ({pct}%)", end="")
                if total > 5 << 20:
                    print()
                tmp.replace(target)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            if fname in ESSENTIAL:
                raise RuntimeError(f"下载必需文件 {fname} 失败: {e}"
                                   "（网络不稳可重试；或在 config.json 把 hf_endpoint 改成 https://hf-mirror.com）")
            print(f"[model] 跳过可选文件 {fname}: {e}")
            continue
    return str(dest)


# ────────────────────── Windows 焦点窗口 ──────────────────────
user32 = ctypes.windll.user32 if sys.platform == "win32" else None


def get_foreground_window():
    return user32.GetForegroundWindow() if user32 else None


def set_foreground_window(hwnd):
    """尽力把焦点还给目标窗口（Windows 抢焦点规则下尽力而为）。"""
    if not user32 or not hwnd:
        return
    try:
        # AttachThreadInput 绕过抢焦点限制
        fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        user32.AttachThreadInput(fg_thread, target_thread, True)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(fg_thread, target_thread, False)
    except Exception:
        try:
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass


# ────────────────────── CUDA DLL（faster-whisper GPU）──────────────────────
def add_cuda_dll_dirs():
    """把 pip 装的 nvidia-* CUDA DLL 目录加进搜索路径，否则 cuda 初始化/推理失败。

    Windows 上 DLL 在 nvidia/<pkg>/bin（不是 lib）；cublas 依赖 cuda_runtime 的
    cudart64_12.dll。对 ctranslate2 的加载器，prepend 到 PATH 比 add_dll_directory 更可靠。
    """
    try:
        import nvidia
        base = list(nvidia.__path__)[0]
        dll_dirs = []
        for sub in ("cublas", "cuda_runtime", "cudnn", "cuda_nvrtc"):
            d = os.path.join(base, sub, "bin")
            if os.path.isdir(d):
                os.add_dll_directory(d)
                dll_dirs.append(d)
        if dll_dirs:
            os.environ["PATH"] = os.pathsep.join(dll_dirs) + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        pass  # 系统级 CUDA 已在 PATH，或走 CPU


# ────────────────────────── STT 引擎 ──────────────────────────
class Transcriber:
    def __init__(self, cfg):
        self.cfg = cfg
        self.model = None

    def load(self):
        from faster_whisper import WhisperModel

        device = self.cfg["whisper_device"]
        compute = self.cfg["whisper_compute_type"]
        model_name = self.cfg["whisper_model"]

        if self.cfg.get("self_download", True):
            models_dir = self.cfg.get("models_dir") or str(HERE / "models")
            print(f"[stt] 检查/下载模型 {model_name} → {models_dir}")
            model_ref = ensure_model(model_name, models_dir,
                                     self.cfg.get("hf_endpoint", "https://hf-mirror.com"),
                                     self.cfg.get("model_use_proxy", False))
        else:
            model_ref = model_name

        if device in ("cuda", "auto"):
            add_cuda_dll_dirs()
            try:
                print(f"[stt] 加载 {model_name} 到 CUDA ({compute}) …")
                self.model = WhisperModel(model_ref, device="cuda", compute_type=compute)
                print("[stt] CUDA 就绪 [OK]")
                return
            except Exception as e:
                print(f"[stt] CUDA 加载失败，回退 CPU: {e}")
                if device == "cuda":
                    print("[stt] 提示：装 GPU 依赖 → pip install nvidia-cublas-cu12 nvidia-cuda-runtime-cu12")

        print(f"[stt] 加载 {model_name} 到 CPU (int8) …（首次较慢）")
        self.model = WhisperModel(model_ref, device="cpu", compute_type="int8")
        print("[stt] CPU 就绪 [OK]")

    def transcribe(self, audio_f32, initial_prompt=None):
        lang = self.cfg["language"]
        segments, info = self.model.transcribe(
            audio_f32,
            language=lang,
            vad_filter=True,
            beam_size=self.cfg.get("beam_size", 1),
            initial_prompt=initial_prompt or None,
        )
        text = "".join(seg.text for seg in segments).strip()
        return text, getattr(info, "language", lang)


# ────────────────────────── AI 润色 ──────────────────────────
def cleanup_text(raw, cfg, api_key):
    if not cfg["cleanup_enabled"] or not raw or not api_key:
        return raw
    try:
        resp = requests.post(
            f"{cfg['llm_base_url'].rstrip('/')}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": cfg["llm_model"],
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": CLEANUP_SYSTEM_PROMPT},
                    {"role": "user", "content": raw},
                ],
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[cleanup] 润色失败，用原始转写: {e}")
        return raw


# ────────────────────────── 录音 ──────────────────────────
class Recorder:
    def __init__(self, sample_rate):
        self.sample_rate = sample_rate
        self.frames = []
        self.stream = None
        self.recording = False
        self.level = 0.0   # 最近一帧 RMS 音量（驱动声波）

    def _callback(self, indata, frames, time_info, status):
        if self.recording:
            self.frames.append(indata.copy())
            try:
                self.level = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
            except Exception:
                pass

    def start(self):
        if self.recording:
            return
        self.frames = []
        self.recording = True
        self.stream = sd.InputStream(
            samplerate=self.sample_rate, channels=1, dtype="float32",
            callback=self._callback,
        )
        self.stream.start()

    def stop(self):
        if not self.recording:
            return None
        self.recording = False
        self.level = 0.0
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass
        self.stream = None
        if not self.frames:
            return None
        return np.concatenate(self.frames, axis=0).flatten().astype(np.float32)


# ────────────────────────── 热键解析 ──────────────────────────
def resolve_hotkey(name):
    name = name.strip().lower()
    if hasattr(keyboard.Key, name):
        return getattr(keyboard.Key, name)
    return keyboard.KeyCode.from_char(name)


def key_vk(k):
    """取虚拟键码（用于组合键主键匹配/win32 抑制；取不到返回 None）。"""
    try:
        return k.value.vk      # Key 枚举成员
    except AttributeError:
        vk = getattr(k, "vk", None)  # KeyCode
        if vk is not None:
            return vk
        ch = getattr(k, "char", None)  # from_char 不带 vk → 用 ASCII（A-Z/0-9 的 VK 即大写码）
        return ord(ch.upper()) if ch and len(ch) == 1 else None


# ────────────────────────── 弹窗 + 主循环 ──────────────────────────
class App:
    def __init__(self, cfg, api_key):
        self.cfg = cfg
        self.api_key = api_key
        self.recorder = Recorder(cfg["sample_rate"])
        self.transcriber = Transcriber(cfg)
        self.ui_queue = queue.Queue()
        raw_hk = cfg["hotkey"].strip().lower()
        self.mode = cfg.get("hotkey_mode", "hold")
        self.is_combo = "+" in raw_hk
        self._MODNAME = {
            keyboard.Key.alt_l: "alt", keyboard.Key.alt_r: "alt", keyboard.Key.alt_gr: "alt",
            keyboard.Key.ctrl_l: "ctrl", keyboard.Key.ctrl_r: "ctrl",
            keyboard.Key.shift: "shift", keyboard.Key.shift_l: "shift", keyboard.Key.shift_r: "shift",
            keyboard.Key.cmd: "cmd",
        }
        if self.is_combo:
            # 组合键，如 "alt+v"：拆成修饰键集合 + 主键 vk
            self.req_mods, self.req_main, self.hotkey = set(), None, None
            for p in [x.strip() for x in raw_hk.split("+") if x.strip()]:
                if p in ("ctrl", "control", "alt", "shift", "cmd", "win", "super"):
                    self.req_mods.add({"control": "ctrl", "win": "cmd", "super": "cmd"}.get(p, p))
                else:
                    self.req_main = key_vk(resolve_hotkey(p))
            self.suppress, self.suppress_vk = False, None
        else:
            self.hotkey = resolve_hotkey(raw_hk)
            self.suppress = cfg.get("suppress_hotkey", False) or raw_hk == "caps_lock"
            self.suppress_vk = key_vk(self.hotkey) if self.suppress else None
        self.pressed_mods = set()
        self.main_down = False
        self._chord_active = False
        self.target_hwnd = None
        self.kbctl = keyboard.Controller()
        self.busy = False
        self._t0 = 0.0
        self._last_tap = 0.0
        self._key_down = False
        self._listener = None
        self.indicator = None
        self._ind_shown = False
        self._model_lock = threading.Lock()   # 串行化 partial / final 转写，避免并发撞模型
        self._stream_stop = False
        self._context = ""   # 上一句识别结果，作为下次的 initial_prompt 提准
        self.debug = bool(os.environ.get("VOICETYPE_DEBUG"))

    # --- 录音起停 ---
    def _start_rec(self):
        self.target_hwnd = get_foreground_window()
        self._t0 = time.time()
        self.recorder.start()
        print("[rec] ●录音中…")
        self.ui_queue.put(("status", "录音中…", self.C["red"]))
        self.ui_queue.put(("partial", ""))
        if self.cfg.get("streaming_partial", True):
            self._stream_stop = False
            threading.Thread(target=self._stream_loop, daemon=True).start()

    def _stream_loop(self):
        """录音中每 ~0.6s 拿已录音频跑一次 turbo，实时吐 partial 文字。"""
        sr = self.cfg["sample_rate"]
        while not self._stream_stop and self.recorder.recording:
            time.sleep(0.6)
            frames = list(self.recorder.frames)
            if not frames:
                continue
            audio = np.concatenate(frames, axis=0).flatten().astype(np.float32)
            if len(audio) < sr // 2:          # < 0.5s 不转
                continue
            try:
                with self._model_lock:
                    if self._stream_stop:
                        break
                    segs, _ = self.transcriber.model.transcribe(
                        audio, language=self.cfg["language"], vad_filter=False,
                        beam_size=1, initial_prompt=self._context or None)
                    partial = "".join(s.text for s in segs).strip()
                if partial and not self._stream_stop:
                    self.ui_queue.put(("partial", partial))
            except Exception as e:
                if self.debug:
                    print("[stream] err:", e, flush=True)

    def _stop_rec(self):
        self._stream_stop = True
        dur = time.time() - self._t0
        audio = self.recorder.stop()
        if dur < self.cfg["min_record_seconds"] or audio is None or len(audio) == 0:
            print("[rec] 太短，忽略")
            return
        print(f"[rec] ■停止（{dur:.1f}s），转写中…")
        self.busy = True
        threading.Thread(target=self._process, args=(audio,), daemon=True).start()

    # --- 热键回调（监听线程）---
    @staticmethod
    def _vk_of(key):
        vk = getattr(key, "vk", None)
        if vk is not None:
            return vk
        return getattr(getattr(key, "value", None), "vk", None)

    def on_press(self, key):
        if self.debug:
            print(f"[key↓] {key!r} vk={self._vk_of(key)}", flush=True)
        if self.is_combo:
            return self._combo_event(key, True)
        if key != self.hotkey:
            return
        if self.mode == "hold":
            if not self.recorder.recording and not self.busy:
                self._start_rec()
        else:  # double_tap
            if self._key_down:        # 忽略按住时的自动重复
                return
            self._key_down = True
            now = time.time()
            if now - self._last_tap < 0.45:
                self._last_tap = 0.0
                if self.recorder.recording:
                    self._stop_rec()
                elif not self.busy:
                    self._start_rec()
            else:
                self._last_tap = now

    def on_release(self, key):
        if self.is_combo:
            return self._combo_event(key, False)
        if key != self.hotkey:
            return
        self._key_down = False
        if self.mode == "hold" and self.recorder.recording:
            self._stop_rec()

    def _combo_event(self, key, is_press):
        """组合键（如 alt+v）：维护按下集合，整组按齐=触发，松开任一=停。"""
        m = self._MODNAME.get(key)
        if m:
            (self.pressed_mods.add if is_press else self.pressed_mods.discard)(m)
        elif self._vk_of(key) == self.req_main:
            self.main_down = is_press
        else:
            return
        chord = self.req_mods <= self.pressed_mods and self.main_down
        if chord and not self._chord_active:
            self._chord_active = True
            if self.mode == "toggle":
                if self.recorder.recording:
                    self._stop_rec()
                elif not self.busy:
                    self._start_rec()
            elif not self.recorder.recording and not self.busy:
                self._start_rec()
        elif not chord and self._chord_active:
            self._chord_active = False
            if self.mode != "toggle" and self.recorder.recording:
                self._stop_rec()

    def _win32_filter(self, msg, data):
        """抑制热键的系统默认行为，回调照常触发。
        - 组合键：按住修饰键时拦掉主键（防 Alt+V 触发程序「视图」菜单）
        - 单键：拦掉如 caps_lock 的大小写切换
        """
        vk = getattr(data, "vkCode", None)
        if self.is_combo:
            if vk == self.req_main and self.req_mods <= self.pressed_mods and self._listener is not None:
                self._listener.suppress_event()
        elif self.suppress_vk is not None and vk == self.suppress_vk and self._listener is not None:
            self._listener.suppress_event()
        return True

    def _post(self, msg):
        self.ui_queue.put(msg)

    def _insert(self, text, hwnd):
        if not text.strip():
            return
        pyperclip.copy(text)
        time.sleep(0.1)
        set_foreground_window(hwnd)
        time.sleep(0.1)
        with self.kbctl.pressed(PASTE_KEY):
            self.kbctl.press("v")
            self.kbctl.release("v")
        print("[ok] 已插入")

    def _process(self, audio):
        try:
            self._post(("status", "识别中…", self.C["amber"]))
            with self._model_lock:
                raw, lang = self.transcriber.transcribe(audio, self._context)
            print(f"[stt] [{lang}] {raw}")
            if not raw:
                self._post(("status", "没听清，再说一次", self.C["sub"]))
                self._post(("hide", 1200))
                return
            self._post(("partial", raw))  # 松手即回填完整转写，补上实时预览漏掉的尾巴
            self._context = (raw or "")[-180:]  # 带上下文给下次识别提准
            if self.cfg.get("cleanup_enabled", True):
                self._post(("status", "润色中…", self.C["blue"]))
                cleaned = cleanup_text(raw, self.cfg, self.api_key)
            else:
                cleaned = raw
            if self.cfg.get("confirm_before_insert", False):
                self._post(("hide", 0))
                self._post(("confirm", self.target_hwnd, raw, cleaned))
            else:
                self._insert(cleaned, self.target_hwnd)
                self._post(("status", "已插入", self.C["green"]))
                self._post(("hide", 900))
        finally:
            self.busy = False

    # --- tkinter 弹窗（主线程）---
    def run(self):
        import tkinter as tk

        self.tk = tk
        self.root = tk.Tk()
        self.root.withdraw()
        self._ensure_indicator()

        # 暂时不挂 win32_event_filter（排查监听不响应；确认基础链路后再安全加回抑制）
        self._listener = keyboard.Listener(
            on_press=self.on_press,
            on_release=self.on_release,
        )
        self._listener.start()
        print(f"[dbg] listener started, is_combo={self.is_combo} "
              f"req_mods={getattr(self, 'req_mods', None)} req_main={getattr(self, 'req_main', None)}",
              flush=True)

        self._banner()
        self._poll()
        self.root.mainloop()

    def _banner(self):
        key = self.cfg["hotkey"].upper()
        act = f"按住 [{key}] 说话，松开" if self.mode == "hold" else f"双击 [{key}] 开始，再双击停"
        print("=" * 56)
        print(f" {APP_NAME} 已就绪")
        print(f"  {act} → 转写润色 → {'弹窗确认' if self.cfg.get('confirm_before_insert', False) else '直接插入'}")
        print(f"  润色: {'开' if self.cfg['cleanup_enabled'] else '关'}"
              f"   模型: {self.cfg['whisper_model']}"
              f"{'   (已拦截该键默认行为)' if self.suppress else ''}")
        print("  关闭此窗口或 Ctrl+C 退出")
        print("=" * 56)

    def _poll(self):
        try:
            while True:
                msg = self.ui_queue.get_nowait()
                kind = msg[0]
                if kind == "status":
                    self._set_status(msg[1], msg[2])
                elif kind == "partial":
                    self._set_partial(msg[1])
                elif kind == "hide":
                    self.root.after(msg[1], self._hide_indicator)
                elif kind == "confirm":
                    self._hide_indicator()
                    self._show_popup(msg[1], msg[2], msg[3])
        except queue.Empty:
            pass
        self.root.after(60, self._poll)

    # 暗色磨砂卡片配色（Codex 圆角结构 + 小天偏好的暗色系）
    C = {
        "border": "#2c3242", "shadow": "#0b0d12", "card": "#1a1e28", "card_2": "#232a3a",
        "field": "#222838", "fg": "#eef1f8", "sub": "#aab2c5", "muted": "#6b7384",
        "accent": "#7c8cff", "accent_h": "#8f9bff", "btn2": "#262c3b", "btn2_h": "#313850",
        "red": "#ff5c6a", "amber": "#ffb02e", "blue": "#5b8cff", "green": "#35d07f",
        "red_soft": "#3a232b", "amber_soft": "#352c1c", "blue_soft": "#1f2840", "green_soft": "#163029",
    }

    def _rr(self, canvas, x1, y1, x2, y2, r, **kw):
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return canvas.create_polygon(points, smooth=True, splinesteps=16, **kw)

    # ── 右下角状态指示窗（含录音时实时转写 · Codex 浅色设计）──
    def _ensure_indicator(self):
        if self.indicator is not None:
            return

        tk = self.tk
        C = self.C
        W = 456
        PAD = 10
        BASE_H = 146
        transparent = "#ff00ff"
        self._BASE_W, self._BASE_H = W, BASE_H
        self._ind_h = BASE_H
        self._ind_pill = C["card_2"]

        iw = tk.Toplevel(self.root)
        iw.overrideredirect(True)
        iw.attributes("-topmost", True)
        try:
            iw.attributes("-alpha", 0.0)
        except Exception:
            pass

        use_transparent = True
        try:
            iw.attributes("-transparentcolor", transparent)
            iw.configure(bg=transparent)
        except Exception:
            use_transparent = False
            iw.configure(bg=C["border"])
        self._ind_bg = transparent if use_transparent else C["border"]

        sw, sh = iw.winfo_screenwidth(), iw.winfo_screenheight()
        iw.geometry(f"{W}x{BASE_H}+{sw - W - 24}+{sh - BASE_H - 46}")

        canvas = tk.Canvas(iw, width=W, height=BASE_H, bd=0, highlightthickness=0, bg=self._ind_bg)
        canvas.pack(fill="both", expand=True)

        ids = {}
        ids["pulse"] = canvas.create_oval(34, 32, 50, 48, outline=C["red"], width=2, state="hidden")
        ids["dot"] = canvas.create_oval(38, 36, 46, 44, fill=C["accent"], outline="")
        ids["status"] = canvas.create_text(58, 40, text="", anchor="w", fill=C["fg"],
                                           font=("Microsoft YaHei UI", 10, "bold"))
        ids["partial"] = canvas.create_text(28, 72, text="", anchor="nw", width=W - 56,
                                            fill=C["sub"], font=("Microsoft YaHei UI", 13), justify="left")
        ids["hint"] = canvas.create_text(28, BASE_H - 26, text="按住热键说话，松开后自动插入", anchor="w",
                                         fill=C["muted"], font=("Microsoft YaHei UI", 9))

        ids["wave"] = []
        for i in range(5):
            x = 352 + i * 11
            ids["wave"].append(canvas.create_line(x, 43, x, 43, fill=C["red"], width=3,
                                                  capstyle="round", state="hidden"))

        self.indicator = iw
        self._ind_canvas = canvas
        self._ind_ids = ids
        self._ind_shown = False
        self._ind_status_text = ""
        self._ind_status_color = C["accent"]
        self._ind_partial_text = ""
        self._ind_pulse_phase = 0
        self._ind_pulse_job = None
        self._ind_fade_job = None

        self._redraw_chrome(BASE_H)
        iw.withdraw()

    def _redraw_chrome(self, H):
        C = self.C
        canvas = self._ind_canvas
        W = self._BASE_W
        PAD = 10
        canvas.delete("chrome")
        self._rr(canvas, PAD + 2, PAD + 4, W - PAD + 2, H - PAD + 4, 24,
                 fill=C["shadow"], outline="", tags=("chrome",))
        self._rr(canvas, PAD, PAD, W - PAD, H - PAD, 24,
                 fill=C["card"], outline=C["border"], width=1, tags=("chrome",))
        self._rr(canvas, 26, 25, 190, 55, 15,
                 fill=self._ind_pill, outline="", tags=("chrome", "status_bg"))
        canvas.tag_lower("chrome")
        canvas.coords(self._ind_ids["hint"], 28, H - 26)
        canvas.config(height=H)
        sw, sh = canvas.winfo_screenwidth(), canvas.winfo_screenheight()
        self.indicator.geometry(f"{W}x{H}+{sw - W - 24}+{sh - H - 46}")

    def _resize_to_content(self):
        canvas = self._ind_canvas
        canvas.update_idletasks()
        bbox = canvas.bbox(self._ind_ids["partial"])
        bottom = bbox[3] if bbox else 94
        needed = max(self._BASE_H, min(460, bottom + 34))
        if needed != self._ind_h:
            self._ind_h = needed
            self._redraw_chrome(needed)

    def _fade_in(self, a):
        if not self._ind_shown:
            return
        a = min(0.98, a + 0.16)
        try:
            self.indicator.attributes("-alpha", a)
        except Exception:
            pass
        if a < 0.98:
            self._ind_fade_job = self.root.after(16, lambda: self._fade_in(a))
        else:
            self._ind_fade_job = None

    def _show_ind(self):
        self._ensure_indicator()

        if self._ind_fade_job is not None:
            try:
                self.root.after_cancel(self._ind_fade_job)
            except Exception:
                pass
            self._ind_fade_job = None

        if not self._ind_shown:
            self._ind_shown = True
            self.indicator.deiconify()
            try:
                self.indicator.attributes("-alpha", 0.0)
            except Exception:
                pass
            self._fade_in(0.0)
        else:
            try:
                self.indicator.attributes("-alpha", 0.98)
            except Exception:
                pass

        self.indicator.lift()
        self.indicator.attributes("-topmost", True)

    def _pulse_indicator(self):
        if self.indicator is None or not self._ind_shown:
            self._ind_pulse_job = None
            return

        canvas = self._ind_canvas
        ids = self._ind_ids
        C = self.C
        recording = ("录音" in self._ind_status_text) or (self._ind_status_color == C.get("red"))

        if not recording:
            canvas.itemconfigure(ids["pulse"], state="hidden")
            for item in ids["wave"]:
                canvas.itemconfigure(item, state="hidden")
            self._ind_pulse_job = None
            return

        # 真实音量（RMS）驱动声波与呼吸点
        lvl = getattr(self.recorder, "level", 0.0)
        amp = max(0.05, min(1.0, lvl * 14.0))
        self._ind_pulse_phase += 1
        ph = self._ind_pulse_phase

        grow = 1 + amp * 5
        canvas.coords(ids["pulse"], 38 - grow, 36 - grow, 46 + grow, 44 + grow)
        canvas.itemconfigure(ids["pulse"], state="normal", outline=C["red"])

        for i, item in enumerate(ids["wave"]):
            wob = 0.55 + 0.45 * math.sin(ph * 0.5 + i * 0.9)
            h = 4 + amp * 26 * wob
            x = 352 + i * 11
            canvas.coords(item, x, 43 - h / 2, x, 43 + h / 2)
            canvas.itemconfigure(item, state="normal", fill=self._ind_status_color)

        self._ind_pulse_job = self.root.after(70, self._pulse_indicator)

    def _set_status(self, text, color):
        self._ensure_indicator()

        C = self.C
        self._ind_status_text = text
        self._ind_status_color = color

        soft = {
            C.get("red"): C.get("red_soft", "#ffe5e8"),
            C.get("amber"): C.get("amber_soft", "#fff3d9"),
            C.get("blue"): C.get("blue_soft", "#e8efff"),
            C.get("green"): C.get("green_soft", "#ddf7ea"),
        }.get(color, C["card_2"])
        self._ind_pill = soft

        canvas = self._ind_canvas
        ids = self._ind_ids
        canvas.itemconfigure("status_bg", fill=soft)
        canvas.itemconfigure(ids["dot"], fill=color)
        canvas.itemconfigure(ids["status"], text=text, fill=C["fg"])
        canvas.itemconfigure(ids["hint"],
                             text=("已完成" if color == C.get("green") else "按住热键说话，松开后自动插入"))

        self._show_ind()

        recording = ("录音" in text) or (color == C.get("red"))
        if recording and self._ind_pulse_job is None:
            self._pulse_indicator()
        elif not recording:
            if self._ind_pulse_job is not None:
                try:
                    self.root.after_cancel(self._ind_pulse_job)
                except Exception:
                    pass
                self._ind_pulse_job = None
            canvas.itemconfigure(ids["pulse"], state="hidden")
            for item in ids["wave"]:
                canvas.itemconfigure(item, state="hidden")

    def _set_partial(self, text):
        self._ensure_indicator()

        text = text or ""
        display = text if len(text) <= 160 else "…" + text[-158:]
        self._ind_partial_text = display
        self._ind_canvas.itemconfigure(self._ind_ids["partial"], text=display)

        self._resize_to_content()
        self._show_ind()

    def _hide_indicator(self):
        if self.indicator is None:
            return

        if self._ind_pulse_job is not None:
            try:
                self.root.after_cancel(self._ind_pulse_job)
            except Exception:
                pass
            self._ind_pulse_job = None

        def fade(alpha):
            if self.indicator is None:
                return

            if alpha <= 0.08:
                self.indicator.withdraw()
                self._ind_shown = False
                self._ind_canvas.itemconfigure(self._ind_ids["partial"], text="")
                self._ind_partial_text = ""
                try:
                    self.indicator.attributes("-alpha", 0.98)
                except Exception:
                    pass
                self._ind_fade_job = None
                return

            try:
                self.indicator.attributes("-alpha", alpha)
                self._ind_fade_job = self.root.after(18, lambda: fade(alpha - 0.10))
            except Exception:
                self.indicator.withdraw()
                self._ind_shown = False
                self._ind_canvas.itemconfigure(self._ind_ids["partial"], text="")
                self._ind_partial_text = ""
                self._ind_fade_job = None

        fade(0.98)

    def _show_popup(self, hwnd, raw, cleaned):
        tk = self.tk
        C = self.C
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)          # 去掉系统标题栏 → 自绘卡片
        win.attributes("-topmost", True)
        win.configure(bg=C["border"])       # 外层 1px 描边
        W, H = 600, 360
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{W}x{H}+{(sw - W) // 2}+{int(sh * 0.16)}")

        card = tk.Frame(win, bg=C["card"])
        card.pack(fill="both", expand=True, padx=1, pady=1)

        # ── 顶部条（可拖动）──
        header = tk.Frame(card, bg=C["card"])
        header.pack(fill="x")
        dot = tk.Label(header, text="●", fg=C["accent"], bg=C["card"], font=("Segoe UI", 11))
        dot.pack(side="left", padx=(18, 8), pady=14)
        title = tk.Label(header, text="VoiceType · 确认插入", fg=C["fg"], bg=C["card"],
                         font=("Microsoft YaHei UI", 11, "bold"))
        title.pack(side="left")
        close = tk.Label(header, text="✕", fg=C["sub"], bg=C["card"],
                         font=("Segoe UI", 12), cursor="hand2")
        close.pack(side="right", padx=18)
        tk.Frame(card, bg=C["border"], height=1).pack(fill="x")

        # ── 正文（可编辑）──
        body = tk.Frame(card, bg=C["card"])
        body.pack(fill="both", expand=True, padx=18, pady=(14, 8))
        txt = tk.Text(body, wrap="word", height=5, font=("Microsoft YaHei UI", 13),
                      bg=C["field"], fg=C["fg"], insertbackground=C["accent"],
                      relief="flat", bd=0, padx=14, pady=12, highlightthickness=1,
                      highlightbackground=C["border"], highlightcolor=C["accent"],
                      spacing1=2, spacing3=6)
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", cleaned)

        # ── 原文（灰、截断）──
        rawtext = raw if len(raw) <= 80 else raw[:78] + "…"
        tk.Label(card, text="原文   " + rawtext, fg=C["sub"], bg=C["card"], anchor="w",
                 justify="left", wraplength=W - 44,
                 font=("Microsoft YaHei UI", 9)).pack(fill="x", padx=18, pady=(0, 4))

        # ── 按钮条 ──
        bar = tk.Frame(card, bg=C["card"])
        bar.pack(fill="x", padx=18, pady=(4, 16))
        tk.Label(bar, text="Ctrl+Enter 插入 · Esc 取消", fg=C["sub"], bg=C["card"],
                 font=("Microsoft YaHei UI", 9)).pack(side="left")

        def do_insert(event=None):
            text = txt.get("1.0", "end-1c")
            win.destroy()
            self.root.update()
            if text.strip():
                pyperclip.copy(text)
                time.sleep(0.12)
                set_foreground_window(hwnd)
                time.sleep(0.12)
                with self.kbctl.pressed(keyboard.Key.ctrl):
                    self.kbctl.press("v")
                    self.kbctl.release("v")
                print("[ok] 已插入")
            return "break"

        def do_cancel(event=None):
            win.destroy()
            print("[x] 取消")
            return "break"

        def mkbtn(text, bg, bg_h, fg, cmd, bold=False):
            b = tk.Label(bar, text=text, bg=bg, fg=fg, cursor="hand2",
                         font=("Microsoft YaHei UI", 10, "bold" if bold else "normal"),
                         padx=22, pady=9)
            b.bind("<Enter>", lambda e: b.config(bg=bg_h))
            b.bind("<Leave>", lambda e: b.config(bg=bg))
            b.bind("<Button-1>", lambda e: cmd())
            return b

        mkbtn("插入", C["accent"], C["accent_h"], "#ffffff", do_insert, bold=True).pack(side="right")
        mkbtn("取消", C["btn2"], C["btn2_h"], C["fg"], do_cancel).pack(side="right", padx=(0, 10))
        close.bind("<Button-1>", do_cancel)

        # ── 拖动移动 ──
        def start_move(e):
            win._mx, win._my = e.x_root, e.y_root
            win._ox, win._oy = win.winfo_x(), win.winfo_y()

        def on_move(e):
            win.geometry(f"+{win._ox + (e.x_root - win._mx)}+{win._oy + (e.y_root - win._my)}")

        for wdg in (header, title, dot):
            wdg.bind("<Button-1>", start_move)
            wdg.bind("<B1-Motion>", on_move)

        win.bind("<Control-Return>", do_insert)
        win.bind("<Escape>", do_cancel)
        win.after(20, lambda: (win.focus_force(), txt.focus_set(), txt.mark_set("insert", "end")))
        self._last_win = win  # 便于预览截图/测试


def run_settings(cfg, _preview_png=None):
    """GUI 设置窗——填好直接保存到 config.json。返回更新后的 cfg 或 None（取消）。"""
    import tkinter as tk
    from tkinter import ttk

    C = App.C
    root = tk.Tk()
    root.title(f"{APP_NAME} · 设置")
    root.configure(bg=C["card"])
    root.geometry("470x690")
    try:
        root.attributes("-topmost", True)
        root.eval('tk::PlaceWindow . center')
    except Exception:
        pass

    def label(t):
        tk.Label(root, text=t, bg=C["card"], fg=C["sub"], anchor="w",
                 font=("Microsoft YaHei UI", 9)).pack(fill="x", padx=24, pady=(10, 2))

    def entry(initial, show=None):
        e = tk.Entry(root, show=show, bg=C["field"], fg=C["fg"], insertbackground=C["accent"],
                     relief="flat", font=("Microsoft YaHei UI", 11))
        e.insert(0, "" if initial is None else str(initial))
        e.pack(fill="x", padx=24, ipady=6)
        return e

    def combo(value, options):
        var = tk.StringVar(value=value)
        cb = ttk.Combobox(root, textvariable=var, state="readonly", values=options)
        cb.pack(fill="x", padx=24, ipady=2)
        return var

    tk.Label(root, text=APP_NAME, bg=C["card"], fg=C["fg"],
             font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w", padx=24, pady=(20, 0))
    tk.Label(root, text="填好下面几项就能用。不想用 AI 润色就把 Key 留空（纯本地）。",
             bg=C["card"], fg=C["muted"], font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=24)

    label("AI 润色 API Key（OpenAI 兼容；留空 = 关闭润色，纯本地）")
    e_key = entry(cfg.get("llm_api_key", ""), show="•")
    label("API Base URL")
    e_base = entry(cfg.get("llm_base_url", "https://api.deepseek.com"))
    label("模型名")
    e_model = entry(cfg.get("llm_model", "deepseek-v4-flash"))
    label("热键（按住说话）：alt+v / ctrl_r / caps_lock / f8 …")
    e_hk = entry(cfg.get("hotkey", "alt+v"))
    label("识别模型（越大越准越慢；CPU 建议 small/medium）")
    v_wm = combo(cfg.get("whisper_model", "large-v3-turbo"),
                 ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"])
    label("设备（auto 自动；无 N 卡会退 CPU）")
    v_dev = combo(cfg.get("whisper_device", "auto"), ["auto", "cuda", "cpu"])
    label("语言（留空 = 自动检测；可填 zh / en）")
    e_lang = entry(cfg.get("language") or "")
    label("模型下载源（中国大陆改 https://hf-mirror.com）")
    e_hf = entry(cfg.get("hf_endpoint", "https://huggingface.co"))

    out = {"cfg": None}

    def do_save():
        cfg["llm_api_key"] = e_key.get().strip()
        cfg["llm_base_url"] = e_base.get().strip() or "https://api.deepseek.com"
        cfg["llm_model"] = e_model.get().strip() or "deepseek-v4-flash"
        cfg["hotkey"] = e_hk.get().strip() or "alt+v"
        cfg["whisper_model"] = v_wm.get()
        cfg["whisper_device"] = v_dev.get()
        cfg["language"] = e_lang.get().strip() or None
        cfg["hf_endpoint"] = e_hf.get().strip() or "https://huggingface.co"
        cfg["cleanup_enabled"] = bool(cfg["llm_api_key"])
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        out["cfg"] = cfg
        root.destroy()

    bar = tk.Frame(root, bg=C["card"])
    bar.pack(side="bottom", fill="x", pady=18)

    def mkbtn(text, bg, bg_h, fg, cmd):
        b = tk.Label(bar, text=text, bg=bg, fg=fg, cursor="hand2",
                     font=("Microsoft YaHei UI", 10, "bold"), padx=22, pady=10)
        b.bind("<Enter>", lambda e: b.config(bg=bg_h))
        b.bind("<Leave>", lambda e: b.config(bg=bg))
        b.bind("<Button-1>", lambda e: cmd())
        return b

    mkbtn("保存并启动", C["accent"], C["accent_h"], "#ffffff", do_save).pack(side="right", padx=(0, 24))
    mkbtn("取消", C["btn2"], C["btn2_h"], C["fg"], root.destroy).pack(side="right", padx=8)

    if _preview_png:  # 仅供截图预览/测试
        root.update_idletasks()
        for _ in range(20):
            root.update()
            time.sleep(0.02)
        x, y = root.winfo_rootx(), root.winfo_rooty()
        w, h = root.winfo_width(), root.winfo_height()
        from PIL import ImageGrab
        ImageGrab.grab((x, y, x + w, y + h)).save(_preview_png)
        root.destroy()
        return None

    root.mainloop()
    return out["cfg"]


def main():
    force_settings = "--settings" in sys.argv
    config_existed = CONFIG_PATH.exists()
    cfg = load_config()
    api_key = load_api_key(cfg)

    # 首次运行 / 显式 --settings / 想润色但没 key → 先弹 GUI 设置
    need_setup = force_settings or (not config_existed) or (cfg.get("cleanup_enabled", True) and not api_key)
    if need_setup:
        result = run_settings(cfg)
        if result is None and force_settings:
            return
        if result is not None:
            cfg = result
            api_key = load_api_key(cfg)

    if cfg["cleanup_enabled"] and not api_key:
        print("[warn] 没填 API Key → 润色关闭，只输出原始转写（纯本地）。")

    app = App(cfg, api_key)
    app.transcriber.load()
    try:
        app.run()
    except KeyboardInterrupt:
        print("\n再见")


if __name__ == "__main__":
    main()
