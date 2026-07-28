from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import web_gui
from order_batch.capture import capture_order
from order_batch.fetcher import apply_date_filter
from order_batch.filters import filter_paid_orders
from order_batch.models import OrderSummary
from order_batch.receiver import parse_receiver_info
from order_batch.saver import save_batch_results, save_payment_tables
from order_batch.time_ranges import resolve_time_range


class FakeForm(dict):
    def getfirst(self, name: str, default: str = "") -> str:
        return str(self.get(name, default))


class FakePage:
    def __init__(self, contexts: list[str] | None = None) -> None:
        self.contexts = contexts or []

    def evaluate(self, _script: str):
        return self.contexts


class TimeRangeTests(unittest.TestCase):
    def test_presets_are_inclusive(self) -> None:
        today = date(2026, 7, 26)
        self.assertEqual(resolve_time_range("today", today=today), (today, today))
        self.assertEqual(resolve_time_range("yesterday", today=today), (date(2026, 7, 25), date(2026, 7, 25)))
        self.assertEqual(resolve_time_range("last3", today=today), (date(2026, 7, 24), today))
        self.assertEqual(resolve_time_range("last7", today=today), (date(2026, 7, 20), today))
        self.assertEqual(resolve_time_range("month", today=today), (date(2026, 7, 1), today))

    def test_custom_range_rejects_reverse_dates(self) -> None:
        with self.assertRaisesRegex(ValueError, "开始日期"):
            resolve_time_range("custom", "2026-07-27", "2026-07-26")


class FilterTests(unittest.TestCase):
    def test_only_paid_orders_in_range_are_returned(self) -> None:
        orders = [
            OrderSummary("100000000000", "等待卖家发货", "2026-07-26 10:00:00"),
            OrderSummary("200000000000", "等待买家付款", "2026-07-26 11:00:00"),
            OrderSummary("300000000000", "交易关闭", "2026-07-26 12:00:00"),
            OrderSummary("400000000000", "退款成功", "2026-07-26 13:00:00", raw_text="交易成功"),
            OrderSummary("500000000000", "已支付", "2026-07-20 10:00:00"),
            OrderSummary("600000000000", "已支付", ""),
            OrderSummary("700000000000", "发货超时", "2026-07-26 14:00:00"),
        ]

        paid, stats = filter_paid_orders(orders, date(2026, 7, 26), date(2026, 7, 26))

        self.assertEqual([order.order_id for order in paid], ["100000000000", "700000000000"])
        self.assertEqual(stats.scanned_total, 7)
        self.assertEqual(stats.total, 5)
        self.assertEqual(stats.paid, 2)
        self.assertEqual(stats.ignored_unpaid, 1)
        self.assertEqual(stats.ignored_cancelled, 1)
        self.assertEqual(stats.ignored_other, 1)
        self.assertEqual(stats.ignored_out_of_range, 1)
        self.assertEqual(stats.ignored_date_unknown, 1)


class ReceiverAndCaptureTests(unittest.TestCase):
    def test_receiver_parser_handles_single_line_text(self) -> None:
        page = FakePage(["收货人：张三 手机号码：13800138000 收货地址：浙江省杭州市西湖区测试路1号 订单信息"])

        info = parse_receiver_info(page)

        self.assertEqual(info.receiver, "张三")
        self.assertEqual(info.phone, "13800138000")
        self.assertEqual(info.address, "浙江省杭州市西湖区测试路1号")

    def test_capture_reuses_callbacks_and_classifies_by_receiver(self) -> None:
        page = FakePage(["收货人：张三 手机号：13800138000 收货地址：浙江省杭州市测试路1号 订单信息"])
        order = OrderSummary("5118000000000000001", "已支付", "2026-07-26 10:00:00")
        opened: list[str] = []

        def open_order(_page, _context, order_id, manual_waiter=None):
            opened.append(order_id)

        def screenshot(_page, order_id, output_dir):
            path = Path(output_dir) / f"{order_id}.png"
            path.write_bytes(b"png")
            return path, "现有截图逻辑"

        with tempfile.TemporaryDirectory() as temp_dir:
            result = capture_order(
                page,
                object(),
                order,
                temp_dir,
                "receiver",
                1,
                open_order,
                screenshot,
                lambda _page: "",
            )

            self.assertEqual(opened, [order.order_id])
            self.assertEqual(result["截图结果"], "成功")
            self.assertEqual(Path(result["截图文件"]).name, f"{order.order_id}.png")
            self.assertEqual(Path(result["分类目录"]).name, "张三")

    def test_capture_without_classification_reads_payment_into_base_folder(self) -> None:
        page = FakePage(["收货人：李四 手机号：13900139000 收货地址：广东省深圳市测试路2号 订单信息"])
        order = OrderSummary("5118000000000000002")

        def screenshot(_page, order_id, output_dir):
            path = Path(output_dir) / f"{order_id}.png"
            path.write_bytes(b"png")
            return path, "现有截图逻辑"

        with tempfile.TemporaryDirectory() as temp_dir:
            result = capture_order(
                page,
                object(),
                order,
                temp_dir,
                "none",
                1,
                lambda *_args, **_kwargs: None,
                screenshot,
                lambda _page: "",
                extract_payment=lambda _page: {"amount": "¥88.50", "status": "读取成功", "raw": "实付款 ¥88.50"},
            )

            self.assertEqual(Path(result["分类目录"]), Path(temp_dir))
            self.assertEqual(Path(result["截图文件"]).parent, Path(temp_dir))
            self.assertEqual(result["实付款金额"], "¥88.50")
            self.assertEqual(result["分类方式"], "不分类")


