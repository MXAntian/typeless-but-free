# Typeless but Free

[English](README.md) · 中文

**本地、隐私、按住说话的语音听写，带 AI 润色 —— Windows 版。**
*就像 Typeless，但免费、开源。*

![截图](docs/screenshot.png)

按住热键，说话，松开。你的语音在**你自己的电脑上**转写（faster-whisper / CUDA），
可选用 LLM 润色（去口水词、补标点、轻度改写），然后自动粘到光标处。
右下角的小浮窗会**在你说话时实时显示**转写内容。

> 语音转写全程本地，不上传。只有"已经转好的文字"才会发给润色 LLM——
> 你也可以彻底关掉润色，做到 100% 本地。

```
按住 ALT+V → 说话 → 松开 → 本地 Whisper → （可选）LLM 润色
            → 文字自动粘到光标处
```

## 直接下载用（不想碰代码）

去 [Releases](https://github.com/AgentGameLab/typeless-but-free/releases) 下 Windows 免安装包，
解压双击 `TypelessButFree.exe`，首次弹设置窗填 Key（或留空纯本地）即可用。

## 功能

- **本地转写** —— `faster-whisper`，用你的 GPU（CUDA）或 CPU。语音不出本机。
- **实时预览** —— 说话时转写文字一截一截冒出来。
- **AI 润色（可选）** —— 任意 OpenAI 兼容端点（DeepSeek / OpenAI / 本地 LLM…）。填自己的 Key，或干脆关掉。
- **自动插入** —— 直接粘到当前焦点窗口，省掉复制粘贴。
- **越用越准** —— 插入后点一下浮窗改词，它记住这次纠正（替换规则 + 把词加进 hotwords），下次自动认对。全本地。
- **可配置热键** —— 按住说话 / 双击开关，单键或组合键（如 `alt+v`、`ctrl_r`、`caps_lock`）。
- **纯 Python + tkinter** 界面 —— 暗色、圆角、声波跟着你的音量跳。

## 系统要求

- Windows 10/11
- Python 3.10+
- （推荐）NVIDIA 显卡，转写更快。**没显卡也行，自动退回 CPU**（慢一些）。

## 从源码安装

```powershell
git clone https://github.com/AgentGameLab/typeless-but-free voicetype
cd voicetype
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

`setup.ps1` 会建 venv、装依赖、并卸掉 `hf-xet`（原因见"故障排查"）。
首次运行会下载语音模型（`large-v3-turbo` 约 1.6 GB），下一次就不用了。

## 配置

复制示例，填你自己的值：

```powershell
copy config.example.json config.json
```

`config.json` 已被 **git 忽略**——你的 Key 不会外泄。

要开 AI 润色，三选一填上：
- `config.json` 里的 `llm_api_key`，或
- 环境变量 `LLM_API_KEY` / `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`，或
- `llm_api_key_file` 指向一个含上述变量的 `.env` 文件。

**想 100% 本地、不要 Key？** 设 `"cleanup_enabled": false`，直接插入原始转写
（保留"呃/那个"，但什么都不出本机）。

### 配置项速查

| 字段 | 默认 | 说明 |
|---|---|---|
| `hotkey` | `alt+v` | 触发键/组合键：`alt+v`、`ctrl_r`、`caps_lock`、`f8`… |
| `hotkey_mode` | `hold` | `hold` = 按住说话；`double_tap` = 双击开始/再双击停 |
| `whisper_model` | `large-v3-turbo` | `large-v3`（最准）· `large-v3-turbo`（约 2× 快，质量相近）· `medium`/`small`/`tiny`（更快、较不准） |
| `whisper_device` | `auto` | `auto` / `cuda` / `cpu`（`auto`+`cuda` 无显卡时退回 CPU） |
| `language` | `null` | 自动检测；填 `"zh"` / `"en"` 锁定 |
| `simplify_chinese` | `true` | 中文路繁→简（Whisper 偶发漂繁体）；想要繁体设 `false` |
| `beam_size` | `1` | `1` 最快；调到 `5` 略微更准 |
| `cleanup_enabled` | `true` | LLM 润色开关（关 = 原始转写，无需 Key） |
| `confirm_before_insert` | `false` | `true` = 插入前弹可编辑确认窗 |
| `streaming_partial` | `true` | 录音时实时转写预览 |
| `linger_ms` | `4000` | 插入后右下角浮窗停留毫秒数——这期间点它可改词/教它认对 |
| `max_display_lines` | `4` | 浮窗最多显示几行（早先内容滚出顶部）|
| `vocabulary` | `[]` | 让这些词识别更准，如 `["CLAUDE.md","agent"]`（设置窗里也能填）|
| `insert_suffix` | `""` | 每次插入后追加（`" "`空格 / `"\n"`换行；聊天框慎用换行=可能发送）|
| `restore_clipboard` | `true` | 插入后恢复你原来的剪贴板内容 |
| `max_record_seconds` | `120` | 录音超过 N 秒自动停（0=不限）|
| `paste_delay_ms` | `120` | 焦点/粘贴间隔；慢应用粘贴丢字就调大 |
| `llm_timeout_seconds` | `30` | 润色请求超时 |
| `llm_base_url` | `https://api.deepseek.com` | 任意 OpenAI 兼容 base URL |
| `llm_model` | `deepseek-v4-flash` | 该端点的模型名 |
| `hf_endpoint` | `https://huggingface.co` | 模型下载源 —— **中国大陆改成 `https://hf-mirror.com`** |
| `model_use_proxy` | `false` | `false` = 下模型时绕过系统代理 |

## 用法

```powershell
.\run.bat
```

按住 **Alt+V**，说话，松开。看右下角小窗。完事。
关掉窗口或按 Ctrl+C 退出。

## 故障排查

踩过的坑（省得你再踩）：

- **模型 `config.json` / tokenizer 下成 0 字节 /「JSON parse error」** → `hf-xet` 后端在某些
  网络/镜像下会把小文件下坏。`setup.ps1` 已卸它；要是又被装回来，跑 `pip uninstall -y hf-xet`。
- **`cublas64_12.dll cannot be loaded`** → 缺 CUDA runtime。确认装了
  `nvidia-cublas-cu12`、`nvidia-cuda-runtime-cu12`、`nvidia-cudnn-cu12`（都在 `requirements.txt`）。
- **下载被重置 / 挂 VPN（Clash 等）报 `Connection aborted`** → 代理在掐连接。保持
  `model_use_proxy: false`（默认），下模型自动绕过代理。
- **在中国大陆** → 把 `hf_endpoint` 设成 `https://hf-mirror.com`。
- **热键没反应** → 从你自己的桌面会话双击 `run.bat` 启动（全局键盘钩子需要交互会话）。

## 工作原理

1. 全局热键监听（`pynput`）检测按下/松开。
2. `sounddevice` 把麦克风录到内存；RMS 音量驱动声波。
3. `faster-whisper` 转写——录音时每约 0.6 秒对已录音频转一次做实时预览，松手再做一次完整转写。
   上一句会作为 `initial_prompt` 喂进去提升连贯性。
4. 可选：经 OpenAI 兼容 chat 端点润色。
5. 剪贴板 + 模拟 Ctrl+V（macOS 为 Cmd+V）插入到焦点窗口。

## 隐私

语音在本地转写，绝不上传。`cleanup_enabled: false` 时，什么都不出本机。
开了润色时，也只把"转写后的文字"发给你选的 LLM 端点。

## 致谢

基于 [faster-whisper](https://github.com/SYSTRAN/faster-whisper)、
[CTranslate2](https://github.com/OpenNMT/CTranslate2)、
[pynput](https://github.com/moses-palmer/pynput)、[sounddevice](https://github.com/spatialaudio/python-sounddevice) 构建。

## 许可证

MIT —— 见 [LICENSE](LICENSE)。
