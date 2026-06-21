# Contributing to Typeless but Free

Thanks for your interest! This is a small, focused tool — contributions that keep it
simple and dependency-light are very welcome. Issues and PRs in **English or Chinese**
are both fine（中英文都欢迎）.

## Reporting bugs / requesting features

Open an [issue](https://github.com/MXAntian/typeless-but-free/issues). For bugs, please include:

- OS + Python version (`python --version`)
- GPU or CPU, and your `whisper_model` / `whisper_device`
- What you did, what happened, what you expected
- Relevant lines from the console (it prints status and errors)

Tip: a lot of setup problems are already covered in the **Troubleshooting** section of the
[README](README.md) (hf-xet, CUDA DLLs, proxy, mirror) — please check there first.

## Submitting changes

You can't push to this repo directly — use the standard fork + pull request flow:

1. **Fork** the repo to your account.
2. Create a branch: `git checkout -b fix-something`.
3. Make your change. Keep it focused — one logical change per PR.
4. Test it (see below).
5. Push to your fork and open a **Pull Request** against `main`.

The maintainer reviews and merges. Small, well-scoped PRs get merged fastest.

## Dev setup

```powershell
git clone https://github.com/<you>/typeless-but-free
cd typeless-but-free
powershell -ExecutionPolicy Bypass -File .\setup.ps1
copy config.example.json config.json   # then fill in your key (or leave blank for local-only)
.\run.bat
```

## Testing your change

There's no formal test suite (yet). Before opening a PR, please verify by hand:

- `python -m py_compile voicetype.py` passes.
- The app launches, the hotkey triggers recording, transcription appears, and text inserts.
- If you touched the floating window, check it still renders (status states + live text).
- If you touched the hotkey/combo logic, test both a single key and a chord (e.g. `alt+v`).

## Code style

- Match the surrounding style — 4-space indent, no heavy frameworks, standard library first.
- Keep dependencies minimal. New runtime deps need a good reason.
- This is a Windows-first app with macOS support in progress. Platform-specific code should be
  guarded (`sys.platform == "win32"` / `"darwin"`), not assumed.

## Scope / philosophy

The goal is a clean, private, *free* alternative to paid dictation apps — local-first,
bring-your-own-key, no telemetry, no account. Features that add network dependencies,
accounts, or telemetry are out of scope. Speed, polish, and platform coverage are in scope.

## Building releases

- Windows: `build_release.ps1` → `release/TypelessButFree-windows-x64.zip`
- macOS: `build_release_mac.sh` (must run on a Mac — see [MAC_BUILD.md](MAC_BUILD.md))

By contributing, you agree your contributions are licensed under the MIT License.
