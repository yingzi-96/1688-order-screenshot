from __future__ import annotations

import cgi
import contextlib
import ctypes
import json
import os
import queue
import random
import socket
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import main as core
import order_batch as batch


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


class AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.manual_event: threading.Event | None = None
        self.confirmation_event: threading.Event | None = None
        self.worker: threading.Thread | None = None
        self.logs: list[str] = []
        self.status = "请选择订单文件和截图保存目录，然后点击开始。"
        self.current = 0
        self.total = 0
        self.running = False
        self.manual_needed = False
        self.manual_message = ""
        self.success_count = 0
        self.failed_count = 0
        self.last_error = ""
        self.output_dir = str(APP_DIR / "output")
        self.awaiting_confirmation = False
        self.preview_stats: dict = {}
        self.result_files: list[str] = []

    def add_log(self, text: str) -> None:
        line = f"{datetime.now().strftime('%H:%M:%S')}  {text}"
        with self.lock:
            self.logs.append(line)
            self.logs = self.logs[-1000:]

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "status": self.status,
                "current": self.current,
                "total": self.total,
                "running": self.running,
                "manualNeeded": self.manual_needed,
                "manualMessage": self.manual_message,
                "successCount": self.success_count,
                "failedCount": self.failed_count,
                "lastError": self.last_error,
                "outputDir": self.output_dir,
                "awaitingConfirmation": self.awaiting_confirmation,
                "previewStats": dict(self.preview_stats),
                "resultFiles": list(self.result_files),
                "logs": list(self.logs),
            }


STATE = AppState()


def find_free_port(preferred: int = 8765) -> int:
    for port in range(preferred, preferred + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("无法找到可用本地端口")


def save_upload(field: cgi.FieldStorage) -> str:
    filename = Path(field.filename or "orders.txt").name
    safe_name = "".join(ch for ch in filename if ch not in r'\/:*?"<>|') or "orders.txt"
    target = APP_DIR / "input" / f"uploaded_{int(time.time())}_{safe_name}"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as file:
        while True:
            chunk = field.file.read(1024 * 1024)
            if not chunk:
                break
            file.write(chunk)
    return str(target)


def choose_output_dir(initial_dir: str = "") -> str:
    if os.name != "nt":
        raise RuntimeError("目录选择按钮目前只支持 Windows。")

    start_dir = str(Path(initial_dir.strip() or APP_DIR / "output").expanduser())
    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    user32 = ctypes.windll.user32

    BIF_RETURNONLYFSDIRS = 0x0001
    BIF_NEWDIALOGSTYLE = 0x0040
    BFFM_INITIALIZED = 1
    BFFM_SETSELECTIONW = 0x0467
    MAX_PATH = 260

    class BROWSEINFO(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", wintypes.LPWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", ctypes.c_void_p),
            ("lParam", wintypes.LPARAM),
            ("iImage", ctypes.c_int),
        ]

    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFO)]
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = wintypes.LPARAM

    callback_ref = None
    start_path_buffer = None
    if start_dir:
        start_path_buffer = ctypes.create_unicode_buffer(start_dir)
        CALLBACK = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, wintypes.UINT, wintypes.LPARAM, wintypes.LPARAM)

        def callback(hwnd, msg, _lparam, _data):
            if msg == BFFM_INITIALIZED:
                user32.SendMessageW(hwnd, BFFM_SETSELECTIONW, 1, ctypes.addressof(start_path_buffer))
            return 0

        callback_ref = CALLBACK(callback)

    display_name = ctypes.create_unicode_buffer(MAX_PATH)
    browse_info = BROWSEINFO(
        hwndOwner=None,
        pidlRoot=None,
        pszDisplayName=ctypes.cast(display_name, wintypes.LPWSTR),
        lpszTitle="选择截图保存目录",
        ulFlags=BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE,
        lpfn=ctypes.cast(callback_ref, ctypes.c_void_p).value if callback_ref else None,
        lParam=0,
        iImage=0,
    )

    pidl = shell32.SHBrowseForFolderW(ctypes.byref(browse_info))
    if not pidl:
        return ""

    path_buffer = ctypes.create_unicode_buffer(MAX_PATH)
    ok = shell32.SHGetPathFromIDListW(pidl, ctypes.cast(path_buffer, wintypes.LPWSTR))
    ole32.CoTaskMemFree(pidl)
    if not ok:
        raise RuntimeError("无法读取所选目录路径。")
    return path_buffer.value


