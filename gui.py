from __future__ import annotations

import contextlib
import queue
import random
import sys
import threading
import time
import traceback
from pathlib import Path
from tkinter import BooleanVar, DoubleVar, IntVar, StringVar, Tk, filedialog, messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

import main as core


if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent


class QueueWriter:
    def __init__(self, out_queue: queue.Queue):
        self.out_queue = out_queue
        self.buffer = ""

    def write(self, text: str) -> int:
        if not text:
            return 0
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line.strip():
                self.out_queue.put(("log", line.rstrip()))
        return len(text)

    def flush(self) -> None:
        if self.buffer.strip():
            self.out_queue.put(("log", self.buffer.rstrip()))
        self.buffer = ""


class OrderScreenshotApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("1688 订单截图工具")
        self.root.geometry("1040x720")
        self.root.minsize(960, 640)

        self.events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.manual_event: threading.Event | None = None

        self.input_path = StringVar(value=str(APP_DIR / "input" / "orders.txt"))
        self.output_path = StringVar(value=str(APP_DIR / "output"))
        self.browser_path = StringVar(value="")
        self.headless = BooleanVar(value=False)
        self.viewport_width = IntVar(value=1440)
        self.viewport_height = IntVar(value=1800)
        self.login_window_width = IntVar(value=core.DEFAULT_LOGIN_WINDOW_WIDTH)
        self.login_window_height = IntVar(value=core.DEFAULT_LOGIN_WINDOW_HEIGHT)
        self.clip_x = IntVar(value=250)
        self.clip_y = IntVar(value=80)
        self.clip_width = IntVar(value=950)
        self.clip_height = IntVar(value=1500)
        self.retries = IntVar(value=3)
        self.delay_min = DoubleVar(value=1.0)
        self.delay_max = DoubleVar(value=3.0)
        self.login_url = StringVar(value=core.DEFAULT_LOGIN_URL)
        self.order_url_template = StringVar(value=core.DEFAULT_ORDER_URL_TEMPLATE)
        self.status_text = StringVar(value="请选择订单文件和截图保存目录，然后点击开始。")
        self.progress_text = StringVar(value="0 / 0")

        self._build_ui()
        self.root.after(100, self._poll_events)

    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Hint.TLabel", foreground="#555555")
        style.configure("Danger.TButton", foreground="#a00000")
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"))

        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=0, minsize=380)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(1, weight=1)

        title = ttk.Label(outer, text="1688 订单截图工具", style="Title.TLabel")
        title.grid(row=0, column=0, sticky="w")

        subtitle = ttk.Label(
            outer,
            text="批量读取订单号，自动打开订单详情页，并保存主体区域 PNG 截图。",
            style="Hint.TLabel",
        )
        subtitle.grid(row=0, column=1, sticky="e", padx=(16, 0))

        left = ttk.Frame(outer)
        left.grid(row=1, column=0, sticky="nsew", pady=(14, 0), padx=(0, 14))
        left.columnconfigure(0, weight=1)

        right = ttk.Frame(outer)
        right.grid(row=1, column=1, sticky="nsew", pady=(14, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(3, weight=1)

        self._build_paths(left)
        self._build_basic_options(left)
        self._build_clip_options(left)
        self._build_actions(left)
        self._build_progress(right)
        self._build_log(right)

    def _build_paths(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="文件")
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(0, weight=1)

        ttk.Label(box, text="订单文件（txt / xlsx / xls）").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 4))
        input_row = ttk.Frame(box)
        input_row.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))
        input_row.columnconfigure(0, weight=1)
        ttk.Entry(input_row, textvariable=self.input_path).grid(row=0, column=0, sticky="ew")
        ttk.Button(input_row, text="选择", command=self._choose_input).grid(row=0, column=1, padx=(8, 0))

        ttk.Label(box, text="截图保存目录").grid(row=2, column=0, sticky="w", padx=10, pady=(0, 4))
        output_row = ttk.Frame(box)
        output_row.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 10))
        output_row.columnconfigure(0, weight=1)
        ttk.Entry(output_row, textvariable=self.output_path).grid(row=0, column=0, sticky="ew")
        ttk.Button(output_row, text="选择", command=self._choose_output).grid(row=0, column=1, padx=(8, 0))

        ttk.Label(box, text="浏览器路径（可选，留空自动找 Chrome / Edge）").grid(
            row=4, column=0, sticky="w", padx=10, pady=(0, 4)
        )
        browser_row = ttk.Frame(box)
        browser_row.grid(row=5, column=0, sticky="ew", padx=10, pady=(0, 10))
        browser_row.columnconfigure(0, weight=1)
        ttk.Entry(browser_row, textvariable=self.browser_path).grid(row=0, column=0, sticky="ew")
        ttk.Button(browser_row, text="选择", command=self._choose_browser).grid(row=0, column=1, padx=(8, 0))

    def _build_basic_options(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="运行设置")
        box.grid(row=1, column=0, sticky="ew", pady=(12, 0))

        for i in range(4):
            box.columnconfigure(i, weight=1)

        ttk.Checkbutton(box, text="无头模式", variable=self.headless).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 6)
        )
        ttk.Label(box, text="需要手动登录/验证时请不要勾选无头模式。", style="Hint.TLabel").grid(
            row=0, column=2, columnspan=2, sticky="w", padx=10, pady=(10, 6)
        )

        self._number_field(box, "视口宽", self.viewport_width, 1, 0)
        self._number_field(box, "视口高", self.viewport_height, 1, 2)
        self._number_field(box, "重试次数", self.retries, 2, 0)
        self._number_field(box, "等待最短秒", self.delay_min, 2, 2)
        self._number_field(box, "等待最长秒", self.delay_max, 3, 0)
        self._number_field(box, "登录窗宽", self.login_window_width, 3, 2)

        self._number_field(box, "登录窗高", self.login_window_height, 4, 0)

        ttk.Label(box, text="登录入口").grid(row=5, column=0, sticky="w", padx=(10, 4), pady=6)
        ttk.Entry(box, textvariable=self.login_url).grid(
            row=5, column=1, columnspan=3, sticky="ew", padx=(0, 10), pady=6
        )

        ttk.Label(box, text="订单链接模板").grid(row=6, column=0, sticky="w", padx=(10, 4), pady=6)
        ttk.Entry(box, textvariable=self.order_url_template).grid(
            row=6, column=1, columnspan=3, sticky="ew", padx=(0, 10), pady=6
        )

    def _build_clip_options(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="固定截图区域（兜底方案）")
        box.grid(row=2, column=0, sticky="ew", pady=(12, 0))

        for i in range(4):
            box.columnconfigure(i, weight=1)

        self._number_field(box, "X", self.clip_x, 0, 0)
        self._number_field(box, "Y", self.clip_y, 0, 2)
        self._number_field(box, "宽度", self.clip_width, 1, 0)
        self._number_field(box, "高度", self.clip_height, 1, 2)

        ttk.Label(
            box,
            text="通常不用改；只有精准定位和备用定位都失败时才会使用这里的区域。",
            style="Hint.TLabel",
        ).grid(row=2, column=0, columnspan=4, sticky="w", padx=10, pady=(2, 10))

    def _build_actions(self, parent: ttk.Frame) -> None:
        box = ttk.Frame(parent)
        box.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        box.columnconfigure(0, weight=1)
        box.columnconfigure(1, weight=1)

        self.start_button = ttk.Button(box, text="开始截图", style="Accent.TButton", command=self._start)
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self.stop_button = ttk.Button(box, text="停止", style="Danger.TButton", command=self._stop, state="disabled")
        self.stop_button.grid(row=0, column=1, sticky="ew")

        self.manual_button = ttk.Button(
            parent,
            text="我已处理完成，继续",
            command=self._continue_after_manual,
            state="disabled",
        )
        self.manual_button.grid(row=4, column=0, sticky="ew", pady=(10, 0))

    def _build_progress(self, parent: ttk.Frame) -> None:
        top = ttk.LabelFrame(parent, text="状态")
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)

        ttk.Label(top, textvariable=self.status_text, wraplength=560).grid(
            row=0, column=0, sticky="w", padx=10, pady=(10, 4)
        )
        self.progress_bar = ttk.Progressbar(top, mode="determinate", maximum=1)
        self.progress_bar.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 4))
        ttk.Label(top, textvariable=self.progress_text, style="Hint.TLabel").grid(
            row=2, column=0, sticky="e", padx=10, pady=(0, 10)
        )

    def _build_log(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="运行日志").grid(row=2, column=0, sticky="w", pady=(14, 4))
        self.log_text = ScrolledText(parent, height=20, wrap="word", state="disabled")
        self.log_text.grid(row=3, column=0, sticky="nsew")

    def _number_field(self, parent: ttk.Frame, label: str, variable, row: int, column: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", padx=(10, 4), pady=6)
        ttk.Entry(parent, textvariable=variable, width=10).grid(
            row=row, column=column + 1, sticky="ew", padx=(0, 10), pady=6
        )

    def _choose_input(self) -> None:
        path = filedialog.askopenfilename(
            title="选择订单文件",
            filetypes=[
                ("订单文件", "*.txt *.xlsx *.xls"),
                ("文本文件", "*.txt"),
                ("Excel 文件", "*.xlsx *.xls"),
                ("所有文件", "*.*"),
            ],
        )
        if path:
            self.input_path.set(path)

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(title="选择截图保存目录")
        if path:
            self.output_path.set(path)

    def _choose_browser(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 Chrome 或 Edge",
            filetypes=[
                ("浏览器程序", "chrome.exe msedge.exe *.exe"),
                ("可执行文件", "*.exe"),
                ("所有文件", "*.*"),
            ],
        )
        if path:
            self.browser_path.set(path)

    def _collect_config(self) -> dict:
        input_path = self.input_path.get().strip()
        output_path = self.output_path.get().strip()
        browser_path = self.browser_path.get().strip()
        if not input_path:
            raise ValueError("请先选择订单文件。")
        if not output_path:
            raise ValueError("请先选择截图保存目录。")
        if not Path(input_path).exists():
            raise ValueError(f"订单文件不存在: {input_path}")
        if browser_path and not Path(browser_path).exists():
            raise ValueError(f"浏览器路径不存在: {browser_path}")
        order_url_template = self.order_url_template.get().strip() or core.DEFAULT_ORDER_URL_TEMPLATE
        if "{order_id}" not in order_url_template and "{orderId}" not in order_url_template:
            raise ValueError("订单链接模板必须包含 {order_id} 或 {orderId}。")

        viewport_width = max(600, int(self.viewport_width.get()))
        viewport_height = max(600, int(self.viewport_height.get()))
        login_window_width = max(900, int(self.login_window_width.get()))
        login_window_height = max(650, int(self.login_window_height.get()))
        retries = max(1, int(self.retries.get()))
        delay_min = max(0.0, float(self.delay_min.get()))
        delay_max = max(0.0, float(self.delay_max.get()))

        return {
            "input": input_path,
            "output": output_path,
            "browser_path": browser_path,
            "headless": bool(self.headless.get()),
            "viewport_width": viewport_width,
            "viewport_height": viewport_height,
            "login_window_width": login_window_width,
            "login_window_height": login_window_height,
            "retries": retries,
            "delay_min": min(delay_min, delay_max),
            "delay_max": max(delay_min, delay_max),
            "login_url": self.login_url.get().strip() or core.DEFAULT_LOGIN_URL,
            "order_url_template": order_url_template,
            "clip": {
                "x": max(0, int(self.clip_x.get())),
                "y": max(0, int(self.clip_y.get())),
                "width": max(100, int(self.clip_width.get())),
                "height": max(100, int(self.clip_height.get())),
            },
        }

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        try:
            config = self._collect_config()
        except Exception as exc:
            messagebox.showwarning("请检查设置", str(exc))
            return

        self.stop_event.clear()
        self._clear_log()
        self.status_text.set("正在启动浏览器...")
        self.progress_text.set("0 / 0")
        self.progress_bar.configure(value=0, maximum=1)
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.manual_button.configure(state="disabled")

        self.worker = threading.Thread(target=self._run_worker, args=(config,), daemon=True)
        self.worker.start()

    def _stop(self) -> None:
        self.stop_event.set()
        self.status_text.set("已请求停止，当前订单处理结束后会停下来。")
        self._log("已请求停止。")
        if self.manual_event:
            self.manual_event.set()

    def _continue_after_manual(self) -> None:
        if self.manual_event:
            self.manual_event.set()
        self.manual_button.configure(state="disabled")
        self.status_text.set("已继续，正在保存登录状态并处理订单...")

    def _manual_waiter(self, message: str) -> None:
        event = threading.Event()
        self.events.put(("manual", message, event))
        while not event.wait(0.2):
            if self.stop_event.is_set():
                raise RuntimeError("任务已停止")
        if self.stop_event.is_set():
            raise RuntimeError("任务已停止")

    def _sleep_with_stop(self, seconds: float) -> None:
        end_time = time.time() + seconds
        while time.time() < end_time:
            if self.stop_event.is_set():
                return
            time.sleep(min(0.2, end_time - time.time()))

    def _run_worker(self, config: dict) -> None:
        writer = QueueWriter(self.events)
        with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
            success_count = 0
            failed_count = 0
            bundle = None
            stopped = False

            try:
                core.RUN_HEADLESS = bool(config["headless"])
                core.FIXED_CLIP_CONFIG = dict(config["clip"])
                core.ORDER_URL_TEMPLATE = config["order_url_template"]
                core.MANUAL_LOGIN_VIEWPORT = {
                    "width": int(config["login_window_width"]),
                    "height": int(config["login_window_height"]),
                }
                core.ensure_project_dirs()

                output_dir = Path(config["output"]).expanduser()
                output_dir.mkdir(parents=True, exist_ok=True)

                orders = core.read_orders(config["input"])
                self.events.put(("progress", 0, len(orders)))
                self.events.put(("status", f"读取到 {len(orders)} 个订单，正在打开浏览器。"))

                with core.sync_playwright() as playwright:
                    bundle = core.create_browser_context(
                        playwright,
                        headless=bool(config["headless"]),
                        viewport_width=int(config["viewport_width"]),
                        viewport_height=int(config["viewport_height"]),
                        manual_waiter=self._manual_waiter,
                        login_url=config["login_url"],
                        login_window_width=int(config["login_window_width"]),
                        login_window_height=int(config["login_window_height"]),
                        browser_path=config["browser_path"] or None,
                    )
                    page = bundle.context.new_page()

                    for index, order_id in enumerate(orders, start=1):
                        if self.stop_event.is_set():
                            stopped = True
                            break

                        self.events.put(("status", f"正在处理订单 {index}/{len(orders)}：{order_id}"))
                        print(f"进度 {index}/{len(orders)}")

                        ok = core.process_order(
                            page,
                            bundle.context,
                            order_id,
                            output_dir,
                            max(1, int(config["retries"])),
                            manual_waiter=self._manual_waiter,
                            should_stop=self.stop_event.is_set,
                        )

                        if ok:
                            success_count += 1
                        else:
                            failed_count += 1

                        self.events.put(("progress", index, len(orders)))

                        if index < len(orders):
                            delay = random.uniform(float(config["delay_min"]), float(config["delay_max"]))
                            print(f"等待 {delay:.1f} 秒后继续下一个订单。")
                            self._sleep_with_stop(delay)

            except Exception as exc:
                if self.stop_event.is_set():
                    stopped = True
                else:
                    print(traceback.format_exc())
                    self.events.put(("error", str(exc)))
            finally:
                if bundle is not None:
                    try:
                        core.save_login_state(bundle.context)
                    except Exception:
                        pass
                    try:
                        bundle.browser.close()
                    except Exception:
                        pass
                writer.flush()
                self.events.put(("done", success_count, failed_count, stopped))

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]

                if kind == "log":
                    self._log(event[1])
                elif kind == "status":
                    self.status_text.set(event[1])
                elif kind == "progress":
                    current, total = event[1], event[2]
                    self.progress_bar.configure(maximum=max(1, total), value=current)
                    self.progress_text.set(f"{current} / {total}")
                elif kind == "manual":
                    self.manual_event = event[2]
                    self.status_text.set(event[1])
                    self.manual_button.configure(state="normal")
                    messagebox.showinfo("需要手动处理", event[1])
                elif kind == "error":
                    self.status_text.set(f"出错：{event[1]}")
                    messagebox.showerror("运行出错", event[1])
                elif kind == "done":
                    success_count, failed_count, stopped = event[1], event[2], event[3]
                    self._finish_run(success_count, failed_count, stopped)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._poll_events)

    def _finish_run(self, success_count: int, failed_count: int, stopped: bool) -> None:
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.manual_button.configure(state="disabled")
        self.manual_event = None

        if stopped:
            self.status_text.set(f"已停止。成功 {success_count} 个，失败 {failed_count} 个。")
        else:
            self.status_text.set(f"任务完成。成功 {success_count} 个，失败 {failed_count} 个。")

        self._log(self.status_text.get())

    def _log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")


def main() -> None:
    root = Tk()
    app = OrderScreenshotApp(root)
    root.protocol("WM_DELETE_WINDOW", lambda: on_close(root, app))
    root.mainloop()


def on_close(root: Tk, app: OrderScreenshotApp) -> None:
    if app.worker and app.worker.is_alive():
        if not messagebox.askyesno("确认退出", "任务仍在运行，确定要停止并退出吗？"):
            return
        app.stop_event.set()
        if app.manual_event:
            app.manual_event.set()
    root.destroy()


if __name__ == "__main__":
    main()