class WebFormTests(unittest.TestCase):
    def test_batch_mode_does_not_require_an_order_file(self) -> None:
        config = web_gui.normalize_form(FakeForm({"source_mode": "batch", "time_preset": "today"}))
        self.assertEqual(config["source_mode"], "batch")
        self.assertEqual(config["classify_by"], "none")

    def test_file_mode_still_requires_an_order_file(self) -> None:
        with self.assertRaisesRegex(ValueError, "订单文件"):
            web_gui.normalize_form(FakeForm({"source_mode": "file"}))

    def test_batch_controls_have_unique_ids(self) -> None:
        required_ids = (
            "sourceMode",
            "batchModeFields",
            "timePreset",
            "customDates",
            "confirmBox",
            "confirmBatchBtn",
            "cancelBatchBtn",
        )
        for element_id in required_ids:
            self.assertEqual(web_gui.INDEX_HTML.count(f'id="{element_id}"'), 1)


class ResultSaverTests(unittest.TestCase):
    def test_excel_and_json_reports_are_created(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            excel_path, json_path = save_batch_results(
                [{"订单号": "5118000000000000001", "截图结果": "成功"}],
                temp_dir,
                {"total": 1, "paid": 1},
                {"开始日期": "2026-07-26", "结束日期": "2026-07-26"},
            )

            self.assertTrue(excel_path.is_file())
            self.assertTrue(json_path.is_file())

    def test_payment_tables_are_saved_per_category_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / "张三"
            second = Path(temp_dir) / "李四"
            rows = [
                {"订单号": "1", "分类目录": str(first), "实付款金额": "¥10.00"},
                {"订单号": "2", "分类目录": str(first), "实付款金额": "¥20.00"},
                {"订单号": "3", "分类目录": str(second), "实付款金额": "¥30.00"},
            ]

            paths = save_payment_tables(rows, temp_dir)

            self.assertEqual({path.parent.name for path in paths}, {"张三", "李四"})
            self.assertTrue(all(path.name == "订单实付款汇总.xlsx" for path in paths))
            self.assertTrue(all(path.is_file() for path in paths))


class BrowserDateFilterTests(unittest.TestCase):
    def test_real_browser_selects_custom_time_and_searches(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            self.skipTest(str(exc))

        html = """
        <style>
          .filter { display:flex; align-items:center; gap:14px; margin:30px; }
          .fake-select { width:230px; height:34px; border:1px solid #aaa; }
          #customOption { display:none; width:230px; height:34px; margin-left:110px; }
          input, button { width:180px; height:34px; margin:8px; }
        </style>
        <div class="filter"><span>下单时间</span><div class="fake-select" role="combobox">全部</div></div>
        <div id="customOption">自定义时间</div>
        <input placeholder="开始日期" disabled>
        <input placeholder="结束日期" disabled>
        <button>搜索</button>
        <script>
          document.querySelector('.fake-select').onclick = () => {
            document.querySelector('#customOption').style.display = 'block';
          };
          document.querySelector('#customOption').onclick = () => {
            document.querySelectorAll('input').forEach(input => input.disabled = false);
            document.querySelector('#customOption').style.display = 'none';
          };
          document.querySelector('button').onclick = () => document.body.dataset.searched = 'yes';
        </script>
        """

        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(channel="chrome", headless=True)
            except Exception as exc:
                self.skipTest(f"本机 Chrome 不可用于浏览器级测试: {exc}")
            try:
                page = browser.new_page()
                page.set_content(html)
                applied = apply_date_filter(page, date(2026, 7, 1), date(2026, 7, 26))

                self.assertTrue(applied)
                self.assertEqual(page.locator('input[placeholder="开始日期"]').input_value(), "2026-07-01")
                self.assertEqual(page.locator('input[placeholder="结束日期"]').input_value(), "2026-07-26")
                self.assertEqual(page.locator("body").get_attribute("data-searched"), "yes")
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