def normalize_form(form: cgi.FieldStorage) -> dict:
    upload_path = ""
    if "order_file" in form:
        field = form["order_file"]
        if isinstance(field, list):
            field = field[0]
        if getattr(field, "filename", None):
            upload_path = save_upload(field)

    input_path = upload_path or str(form.getfirst("input_path", "")).strip()
    output_path = str(form.getfirst("output_path", "")).strip() or str(APP_DIR / "output")
    browser_path = str(form.getfirst("browser_path", "")).strip()
    source_mode = str(form.getfirst("source_mode", "file")).strip()
    order_url_template = str(form.getfirst("order_url_template", "")).strip() or core.DEFAULT_ORDER_URL_TEMPLATE
    time_preset = str(form.getfirst("time_preset", "today")).strip()
    custom_start = str(form.getfirst("custom_start", "")).strip()
    custom_end = str(form.getfirst("custom_end", "")).strip()
    classify_by = str(form.getfirst("classify_by", "none")).strip()

    if source_mode not in {"file", "batch"}:
        raise ValueError(f"不支持的任务来源: {source_mode}")
    if source_mode == "file" and not input_path:
        raise ValueError("请选择订单文件，或填写订单文件路径。")
    if source_mode == "file" and not Path(input_path).exists():
        raise ValueError(f"订单文件不存在: {input_path}")
    if browser_path and not Path(browser_path).exists():
        raise ValueError(f"浏览器路径不存在: {browser_path}")
    if "{order_id}" not in order_url_template and "{orderId}" not in order_url_template:
        raise ValueError("订单链接模板必须包含 {order_id} 或 {orderId}。")
    if classify_by not in {"none", "receiver", "phone"}:
        raise ValueError(f"不支持的分类方式: {classify_by}")
    if source_mode == "batch":
        batch.resolve_time_range(time_preset, custom_start, custom_end)

    return {
        "input": input_path,
        "source_mode": source_mode,
        "output": output_path,
        "browser_path": browser_path,
        "headless": form.getfirst("headless", "") == "on",
        "export_payment_table": form.getfirst("export_payment_table", "") == "on",
        "viewport_width": max(600, int(form.getfirst("viewport_width", "1440"))),
        "viewport_height": max(600, int(form.getfirst("viewport_height", "1800"))),
        "login_window_width": max(900, int(form.getfirst("login_window_width", "1280"))),
        "login_window_height": max(650, int(form.getfirst("login_window_height", "900"))),
        "auth_max_age_hours": max(0.0, float(form.getfirst("auth_max_age_hours", "24"))),
        "retries": max(1, int(form.getfirst("retries", "3"))),
        "delay_min": max(0.0, float(form.getfirst("delay_min", "1"))),
        "delay_max": max(0.0, float(form.getfirst("delay_max", "3"))),
        "login_url": str(form.getfirst("login_url", "")).strip() or core.DEFAULT_LOGIN_URL,
        "order_url_template": order_url_template,
        "time_preset": time_preset,
        "custom_start": custom_start,
        "custom_end": custom_end,
        "classify_by": classify_by,
        "order_list_url": str(form.getfirst("order_list_url", "")).strip() or batch.DEFAULT_ORDER_LIST_URL,
        "clip": {
            "x": max(0, int(form.getfirst("clip_x", "250"))),
            "y": max(0, int(form.getfirst("clip_y", "80"))),
            "width": max(100, int(form.getfirst("clip_width", "950"))),
            "height": max(100, int(form.getfirst("clip_height", "1500"))),
        },
    }


def manual_waiter(message: str) -> None:
    event = threading.Event()
    STATE.manual_event = event
    with STATE.lock:
        STATE.manual_needed = True
        STATE.manual_message = message
        STATE.status = message
    while not event.wait(0.2):
        if STATE.stop_event.is_set():
            raise RuntimeError("任务已停止")
    if STATE.stop_event.is_set():
        raise RuntimeError("任务已停止")
    with STATE.lock:
        STATE.manual_needed = False
        STATE.manual_message = ""


def sleep_with_stop(seconds: float) -> None:
    end_time = time.time() + seconds
    while time.time() < end_time:
        if STATE.stop_event.is_set():
            return
        time.sleep(min(0.2, end_time - time.time()))


