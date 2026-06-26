# Typeless (macOS 原生版)

> Windows 版 `voicetype.py` 用 `faster-whisper` + `win32` + tkinter 浮窗;这些在 macOS 跑不了。
> 本目录的 **`voicetype_mac.py` 是对等功能的 macOS 原生重写**(mlx-whisper + rumps 托盘 + pynput),
> 真正可运行、可打包成 `.app`。与仓库里 `build_release_mac.sh`(早期演示用、产物跑不起来)不同。

按住 **右 ⌘ Command** 说话,松开后自动把语音转成文字、粘贴到当前光标处。全程本地、免 API key。

## 特性

- 🎤 按住右 ⌘ 录音,松开即转写并自动输入到当前光标
- 🧠 **本地 ASR**(mlx-whisper,Apple Silicon Metal 原生)—— 无需 API key、无需联网(模型首次下载后离线可用)
- 🀄 中文友好:「剪贴板 + Cmd+V」注入,中文/emoji 都可靠;默认 opencc 繁→简(whisper 倾向输出繁体)
- 🔁 **纠错飞轮**:托盘「纠正上一句…」改对一次,系统记住「误听→正确」并自动应用到以后(字符级 diff 学习 + 收进常用词)
- 🔤 **常用词热词偏置**:`vocabulary` 喂 whisper `initial_prompt`,提升专名/术语(如 `CLAUDE.md`/`MCP`)识别
- 🍔 状态栏托盘常驻,看门狗防麦克风泄漏

## 环境要求

- Apple Silicon Mac(arm64)、macOS 12+
- `ffmpeg`:`brew install ffmpeg`
- Python 3.11+:`brew install python@3.12`

## 快速开始(源码运行)

```bash
# 1. 建虚拟环境 + 装依赖
/opt/homebrew/bin/python3.12 -m venv .venv-mac
.venv-mac/bin/pip install -r requirements-mac.txt

# 2. 自检(免权限,验证 ASR 链路):合成中文语音 → 转写 → 比对
.venv-mac/bin/python voicetype_mac.py --selftest

# 2b. 麦克风自测(只需麦克风权限):录 4s 真人声 → 转写
.venv-mac/bin/python voicetype_mac.py --mictest

# 2c. 热键诊断(验 pynput 能否收到全局键):打印每个按键的真实键名
.venv-mac/bin/python voicetype_mac.py --keytest

# 3. 跑起来(托盘模式)
.venv-mac/bin/python voicetype_mac.py
```

首次运行自动下载 whisper 模型(默认 `whisper-small`,约 500MB),之后离线可用。

## 打 .app 包

```bash
./setup_signing_cert.sh   # 首次:建稳定自签证书(见下「签名」一节,只需跑一次)
./build_mac.sh            # 产物: dist/Typeless.app
```

## 纠错飞轮怎么用

1. 按住右 ⌘ 说话,松开 → 文字自动粘到光标处
2. 若某个专名/词被听错 → 点菜单栏 Typeless 图标 → **「纠正上一句…」** → 把它改对 → 点「学习」
3. 系统记住这条「误听→正确」(存 `~/Library/Application Support/Typeless/corrections.json`),**以后自动纠正**,同时把正确词收进常用词热词表

## 权限(首次必做,最容易卡的一步)

⚠️ **macOS 里有两个都叫「辅助功能」的东西,别搞混**:
- ❌ 不是 **系统设置 → 辅助功能(无障碍)→ 语音控制** —— 那是 macOS 自带的语音控制特性,与本 App 无关
- ✅ 是 **系统设置 → 隐私与安全性 → 辅助功能** —— 「允许下面的应用程序控制你的电脑」那个权限列表

pynput 的**收键**和**注入**是两个不同权限,缺一不可:

| 权限(隐私与安全性内) | 管什么 | 不给的后果 |
|------|------|-------------|
| **麦克风** | 录音 | 录不到声音 |
| **允许应用程序监控键盘输入**(输入监控) | pynput 监听器**收**右 ⌘ 按键 | 按右 ⌘ 完全无反应 |
| **辅助功能**(控制你的电脑) | pynput **模拟** Cmd+V 注入文字 | 录音转写都正常,但**文字粘不进去**(静默失败) |

口诀:**收键=输入监控,粘贴=辅助功能**。给完三个开关后 **退出 App 重开**(TCC 改动需重启 App 生效)。

