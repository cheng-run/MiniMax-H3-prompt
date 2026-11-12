# D5-1 打包 spike：Python 3.14 + PyInstaller + pywebview（#236）

> 结论先行：**三条全部成立**（2026-11-12 本机实测）。pywebview 能在 Python 3.14 上建
> WebView2 窗口，PyInstaller 能产出脱离本机 Python 的 onedir EXE，Playwright 能用
> `connect_over_cdp` 连上**打包后的**真实窗口。D5 后续票按「桌面外壳」推进，**不降级**。
>
> 一条附带结论**不成立**：spec §12 提到的 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`
> 环境变量这条路线在 pywebview 6.2.1 上**打不开调试端口**，必须走
> `webview.settings['REMOTE_DEBUGGING_PORT']`——机制见第 6 节。

## 1. 三条结论

| # | 问题 | 结论 | 证据 |
|---|---|---|---|
| 1 | pywebview 在 Python 3.14 上能否创建 WebView2 窗口（PyPI classifier 只到 3.13） | **成立** | 源码形态与冻结形态都建出窗口、页面脚本执行成功（`probe=probe-ok …`），窗口标题 `H3 EXE Spike`、UA 含 `Edg/157.0.0.0` |
| 2 | PyInstaller 能否产出**无本机 Python 参与**也能跑的 onedir EXE | **成立** | `env -i`（PATH 只剩 `system32`／`C:\Windows`、无 `PYTHONPATH`／`VIRTUAL_ENV`、`which python` 为空）下 EXE 退出码 0，页面从 `_internal/spike_page.html` 加载；包里带 `_internal/python314.dll`（6.3 MB） |
| 3 | Playwright 能否经 CDP 连上**真实窗口** | **成立** | `playwright.sync_api` 的 `chromium.connect_over_cdp("http://127.0.0.1:9444")` 读到标题与页内探针，输出 `[cdp] PASS` |

「未装 Python 的 Windows 11 机器」这一条**只做到 R 降级的形态**（同机清环境变量与 PATH），
没有干净 VM——见第 8 节。

## 2. 环境与实测版本

| 项 | 值 | 备注 |
|---|---|---|
| OS | Windows 11 Pro 10.0.26200 | 目标平台（spec §11） |
| WebView2 Runtime | 157.0.4341.42（Evergreen，系统预装） | 注册表 `HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-…}` 的 `pv` |
| Python | **3.14.3**（`uv` 管理的 cpython-3.14.3-windows-x86_64） | classifier 未声明 3.14 的就是这条链路的赌注 |
| pywebview | **6.2.1** | `Requires-Python: >=3.8`，classifiers **只到 3.13** |
| pythonnet | **3.2.1** | `Requires-Python: <3.16,>=3.11`——3.14 在声明范围内，这是 3.14 能通的直接原因 |
| clr-loader | 0.3.1 | |
| PyInstaller | **6.22.3** | 3.14 支持实测可用（无 pin、无补丁） |
| playwright | 1.65.0 | 只用于 `connect_over_cdp`，**不下载浏览器**也能连 |

冻结后的 EXE 自报版本（`_internal` 里 `pywebview-6.2.1.dist-info` 等被打进去）：
`pywebview=6.2.1`、`pythonnet=3.2.1`、`clr-loader=0.3.1`；`pyinstaller=未安装` 是**预期**
——它是构建期工具，不进运行时包。

## 3. 构建

一条命令（仓库根，Windows）：

```bash
python scripts/build_spike_exe.py                # 干净重建，产物落 build-exe/spike/
python scripts/build_spike_exe.py --no-clean     # 复用 PyInstaller 缓存
python scripts/build_spike_exe.py --windowed     # 不带控制台的窗口程序（本票未实测）
```

实际下发给 PyInstaller 的命令（脚本自动打印，可原样复现）：

```
python -m PyInstaller --noconfirm --onedir --name H3Spike \
  --distpath build-exe/spike --workpath build-exe/work --specpath build-exe/spec \
  --add-data scripts/spike_page.html;. \
  --copy-metadata pywebview --copy-metadata pythonnet --copy-metadata clr-loader \
  --console \
  --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PySide2 \
  --exclude-module PySide6 --exclude-module gtk --exclude-module gi \
  --exclude-module cefpython3 scripts/spike_exe.py