def wait_batch_confirmation(stats: dict, start_date: str, end_date: str) -> None:
    event = threading.Event()
    STATE.confirmation_event = event
    with STATE.lock:
        STATE.awaiting_confirmation = True
        STATE.preview_stats = {**stats, "startDate": start_date, "endDate": end_date}
        STATE.status = "订单统计已完成，请确认后开始截图。"
    while not event.wait(0.2):
        if STATE.stop_event.is_set():
            raise RuntimeError("任务已取消")
    if STATE.stop_event.is_set():
        raise RuntimeError("任务已取消")
    with STATE.lock:
        STATE.awaiting_confirmation = False
        STATE.confirmation_event = None


def export_folder_payment_tables(rows: list[dict], output_dir: Path) -> list[Path]:
    if not rows:
        print("没有已处理订单，不生成实付款表格。")
        return []
    try:
        paths = batch.save_payment_tables(rows, output_dir)
    except Exception as exc:
        print(f"实付款表格保存失败，但截图结果仍保留: {exc}")
        return []
    STATE.result_files.extend(str(path) for path in paths)
    for path in paths:
        print(f"实付款汇总表已导出: {path}")
    return paths


def run_file_mode(config: dict, page: object, context: object, output_dir: Path) -> tuple[int, int]:
    success_count = 0
    failed_count = 0
    rows: list[dict] = []
    orders = core.read_orders(config["input"])
    STATE.events.put(("progress", 0, len(orders)))
    STATE.events.put(("status", f"读取到 {len(orders)} 个订单，正在开始截图。"))

    for index, order_id in enumerate(orders, start=1):
        if STATE.stop_event.is_set():
            break
        STATE.events.put(("status", f"正在处理订单 {index}/{len(orders)}：{order_id}"))
        try:
            result = batch.capture_order(
                page,
                context,
                batch.OrderSummary(order_id=order_id),
                output_dir,
                config["classify_by"],
                max(1, int(config["retries"])),
                core.open_order_page,
                core.screenshot_order,
                core.get_page_text,
                manual_waiter=manual_waiter,
                should_stop=STATE.stop_event.is_set,
                extract_payment=core.extract_paid_amount if config.get("export_payment_table") else None,
            )
        except RuntimeError:
            if STATE.stop_event.is_set():
                print("收到停止请求，保留已完成结果并结束任务。")
                break
            raise

        rows.append(result)
        ok = result.get("截图结果") == "成功"
        success_count += int(ok)
        failed_count += int(not ok)
        if ok:
            core.write_success_log(order_id)
        else:
            core.write_failed_log(order_id, result.get("失败原因", "未知错误"))
        STATE.events.put(("progress", index, len(orders)))
        if index < len(orders):
            delay = random.uniform(float(config["delay_min"]), float(config["delay_max"]))
            print(f"等待 {delay:.1f} 秒后继续下一个订单。")
            sleep_with_stop(delay)

    if config.get("export_payment_table"):
        export_folder_payment_tables(rows, output_dir)
    return success_count, failed_count

