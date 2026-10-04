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
opencode2api-console/
├── LICENSE
├── README.md
├── .gitignore
├── opencode2api_gui.py   # 图形控制台（入口）
├── codex_shim.py         # codex 协议适配器
└── wb_shim.py            # WorkBuddy 中继层
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

## 使用步骤

1. **获取网关**：前往上游仓库 Releases 下载 `opencode2api.exe`，或按上游说明 `go build` 自行编译。把它放到本仓库同级或任意目录。
2. **准备配置**：使用上游仓库自带的 `config.example.json` 复制为 `config.json` 并按需填写（`server_keys`、上游 `zen`/`go` 密钥或 `anonymous:true`、`webui.password` 等）。**注意：`config.json` 含密钥，已被 `.gitignore` 排除，不会进入本仓库。**
3. **运行控制台**：
   ```bash
   python opencode2api_gui.py
   ```
   或在 GUI 中配置网关路径后点「启动」。
4. **（可选）WorkBuddy 抗断流**：如需启用 `wb_shim` 中继层，按 `wb_shim.py` 顶部说明以独立进程启动，并把 WorkBuddy 的模型端点指向 shim 监听端口（默认 `127.0.0.1:18090`）。

## 免责声明

本工具按「现状」提供，不含任何明示或暗示担保。使用者需自行遵守上游网关的许可证与当地法律法规；因使用本工具产生的任何后果由使用者自行承担。

若上游 `opencode2api` 未来添加了开源许可证，请以其许可证条款为准。