```

- **耗时：8.8 s**（`--clean` 干净重建；同脚本另两次 9.2 s／8.6 s）。
- 产物：`build-exe/spike/H3Spike/H3Spike.exe`（5.3 MB）＋ `_internal/`（33.3 MB 文件字节，
  80 个顶层条目），目录合计 38.6 MB（`du -sh` 报 39 MB）。
- `build-exe/` 已 gitignore；**不叫 `dist/`** 是为了不和 Vite 的 `frontend/dist` 撞（spec §9）。
- 两处**必须显式写**、漏了不报错只会「静默少东西」的点：
  - `--add-data` 把本地页面打进解包根，`spike_exe.resolve_page_dir` 冻结后走 `sys._MEIPASS`；
    漏了就是窗口一片空白。
  - `--copy-metadata`：PyInstaller **默认不带 dist-info**，冻结后 `importlib.metadata` 读
    版本号直接抛 `PackageNotFoundError`——首次构建实测 EXE 自报「pywebview=未安装」，
    证据当场断掉（`tests/test_spike_exe.py::test_pyinstaller_args_shape` 现在盯着这三个）。
- `--exclude-module` 生效：`_internal` 里 `pyqt|pyside|gtk|cefpython` 命中 **0** 条。

## 4. 验证 A：源码形态（先排除打包变量）

```bash
PYTHONIOENCODING=utf-8 python scripts/spike_exe.py --auto-close-seconds 6
```

输出要点：`loaded=True`、`title='H3 EXE Spike'`、
`probe='probe-ok file:///…/scripts/spike_page.html … Edg/157.0.0.0'`、`auto-close in 6s`、
`window closed, exiting`，退出码 0。窗口**自己关**（无人值守，不留要点掉的窗口）。

## 5. 验证 B/C：打包后的 EXE

### B）无本机 Python 参与

```bash
env -i SYSTEMROOT='C:\Windows' SystemRoot='C:\Windows' SYSTEMDRIVE='C:' SystemDrive='C:' \
    ProgramFiles='C:\Program Files' windir='C:\Windows' \
    LOCALAPPDATA="$LOCALAPPDATA" APPDATA="$APPDATA" USERPROFILE="$USERPROFILE" \
    TEMP="$TEMP" TMP="$TMP" PATH="/c/Windows/system32:/c/Windows" \
    build-exe/spike/H3Spike/H3Spike.exe --auto-close-seconds 5
```

同一环境里先确认 `PYTHONPATH=[unset] VIRTUAL_ENV=[unset]`、`which python` → 「PATH 上没有 python」。
结果：退出码 **0**，`page=file:///…/build-exe/spike/H3Spike/_internal/spike_page.html`，
页面探针执行成功，5 s 后自行关窗。

> **坑（实测踩到）**：`env -i` 若是漏给 `SystemDrive`，WebView2 会把 `%SystemDrive%` 当字面量
> 拼进路径，**在当前工作目录里造出 `%SystemDrive%/ProgramData/Microsoft/Windows/Caches/`**
> （4 个 `.db` 缓存文件），`git status` 里多出一个未跟踪目录。所以清单里的
> `SystemDrive`／`ProgramFiles` 不是凑数——它们是「不往仓里拉屎」的前提。

### C）CDP 连真实窗口

```bash
# 终端 1：把窗口连同调试端口起起来
build-exe/spike/H3Spike/H3Spike.exe --debug-port 9444 --auto-close-seconds 30
# 终端 2：
python scripts/spike_cdp_check.py --port 9444 --timeout 20
```

输出：

```
[cdp] page title='H3 EXE Spike' url='file:///…/_internal/spike_page.html'
[cdp] probe='probe-ok file:///…/_internal/spike_page.html Mozilla/5.0 … Edg/157.0.0.0'
[cdp] PASS：CDP 连上了真实窗口
```

`spike_cdp_check.py` 退出码即判据（0 通／非 0 不通），可直接进 CI 或验收脚本。
它在窗口起来之前会轮询到超时，所以「谁先起」不用掐点。