def run_batch_mode(config: dict, page: object, context: object, output_dir: Path) -> tuple[int, int]:
    start, end = batch.resolve_time_range(config["time_preset"], config["custom_start"], config["custom_end"])
    STATE.events.put(("status", f"正在拉取 {start.isoformat()} 至 {end.isoformat()} 的订单列表。"))

    def manual_list_login() -> None:
        core.wait_manual_login_or_verify(page, context, manual_waiter=manual_waiter)

    all_orders = batch.fetch_orders(
        page,
        config["order_list_url"],
        start,
        end,
        check_login=core.check_login_or_verify_page,
        manual_login=manual_list_login,
    )
    paid_orders, stats = batch.filter_paid_orders(all_orders, start, end)
    print(
        f"抓单统计：扫描 {stats.scanned_total}，时间范围内 {stats.total}，已支付 {stats.paid}，"
        f"忽略未支付 {stats.ignored_unpaid}，忽略取消/关闭 {stats.ignored_cancelled}，"
        f"其它状态 {stats.ignored_other}，超出范围 {stats.ignored_out_of_range}，"
        f"日期不明 {stats.ignored_date_unknown}。"
    )
    wait_batch_confirmation(stats.to_dict(), start.isoformat(), end.isoformat())

    success_count = 0
    failed_count = 0
    rows: list[dict] = []
    STATE.events.put(("progress", 0, len(paid_orders)))
    for index, order in enumerate(paid_orders, start=1):
        if STATE.stop_event.is_set():
            break
        STATE.events.put(("status", f"正在截图已支付订单 {index}/{len(paid_orders)}：{order.order_id}"))
        try:
            result = batch.capture_order(
                page,
                context,
                order,
                output_dir,
                config["classify_by"],
                max(1, int(config["retries"])),
                core.open_order_page,
                core.screenshot_order,
                core.get_page_text,
                manual_waiter=manual_waiter,
                should_stop=STATE.stop_event.is_set,
                extract_payment=core.extract_paid_amount if config.get("export_payment_table") else None,
            )
        except RuntimeError:
            if STATE.stop_event.is_set():
                print("收到停止请求，保留已完成结果并结束任务。")
                break
            raise
        rows.append(result)
        ok = result.get("截图结果") == "成功"
        success_count += int(ok)
        failed_count += int(not ok)
        if ok:
            core.write_success_log(order.order_id)
        else:
            core.write_failed_log(order.order_id, result.get("失败原因", "未知错误"))
        STATE.events.put(("progress", index, len(paid_orders)))
        if index < len(paid_orders):
            delay = random.uniform(float(config["delay_min"]), float(config["delay_max"]))
            print(f"等待 {delay:.1f} 秒后继续下一个订单。")
            sleep_with_stop(delay)

    if config.get("export_payment_table"):
        export_folder_payment_tables(rows, output_dir)

    try:
        excel_path, json_path = batch.save_batch_results(
            rows,
            output_dir,
            stats.to_dict(),
            {"开始日期": start.isoformat(), "结束日期": end.isoformat()},
        )
        STATE.result_files.extend([str(excel_path), str(json_path)])
        print(f"批量结果已保存: {excel_path}")
    except Exception as exc:
        print(f"批量结果表保存失败，但已完成的截图仍保留在输出目录中: {exc}")
    return success_count, failed_count


def run_worker(config: dict) -> None:
    writer = QueueWriter(STATE.events)
    with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
        success_count = 0
        failed_count = 0
        bundle = None
        stopped = False
        output_dir = Path(config["output"]).expanduser()

        try:
            core.RUN_HEADLESS = bool(config["headless"])
            core.FIXED_CLIP_CONFIG = dict(config["clip"])
            core.ORDER_URL_TEMPLATE = config["order_url_template"]
            core.MANUAL_LOGIN_VIEWPORT = {
                "width": int(config["login_window_width"]),
                "height": int(config["login_window_height"]),
            }
            core.ensure_project_dirs()

            output_dir.mkdir(parents=True, exist_ok=True)
            STATE.output_dir = str(output_dir)

            with core.sync_playwright() as playwright:
                bundle = core.create_browser_context(
                    playwright,
                    headless=bool(config["headless"]),
                    viewport_width=int(config["viewport_width"]),
                    viewport_height=int(config["viewport_height"]),
                    manual_waiter=manual_waiter,
                    login_url=config["login_url"],
                    login_window_width=int(config["login_window_width"]),
                    login_window_height=int(config["login_window_height"]),
                    browser_path=config["browser_path"] or None,
                    auth_max_age_hours=float(config["auth_max_age_hours"]),
                )
                page = bundle.context.new_page()
                if config["source_mode"] == "batch":
                    success_count, failed_count = run_batch_mode(config, page, bundle.context, output_dir)
                else:
                    success_count, failed_count = run_file_mode(config, page, bundle.context, output_dir)
                stopped = STATE.stop_event.is_set()

        except Exception as exc:
            if STATE.stop_event.is_set():
                stopped = True
            else:
                print(traceback.format_exc())
                STATE.events.put(("error", str(exc)))
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
            STATE.events.put(("done", success_count, failed_count, stopped))


