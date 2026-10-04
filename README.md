# opencode2api-console

一个给 [opencode2api](https://github.com/jasonxu114514/opencode2api) 网关配套使用的**图形控制台**与 **WorkBuddy / codex 适配中继层**，用 Python 编写。

本仓库**只包含作者独立编写的 Python 代码**。它让 opencode2api 网关在 Windows 上更好用：可视化编辑配置、系统托盘启停、长会话抗断流、codex 协议适配等。

---

## ⚠️ 许可证与上游声明（请先读）

- 本仓库的代码以 **MIT 许可证**开源（见 `LICENSE`）。
- 核心网关 **`opencode2api` 是一个独立的 Go 程序**，来自上游仓库
  [`jasonxu114514/opencode2api`](https://github.com/jasonxu114514/opencode2api)。
- **上游仓库未声明任何开源许可证（`license: null`，无 LICENSE 文件）**。按 GitHub 规则，这意味着其作者保留所有权利，未经明确许可**不得再分发其二进制或源码**。
- 因此，**本仓库不包含、不分发上游网关的可执行文件或源码**。请使用者自行从上游仓库的 Releases 下载，或用 `go build` 自行编译网关。
- 本仓库与上游作者无隶属关系，仅为个人使用便利的配套工具。

---

## 功能

| 文件 | 作用 |
| --- | --- |
| `opencode2api_gui.py` | 图形控制台：可视化编辑 `config.json`、启停/重启网关、系统托盘、测试连接 |
| `codex_shim.py` | codex 专用的 `/v1/responses` 协议适配层（过滤不被上游支持的 tool 类型、补齐 `required`） |
| `wb_shim.py` | WorkBuddy 专用的 chat→chat 中继层：上下文开窗、上游断流续接、429 退避 |

## 目录结构

```
opencode2api-console/        # Git 仓库（纯源码 + 脚本，不含 exe）
├── LICENSE
├── README.md
├── .gitignore
├── opencode2api_gui.py   # 图形控制台（入口）
├── codex_shim.py         # codex 协议适配器
├── wb_shim.py            # WorkBuddy 中继层
├── download_gateway.ps1  # 一键从上游拉取网关 exe 到本目录
└── start_console.bat     # 启动入口（检查网关/控制台是否齐备）

# Release 资产（*-windows-amd64.zip）额外包含：
#   o2a_console.exe        # 控制台编译产物（PyInstaller 冻结）
#   （下载脚本会把官方 opencode2api.exe 放进同目录）
```

## 依赖

运行时依赖（打包进 exe 或运行脚本时需要）：

- Python 3.10+
- `tkinter`（Python 标准库，通常自带）
- `pillow`（PIL）
- `pystray`
- `PyInstaller`（仅当你想自行打包成 exe 时需要；其许可证为 GPLv2 + Bootloader 例外，**用其打包不会要求你开源自己的代码**）

安装依赖：

```bash
pip install pillow pystray
```

## 从 Release 下载即用（推荐 Windows 用户）

GitHub Releases 提供开箱即用的打包（`<版本>-windows-amd64.zip`），已包含：

- 本控制台编译好的 `o2a_console.exe`
- `download_gateway.ps1`（一键拉取上游网关）
- `start_console.bat`（启动入口）
- 全部 Python 源码与 `LICENSE` / `README`

使用步骤：

1. 从 [Releases](https://github.com/puppywxz-hash/opencode2api-console/releases) 下载最新的 `*-windows-amd64.zip` 并解压到任意目录。
2. 在解压目录里运行一次下载脚本，把官方网关拉到本地（**仅下载官方二进制，本仓库不分发它**）：
   ```bat
   powershell -ExecutionPolicy Bypass -File download_gateway.ps1
   ```
3. 双击 `start_console.bat` 启动控制台（首次若缺网关会提示，先完成第 2 步即可）。
4. 在控制台「密钥」页填写 `server_keys`（客户端 → 本机门禁）与上游 `zen_keys`（你的 opencode 账号 key），保存后点「启动代理」。

> ⚠️ 网关 `opencode2api.exe` 必须和 `o2a_console.exe` **在同一目录**，控制台才能找到并启动它（`config.json` 也放在此目录；它含密钥，已被 `.gitignore` 排除，不会进入本仓库）。

## 仅用源码（自行装依赖 / 自行构建）

1. **获取网关**：前往上游仓库 Releases 下载 `opencode2api.exe`，或按上游说明 `go build` 自行编译；放到本仓库目录（与 `opencode2api_gui.py` **同目录**）。
2. **安装依赖**：
   ```bash
   pip install pillow pystray
   ```
3. **运行控制台**：
   ```bash
   python opencode2api_gui.py
   ```
4. **（可选）WorkBuddy 抗断流**：如需启用 `wb_shim` 中继层，按 `wb_shim.py` 顶部说明以独立进程启动，并把 WorkBuddy 的模型端点指向 shim 监听端口（默认 `127.0.0.1:18090`）。

## 自行构建控制台 exe

想自己从源码冻结出 `o2a_console.exe`（与 Release 中一致）：

```bash
pip install pillow pystray pyinstaller
pyinstaller --noconfirm --onefile --windowed --name o2a_console opencode2api_gui.py
```

产物在 `dist/o2a_console.exe`。注意 `*.exe` 已被 `.gitignore` 排除，不会进入本仓库。

## 免责声明

本工具按「现状」提供，不含任何明示或暗示担保。使用者需自行遵守上游网关的许可证与当地法律法规；因使用本工具产生的任何后果由使用者自行承担。

若上游 `opencode2api` 未来添加了开源许可证，请以其许可证条款为准。
