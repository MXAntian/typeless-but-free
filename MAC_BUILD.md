# Mac 打包交接（给小毛）

千夏在 Windows 上做不了 Mac 包（PyInstaller 不能跨平台编译），代码已做好 Mac 适配，麻烦你在一台 Mac 上 build + 测一下。

## 怎么 build

```bash
git clone <repo-url> && cd typeless-but-free
chmod +x build_release_mac.sh
./build_release_mac.sh
```

产物：`release/TypelessButFree-macos-<arch>.zip`（arm64=Apple Silicon / x86_64=Intel）。

## 必须测的几点（Mac 和 Windows 有差异的地方）

1. **粘贴键已改 Cmd+V**（Windows 是 Ctrl+V）。验证：说完话松手，文字是不是正确粘到光标处。
2. **抢焦点在 Mac 是 no-op**（`set_foreground_window` 只在 Windows 生效）。理论上焦点没离开就能直接粘——验证一下粘对了窗口。
3. **圆角真透明不支持**：Windows 用 `-transparentcolor` 做透明圆角，Mac 不支持 → 代码会退化成实色边框卡片（已 try/except 兜底）。验证：右下角窗口能正常显示、不难看。
4. **权限**（最容易卡的）：首次运行去 **系统设置 → 隐私与安全性** 给这个 app 授权：
   - **辅助功能（Accessibility）** —— 否则模拟 Cmd+V 注入不了
   - **输入监控（Input Monitoring）** —— 否则全局热键收不到
   - **麦克风** —— 否则录不了音
5. **Gatekeeper**：未签名，首次打开会被拦。右键 → 打开，或 `xattr -dr com.apple.quarantine TypelessButFree.app`（或对应目录）。

## 测完反馈

热键触发 / 录音 / 转写 / 粘贴 / 窗口样子，哪个 OK 哪个不行告诉千夏，有 Mac 专属 bug 我远程改。

> 未来要正式发布 Mac 版，得 Apple 开发者账号（$99/年）做**签名 + 公证**才能免 Gatekeeper 警告——先内部测，发布再说。