## 6. 不成立的那条：`WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`

spec §12 给的两种写法里，环境变量那条**实测不通**：

```bash
WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS="--remote-debugging-port=9555" \
  build-exe/spike/H3Spike/H3Spike.exe --auto-close-seconds 30
python scripts/spike_cdp_check.py --port 9555        # → connect ECONNREFUSED
```

窗口照常打开（日志里 `loaded=True`、探针正常），**只是调试端口没开**。机制在
`webview/platforms/edgechromium.py`：pywebview 自己给 `props.AdditionalBrowserArguments`
赋值（`:82`，`--disable-features=ElasticOverscroll`；`:85` 追加
`--allow-file-access-from-files`），**显式赋值覆盖了 WebView2 从环境变量读到的值**；
调试端口只在 `webview_settings['REMOTE_DEBUGGING_PORT']` 非空时才被拼进去（`:87-89`，
默认值 `None` 在 `webview/__init__.py:127`）。

**给 D5-6/D5-7 的口径**：要 CDP，就在建窗口之前设
`webview.settings['REMOTE_DEBUGGING_PORT'] = port`（`webview.settings` 是只能改既有键的
`ImmutableDict`，这个键是预置的）。别依赖环境变量，也别指望启动器把参数透传下去。

## 7. 对后续票的接口结论

- **D5-5 可以按「桌面外壳」做**，不需要降级成「无外壳 + 系统浏览器」。
- **D5-6 的 CI `exe-build`**：一次干净构建 ~9 s（**spike 规模**；真包要拖 langchain/langgraph，
  时间不可直接外推，只作下界参考）。需要显式 `--add-data`（资源）与 `--copy-metadata`（版本信息）。
- **D5-7 的自动化验收可用 CDP 主路径**：`--debug-port` → `connect_over_cdp` → 读页面；
  兜底 B（外部浏览器 + 人工点窗口）仍要留，理由是第 8 节的风险。
- **日志编码**：冻结后控制台按 cp936 输出，Git Bash 里中文乱码（`响应` 变 `��Ӧ`）。
  spike 的日志因此只用了 ASCII 字样；真正的日志落盘（D5-4）要按 UTF-8 写文件，别依赖控制台。

## 8. 未验证项与风险

| 项 | 状态 |
|---|---|
| **全新 Windows 11 VM（未装 Python／Node）双击运行** | **未做**，只做了「同机 `env -i` 清 `PYTHONPATH`／`VIRTUAL_ENV`／PATH 无 python」。按 spec §11 这属 R 降级，**不能算 D5-AC1 通过** |
| 双击（资源管理器）启动而非命令行启动 | 未做；`--console` 形态双击会多一个黑窗，`--windowed` 形态**未实测** |
| 未签名 EXE 的 SmartScreen／杀软拦截 | 未遇到，但未做新机器首次运行验证 |
| WebView2 缺失时的行为 | 未验；目标机假定 Win11 预装（本机 157.0.4341.42） |
| pywebview 6.2.1 的上游长期支持 | classifiers 只到 3.13；本次能通靠 pythonnet 3.2.1 声明 `<3.16`，**升级 pywebview 需要重跑本 spike** |
| 真包（含 langchain／langgraph／ffmpeg）的构建时间与漏收动态导入 | 不在本票范围（spec §15 已列风险，归 D5-6） |

## 9. 复现清单

1. `python scripts/build_spike_exe.py`（约 9 s）→ `build-exe/spike/H3Spike/H3Spike.exe`
2. `env -i … PATH="/c/Windows/system32:/c/Windows" build-exe/spike/H3Spike/H3Spike.exe --auto-close-seconds 5`
3. `build-exe/spike/H3Spike/H3Spike.exe --debug-port 9444 --auto-close-seconds 30` ＋
   `python scripts/spike_cdp_check.py --port 9444`
4. 机械部分（参数解析、输出目录命名、`--add-data`／`--copy-metadata`／excludes 形态）由
   `tests/test_spike_exe.py` 守护，跑 `scripts/check.sh fast` 即可（不进全量的重活儿）。
