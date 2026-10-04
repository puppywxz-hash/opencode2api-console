# -*- coding: utf-8 -*-
"""
opencode2api 图形控制台
========================
1. 可视化编辑 config.json（监听地址 / 密钥 / 代理 / 重试 / 思考强度 / 日志）
2. 启动 / 停止 / 重启 opencode2api.exe（自动轮转 server.log）
3. 最小化到系统托盘，托盘图标右键即可 启动 / 停止 / 重启
4. 测试连接（GET /v1/models），确认 WorkBuddy 能不能连上

依赖: tkinter + pillow + pystray
运行: gui314/Scripts/pythonw.exe opencode2api_gui.py   或直接双击编译好的 exe
"""

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageDraw
import pystray

import codex_shim
import wb_shim

# ---------------------------------------------------------------- 路径常量
CREATE_NO_WINDOW = 0x08000000


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = app_dir()
GUI_SETTINGS_PATH = os.path.join(APP_DIR, "gui_settings.json")


def load_gui_settings():
    """GUI 专用配置（codex 兼容层等），独立存放，绝不写进网关 config.json。

    opencode2api 网关用 Go 严格解析 config.json（不允许未知字段），
    把 Python 中间层配置塞进去会导致网关启动即报错退出（code 1）。
    """
    try:
        with open(GUI_SETTINGS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except (OSError, ValueError):
        pass
    return {}


def save_gui_settings(d):
    try:
        with open(GUI_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
BACKUP_PATH = CONFIG_PATH + ".bak"
LOG_PATH = os.path.join(APP_DIR, "server.log")
LOG_OLD = LOG_PATH + ".1"
EXE_PATH = os.path.join(APP_DIR, "opencode2api.exe")
ICON_PATH = os.path.join(APP_DIR, "app_icon.ico")

EFFORT_VALUES = ["（关闭）", "minimal", "low", "medium", "high", "xhigh", "max", "none"]
LOG_LEVELS = ["debug", "info", "warn", "error"]


# ---------------------------------------------------------------- 小工具
def ensure_icon():
    """生成一个托盘/窗口图标（蓝色圆角方块 + 白色闪电）。"""
    if os.path.exists(ICON_PATH):
        return ICON_PATH
    try:
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([1, 1, size - 2, size - 2], radius=14, fill=(23, 64, 133, 255))
        d.polygon([(37, 8), (18, 35), (29, 35), (25, 56), (47, 27), (35, 27)],
                  fill=(255, 255, 255, 255))
        img.save(ICON_PATH, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])
    except Exception:
        return None
    return ICON_PATH


def port_open(host, port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex((host, port)) == 0
    except OSError:
        return False


def find_pid(port):
    """找出监听该端口的 PID。"""
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True,
                           text=True, errors="replace", creationflags=CREATE_NO_WINDOW,
                           timeout=8)
    except Exception:
        return None
    for line in (r.stdout or "").splitlines():
        p = line.split()
        if len(p) >= 5 and p[0].upper() == "TCP" and p[3] == "LISTENING":
            local = p[1]
            if local.rsplit(":", 1)[-1] == str(port):
                try:
                    return int(p[4])
                except ValueError:
                    return None
    return None


def tail_log(n=14, limit=3500):
    try:
        with open(LOG_PATH, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8000))
            data = f.read().decode("utf-8", "replace")
        txt = "\n".join(data.splitlines()[-n:])
        return txt[-limit:]
    except OSError:
        return "（暂无日志）"


def split_listen(value):
    value = (value or "").strip()
    if ":" not in value:
        return "127.0.0.1", 8080
    host, _, port = value.rpartition(":")
    try:
        return host.strip() or "127.0.0.1", int(port)
    except ValueError:
        return host.strip() or "127.0.0.1", 8080


def lines_of(text):
    return [x.strip() for x in (text or "").splitlines() if x.strip()]


def setpath(d, path, value):
    cur = d
    for k in path[:-1]:
        if not isinstance(cur.get(k), dict):
            cur[k] = {}
        cur = cur[k]
    cur[path[-1]] = value


