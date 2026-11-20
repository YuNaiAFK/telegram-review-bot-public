"""Windows control panel for the local Telegram review bot."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tkinter as tk
from tkinter import messagebox, ttk

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
STOP = ROOT / "stop.request"


def read_config():
    return json.loads(CONFIG.read_text(encoding="utf-8-sig"))


def ids(text):
    return [int(item.strip()) for item in text.split(",") if item.strip()]


BG = "#121820"
FIELD = "#1a232e"
BORDER = "#354656"
TEXT = "#eef3f7"
MUTED = "#a9bac9"
ACCENT = "#92d4fb"


def apply_dark_theme(window):
    window.configure(background=BG)
    style = ttk.Style(window)
    style.theme_use("clam")
    style.configure(".", background=BG, foreground=TEXT, font=("Microsoft YaHei UI", 10))
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("TEntry", fieldbackground=FIELD, bordercolor=BORDER, insertcolor=TEXT,
                    selectbackground="#325d78", selectforeground=TEXT, padding=6)
    style.configure("TCombobox", fieldbackground=FIELD, bordercolor=BORDER,
                    arrowcolor=TEXT, padding=5)
    style.map("TCombobox", fieldbackground=[("readonly", FIELD)],
              foreground=[("readonly", TEXT)], selectbackground=[("readonly", FIELD)],
              selectforeground=[("readonly", TEXT)])
    for option, color in (("background", FIELD), ("foreground", TEXT),
                          ("selectBackground", "#325d78"), ("selectForeground", TEXT)):
        window.option_add(f"*TCombobox*Listbox.{option}", color)
    style.configure("TButton", background=FIELD, bordercolor=BORDER, padding=(12, 7))
    style.map("TButton", background=[("active", "#293b4c")],
              foreground=[("disabled", "#8193a3")])
    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure("TNotebook.Tab", background=FIELD, foreground=MUTED, padding=(15, 6))
    style.map("TNotebook.Tab", background=[("selected", "#293b4c")],
              foreground=[("selected", TEXT)])
    style.configure("Vertical.TScrollbar", background=FIELD, troughcolor=BG, arrowcolor=MUTED)
    style.configure("Accent.TButton", background=ACCENT, foreground="#102634", bordercolor=ACCENT)
    style.map("Accent.TButton", background=[("disabled", FIELD), ("active", "#b2e2ff")],
              foreground=[("disabled", "#8193a3"), ("!disabled", "#102634")])


class LogTail:
    def __init__(self, path):
        self.path = path
        self.identity = None
        self.offset = 0
        self.partial = b""

    def read(self):
        try:
            with self.path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                if self.identity != identity or stat.st_size < self.offset:
                    self.partial = b""
                    self.offset = max(0, stat.st_size - 24576) if self.identity is None else 0
                    self.identity = identity
                    stream.seek(self.offset)
                    if self.offset:
                        stream.readline()
                else:
                    stream.seek(self.offset)
                chunk = stream.read(65536)
                self.offset = stream.tell()
        except OSError:
            return []
        records = (self.partial + chunk).split(b"\n")
        self.partial = records.pop()
        return [line.decode("utf-8-sig", errors="replace").rstrip("\r") for line in records]


class Panel:
    def __init__(self, window):
        self.window = window
        self.process = None
        self.stopping = False
        self.config = read_config()
        apply_dark_theme(window)
        window.title("Telegram 审查 Bot · Windows 控制台")
        window.geometry("720x670")
        window.minsize(620, 600)
        shell = ttk.Frame(window, padding=22)
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text="审查 Bot", font=("Microsoft YaHei UI", 21, "bold")).pack(anchor="w")
        ttk.Label(shell, text="管理入群审核设置，在同一窗口查看运行日志。",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 12))
        self.tabs = ttk.Notebook(shell)
        self.tabs.pack(fill="both", expand=True)
        body = ttk.Frame(self.tabs, padding=(0, 10, 0, 0))
        self.tabs.add(body, text="审核设置")
        body.columnconfigure(1, weight=1)
        body.rowconfigure(9, weight=1)
        self.log_tab = ttk.Frame(self.tabs, padding=(0, 10, 0, 0))
        self.tabs.add(self.log_tab, text="运行日志")
        self.log_tab.columnconfigure(0, weight=1)
        self.log_tab.rowconfigure(1, weight=1)
        ttk.Label(self.log_tab, text="实时显示启动和审核日志 · 可选中文字复制",
                  style="Muted.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.log_view = tk.Text(self.log_tab, wrap="word", state="disabled",
                                font=("Consolas", 10), padx=10, pady=8,
                                background=FIELD, foreground=TEXT, relief="flat", borderwidth=0,
                                selectbackground="#325d78", selectforeground=TEXT,
                                highlightthickness=1, highlightbackground=BORDER)
        scroll = ttk.Scrollbar(self.log_tab, orient="vertical", command=self.log_view.yview)
        self.log_view.configure(yscrollcommand=scroll.set)
        self.log_view.grid(row=1, column=0, sticky="nsew")
        scroll.grid(row=1, column=1, sticky="ns")
        self.log_view.tag_configure("startup", foreground=MUTED)
        self.log_view.tag_configure("error", foreground="#ffb0b0")
        self.log_view.tag_configure("warning", foreground="#ffd48d")
        self.tails = [(LogTail(ROOT / "startup.log"), "startup"), (LogTail(ROOT / "bot.log"), "")]
        self.fields = {}
        fields = [
            ("owner_id", "号主 Telegram ID"),
            ("reviewer_ids", "审核管理员 ID（逗号分隔）"),
            ("min_common_groups", "共同群门槛"),
            ("common_group_mode", "共同群核验方式"),
            ("common_group_ids", "指定群 ID（逗号分隔）"),
            ("telegram_http_proxy", "Telegram HTTP 代理"),
        ]
        for row, (key, label) in enumerate(fields, 2):
            value = self.config.get(key, "")
            if isinstance(value, list):
                value = ", ".join(map(str, value))
            field = tk.StringVar(value=value)
            self.fields[key] = field
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", padx=(0, 15), pady=4)
            widget = (ttk.Combobox(body, textvariable=field, state="readonly",
                                   values=("owner_account", "configured"))
                      if key == "common_group_mode" else ttk.Entry(body, textvariable=field))
            widget.grid(row=row, column=1, sticky="ew", pady=4)

        ttk.Label(body, text="群 / 频道审核问题（JSON：聊天 ID → 问题）").grid(
            row=8, columnspan=2, sticky="w", pady=(12, 5))
        self.targets = tk.Text(body, height=7, wrap="word", font=("Microsoft YaHei UI", 10),
                               padx=9, pady=8, background=FIELD, foreground=TEXT,
                               insertbackground=TEXT, selectbackground="#325d78",
                               selectforeground=TEXT, relief="flat", borderwidth=0,
                               highlightthickness=1, highlightbackground=BORDER,
                               highlightcolor=ACCENT)
        self.targets.grid(row=9, columnspan=2, sticky="nsew")
        self.targets.insert("1.0", json.dumps(self.config.get("targets", {}), ensure_ascii=False, indent=2))

        actions = ttk.Frame(shell)
        actions.pack(fill="x", pady=(13, 0))
        ttk.Button(actions, text="保存设置", command=self.save).pack(side="left", padx=(0, 8))
        self.start_button = ttk.Button(actions, text="启动 Bot", command=self.start, style="Accent.TButton")
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(actions, text="停止 Bot", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(0, 8))
        self.login_button = ttk.Button(actions, text="登录 / 配置凭据", command=lambda: self.start(interactive=True))
        self.login_button.pack(side="right")
        self.status = tk.StringVar(value="尚未从此窗口启动。已有 Bot 运行时，请勿重复启动。")
        ttk.Label(shell, textvariable=self.status, wraplength=560, style="Muted.TLabel").pack(
            anchor="w", pady=(8, 0))
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh_logs()

    def save(self):
        try:
            config = read_config()
            f = {k: v.get().strip() for k, v in self.fields.items()}
            owner = int(f["owner_id"])
            reviewers = ids(f["reviewer_ids"])
            minimum = int(f["min_common_groups"])
            groups = ids(f["common_group_ids"])
            targets = json.loads(self.targets.get("1.0", "end"))
            mode = f["common_group_mode"]
            if owner <= 0 or not reviewers or any(uid <= 0 for uid in reviewers):
                raise ValueError("请填写有效的号主及管理员 ID。")
            if minimum < 0 or mode not in {"owner_account", "configured"}:
                raise ValueError("共同群门槛不能为负数，请选择核验方式。")
            if mode == "configured" and (len(set(groups)) < minimum or any(g >= 0 for g in groups)):
                raise ValueError("指定群模式需要足够的不同群 ID。")
            if not isinstance(targets, dict) or not targets or any(
                not key.startswith("-") or not key[1:].isdigit()
                or not isinstance(value, str) or not value.strip() or len(value) > 1000
                for key, value in targets.items()
            ):
                raise ValueError("请填写至少一条有效的聊天 ID 和审核问题（最多 1000 字）。")
            config.update(owner_id=owner, reviewer_ids=reviewers, min_common_groups=minimum,
                          common_group_mode=mode, common_group_ids=groups,
                          telegram_http_proxy=f["telegram_http_proxy"], targets=targets)
            temp = CONFIG.with_suffix(".gui.tmp")
            temp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temp.replace(CONFIG)
        except (ValueError, OSError) as exc:
            messagebox.showerror("无法保存", str(exc), parent=self.window)
            return False
        self.status.set("设置已保存。若 Bot 已在运行，重启后生效。")
        return True

    def start(self, interactive=False):
        if self.process is not None or not self.save():
            return
        shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
        try:
            cmd = [shell or "powershell.exe", "-NoProfile"]
            if not interactive:
                cmd.append("-NonInteractive")
            cmd.extend(["-File", str(ROOT / "启动.ps1")])
            if not interactive:
                cmd.append("-Gui")
            flags = subprocess.CREATE_NEW_CONSOLE if interactive else subprocess.CREATE_NO_WINDOW
            streams = {} if interactive else dict.fromkeys(("stdin", "stdout", "stderr"), subprocess.DEVNULL)
            self.process = subprocess.Popen(cmd, cwd=ROOT, creationflags=flags, **streams)
        except OSError as exc:
            messagebox.showerror("无法启动", str(exc), parent=self.window)
            return
        self.interactive = interactive
        self.start_button.configure(state="disabled")
        self.login_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.tabs.select(self.log_tab)
        self.status.set("请在登录窗口完成凭据输入。" if interactive else "正在连接 Telegram…")
        self.window.after(300, self.poll)

    def stop(self):
        if self.process is None:
            return
        try:
            STOP.touch()
        except OSError as exc:
            messagebox.showerror("无法发送停止请求", str(exc), parent=self.window)
            return
        self.stopping = True
        self.stop_button.configure(state="disabled")
        self.status.set("已请求停止，等待当前网络请求结束。"
                        + ("若停在凭据输入提示，请关闭登录窗口。" if self.interactive else ""))

    def poll(self):
        code = self.process.poll()
        if code is None:
            self.window.after(350, self.poll)
            return
        self.process = None
        STOP.unlink(missing_ok=True)
        self.start_button.configure(state="normal")
        self.login_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status.set("Bot 已停止。" if self.stopping else f"Bot 进程已退出（{code}），详情见运行日志。")
        self.stopping = False

    def refresh_logs(self):
        at_end = self.log_view.yview()[1] >= 0.999
        for tail, default in self.tails:
            lines = tail.read()
            if not lines:
                continue
            self.log_view.configure(state="normal")
            for line in lines:
                tag = "error" if " ERROR " in line else "warning" if " WARNING " in line else default
                self.log_view.insert("end", line + "\n", tag)
                if self.process is not None and not self.stopping and "机器人 @" in line and "已启动" in line:
                    self.status.set("Bot 正在运行。")
            excess = int(self.log_view.index("end-1c").split(".")[0]) - 600
            if excess > 0:
                self.log_view.delete("1.0", f"{excess + 1}.0")
            self.log_view.configure(state="disabled")
        if at_end:
            self.log_view.see("end")
        self.window.after(550, self.refresh_logs)

    def close(self):
        if self.process is not None:
            messagebox.showinfo("程序仍在运行", "请先停止 Bot，待进程退出后再关闭控制台。",
                                parent=self.window)
            return
        self.window.destroy()


if __name__ == "__main__":
    app = tk.Tk()
    Panel(app)
    app.mainloop()