def pump_events() -> None:
    while True:
        try:
            event = STATE.events.get_nowait()
        except queue.Empty:
            break

        kind = event[0]
        if kind == "log":
            STATE.add_log(event[1])
        elif kind == "status":
            with STATE.lock:
                STATE.status = event[1]
        elif kind == "progress":
            with STATE.lock:
                STATE.current = event[1]
                STATE.total = event[2]
        elif kind == "error":
            with STATE.lock:
                STATE.last_error = event[1]
                STATE.status = f"出错：{event[1]}"
        elif kind == "done":
            success_count, failed_count, stopped = event[1], event[2], event[3]
            with STATE.lock:
                STATE.running = False
                STATE.manual_needed = False
                STATE.awaiting_confirmation = False
                STATE.success_count = success_count
                STATE.failed_count = failed_count
                if stopped:
                    STATE.status = f"已停止。成功 {success_count} 个，失败 {failed_count} 个。"
                elif not STATE.last_error:
                    STATE.status = (
                        f"任务完成。成功 {success_count} 个，失败 {failed_count} 个。"
                        f"保存目录：{STATE.output_dir}"
                    )
                STATE.manual_event = None
                STATE.confirmation_event = None
            STATE.add_log(STATE.status)


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>1688 订单截图工具</title>
  <style>
    :root { font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif; color: #1f2328; background: #f6f8fa; }
    body { margin: 0; }
    .wrap { max-width: 1180px; margin: 0 auto; padding: 24px; }
    h1 { margin: 0 0 6px; font-size: 24px; }
    .sub { color: #59636e; margin-bottom: 18px; }
    .grid { display: grid; grid-template-columns: 390px 1fr; gap: 18px; align-items: start; }
    .panel { background: #fff; border: 1px solid #d8dee4; border-radius: 8px; padding: 16px; }
    .panel h2 { font-size: 16px; margin: 0 0 12px; }
    label { display: block; font-size: 13px; color: #59636e; margin: 10px 0 4px; }
    input[type="text"], input[type="number"], input[type="file"], input[type="date"], select {
      width: 100%; box-sizing: border-box; padding: 8px 10px; border: 1px solid #d0d7de; border-radius: 6px; background: #fff;
    }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .pathrow { display: grid; grid-template-columns: 1fr auto; gap: 8px; }
    .check { display: flex; gap: 8px; align-items: center; margin-top: 12px; color: #59636e; font-size: 13px; }
    button { border: 1px solid #d0d7de; background: #fff; padding: 9px 14px; border-radius: 6px; cursor: pointer; }
    button.primary { background: #0969da; color: #fff; border-color: #0969da; font-weight: 600; }
    button.danger { color: #cf222e; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 14px; }
    .manual { display: none; margin-top: 12px; padding: 12px; border: 1px solid #f0b429; background: #fff8c5; border-radius: 8px; }
    .manual.show { display: block; }
    .confirm { display: none; margin-top: 12px; padding: 14px; border: 1px solid #0969da; background: #ddf4ff; border-radius: 8px; }
    .confirm.show { display: block; }
    .stats { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin-top: 10px; }
    .stat { background: #fff; border: 1px solid #d8dee4; border-radius: 6px; padding: 8px; }
    .hidden { display: none; }
    .status { min-height: 42px; color: #24292f; }
    progress { width: 100%; height: 14px; }
    .counts { color: #59636e; font-size: 13px; margin-top: 8px; }
    pre { height: 430px; overflow: auto; background: #0d1117; color: #d1d7e0; padding: 12px; border-radius: 8px; white-space: pre-wrap; word-break: break-word; }
    .hint { font-size: 12px; color: #6e7781; margin-top: 6px; line-height: 1.5; }
    @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>1688 订单截图工具</h1>
    <div class="sub">选择订单文件，自动打开订单详情页并保存 PNG 截图。遇到登录或验证时，在浏览器里手动处理后点击继续。</div>
    <div class="grid">
      <form id="runForm" class="panel">
        <h2>设置</h2>
        <label>任务来源</label>
        <select id="sourceMode" name="source_mode">
          <option value="file">导入订单号文件（原功能）</option>
          <option value="batch">按时间抓取已支付订单</option>
        </select>
        <div id="fileModeFields">
          <label>订单文件（推荐直接选择 txt / xlsx / xls）</label>
          <input name="order_file" type="file" accept=".txt,.xlsx,.xls">
          <div class="hint">也可以不选文件，改为在下面填写本机订单文件路径。</div>
          <label>订单文件路径（可选）</label>
          <input name="input_path" type="text" value="">
        </div>
        <div id="batchModeFields" class="hidden">
          <label>时间范围</label>
          <select id="timePreset" name="time_preset">
            <option value="today">今天</option>
            <option value="yesterday">昨天</option>
            <option value="last3">最近三天</option>
            <option value="last7">最近七天</option>
            <option value="month">本月</option>
            <option value="custom">自定义</option>
          </select>
          <div id="customDates" class="row hidden">
            <div><label>开始日期</label><input name="custom_start" type="date"></div>
            <div><label>结束日期</label><input name="custom_end" type="date"></div>
          </div>

          <label>订单列表地址</label>
          <input name="order_list_url" type="text" value="https://work.1688.com/home/buyer.htm?_path_=buyer2017Base/2017buyerbase_trade/buyList">
          <div class="hint">批量模式只处理已支付、已付款、待发货、待收货、交易成功等已付款状态，其它状态自动忽略。</div>
        </div>
        <label>分类保存方式</label>
        <select name="classify_by">
          <option value="none" selected>全部导入到一个文件夹（默认）</option>
          <option value="receiver">按收货人分文件夹</option>
          <option value="phone">按手机号分文件夹</option>
        </select>
        <div class="hint">订单号文件模式和按日期模式都会使用此分类设置。</div>
        <label>截图保存目录</label>
        <div class="pathrow">
          <input id="outputPath" name="output_path" type="text" value="">
          <button id="chooseOutputBtn" type="button">选择</button>
        </div>
        <label>浏览器路径（可选，留空自动找 Chrome / Edge）</label>
        <input name="browser_path" type="text" value="">
        <div class="check"><input name="headless" type="checkbox"> 无头模式（需要人工登录/验证时不要勾选）</div>
        <div class="check"><input name="export_payment_table" type="checkbox"> 读取每笔订单实付款，并在每个分类文件夹导出 Excel 汇总表</div>
        <div class="row">
          <div><label>截图视口宽</label><input name="viewport_width" type="number" value="1440"></div>
          <div><label>截图视口高</label><input name="viewport_height" type="number" value="1800"></div>
        </div>
        <div class="row">
          <div><label>登录窗宽</label><input name="login_window_width" type="number" value="1280"></div>
          <div><label>登录窗高</label><input name="login_window_height" type="number" value="900"></div>
        </div>
        <label>登录状态有效期（小时，0 表示不按时间失效）</label>
        <input name="auth_max_age_hours" type="number" min="0" step="0.5" value="24">
        <div class="row">
          <div><label>重试次数</label><input name="retries" type="number" value="3"></div>
          <div><label>等待秒数</label><input name="delay_min" type="number" step="0.1" value="1"><input name="delay_max" type="number" step="0.1" value="3" style="margin-top:6px"></div>
        </div>
        <div class="row">
          <div><label>固定 X</label><input name="clip_x" type="number" value="250"></div>
          <div><label>固定 Y</label><input name="clip_y" type="number" value="80"></div>
        </div>
        <div class="row">
          <div><label>固定宽</label><input name="clip_width" type="number" value="950"></div>
          <div><label>固定高</label><input name="clip_height" type="number" value="1500"></div>
        </div>
        <label>登录入口</label>
        <input name="login_url" type="text" value="https://login.1688.com/member/signin.htm">
        <label>订单链接模板</label>
        <input name="order_url_template" type="text" value="https://air.1688.com/app/ctf-page/trade-order-detail/index.html?orderId={order_id}">
        <div class="actions">
          <button id="startBtn" class="primary" type="submit">开始截图</button>
          <button id="stopBtn" class="danger" type="button">停止</button>
        </div>
      </form>
      <div>
        <div class="panel">
          <h2>状态</h2>
          <div id="status" class="status"></div>
          <progress id="progress" value="0" max="1"></progress>
          <div id="counts" class="counts">0 / 0</div>
          <div id="resultFiles" class="hint"></div>
          <div id="manualBox" class="manual">
            <div id="manualMessage"></div>
            <button id="continueBtn" class="primary" type="button" style="margin-top:10px">我已处理完成，继续</button>
          </div>
          <div id="confirmBox" class="confirm">
            <strong>抓单统计，请确认</strong>
            <div id="dateRange" class="hint"></div>
            <div class="stats">
              <div class="stat">扫描候选：<strong id="statScanned">0</strong></div>
              <div class="stat">范围内订单：<strong id="statTotal">0</strong></div>
              <div class="stat">已支付：<strong id="statPaid">0</strong></div>
              <div class="stat">忽略未支付：<strong id="statUnpaid">0</strong></div>
              <div class="stat">忽略取消/关闭：<strong id="statCancelled">0</strong></div>
              <div class="stat">其它状态：<strong id="statOther">0</strong></div>
              <div class="stat">超出范围：<strong id="statOutOfRange">0</strong></div>
              <div class="stat">日期不明：<strong id="statDateUnknown">0</strong></div>
            </div>
            <div class="actions">
              <button id="confirmBatchBtn" class="primary" type="button">确认并开始截图</button>
              <button id="cancelBatchBtn" class="danger" type="button">取消</button>
            </div>
          </div>
          <div class="actions">
            <button id="openOutputBtn" type="button">打开输出目录</button>
            <button id="exitBtn" type="button">退出程序</button>
          </div>
        </div>
        <div class="panel" style="margin-top:18px">
          <h2>运行日志</h2>
          <pre id="logs"></pre>
        </div>
      </div>
    </div>
  </div>
<script>
const form = document.getElementById('runForm');
const startBtn = document.getElementById('startBtn');
const stopBtn = document.getElementById('stopBtn');
const continueBtn = document.getElementById('continueBtn');
const openOutputBtn = document.getElementById('openOutputBtn');
const exitBtn = document.getElementById('exitBtn');
const chooseOutputBtn = document.getElementById('chooseOutputBtn');
const sourceMode = document.getElementById('sourceMode');
const fileModeFields = document.getElementById('fileModeFields');
const batchModeFields = document.getElementById('batchModeFields');

const timePreset = document.getElementById('timePreset');
const customDates = document.getElementById('customDates');
const confirmBatchBtn = document.getElementById('confirmBatchBtn');
const cancelBatchBtn = document.getElementById('cancelBatchBtn');

form.output_path.value = '';

async function post(url, body) {
  const res = await fetch(url, { method: 'POST', body });
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || '请求失败');
  return data;
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    startBtn.disabled = true;
    await post('/api/start', new FormData(form));
  } catch (err) {
    alert(err.message);
    startBtn.disabled = false;
  }
});
stopBtn.onclick = () => post('/api/stop').catch(err => alert(err.message));
continueBtn.onclick = () => post('/api/continue').catch(err => alert(err.message));
openOutputBtn.onclick = () => post('/api/open-output').catch(err => alert(err.message));
chooseOutputBtn.onclick = async () => {
  try {
    const body = new URLSearchParams();
    body.set('current', document.getElementById('outputPath').value || '');
    const data = await post('/api/choose-output', body);
    if (data.path) document.getElementById('outputPath').value = data.path;
  } catch (err) {
    alert(err.message);
  }
};
exitBtn.onclick = async () => { await post('/api/exit').catch(() => {}); document.body.innerHTML = '<div class="wrap"><h1>程序已退出</h1></div>'; };
sourceMode.onchange = () => {
  const batch = sourceMode.value === 'batch';
  fileModeFields.className = batch ? 'hidden' : '';
  batchModeFields.className = batch ? '' : 'hidden';

};
timePreset.onchange = () => {
  customDates.className = timePreset.value === 'custom' ? 'row' : 'row hidden';
};
confirmBatchBtn.onclick = () => post('/api/confirm-batch').catch(err => alert(err.message));
cancelBatchBtn.onclick = () => post('/api/cancel-batch').catch(err => alert(err.message));

async function refresh() {
  try {
    const data = await (await fetch('/api/status')).json();
    document.getElementById('status').textContent = data.status;
    document.getElementById('progress').max = Math.max(1, data.total || 1);
    document.getElementById('progress').value = data.current || 0;
    document.getElementById('counts').textContent = `${data.current || 0} / ${data.total || 0}，成功 ${data.successCount || 0}，失败 ${data.failedCount || 0}`;
    const resultFiles = data.resultFiles || [];
    document.getElementById('resultFiles').textContent = resultFiles.length ? `结果文件：${resultFiles.join('；')}` : '';
    const manualBox = document.getElementById('manualBox');
    manualBox.className = data.manualNeeded ? 'manual show' : 'manual';
    document.getElementById('manualMessage').textContent = data.manualMessage || '';
    const confirmBox = document.getElementById('confirmBox');
    confirmBox.className = data.awaitingConfirmation ? 'confirm show' : 'confirm';
    const stats = data.previewStats || {};
    document.getElementById('dateRange').textContent = stats.startDate ? `时间范围：${stats.startDate} 至 ${stats.endDate}` : '';
    document.getElementById('statScanned').textContent = stats.scanned_total || 0;
    document.getElementById('statTotal').textContent = stats.total || 0;
    document.getElementById('statPaid').textContent = stats.paid || 0;
    document.getElementById('statUnpaid').textContent = stats.ignored_unpaid || 0;
    document.getElementById('statCancelled').textContent = stats.ignored_cancelled || 0;
    document.getElementById('statOther').textContent = stats.ignored_other || 0;
    document.getElementById('statOutOfRange').textContent = stats.ignored_out_of_range || 0;
    document.getElementById('statDateUnknown').textContent = stats.ignored_date_unknown || 0;
    document.getElementById('logs').textContent = (data.logs || []).join('\n');
    document.getElementById('logs').scrollTop = document.getElementById('logs').scrollHeight;
    startBtn.disabled = !!data.running;
  } catch (err) {}
}
setInterval(refresh, 1000);
refresh();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        pump_events()
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/status":
            self.send_json(STATE.snapshot())
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        pump_events()
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/start":
                self.handle_start()
            elif parsed.path == "/api/stop":
                STATE.stop_event.set()
                if STATE.manual_event:
                    STATE.manual_event.set()
                if STATE.confirmation_event:
                    STATE.confirmation_event.set()
                with STATE.lock:
                    STATE.status = "已请求停止，当前订单处理结束后会停下来。"
                STATE.add_log("已请求停止。")
                self.send_json({"ok": True})
            elif parsed.path == "/api/continue":
                if STATE.manual_event:
                    STATE.manual_event.set()
                with STATE.lock:
                    STATE.manual_needed = False
                    STATE.manual_message = ""
                    STATE.status = "已继续，正在保存登录状态并处理订单..."
                self.send_json({"ok": True})
            elif parsed.path == "/api/confirm-batch":
                if not STATE.awaiting_confirmation or not STATE.confirmation_event:
                    raise ValueError("当前没有等待确认的批量任务。")
                STATE.confirmation_event.set()
                with STATE.lock:
                    STATE.awaiting_confirmation = False
                    STATE.status = "已确认，开始截图已支付订单。"
                self.send_json({"ok": True})
            elif parsed.path == "/api/cancel-batch":
                STATE.stop_event.set()
                if STATE.confirmation_event:
                    STATE.confirmation_event.set()
                with STATE.lock:
                    STATE.awaiting_confirmation = False
                    STATE.status = "已取消本次批量截图任务。"
                self.send_json({"ok": True})
            elif parsed.path == "/api/open-output":
                output_dir = Path(STATE.output_dir).expanduser()
                output_dir.mkdir(parents=True, exist_ok=True)
                os.startfile(str(output_dir))
                self.send_json({"ok": True})
            elif parsed.path == "/api/choose-output":
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length).decode("utf-8", errors="replace")
                current = parse_qs(body).get("current", [""])[0]
                selected = choose_output_dir(current)
                self.send_json({"ok": True, "path": selected})
            elif parsed.path == "/api/exit":
                self.send_json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send_error(404)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)

    def handle_start(self) -> None:
        if STATE.worker and STATE.worker.is_alive():
            raise ValueError("任务正在运行。")

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
            },
        )
        config = normalize_form(form)
        config["delay_min"], config["delay_max"] = sorted([config["delay_min"], config["delay_max"]])

        STATE.stop_event.clear()
        with STATE.lock:
            STATE.logs.clear()
            STATE.status = "正在启动浏览器..."
            STATE.current = 0
            STATE.total = 0
            STATE.running = True
            STATE.manual_needed = False
            STATE.manual_message = ""
            STATE.success_count = 0
            STATE.failed_count = 0
            STATE.last_error = ""
            STATE.output_dir = config["output"]
            STATE.awaiting_confirmation = False
            STATE.preview_stats = {}
            STATE.result_files = []
            STATE.manual_event = None
            STATE.confirmation_event = None

        STATE.worker = threading.Thread(target=run_worker, args=(config,), daemon=True)
        STATE.worker.start()
        self.send_json({"ok": True})


def main() -> int:
    core.ensure_project_dirs()
    (APP_DIR / "input").mkdir(exist_ok=True)
    (APP_DIR / "output").mkdir(exist_ok=True)

    port = find_free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"打开控制台: {url}")
    webbrowser.open(url)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