# ---------------------------------------------------------------- 主界面
class App(object):
    def __init__(self):
        self.root = tk.Tk()
        self.proc = None
        self.busy = False
        self.exiting = False
        self._running = False
        self._pid = None
        self._tray_ballooned = False
        self._tray_listen_cache = ""
        # codex 兼容层的开关/地址：在 Tk 主线程预取，worker 线程只读
        self._pending_shim = (False, "127.0.0.1:18000", "127.0.0.1:8080")
        # WorkBuddy 抗断流层：在 Tk 主线程预取，worker 线程只读
        self._pending_wb = (False, "127.0.0.1:18090", "127.0.0.1:8080")
        self.ui_q = queue.Queue()
        self.stop_flag = threading.Event()
        self.cfg_error = None
        self.cfg = self.read_config()

        ensure_icon()
        self.root.title("opencode2api 控制台")
        self.root.geometry("880x760")
        self.root.minsize(800, 640)
        try:
            self.root.iconbitmap(ICON_PATH)
        except Exception:
            pass

        self._build_topbar()
        self._build_notebook()
        self._build_bottom()
        self.load_fields(self.cfg)

        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self._build_tray()
        self._drain_loop()
        threading.Thread(target=self._status_worker, daemon=True).start()

    # ---------------------------------------------------------- 配置读写
    def read_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            self.cfg_error = "config.json 不存在，已按空配置启动"
            return {}
        except Exception as e:
            self.cfg_error = "config.json 解析失败：%s" % e
            return {}

    def write_config(self, cfg):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "rb") as f:
                    data = f.read()
                with open(BACKUP_PATH, "wb") as f:
                    f.write(data)
            except OSError:
                pass
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")

    # ---------------------------------------------------------- 界面搭建
    def _build_topbar(self):
        """顶部栏。

        踩坑记录（勿改）
        ----------------
        旧版用 pack：状态标签和地址标签都是 side="left"，宽度会随文本增长，
        把 side="right" 的按钮挤出窗口可视区——表现为「启动后按钮消失」。
        现在改成 grid：中间列 weight=1 吸收全部富余宽度，按钮独占右侧固定列，
        任何文本长度都不会把按钮挤掉。
        """
        bar = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        bar.pack(fill="x")
        bar.columnconfigure(2, weight=1)

        self.dot = tk.Label(bar, text="●", fg="#999999", font=("Segoe UI", 14))
        self.dot.grid(row=0, column=0, sticky="w")

        self.status_lbl = tk.Label(bar, text="检测中…", anchor="w",
                                   font=("Microsoft YaHei UI", 10, "bold"))
        self.status_lbl.grid(row=0, column=1, sticky="w", padx=(6, 0))

        btns = ttk.Frame(bar)
        btns.grid(row=0, column=2, sticky="e")
        # 单个切换按钮：按当前状态显示「启动」或「停止」，避免启动后还显示「启动」
        self.btn_toggle = ttk.Button(btns, text="启动", width=8,
                                     command=self.toggle_service)
        self.btn_toggle.pack(side="left", padx=3)
        ttk.Button(btns, text="重启", width=7,
                   command=self.restart).pack(side="left", padx=3)
        ttk.Button(btns, text="测试连接", width=9,
                   command=self.test_conn).pack(side="left", padx=3)

        self.msg_lbl = tk.Label(self.root, text="", anchor="w", fg="#1a7f37",
                                font=("Microsoft YaHei UI", 9), padx=12)
        self.msg_lbl.pack(fill="x")

    def codex_base_url(self):
        """codex 里要填的 Base URL（完整 http://…/v1）。"""
        front = self.v_shim_front.get().strip() or "127.0.0.1:18000"
        if not front.startswith("http"):
            front = "http://" + front
        return front.rstrip("/") + "/v1"

    def wb_base_url(self):
        """WorkBuddy 要填的 Base URL（完整 http://…/v1）。"""
        front = self.v_wb_front.get().strip() or "127.0.0.1:18090"
        if not front.startswith("http"):
            front = "http://" + front
        return front.rstrip("/") + "/v1"

    def _update_codex_url(self):
        """把当前监听/兼容层地址算成完整 URL，填进服务页那两个可选中的框。"""
        if not hasattr(self, "var_codex_url"):
            return
        self.var_codex_url.set(self.codex_base_url())
        # WorkBuddy：若开了抗断流层则指向该层（18090），否则直接指向网关（8080）
        if getattr(self, "v_wb_on", None) and self.v_wb_on.get():
            self.var_wb_url.set(self.wb_base_url())
        else:
            listen = self.v_listen.get().strip() or "127.0.0.1:8080"
            if not listen.startswith("http"):
                listen = "http://" + listen
            self.var_wb_url.set(listen.rstrip("/") + "/v1")

    def copy_codex_url(self):
        url = self.codex_base_url()
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self.root.update_idletasks()
        self.flash("已复制到剪贴板：%s（直接粘到 codex 的 Base URL）" % url, "ok")

    def toggle_service(self):
        """顶部主按钮：运行中→停止，已停止→启动。"""
        if self._running:
            self.stop()
        else:
            self.start()

    def _build_notebook(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(4, 0))

        self._tab_canvases = []
        self._tab_service()
        self._tab_keys()
        self._tab_network()
        self._tab_thinking()
        self._tab_models()
        # 内容建完后才绑滚轮：Tk 的事件不冒泡，得给每个子控件都绑一遍
        for cv, inner in self._tab_canvases:
            self._bind_mw(cv, inner)

    def _new_tab(self, text):
        """建一个**可滚动**的标签页，返回内容容器。

        为什么：服务页内容多（监听/兼容层/两个 URL/WebUI/日志），窗口装不下时
        底部会被挤没、按钮点不到（用户反馈"下面已经填不下了"）。
        套一层 Canvas 就能滚动，内容再多也不会丢。
        """
        outer = ttk.Frame(self.nb)
        cv = tk.Canvas(outer, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=cv.yview)
        inner = ttk.Frame(cv, padding=6)
        win = cv.create_window((0, 0), window=inner, anchor="nw")

        def _sync(_e=None):
            try:
                cv.configure(scrollregion=cv.bbox("all"))
            except Exception:
                pass
        inner.bind("<Configure>", _sync)
        cv.bind("<Configure>", lambda e: (cv.itemconfigure(win, width=e.width),
                                          _sync()))
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        self.nb.add(outer, text=text)
        self._tab_canvases.append((cv, inner))
        return inner

    def _bind_mw(self, cv, widget):
        def handler(e, cv=cv):
            try:
                cv.yview_scroll(int(-1 * (e.delta / 120)), "units")
            except Exception:
                pass
        try:
            widget.bind("<MouseWheel>", handler)
        except Exception:
            pass
        for ch in widget.winfo_children():
            self._bind_mw(cv, ch)

    def _grid(self, parent):
        parent.columnconfigure(1, weight=1)

    def _row(self, parent, row, label, widget, hint=None, span=2):
        ttk.Label(parent, text=label, width=16, anchor="e").grid(
            row=row, column=0, sticky="ne", padx=(10, 8), pady=(6, 0))
        widget.grid(row=row, column=1, columnspan=span, sticky="we",
                    padx=(0, 10), pady=(6, 0))
        if hint:
            ttk.Label(parent, text=hint, foreground="#7a7a7a",
                      font=("Microsoft YaHei UI", 8), wraplength=560,
                      justify="left").grid(
                row=row + 1, column=1, columnspan=span, sticky="w",
                padx=(0, 10), pady=(1, 0))
        return row + (2 if hint else 1)

    def _group(self, parent, title, row):
        fr = ttk.LabelFrame(parent, text=title, padding=8)
        fr.grid(row=row, column=0, sticky="we", padx=8, pady=(8, 4))
        parent.columnconfigure(0, weight=1)
        self._grid(fr)
        return fr

    def _tab_service(self):
        tab = self._new_tab("  服务  ")

        g = self._group(tab, "监听与通道", 0)
        r = 0
        self.v_listen = tk.StringVar()
        r = self._row(g, r, "监听地址", ttk.Entry(g, textvariable=self.v_listen),
                      "网关本体监听地址；WorkBuddy 的 Base URL = http://此地址/v1，Key 填 server_keys")
        self.v_prefer = tk.StringVar()
        cb = ttk.Combobox(g, textvariable=self.v_prefer, values=["zen", "go"],
                          state="readonly", width=18)
        r = self._row(g, r, "默认通道 prefer", cb,
                      "zen = 免费/标准模型（mimo-v2.6-flash-free 等）；go = 需 Go 订阅")
        self.v_anon = tk.BooleanVar()
        ttk.Checkbutton(g, text="匿名模式 anonymous（用官方公共凭据，不用自己的 key）",
                        variable=self.v_anon).grid(
            row=r, column=0, columnspan=3, sticky="w", padx=(10, 0), pady=(8, 2))
        r += 1

        # ---- codex 兼容中间层 -------------------------------------------
        ttk.Separator(g, orient="horizontal").grid(
            row=r, column=0, columnspan=3, sticky="ew", padx=10, pady=(10, 6))
        r += 1
        self.v_shim_on = tk.BooleanVar()
        ttk.Checkbutton(
            g, text="启用 codex 兼容层（codex 走 responses 协议必需）",
            variable=self.v_shim_on, command=self._toggle_shim,
        ).grid(row=r, column=0, columnspan=3, sticky="w", padx=(10, 0), pady=(2, 2))
        r += 1
        self.v_shim_front = tk.StringVar()
        r = self._row(g, r, "codex 入口地址",
                      ttk.Entry(g, textvariable=self.v_shim_front),
                      "codex 的 Base URL = http://此地址/v1；网关本体仍在上面的监听地址")
        self.v_shim_back = tk.StringVar()
        r = self._row(g, r, "codex 后端地址",
                      ttk.Entry(g, textvariable=self.v_shim_back),
                      "中间层把请求转到这个地址（一般等于上面的监听地址，不用改）")
        self.lbl_shim_stat = tk.Label(g, text="", fg="#666666",
                                      font=("Microsoft YaHei UI", 8))
        self.lbl_shim_stat.grid(row=r, column=0, columnspan=3, sticky="w",
                                padx=(10, 0), pady=(4, 2))
        r += 1

        # ---- WorkBuddy 抗断流层 ----------------------------------------
        ttk.Separator(g, orient="horizontal").grid(
            row=r, column=0, columnspan=3, sticky="ew", padx=10, pady=(10, 6))
        r += 1
        self.v_wb_on = tk.BooleanVar()
        ttk.Checkbutton(
            g, text="启用 WorkBuddy 抗断流层（WorkBuddy 专用，防上游掐断 / 上下文爆掉）",
            variable=self.v_wb_on, command=self._toggle_wb,
        ).grid(row=r, column=0, columnspan=3, sticky="w", padx=(10, 0), pady=(2, 2))
        r += 1
        self.v_wb_front = tk.StringVar()
        r = self._row(g, r, "WorkBuddy 入口地址",
                      ttk.Entry(g, textvariable=self.v_wb_front),
                      "WorkBuddy 的 Base URL = http://此地址/v1；默认 127.0.0.1:18090，一般不改")
        self.lbl_wb_stat = tk.Label(g, text="", fg="#666666",
                                    font=("Microsoft YaHei UI", 8))
        self.lbl_wb_stat.grid(row=r, column=0, columnspan=3, sticky="w",
                              padx=(10, 0), pady=(4, 2))
        r += 1

        # ---- 客户端怎么填：完整 URL，做成可选中复制的输入框 ----
        # 说明：不用单独的地址栏 + 复制按钮了，地址就写在页面里，
        # 用只读 Entry 呈现，鼠标选中后 Ctrl+C 即可。
        ttk.Separator(g, orient="horizontal").grid(
            row=r, column=0, columnspan=3, sticky="ew", padx=10, pady=(10, 6))
        r += 1
        self.var_codex_url = tk.StringVar()
        self.var_wb_url = tk.StringVar()
        r = self._row(
            g, r, "codex Base URL",
            ttk.Entry(g, textvariable=self.var_codex_url,
                      font=("Consolas", 9), state="readonly"),
            "鼠标选中即可 Ctrl+C；codex 会自动在后面拼 /v1/responses，"
            "Key 填「密钥」页 server_keys 里的任意一个")
        r = self._row(
            g, r, "WorkBuddy URL",
            ttk.Entry(g, textvariable=self.var_wb_url,
                      font=("Consolas", 9), state="readonly"),
            "直接选中复制；若勾选了「WorkBuddy 抗断流层」则指向 18090 那层，"
            "否则指向网关 8080；「模型」页能查每个模型当前是否可用")
        r += 1

        g2 = self._group(tab, "WebUI 管理页", 1)
        r = 0
        self.v_webui_on = tk.BooleanVar()
        ttk.Checkbutton(g2, text="启用 WebUI", variable=self.v_webui_on).grid(
            row=r, column=0, sticky="w", padx=(10, 0), pady=(4, 2))
        self.v_webui_listen = tk.StringVar()
        ttk.Entry(g2, textvariable=self.v_webui_listen).grid(
            row=r, column=1, columnspan=2, sticky="we", padx=(0, 10), pady=(4, 2))
        r += 1
        self.v_webui_user = tk.StringVar()
        r = self._row(g2, r, "用户名", ttk.Entry(g2, textvariable=self.v_webui_user),
                      "密码在 config.json 的 webui.password_hash 里，界面不展示")

        g3 = self._group(tab, "日志", 2)
        r = 0
        self.v_level = tk.StringVar()
        cb = ttk.Combobox(g3, textvariable=self.v_level, values=LOG_LEVELS,
                          state="readonly", width=18)
        r = self._row(g3, r, "日志级别", cb,
                      "日志文件：%s" % LOG_PATH)
        self.v_dump = tk.BooleanVar()
        ttk.Checkbutton(g3, text="打印请求体 logging.dump_request_bodies（排障用，日志会很大）",
                        variable=self.v_dump).grid(
            row=r, column=0, columnspan=3, sticky="w", padx=(10, 0), pady=(8, 2))

    def _tab_keys(self):
        tab = self._new_tab("  密钥  ")

        g = self._group(tab, "本地密钥 server_keys（客户端 → 本机网关）", 0)
        self.t_server = tk.Text(g, height=3, width=70, undo=True,
                                font=("Consolas", 9))
        self.t_server.grid(row=0, column=0, columnspan=3, sticky="we",
                           padx=(4, 6), pady=(4, 2))
        ttk.Label(g, text="WorkBuddy 里「Key」填的就是这里的内容，每行一个，可多个并存。"
                          "本地门禁，不绑模型、可被任意多客户端/多模型共用。",
                  foreground="#7a7a7a", font=("Microsoft YaHei UI", 8)).grid(
            row=1, column=0, columnspan=3, sticky="w", padx=(4, 6))

        g1 = self._group(tab, "Zen 上游账号（网关 → opencode.ai，可多账号切换）", 1)
        # ---- 账号切换行：多账号换着用的核心控件 ----
        top = ttk.Frame(g1)
        top.grid(row=0, column=0, columnspan=3, sticky="we", padx=(4, 6), pady=(4, 2))
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="当前账号", width=10,
                   anchor="e").grid(row=0, column=0, sticky="e", padx=(0, 6))
        self.v_zen_acct = tk.StringVar()
        self.cb_zen_acct = ttk.Combobox(top, textvariable=self.v_zen_acct,
                                        state="readonly", width=46)
        self.cb_zen_acct.grid(row=0, column=1, sticky="ew")
        ttk.Button(top, text="新增", width=6,
                   command=self.add_zen_account).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(top, text="删除", width=6,
                   command=self.del_zen_account).grid(row=0, column=3, padx=(3, 0))
        ttk.Label(top, text="选中的账号会写入 config.json 的 zen_keys；"
                            "点下面的「设为当前」并保存即可生效。",
                  foreground="#7a7a7a",
                  font=("Microsoft YaHei UI", 8)).grid(
            row=1, column=0, columnspan=4, sticky="w", padx=(0, 6), pady=(1, 0))

        row = ttk.Frame(g1)
        row.grid(row=2, column=0, columnspan=3, sticky="we", padx=(4, 6), pady=(2, 2))
        self.t_zen = tk.Text(row, height=3, width=70, undo=True,
                             font=("Consolas", 9))
        self.t_zen.pack(side="left", fill="both", expand=True)
        ttk.Button(row, text="设为当前", width=10,
                   command=self.set_zen_current).pack(side="right", padx=(6, 0))
        ttk.Button(row, text="保存配置", width=10,
                   command=self.save).pack(side="right", padx=(6, 0))
        ttk.Label(g1, text="你在 opencode 开的 oc_sk_... key，免费/标准模型走这里。"
                           "多个账号可轮换（限流时换号很有用）。",
                  foreground="#7a7a7a", font=("Microsoft YaHei UI", 8)).grid(
            row=3, column=0, columnspan=3, sticky="w", padx=(4, 6))

        g2 = self._group(tab, "Go 上游密钥 go_keys（需 Go 订阅）", 2)
        self.t_go = tk.Text(g2, height=2, width=70, undo=True,
                            font=("Consolas", 9))
        self.t_go.grid(row=0, column=0, columnspan=3, sticky="we",
                       padx=(4, 6), pady=(4, 2))
        ttk.Label(g2, text="没有 Go 订阅就留空；填了非 Go 订阅的 key 会报 403",
                  foreground="#c0392b", font=("Microsoft YaHei UI", 8)).grid(
            row=1, column=0, columnspan=3, sticky="w", padx=(4, 6))

        note = ttk.Label(tab, text="注意：server_keys（sk-zen-local-…）与 zen_keys/go_keys（oc_sk_…）"
                                   "是两套凭据，不能互相混填，混用会 401 invalid local API key。",
                         foreground="#c0392b", font=("Microsoft YaHei UI", 8),
                         wraplength=760, justify="left")
        note.grid(row=3, column=0, sticky="w", padx=14, pady=(8, 0))
        tab.columnconfigure(0, weight=1)

    def _tab_network(self):
        tab = self._new_tab("  网络与重试  ")

        g = self._group(tab, "代理", 0)
        self.t_proxy = tk.Text(g, height=2, width=70, font=("Consolas", 9))
        self.t_proxy.grid(row=0, column=0, columnspan=3, sticky="we",
                          padx=(4, 6), pady=(4, 2))
        ttk.Label(g, text="每行一个，例如 http://127.0.0.1:10808；填 direct 表示直连。"
                          "代理断流后网关会进冷却，恢复后需重启代理。",
                  foreground="#7a7a7a", font=("Microsoft YaHei UI", 8),
                  wraplength=560, justify="left").grid(
            row=1, column=0, columnspan=3, sticky="w", padx=(4, 6))
        self.v_proxyfile = tk.StringVar()
        r = self._row(g, 2, "代理文件 proxyfile",
                      ttk.Entry(g, textvariable=self.v_proxyfile),
                      "留空即可；填写后按行读取代理")

        g1 = self._group(tab, "上游地址", 1)
        self.v_up_zen = tk.StringVar()
        r = self._row(g1, 0, "Zen 上游", ttk.Entry(g1, textvariable=self.v_up_zen))
        self.v_up_go = tk.StringVar()
        self._row(g1, r, "Go 上游", ttk.Entry(g1, textvariable=self.v_up_go))

        g2 = self._group(tab, "重试", 2)
        self.v_retry_n = tk.StringVar()
        r = self._row(g2, 0, "最大重试次数",
                      ttk.Entry(g2, textvariable=self.v_retry_n, width=12))
        self.v_retry_t = tk.StringVar()
        self._row(g2, r, "单次超时（秒）",
                  ttk.Entry(g2, textvariable=self.v_retry_t, width=12))

        g3 = self._group(tab, "性能与故障", 3)
        self.v_cooldown = tk.StringVar()
        r = self._row(g3, 0, "故障冷却（秒）",
                      ttk.Entry(g3, textvariable=self.v_cooldown, width=12),
                      "上游出错后暂停转发的时长")
        self.v_conn_t = tk.StringVar()
        r = self._row(g3, r, "连接超时（秒）",
                      ttk.Entry(g3, textvariable=self.v_conn_t, width=12))
        self.v_idle_t = tk.StringVar()
        self._row(g3, r, "空闲连接超时（秒）",
                  ttk.Entry(g3, textvariable=self.v_idle_t, width=12))

    def _tab_thinking(self):
        tab = self._new_tab("  思考与模型  ")

        g = self._group(tab, "思考强度 reasoning", 0)
        self.v_effort = tk.StringVar()
        cb = ttk.Combobox(g, textvariable=self.v_effort, values=EFFORT_VALUES,
                          state="readonly", width=18)
        r = self._row(g, 0, "全局强度 effort", cb,
                      "请求自己没带强度参数时用它；客户端带了则以客户端为准。"
                      "免费 flash 模型实际思考量很小，high 也不会慢。")

        g1 = self._group(tab, "按模型覆盖 effort_by_model", 1)
        self.t_effort_model = tk.Text(g1, height=4, width=70, font=("Consolas", 9))
        self.t_effort_model.grid(row=0, column=0, columnspan=3, sticky="we",
                                 padx=(4, 6), pady=(4, 2))
        ttk.Label(g1, text="每行一条：模型ID=强度，例如  mimo-v2.6-flash-free=low",
                  foreground="#7a7a7a", font=("Microsoft YaHei UI", 8)).grid(
            row=1, column=0, columnspan=3, sticky="w", padx=(4, 6))

        g2 = self._group(tab, "模型列表", 2)
        self.v_refresh = tk.StringVar()
        r = self._row(g2, 0, "刷新间隔（秒）",
                      ttk.Entry(g2, textvariable=self.v_refresh, width=12),
                      "从 opencode 拉取模型目录的周期")
        ttk.Button(g2, text="打开模型列表文件", command=self.open_models).grid(
            row=r, column=0, columnspan=3, sticky="w", padx=(10, 0), pady=(8, 2))

    # ------------------------------------------------------ 模型可用性
    def _catalog_path(self):
        return os.path.join(APP_DIR, "config.json.models.catalog.json")

    def _devmodels_path(self):
        return os.path.join(APP_DIR, "config.json.models.dev.json")

    def _read_catalog(self):
        """读网关自动刷新的模型目录 -> [(模型id, 来源, 是否弃用)]，外加目录更新时间。

        用户要求（2026-09-28）：
        * 只显示 zen 免费池，go 池（要订阅）暂时不放进来；
        * 已下线(deprecated)的模型直接不显示。
        """
        try:
            with open(self._catalog_path(), encoding="utf-8") as f:
                cat = json.load(f)
        except Exception:
            return [], ""
        updated = cat.get("updated_at", "")
        try:
            with open(self._devmodels_path(), encoding="utf-8") as f:
                dev = json.load(f)
            devmap = (dev.get("models") or {}) if isinstance(dev, dict) else {}
        except Exception:
            devmap = {}
        out = []
        for mid in (cat.get("zen") or []):
            if not (isinstance(mid, str) and mid):
                continue
            if (devmap.get(mid) or {}).get("deprecated"):
                continue
            out.append((mid, "zen", False))
        return out, updated

    def _tab_models(self):
        tab = self._new_tab("  模型  ")
        tab.columnconfigure(0, weight=1)

        g = self._group(tab, "模型可用性", 0)
        top = ttk.Frame(g)
        top.grid(row=0, column=0, columnspan=3, sticky="we", padx=(4, 6), pady=(4, 2))
        top.columnconfigure(0, weight=1)
        self.lbl_models_info = tk.Label(top, text="", anchor="w", fg="#666666",
                                        font=("Microsoft YaHei UI", 8))
        self.lbl_models_info.grid(row=0, column=0, sticky="w")
        # 同一个容器里只能用一个布局管理器：这里统一用 grid（混 pack 会报错）
        ttk.Button(top, text="刷新目录", width=9,
                   command=self.reload_models).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(top, text="检测可用性", width=10,
                   command=self.probe_models).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(top, text="停止", width=6,
                   command=self.stop_probe).grid(row=0, column=3, padx=(6, 0))

        heads = [("model", "模型", 230), ("source", "来源", 50),
                 ("status", "可用性", 95), ("detail", "说明", 250),
                 ("time", "检测时间", 85)]
        self.tv_models = ttk.Treeview(
            g, columns=[h[0] for h in heads], show="headings", height=16)
        for cid, txt, w in heads:
            self.tv_models.heading(cid, text=txt)
            self.tv_models.column(cid, width=w, anchor="w")
        ys = ttk.Scrollbar(g, orient="vertical", command=self.tv_models.yview)
        self.tv_models.configure(yscrollcommand=ys.set)
        self.tv_models.grid(row=1, column=0, columnspan=3, sticky="nsew",
                            padx=(4, 0), pady=(2, 2))
        ys.grid(row=1, column=3, sticky="ns", padx=(0, 6))
        g.rowconfigure(1, weight=1)
        ttk.Label(
            g,
            text="目录由网关自动刷新（只收录 zen 免费池）；检测前请先点顶部「启动」。"
                 "「检测可用性」会对每个模型发一个极小请求，据此判断："
                 "可用 / 额度用尽(429限流) / 无权 / 不存在 / 上游不通 / 无法连接。",
            foreground="#7a7a7a", font=("Microsoft YaHei UI", 8),
            wraplength=700, justify="left").grid(
            row=2, column=0, columnspan=4, sticky="w", padx=(4, 6), pady=(2, 0))
        self.lbl_probe_key = tk.Label(g, text="", anchor="w", fg="#666666",
                                      font=("Microsoft YaHei UI", 8),
                                      wraplength=700, justify="left")
        self.lbl_probe_key.grid(row=3, column=0, columnspan=4, sticky="w",
                                padx=(4, 6), pady=(2, 0))

        self._probe_stop = threading.Event()
        self.reload_models()

    def _note_gateway_keys(self):
        """网关成功启动时，记下当时生效的 zen 密钥。

        为什么：检测可用性是打到本地网关，真正拿 zen key 去请求 opencode 的是
        **网关进程**（它只在启动时读 config.json）。所以"配置里改了 key"和
        "网关实际在用的 key"可能不一致，必须能看出来，否则检测结果会骗人。
        """
        gs = load_gui_settings()
        gs["zen_keys_at_start"] = list(self.cfg.get("zen_keys") or [])
        gs["zen_keys_at_start_ts"] = time.strftime("%H:%M:%S")
        save_gui_settings(gs)

    def keys_in_sync(self):
        """配置里的 zen key 与网关实际生效的是否一致。返回 True/False/None(未知)。"""
        gs = load_gui_settings()
        started = gs.get("zen_keys_at_start")
        if started is None:
            return None  # 网关不是本控制台启动的，无从判断
        return list(started) == list(self.cfg.get("zen_keys") or [])

    def _update_probe_key(self):
        """模型页上明确写出：检测到底用的是哪个账号。"""
        if not hasattr(self, "lbl_probe_key"):
            return
        cfg_keys = list(self.cfg.get("zen_keys") or [])
        now = self._acct_label(cfg_keys[0]) if cfg_keys else "（未设置）"
        sync = self.keys_in_sync()
        if sync is None:
            self.lbl_probe_key.config(
                text="检测用的上游账号：%s ｜ 网关非本控制台启动，无法确定其生效密钥" % now,
                fg="#b8860b")
        elif sync:
            self.lbl_probe_key.config(
                text="检测用的上游账号：%s（已随网关启动生效）" % now, fg="#1a7f37")
        else:
            started = load_gui_settings().get("zen_keys_at_start") or []
            was = self._acct_label(started[0]) if started else "（空）"
            self.lbl_probe_key.config(
                text="⚠ 网关实际还在用 %s；配置已改为 %s —— 需重启代理才生效" % (was, now),
                fg="#c0392b")

    def reload_models(self):
        """重画模型表：目录 + 上次检测结果（缓存在 gui_settings.json）。"""
        rows, updated = self._read_catalog()
        gs = load_gui_settings()
        cache = (gs.get("model_probe") or {}) if isinstance(gs, dict) else {}
        for i in self.tv_models.get_children():
            self.tv_models.delete(i)
        for mid, src, dep in sorted(rows, key=lambda x: x[0]):
            hit = cache.get(mid) or {}
            status = hit.get("status") or "未检测"
            detail = hit.get("detail") or ""
            tm = hit.get("time") or ""
            self.tv_models.insert("", "end", iid=mid, values=(
                mid, src, status, detail, tm))
        n = len(rows)
        checked = len([1 for m, _, _ in rows if (cache.get(m) or {}).get("status")])
        self.lbl_models_info.config(
            text="免费模型 %d 个（zen 池，go 池未收录）· 已检测 %d 个 · 目录更新 %s"
                 % (n, checked, (updated or "未知")[:19].replace("T", " ")))
        self._update_probe_key()

    def stop_probe(self):
        if hasattr(self, "_probe_stop"):
            self._probe_stop.set()
        self.flash("正在停止检测…", "warn")

    def _probe_set(self, mid, status, detail):
        def apply():
            vals = list(self.tv_models.item(mid, "values"))
            if len(vals) >= 5:
                vals[2], vals[3], vals[4] = status, detail, time.strftime("%H:%M:%S")
            else:
                vals = [mid, "", status, detail, time.strftime("%H:%M:%S")]
            try:
                self.tv_models.item(mid, values=vals)
            except Exception:
                pass
        self.post(apply)
        # 落盘缓存，下次打开还能看到
        gs = load_gui_settings()
        cache = gs.get("model_probe") or {}
        cache[mid] = {"status": status, "detail": detail,
                      "time": time.strftime("%H:%M:%S")}
        gs["model_probe"] = cache
        save_gui_settings(gs)

    def _probe_one(self, model):
        """对单个模型发一个极小的请求，返回 (状态, 说明)。"""
        listen = self.v_listen.get().strip() or "127.0.0.1:8080"
        keys = (self.cfg.get("server_keys") or [])
        key = keys[0] if keys else ""
        url = "http://%s/v1/chat/completions" % listen
        data = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1, "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={
            "Authorization": "Bearer %s" % key,
            "Content-Type": "application/json",
        })
        try:
            # 强制绕过代理：网关就在本机 127.0.0.1，若进程继承了 HTTP(S)_PROXY
            # 环境变量，探测会绕远路走代理，被拒后一律显示 502 误报。
            # 实测踩坑：手动启动网关时 shell 里有 HTTPS_PROXY=127.0.0.1:60999，
            # 导致所有模型都报 "upstream connect failed (os error 10061)"，
            # 看起来像是模型全挂了，其实是代理没绕开。
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=15) as r:
                code = r.getcode() if hasattr(r, "getcode") else 200
                if 200 <= code < 300:
                    return "可用", "HTTP %s" % code
                return "异常", "HTTP %s" % code
        except urllib.error.HTTPError as e:
            code = e.code
            # 只取上游给的 message，别把整段 JSON 糊进表格里
            msg = ""
            try:
                raw = e.read().decode("utf-8", "replace")
                try:
                    msg = ((json.loads(raw).get("error") or {}).get("message")
                           or "").strip()
                except Exception:
                    msg = raw.strip()
            except Exception:
                pass
            msg = msg.replace("\n", " ")[:80]
            if code == 429:
                return "额度用尽/限流", "429 上游限流（今天额度可能已用完）"
            if code == 402:
                # 实测常见：账号没钱了，换号或充值
                return "余额不足", ("账号余额不足，换号或充值" +
                                   ("（%s）" % msg if msg else ""))
            if code in (401, 403):
                return "无权访问", ("当前 key 没有此模型权限" +
                                   ("（%s）" % msg if msg else ""))
            if code == 404:
                return "模型不存在", "上游没有这个模型"
            if code in (502, 504):
                return "上游不通", "网关连不上 opencode 上游（网络/代理问题）"
            return "错误", "HTTP %s %s" % (code, msg or e.reason)
        except Exception as e:
            # 报错说人话：别把 URLError: <urlopen error [WinError 10061]…> 这种
            # 原始串糊在表格里（用户截图吐槽过）
            if isinstance(e, urllib.error.URLError):
                reason = getattr(e, "reason", e)
                text = str(reason)
                if "10061" in text or isinstance(reason, ConnectionRefusedError):
                    return "无法连接", "网关未启动，先点顶部「启动」"
                if "timed out" in text or isinstance(reason, socket.timeout):
                    return "无法连接", "连接超时"
                return "无法连接", text[:70]
            return "无法连接", "%s" % str(e)[:70]

    def probe_models(self):
        """逐个检测可用性（worker 线程，结果即时回写表格）。"""
        if getattr(self, "busy", False):
            self.flash("有操作正在进行，稍后再试", "warn")
            return
        ids = list(self.tv_models.get_children())
        if not ids:
            self.flash("免费模型目录为空：先启动代理让它拉一次目录再检测", "warn")
            return
        # 预检：网关没起就别白测了（否则整列都是"无法连接"，用户截图吐槽过）
        host, port = split_listen(self.v_listen.get().strip() or "127.0.0.1:8080")
        if not port_open(host, port):
            self.flash("网关 %s:%s 未启动：先点顶部「启动」，再检测可用性"
                       % (host, port), "err")
            return
        # 密钥一致性提醒：网关只在启动时读 config.json，
        # 若改了 key 没重启，这里测到的其实是旧号的结果
        if self.keys_in_sync() is False:
            if messagebox.askyesno(
                    "密钥可能没生效",
                    "配置里的 zen 密钥已改，但网关还在用启动时那个旧 key 跑。\n"
                    "继续检测的话，结果是**旧账号**的。\n\n"
                    "要现在重启代理吗？（重启后再点一次「检测可用性」）"):
                self.restart()
                return
            self.flash("继续检测中：结果反映的是旧账号，请留意", "warn")
        self._probe_stop.clear()
        self.busy = True
        total = len(ids)
        self.flash("开始检测 %d 个免费模型…" % total, "warn")

        def job():
            done = 0
            bad = 0
            miss = 0  # 连续"无法连接"计数：网关中途挂掉就早停，别刷一屏错误
            aborted = False
            try:
                for mid in ids:
                    if self._probe_stop.is_set():
                        aborted = True
                        break
                    st, detail = self._probe_one(mid)
                    if st == "无法连接":
                        miss += 1
                        if miss >= 3:
                            aborted = True
                            self._probe_set(mid, st, detail)
                            self.post(lambda: self.flash(
                                "网关连续 3 次连不上，已中止检测（可能中途被停了）",
                                "err"))
                            break
                    else:
                        miss = 0
                    self._probe_set(mid, st, detail)
                    done += 1
                    if st != "可用":
                        bad += 1
            finally:
                self.busy = False
                self.post(lambda: self.flash(
                    "检测结束：%d/%d 个，异常 %d 个%s"
                    % (done, total, bad,
                       "（已中止）" if aborted else
                       "（已手动停止）" if self._probe_stop.is_set() else ""),
                    "warn" if aborted or bad else "ok"))
                self.post(self.reload_models)

        threading.Thread(target=job, daemon=True).start()

    def _build_bottom(self):
        bar = ttk.Frame(self.root, padding=(10, 6, 10, 8))
        bar.pack(fill="x", side="bottom")

        ttk.Button(bar, text="保存配置", command=self.save).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="打开日志", command=self.open_log).pack(side="left", padx=3)
        ttk.Button(bar, text="打开 WebUI", command=self.open_webui).pack(side="left", padx=3)
        ttk.Button(bar, text="配置目录", command=self.open_dir).pack(side="left", padx=3)

        ttk.Label(bar, foreground="#7a7a7a", font=("Microsoft YaHei UI", 8),
                  text="关掉窗口 = 收到托盘；托盘图标右键可 启动/停止/重启").pack(
            side="right", padx=6)

    # ---------------------------------------------------------- 托盘
    def _build_tray(self):
        image = Image.open(ICON_PATH) if os.path.exists(ICON_PATH) else None
        if image is None:
            image = Image.new("RGBA", (64, 64), (23, 64, 133, 255))

        menu = pystray.Menu(
            pystray.MenuItem(lambda i: self._tray_status_text(),
                             self.m_show, default=True),
            pystray.MenuItem(lambda i: self._tray_addr_text(),
                             lambda i: None, enabled=False),
            pystray.MenuItem(lambda i: self._tray_shim_text(),
                             lambda i: None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("启动代理", self.m_start,
                             enabled=lambda i: not self._running and not self.busy),
            pystray.MenuItem("停止代理", self.m_stop,
                             enabled=lambda i: self._running and not self.busy),
            pystray.MenuItem("重启代理", self.m_restart,
                             enabled=lambda i: self._running and not self.busy),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("复制 codex 接入地址", self.m_copy_url),
            pystray.MenuItem("切换 codex 兼容层", self.m_shim_toggle,
                             enabled=lambda i: not self.busy),
            pystray.MenuItem(lambda i: self._tray_wb_text(),
                             lambda i: None, enabled=False),
            pystray.MenuItem("切换 WorkBuddy 抗断流层", self.m_wb_toggle,
                             enabled=lambda i: not self.busy),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开日志", self.m_log),
            pystray.MenuItem("打开 WebUI", self.m_webui),
            pystray.MenuItem("打开配置目录", self.m_dir),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出控制台", self.m_quit),
        )
        self.tray = pystray.Icon("opencode2api", image, "opencode2api 控制台", menu)
        try:
            self.tray.run_detached()
        except Exception as e:
            self.tray = None
            self.flash("托盘图标启动失败：%s" % e, "warn")

    def _tray_status_text(self):
        if self._running:
            return "运行中（PID %s）· 点此打开窗口" % (self._pid or "?")
        return "已停止 · 点此打开窗口"

    def _tray_addr_text(self):
        # 注意：托盘线程里绝对不能碰 tkinter 控件，这里只读缓存
        return "  %s" % (self._tray_listen_cache or "(未配置)")

    def _tray_shim_text(self):
        # 托盘线程只读 python 侧的运行状态，不碰 Tk
        if codex_shim.shim_is_running():
            st = codex_shim.shim_stats()
            return "  codex 兼容层运行中（已改写 %d 次）" % st.get("count", 0)
        return "  codex 兼容层未运行"

    def m_shim_toggle(self, icon=None, item=None):
        # 在主线程里翻转开关，再走正常开关逻辑
        def job():
            self.v_shim_on.set(not self.v_shim_on.get())
            self._toggle_shim()
        self.post(job)

    def _tray_wb_text(self):
        # 托盘线程只读 python 侧的运行状态，不碰 Tk
        if wb_shim.wb_is_running():
            st = wb_shim.wb_stats()
            return "  WorkBuddy 抗断流层运行中（已处理 %d 次 · 续接 %d 次）" % (
                st.get("requests", 0), st.get("resumes", 0))
        return "  WorkBuddy 抗断流层未运行"

    def m_wb_toggle(self, icon=None, item=None):
        def job():
            self.v_wb_on.set(not self.v_wb_on.get())
            self._toggle_wb()
        self.post(job)

    # 托盘回调在托盘线程里执行，必须转到 Tk 主线程
    def post(self, fn):
        self.ui_q.put(fn)

    def _drain_loop(self):
        try:
            while True:
                fn = self.ui_q.get_nowait()
                try:
                    fn()
                except Exception as e:
                    self.flash("操作出错：%s" % e, "err")
        except queue.Empty:
            pass
        if not self.exiting:
            self.root.after(120, self._drain_loop)

    def m_show(self, icon=None, item=None):
        self.post(self.show_window)

    def m_start(self, icon=None, item=None):
        self.post(self.start)

    def m_stop(self, icon=None, item=None):
        self.post(self.stop)

    def m_restart(self, icon=None, item=None):
        self.post(self.restart)

    def m_copy_url(self, icon=None, item=None):
        def job():
            self.root.clipboard_clear()
            self.root.clipboard_append(self.codex_base_url())
            self.root.update_idletasks()
        self.post(job)

    def m_log(self, icon=None, item=None):
        self.post(self.open_log)

    def m_webui(self, icon=None, item=None):
        self.post(self.open_webui)

    def m_dir(self, icon=None, item=None):
        self.post(self.open_dir)

    def m_quit(self, icon=None, item=None):
        self.post(self.quit_app)

    # ---------------------------------------------------------- 窗口显隐
    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        try:
            self.root.attributes("-topmost", True)
            self.root.after(300, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass

    def hide_to_tray(self):
        self.root.withdraw()
        if self.tray and not self._tray_ballooned:
            self._tray_ballooned = True
            try:
                self.tray.notify("已收进系统托盘，右键图标可启动 / 停止 / 重启代理",
                                 "opencode2api 控制台")
            except Exception:
                pass

    def quit_app(self):
        if self.exiting:
            return
        if self._running:
            if not messagebox.askyesno(
                    "退出",
                    "代理服务仍在运行。\n退出控制台会同时关闭代理，WorkBuddy 将连不上。\n\n确定退出吗？"):
                return
        self.exiting = True
        self.stop_flag.set()
        try:
            self._do_stop(quiet=True)
        except Exception:
            pass
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass
        # 托盘线程是非 daemon，确保进程一定能退出
        threading.Timer(0.8, os._exit, args=(0,)).start()

    # ---------------------------------------------------------- 状态显示
    def _status_worker(self):
        while not self.stop_flag.is_set():
            host, port = split_listen(self.cfg.get("listen", "127.0.0.1:8080"))
            running = port_open(host, port)
            pid = find_pid(port) if running else None
            self.post(lambda r=running, p=pid: self._render_status(r, p))
            self.stop_flag.wait(1.3)

    def _render_status(self, running, pid):
        self._running = running
        self._pid = pid
        if running:
            who = "由控制台管理" if (self.proc and self.proc.pid == pid) else "外部启动（start.bat）"
            self.dot.config(fg="#1e9e4a")
            self.status_lbl.config(text="运行中 · PID %s · %s" % (pid, who))
        else:
            self.dot.config(fg="#9a9a9a")
            self.status_lbl.config(text="已停止")
        # 主按钮随状态切换：运行中显示「停止」，已停止显示「启动」
        if hasattr(self, "btn_toggle"):
            try:
                self.btn_toggle.config(text="停止" if running else "启动")
            except Exception:
                pass
        self._tray_listen_cache = self.v_listen.get().strip()
        self._update_codex_url()
        self._render_shim_status()

    def _render_shim_status(self):
        """刷新 codex 兼容层状态行。"""
        if not hasattr(self, "lbl_shim_stat"):
            return
        front = self.v_shim_front.get().strip() or "127.0.0.1:18000"
        base = self.codex_base_url()
        if codex_shim.shim_is_running():
            st = codex_shim.shim_stats()
            n = st.get("count", 0)
            last = st.get("last_model") or ""
            mode = "chat 翻译模式" if not codex_shim.native_responses_models() else "混合模式"
            txt = "运行中 · %s · 已处理 %d 次请求" % (mode, n)
            if last:
                txt += "（最近：%s）" % last
            self.lbl_shim_stat.config(text=txt, fg="#1a7f37")
        else:
            self.lbl_shim_stat.config(
                text="未运行 · 勾选上面的开关后点「启动」即可（codex 填 %s）" % base,
                fg="#9a9a9a")
        # WorkBuddy 抗断流层状态（独立标签，随兼容层一起刷新）
        self._render_wb_status()

    def _render_wb_status(self):
        """刷新 WorkBuddy 抗断流层状态行。"""
        if not hasattr(self, "lbl_wb_stat"):
            return
        base = self.wb_base_url()
        if wb_shim.wb_is_running():
            st = wb_shim.wb_stats()
            txt = ("运行中 · 已处理 %d 次请求 · 续接 %d 次 · 开窗 %d 次"
                   % (st.get("requests", 0), st.get("resumes", 0),
                      st.get("windows", 0)))
            self.lbl_wb_stat.config(text=txt, fg="#1a7f37")
        else:
            self.lbl_wb_stat.config(
                text="未运行 · 勾选上面的开关后点「启动代理」即自动拉起"
                     "（WorkBuddy 填 %s）" % base,
                fg="#9a9a9a")

    def flash(self, text, kind="ok"):
        color = {"ok": "#1a7f37", "err": "#c0392b", "warn": "#b8860b"}.get(kind, "#333")
        self.msg_lbl.config(text=text, fg=color)
        self.root.after(6000, lambda: self.msg_lbl.config(text=""))

    # ------------------------------------------------------ Zen 多账号
    @staticmethod
    def _acct_label(key):
        """把 oc_sk_… 显示成好认的标签：oc_sk_187b8c21… -> oc_sk_187b8c21…（后4位）"""
        k = (key or "").strip()
        if len(k) <= 14:
            return k or "(空)"
        return "%s…%s" % (k[:12], k[-4:])

    def _load_zen_accounts(self):
        """已登记账号列表：来自 gui_settings.json（多账号都存这里）。"""
        gs = load_gui_settings()
        accts = gs.get("zen_accounts") or []
        if isinstance(accts, list) and accts:
            return [a for a in accts if isinstance(a, str) and a.strip()]
        # 没有登记过：用当前 config.json 里的 zen_keys 兜底
        return [k for k in (self.cfg.get("zen_keys") or []) if k.strip()]

    def _save_zen_accounts(self, accts):
        gs = load_gui_settings()
        gs["zen_accounts"] = list(accts)
        save_gui_settings(gs)

    def _refresh_zen_accounts(self, select=None):
        accts = self._load_zen_accounts()
        labels = [self._acct_label(a) for a in accts]
        self.cb_zen_acct.config(values=labels)
        if not labels:
            self.v_zen_acct.set("")
            return
        if select and select in accts:
            self.v_zen_acct.set(self._acct_label(select))
        elif self.v_zen_acct.get() not in labels:
            self.v_zen_acct.set(labels[0])

    def add_zen_account(self):
        key = (self.t_zen.get("1.0", "end-1c") or "").strip().split("\n")[0].strip()
        if not key:
            messagebox.showinfo("新增账号",
                                "请先把要添加的 oc_sk_... 粘贴到下面的密钥框里（第一行）。")
            return
        accts = self._load_zen_accounts()
        if key in accts:
            self._refresh_zen_accounts(select=key)
            self.flash("该账号已在列表中", "warn")
            return
        accts.append(key)
        self._save_zen_accounts(accts)
        self._refresh_zen_accounts(select=key)
        self.flash("已添加账号 %s（共 %d 个）" % (self._acct_label(key), len(accts)), "ok")

    def del_zen_account(self):
        label = self.v_zen_acct.get()
        accts = self._load_zen_accounts()
        key = next((a for a in accts if self._acct_label(a) == label), None)
        if not key:
            return
        if not messagebox.askyesno("删除账号",
                                   "确定从账号列表里删除 %s？\n"
                                   "（不会自动删config.json 里的当前密钥，"
                                   "需要的话再点「设为当前」改）" % label):
            return
        accts = [a for a in accts if a != key]
        self._save_zen_accounts(accts)
        self._refresh_zen_accounts()
        self.flash("已删除账号 %s" % label, "ok")

    def set_zen_current(self):
        """把下拉里选中的账号写成当前唯一生效的 zen_key。"""
        label = self.v_zen_acct.get()
        accts = self._load_zen_accounts()
        key = next((a for a in accts if self._acct_label(a) == label), None)
        if not key:
            messagebox.showinfo("设为当前", "请先在上面的下拉里选中一个账号。")
            return
        self.t_zen.delete("1.0", "end")
        self.t_zen.insert("1.0", key)
        # 直接保存并重启生效，不用再去找底部的「保存配置」按钮
        self.save(restart_after=True)

    # ------------------------------------------------------ codex 兼容层
    def _shim_addrs(self):
        front = self.v_shim_front.get().strip() or "127.0.0.1:18000"
        back = self.v_shim_back.get().strip() or self.v_listen.get().strip() \
            or "127.0.0.1:8080"
        return front, back

    def _toggle_shim(self):
        # 这些方法都在 Tk 主线程跑；_do_* 在 worker 线程跑，
        # worker 里绝对不能碰 Tk 变量（RuntimeError: main thread is not in main loop）。
        front, back = self._shim_addrs()
        self._pending_shim = (self.v_shim_on.get(), front, back)
        if not self.v_shim_on.get():
            codex_shim.stop_shim()
            self.flash("codex 兼容层已关闭", "warn")
            self._render_shim_status()
            return
        if not port_open(*split_listen(back)):
            self.flash("网关 %s 还没起来，请先点「启动代理」" % back, "err")
            self._render_shim_status()
            return
        self._run_async(self._do_shim, "正在启动 codex 兼容层…")

    def _do_shim(self):
        want, front, back = self._pending_shim
        if not want:
            return
        ok, msg = codex_shim.start_shim(front, back, verbose=True)
        self.post(lambda m=msg, o=ok: (
            self.flash(m, "ok" if o else "err"),
            self._render_shim_status()))

    def _auto_shim(self):
        """开局自动拉起（网关已在跑才拉）。"""
        self._pending_shim = (self.v_shim_on.get(),) + self._shim_addrs()
        if not self.v_shim_on.get():
            return
        _, front, back = self._pending_shim
        if not port_open(*split_listen(back)):
            self.post(lambda: self.flash(
                "网关 %s 还没起来，codex 兼容层将在点「启动代理」后一起拉起" % back,
                "warn"))
            self._render_shim_status()
            return
        ok, msg = codex_shim.start_shim(front, back, verbose=True)
        if ok:
            self.post(lambda m=msg: (self.flash(m, "ok"),
                                     self._render_shim_status()))
        else:
            self.post(lambda m=msg: self.flash(m, "err"))

    def _shim_with_gateway(self):
        """网关启动成功后调用：把兼容层一起拉起来。"""
        want, front, back = self._pending_shim
        if not want:
            return
        ok, msg = codex_shim.start_shim(front, back, verbose=True)
        self.post(lambda m=msg, o=ok: (
            self.flash(m, "ok" if o else "err"),
            self._render_shim_status()))

    # ---------------------------------------------------------- WorkBuddy 抗断流层
    def _wb_addrs(self):
        front = self.v_wb_front.get().strip() or "127.0.0.1:18090"
        back = self.v_listen.get().strip() or "127.0.0.1:8080"
        return front, back

    def _toggle_wb(self):
        # 这些方法都在 Tk 主线程跑；_do_* 在 worker 线程跑
        front, back = self._wb_addrs()
        self._pending_wb = (self.v_wb_on.get(), front, back)
        self._update_codex_url()  # 同步「WorkBuddy URL」指向（18090 那层或网关 8080）
        if not self.v_wb_on.get():
            wb_shim.stop_wb()
            self.flash("WorkBuddy 抗断流层已关闭", "warn")
            self._render_wb_status()
            return
        if not port_open(*split_listen(back)):
            self.flash("网关 %s 还没起来，请先点「启动代理」" % back, "err")
            self._render_wb_status()
            return
        self._run_async(self._do_wb, "正在启动 WorkBuddy 抗断流层…")

    def _do_wb(self):
        want, front, back = self._pending_wb
        if not want:
            return
        ok, msg = wb_shim.start_wb(front, back, verbose=True)
        self.post(lambda m=msg, o=ok: (
            self.flash(m, "ok" if o else "err"),
            self._render_wb_status()))

    def _auto_wb(self):
        """开局自动拉起（网关已在跑才拉）。"""
        self._pending_wb = (self.v_wb_on.get(),) + self._wb_addrs()
        if not self.v_wb_on.get():
            return
        _, front, back = self._pending_wb
        if not port_open(*split_listen(back)):
            self.post(lambda: self.flash(
                "网关 %s 还没起来，WorkBuddy 抗断流层将在点「启动代理」后一起拉起" % back,
                "warn"))
            self._render_wb_status()
            return
        ok, msg = wb_shim.start_wb(front, back, verbose=True)
        if ok:
            self.post(lambda m=msg: (self.flash(m, "ok"),
                                     self._render_wb_status()))
        else:
            self.post(lambda m=msg: self.flash(m, "err"))

    def _wb_with_gateway(self):
        """网关启动成功后调用：把抗断流层一起拉起来。"""
        want, front, back = self._pending_wb
        if not want:
            return
        ok, msg = wb_shim.start_wb(front, back, verbose=True)
        self.post(lambda m=msg, o=ok: (
            self.flash(m, "ok" if o else "err"),
            self._render_wb_status()))

    # ---------------------------------------------------------- 启停逻辑
    def _run_async(self, fn, busy_msg="处理中…"):
        if self.busy:
            self.flash("上一个操作还没完成，请稍候", "warn")
            return
        self.busy = True
        self.flash(busy_msg, "warn")

        def worker():
            try:
                fn()
            except Exception:
                # 注意：异常变量在 except 块结束时会被删除，lambda 必须用默认值绑定
                import traceback
                msg = traceback.format_exc().strip().replace("\n", " | ")[:600]
                self.post(lambda m=msg: self.flash("出错：%s" % m, "err"))
            finally:
                self.busy = False

        threading.Thread(target=worker, daemon=True).start()

    def start(self):
        self._pending_shim = (self.v_shim_on.get(),) + self._shim_addrs()
        self._pending_wb = (self.v_wb_on.get(),) + self._wb_addrs()
        self._run_async(self._do_start, "正在启动代理…")

    def stop(self):
        self._pending_shim = (self.v_shim_on.get(),) + self._shim_addrs()
        self._pending_wb = (self.v_wb_on.get(),) + self._wb_addrs()
        self._run_async(self._do_stop, "正在停止代理…")

    def restart(self):
        self._pending_shim = (self.v_shim_on.get(),) + self._shim_addrs()
        self._pending_wb = (self.v_wb_on.get(),) + self._wb_addrs()

        def job():
            self._do_stop()
            time.sleep(0.6)
            self._do_start()
        self._run_async(job, "正在重启代理…")

    def _rotate_log(self):
        try:
            if os.path.exists(LOG_PATH):
                if os.path.exists(LOG_OLD):
                    os.remove(LOG_OLD)
                os.replace(LOG_PATH, LOG_OLD)
        except OSError:
            pass

    def _do_start(self):
        if not os.path.exists(EXE_PATH):
            self.post(lambda: self.flash("找不到 opencode2api.exe：%s" % EXE_PATH, "err"))
            return
        # 本函数运行在 worker 线程，不能碰 Tk 变量；监听地址从 self.cfg 读（已在主线程载入）
        listen_txt = self.cfg.get("listen", "127.0.0.1:8080")
        host, port = split_listen(listen_txt)
        if port_open(host, port):
            pid = find_pid(port)
            self.post(lambda p=pid: self._render_status(True, p))
            self.post(lambda: self.flash("代理已在运行（PID %s），无需重复启动" % find_pid(port), "warn"))
            # 网关已在外运行，也要保证兼容层被拉起
            self._shim_with_gateway()
            self._wb_with_gateway()
            return

        self._rotate_log()
        try:
            fh = open(LOG_PATH, "ab")
        except OSError as e:
            msg = "%s: %s" % (type(e).__name__, e)
            self.post(lambda m=msg: self.flash("无法写日志：%s" % m, "err"))
            return
        try:
            # 清掉可能存在的 http(s)_proxy 环境变量：若指向一个没开的代理，
            # Go 网关会强制走它，导致上游全部 502（os error 10061）。
            env = dict(os.environ)
            for k in list(env):
                if k.lower().endswith("_proxy"):
                    env.pop(k, None)
            proc = subprocess.Popen(
                [EXE_PATH, "-config", os.path.basename(CONFIG_PATH)],
                cwd=APP_DIR,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=fh,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
            )
        finally:
            fh.close()
        self.proc = proc

        deadline = time.time() + 6
        while time.time() < deadline:
            if proc.poll() is not None:
                self.post(lambda: self.flash(
                    "启动失败（进程已退出，code=%s）\n%s" % (proc.returncode, tail_log()),
                    "err"))
                return
            if port_open(host, port):
                self.post(lambda p=proc.pid: self._render_status(True, p))
                self.post(lambda: self.flash("代理已启动，监听 http://%s" % listen_txt, "ok"))
                # 记下这次网关启动时用的 zen 密钥：之后才能判断"配置改了但没重启"
                self._note_gateway_keys()
                self._shim_with_gateway()
                self._wb_with_gateway()
                return
            time.sleep(0.3)
        self.post(lambda: self.flash("已拉起进程但 6 秒内未监听端口，查看日志确认：%s" % LOG_PATH, "warn"))

    def _do_stop(self, quiet=False):
        host, port = split_listen(self.cfg.get("listen", "127.0.0.1:8080"))
        # 兼容层跟着网关一起停
        codex_shim.stop_shim()
        wb_shim.stop_wb()
        pids = []
        pid = find_pid(port)
        if pid:
            pids.append(pid)
        if self.proc is not None and self.proc.poll() is None and self.proc.pid not in pids:
            pids.append(self.proc.pid)

        if not pids:
            self.proc = None
            if not quiet:
                self.post(lambda: self.flash("当前没有运行中的代理", "warn"))
                self.post(lambda: self._render_shim_status())
            return

        for p in pids:
            subprocess.run(["taskkill", "/PID", str(p), "/F", "/T"],
                           capture_output=True, creationflags=CREATE_NO_WINDOW,
                           timeout=10)
        deadline = time.time() + 8
        while time.time() < deadline and port_open(host, port):
            time.sleep(0.3)
        self.proc = None
        if not quiet:
            if port_open(host, port):
                self.post(lambda: self.flash("已发送停止命令，但端口仍被占用，请查看日志", "err"))
            else:
                self.post(lambda: self._render_status(False, None))
                self.post(lambda: self._render_shim_status())
                self.post(lambda: self.flash("代理已停止", "ok"))

    # ---------------------------------------------------------- 配置表单
    def load_fields(self, cfg):
        self.v_listen.set(cfg.get("listen", "127.0.0.1:8080"))
        self._tray_listen_cache = self.v_listen.get().strip()
        self.v_prefer.set(cfg.get("prefer", "zen"))
        self.v_anon.set(bool(cfg.get("anonymous", False)))

        gs = load_gui_settings()
        shim = gs.get("codex_shim") or {}
        self.v_shim_on.set(bool(shim.get("enabled", True)))
        self.v_shim_front.set(shim.get("front", "127.0.0.1:18000"))
        self.v_shim_back.set(shim.get("back") or self.v_listen.get().strip()
                             or "127.0.0.1:8080")

        wb = gs.get("wb_shim") or {}
        self.v_wb_on.set(bool(wb.get("enabled", True)))
        self.v_wb_front.set(wb.get("front", "127.0.0.1:18090"))

        webui = cfg.get("webui") or {}
        self.v_webui_on.set(bool(webui.get("enabled", False)))
        self.v_webui_listen.set(webui.get("listen", "127.0.0.1:8081"))
        self.v_webui_user.set(webui.get("username", "admin"))

        logging_ = cfg.get("logging") or {}
        self.v_level.set(logging_.get("level", "info"))
        self.v_dump.set(bool(logging_.get("dump_request_bodies", False)))

        self._set_text(self.t_server, cfg.get("server_keys") or [])
        self._set_text(self.t_zen, cfg.get("zen_keys") or [])
        self._set_text(self.t_go, cfg.get("go_keys") or [])
        self._set_text(self.t_proxy, cfg.get("proxies") or [])
        self._refresh_zen_accounts(select=(cfg.get("zen_keys") or [None])[0])
        self._update_codex_url()

        self.v_proxyfile.set(cfg.get("proxyfile", ""))
        upstream = cfg.get("upstream") or {}
        self.v_up_zen.set(upstream.get("zen", ""))
        self.v_up_go.set(upstream.get("go", ""))

        retry = cfg.get("retry") or {}
        self.v_retry_n.set(str(retry.get("max_attempts", 3)))
        self.v_retry_t.set(str(retry.get("timeout_seconds", 300)))

        perf = cfg.get("performance") or {}
        self.v_cooldown.set(str(perf.get("failure_cooldown_seconds", 15)))
        self.v_conn_t.set(str(perf.get("connect_timeout_seconds", 5)))
        self.v_idle_t.set(str(perf.get("idle_conn_timeout_seconds", 120)))

        reasoning = cfg.get("reasoning") or {}
        effort = reasoning.get("effort", "")
        self.v_effort.set(effort if effort in EFFORT_VALUES[1:] else "（关闭）")
        by_model = reasoning.get("effort_by_model") or {}
        self._set_text(self.t_effort_model,
                       ["%s=%s" % (k, v) for k, v in sorted(by_model.items())])

        models = cfg.get("models") or {}
        self.v_refresh.set(str(models.get("refresh_seconds", 300)))

    @staticmethod
    def _set_text(widget, items):
        widget.delete("1.0", "end")
        if items:
            widget.insert("1.0", "\n".join(str(x) for x in items))

    def collect(self):
        cfg = self.read_config()
        errs = []

        listen = self.v_listen.get().strip()
        if ":" not in listen:
            errs.append("监听地址格式应为 host:port")
        else:
            cfg["listen"] = listen

        cfg["prefer"] = self.v_prefer.get() or "zen"
        cfg["anonymous"] = bool(self.v_anon.get())

        # codex 兼容中间层 —— 只存 GUI 专用配置文件，绝不写进网关 config.json
        # （Go 网关严格解析，未知字段会直接启动失败 code 1）
        front = self.v_shim_front.get().strip()
        back = self.v_shim_back.get().strip() or listen
        if self.v_shim_on.get():
            if ":" not in front:
                errs.append("codex 入口地址格式应为 host:port")
            elif ":" not in back:
                errs.append("codex 后端地址格式应为 host:port")
            elif front == back:
                errs.append("codex 入口与后端地址不能相同（会自己转发给自己）")
        gs = load_gui_settings()
        gs["codex_shim"] = {"enabled": bool(self.v_shim_on.get()),
                            "front": front, "back": back}
        if not save_gui_settings(gs):
            errs.append("无法写入 gui_settings.json（codex 兼容层配置）")

        gs["wb_shim"] = {"enabled": bool(self.v_wb_on.get()),
                         "front": self.v_wb_front.get().strip() or "127.0.0.1:18090"}
        if not save_gui_settings(gs):
            errs.append("无法写入 gui_settings.json（WorkBuddy 抗断流层配置）")

        setpath(cfg, ["webui", "enabled"], bool(self.v_webui_on.get()))
        setpath(cfg, ["webui", "listen"], self.v_webui_listen.get().strip())
        setpath(cfg, ["webui", "username"], self.v_webui_user.get().strip())

        setpath(cfg, ["logging", "level"], self.v_level.get() or "info")
        setpath(cfg, ["logging", "dump_request_bodies"], bool(self.v_dump.get()))

        cfg["server_keys"] = lines_of(self.t_server.get("1.0", "end-1c"))
        cfg["zen_keys"] = lines_of(self.t_zen.get("1.0", "end-1c"))
        cfg["go_keys"] = lines_of(self.t_go.get("1.0", "end-1c"))
        if not cfg["server_keys"]:
            errs.append("本地密钥 server_keys 不能为空（WorkBuddy 要用它连接）")

        cfg["proxies"] = lines_of(self.t_proxy.get("1.0", "end-1c")) or ["direct"]
        cfg["proxyfile"] = self.v_proxyfile.get().strip()
        setpath(cfg, ["upstream", "zen"], self.v_up_zen.get().strip())
        setpath(cfg, ["upstream", "go"], self.v_up_go.get().strip())

        def as_int(var, path, label):
            try:
                setpath(cfg, path, int(str(var.get()).strip()))
            except ValueError:
                errs.append("%s 必须是整数" % label)

        as_int(self.v_retry_n, ["retry", "max_attempts"], "最大重试次数")
        as_int(self.v_retry_t, ["retry", "timeout_seconds"], "单次超时")
        as_int(self.v_cooldown, ["performance", "failure_cooldown_seconds"], "故障冷却")
        as_int(self.v_conn_t, ["performance", "connect_timeout_seconds"], "连接超时")
        as_int(self.v_idle_t, ["performance", "idle_conn_timeout_seconds"], "空闲连接超时")
        as_int(self.v_refresh, ["models", "refresh_seconds"], "模型刷新间隔")

        effort = self.v_effort.get()
        setpath(cfg, ["reasoning", "effort"], "" if effort == "（关闭）" else effort)

        by_model = {}
        for ln in lines_of(self.t_effort_model.get("1.0", "end-1c")):
            if "=" not in ln:
                errs.append("按模型覆盖格式应为 模型ID=强度，当前行：%s" % ln)
                continue
            k, _, v = ln.partition("=")
            by_model[k.strip()] = v.strip()
        setpath(cfg, ["reasoning", "effort_by_model"], by_model)

        return cfg, errs

    def save(self, restart_after=False):
        """保存配置到 config.json。

        restart_after=True 时（切换账号等场景）不再弹框、直接重启生效——
        底部「保存配置」按钮在页面变长后容易被挤到看不见，
        不能让"找不到保存按钮"卡住换账号这件事。
        """
        if self.busy:
            self.flash("有操作正在进行，稍后再保存", "warn")
            return
        cfg, errs = self.collect()
        if errs:
            messagebox.showerror("配置有误", "\n".join("· " + e for e in errs))
            return
        try:
            self.write_config(cfg)
        except Exception as e:
            messagebox.showerror("保存失败", str(e))
            return
        self.cfg = cfg
        self.flash("配置已保存（已备份到 config.json.bak）", "ok")
        if self._running:
            if restart_after:
                self.flash("已保存，正在重启代理以应用新配置…", "warn")
                self.restart()
            elif messagebox.askyesno("需要重启", "代理正在运行，是否立即重启以应用新配置？"):
                self.restart()

    # ---------------------------------------------------------- 便捷操作
    def test_conn(self):
        if self.busy:
            self.flash("有操作正在进行，稍后再测", "warn")
            return
        cfg, errs = self.collect()
        if errs:
            messagebox.showerror("配置有误", "\n".join("· " + e for e in errs))
            return
        listen = cfg["listen"]
        keys = cfg["server_keys"]
        # 用 server_keys[0] 真实测一遍（和 WorkBuddy 填的 Key 一致）
        key = keys[0] if keys else ""
        self.busy = True
        self.flash("正在测试 http://%s/v1/models …" % listen, "warn")

        def job():
            url = "http://%s/v1/models" % listen
            try:
                req = urllib.request.Request(url, headers={
                    "Authorization": "Bearer %s" % key,
                    "Content-Type": "application/json",
                })
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                n = len(data.get("data", [])) if isinstance(data, dict) else 0
                # 顺带检查 codex 兼容层 + WorkBuddy 抗断流层状态，给出可操作提示
                tip = ""
                if codex_shim.shim_is_running():
                    st = codex_shim.shim_stats()
                    tip = "；codex 兼容层运行中（已改写 %d 次请求）" % st.get("count", 0)
                elif load_gui_settings().get("codex_shim", {}).get("enabled"):
                    tip = "；⚠ codex 兼容层未运行，codex 会报 tools 校验错误"
                if wb_shim.wb_is_running():
                    tip += "；WorkBuddy 抗断流层运行中（请让 WorkBuddy 指向 18090）"
                elif load_gui_settings().get("wb_shim", {}).get("enabled"):
                    tip += "；⚠ WorkBuddy 抗断流层未运行，WorkBuddy 会直连网关裸奔"
                self.post(lambda t=tip: self.flash(
                    "连接成功！HTTP 200，网关返回 %d 个模型（WorkBuddy 可正常使用）%s"
                    % (n, t), "ok" if not tip.startswith("；⚠") else "warn"))
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                self.post(lambda: self.flash(
                    "网关有响应但报错 HTTP %s：%s" % (e.code, body or e.reason), "err"))
            except Exception as e:
                self.post(lambda: self.flash(
                    "连接失败：%s（代理是否已启动？）" % e, "err"))
            finally:
                self.busy = False

        threading.Thread(target=job, daemon=True).start()

    def open_log(self):
        if not os.path.exists(LOG_PATH):
            messagebox.showinfo("日志", "日志文件还不存在：\n%s" % LOG_PATH)
            return
        os.startfile(LOG_PATH)

    def open_webui(self):
        addr = self.v_webui_listen.get().strip() or "127.0.0.1:8081"
        webbrowser.open("http://" + addr)

    def open_dir(self):
        os.startfile(APP_DIR)

    def open_models(self):
        for name in ("config.json.models.catalog.json", "config.json.models.dev.json"):
            p = os.path.join(APP_DIR, name)
            if os.path.exists(p):
                os.startfile(p)
                return
        messagebox.showinfo("模型列表", "还没生成模型列表文件，代理跑起来后会自动缓存。")

    # ---------------------------------------------------------- 运行
    def run(self):
        if self.cfg_error:
            self.flash(self.cfg_error, "err")
        self._render_shim_status()
        # 开局若勾了兼容层且网关已在跑，自动拉起
        self.root.after(500, self._auto_shim)
        self.root.after(600, self._auto_wb)
        self.root.mainloop()


def main():
    App().run()


if __name__ == "__main__":
    main()