> 新版 macOS 这些隐私列表**没有 `+`/删除按钮**:App 请求过就自动出现在列表里,直接拨开关即可。
> 若列表里没有 Typeless,先启动它、按一次右 ⌘,它会自动出现或弹出授权请求。

## 签名:根治「每次重打包都要重新授权」

**问题**:macOS 的 TCC 授权对 **adhoc 签名**的 App 是**按 cdhash(代码哈希)**认的。每次 `build_mac.sh` 重打包,代码变了 → cdhash 变 → 之前给的辅助功能/麦克风/输入监控授权**全部失效**,要重授一遍。开发期反复打包时极其难受。

**根治**:用一个**稳定的自签名证书**签名(而非 adhoc)。这样 designated requirement 变成
`identifier Typeless and certificate root = H"<固定哈希>"`,**跨重建不变** → **授权只需给一次,以后重打包都保留**。

```bash
./setup_signing_cert.sh   # 建/恢复证书「Typeless Dev」到登录钥匙串(幂等,带 p12 备份)
./build_mac.sh            # 自动用该证书签名;证书不在时回退 adhoc
```

要点:
- 自签证书未经 Apple 公证,`spctl` 评估会 rejected,但**本地构建无 quarantine 属性**,双击/`open` 仍能正常启动(不报「已损坏」)
- 证书私钥 p12 备份在 `~/Library/Application Support/Typeless/typeless-dev-cert.p12`(密码 `typeless`),换机/钥匙串丢失后 `setup_signing_cert.sh` 自动从备份恢复
- 正式公开发布免 Gatekeeper 警告仍需 Apple 开发者账号($99/年)做签名 + 公证;本方案是给**内部开发/自用**省掉反复授权

## 配置 `config.json`

放在 `~/Library/Application Support/Typeless/config.json`(打包后)或脚本同目录(源码调试):

```json
{
  "model": "mlx-community/whisper-small-mlx",
  "language": "zh",
  "hotkey": "cmd_r",
  "sample_rate": 16000,
  "min_seconds": 0.3,
  "max_record_seconds": 30,
  "simplified": true,
  "vocabulary": ["CLAUDE.md", "MCP", "agent"],
  "corrections_file": "corrections.json"
}
```

- `model`:换更准的用 `mlx-community/whisper-medium-mlx` / `whisper-large-v3-mlx`(更慢更大);提速用 `whisper-base-mlx`
- `language`:设 `null` 自动检测
- `hotkey`:[pynput Key 名](https://pynput.readthedocs.io/en/latest/keyboard.html#pynput.keyboard.Key),如 `cmd_r` / `alt_r` / `ctrl_r`
- `max_record_seconds`:看门狗超时,防松开事件丢失时麦克风无限常开
- `vocabulary`:常用词/专名,喂 whisper `initial_prompt` 做热词偏置(纠错飞轮也会自动往这里收词)

## 故障排查

- **按右 ⌘ 完全无反应** → 「输入监控」没给(或重打包后失效);给完重启 App。可跑 `--keytest` 确认 pynput 收不收得到键
- **录音转写都正常,但文字粘不进去** → 「辅助功能」没给(注入静默失败)
- **每次重打包又要重新授权** → 没用稳定证书签名,跑 `./setup_signing_cert.sh` 后重新 `./build_mac.sh`
- **松开后麦克风图标一直亮** → 松开事件未停止录音;本版已加「松开退化匹配 + 再按一下 toggle + 看门狗超时」三层兜底
- **转写很慢** → 首次在下模型;或把 `model` 换成 `whisper-base-mlx`
- **模型下载失败** → `export HF_ENDPOINT=https://hf-mirror.com` 走国内镜像后再跑
- **双击报「已损坏」** → 改完 Info.plist 后签名失效;`build_mac.sh` 已在改 plist 后重签兜底,手动重签:`codesign --force --deep --sign "Typeless Dev" dist/Typeless.app`

## 与 Windows 版(`voicetype.py`)的差异

| 能力 | Windows | macOS (`voicetype_mac.py`) |
|------|---------|----------------------------|
| ASR | faster-whisper(CUDA/CPU) | mlx-whisper(Apple Metal) |
| 热键 | `keyboard` | `pynput`(右 ⌘) |
| 文本注入 | `win32` + Ctrl+V | 剪贴板 + Cmd+V |
| 纠错 UI | tkinter 浮窗(双击选词) | 托盘菜单 + `rumps.Window`(避开 tkinter↔rumps 主线程冲突) |
| 繁→简 | zhconv | opencc |
| 托盘/通知 | `pystray` / `win10toast` | `rumps` |
