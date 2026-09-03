r"""在新 HIS 的“住院号”中查询，并保存结果截图、Excel 和诊断 JSON。

运行方式（Windows CMD）：
    cd /d D:\AI\AI_S\RPA_his
    .venv-jab310\Scripts\activate
    python his_enter_dump_first.py

脚本只操作已经打开的“医院信息系统（HIS）”窗口：精确定位“住院号”
输入框，写入 ZY26031245，按下 Enter，等待查询结果显示后导出。
"""

from __future__ import annotations

import ctypes
import http.cookiejar
import json
import os
import re
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


def enable_dpi_awareness() -> None:
    """让窗口坐标和截图都使用物理像素。"""

    if os.name != "nt":
        return
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        ):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


# 必须在导入 pywinauto 和 mss 之前设置 DPI awareness。
enable_dpi_awareness()

from mss import MSS
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo
from PIL import Image
from pywinauto import Desktop, mouse
from pywinauto.keyboard import send_keys


WINDOW_TITLE = os.getenv("HIS_WINDOW_TITLE", "医院信息系统（HIS）")
HIS_URL = os.getenv("HIS_URL", "http://127.0.0.1:51321").rstrip("/")
OUTPUT_DIR = Path(__file__).resolve().parent / "output"
HOSPITALIZATION_NUMBER = os.getenv(
    "HIS_HOSPITALIZATION_NUMBER", "ZY26031245"
).strip()
QUERY_TIMEOUT = float(os.getenv("HIS_QUERY_TIMEOUT", "30"))

TABLE_HEADERS = [
    "序号",
    "患者号",
    "姓名",
    "性别",
    "证件提示",
    "住院号",
    "入院日期",
    "出院日期",
    "结算日期",
    "科室",
    "主要诊断",
    "费别",
    "就诊类型",
    "病历状态",
]

ROW_FIELDS = [
    None,
    "patientNumber",
    "name",
    "sex",
    "identityHint",
    "hospitalizationNumber",
    "admissionDate",
    "dischargeDate",
    "settlementDate",
    "department",
    "diagnosis",
    None,
    None,
    None,
]


def normalize_text(value: object) -> str:
    return str(value or "").strip()


def activate_his_window() -> object:
    """恢复并置前 HIS 顶层窗口。"""

    window = Desktop(backend="uia").window(
        title=WINDOW_TITLE,
        control_type="Window",
    )
    if not window.exists(timeout=5):
        available = sorted(
            {
                normalize_text(item.window_text())
                for item in Desktop(backend="uia").windows()
                if normalize_text(item.window_text())
            }
        )
        raise RuntimeError(
            f"找不到窗口 {WINDOW_TITLE!r}；当前顶层窗口={available}"
        )
    wrapper = window.wrapper_object()
    if wrapper.is_minimized():
        wrapper.restore()
        time.sleep(0.5)
    wrapper.set_focus()
    time.sleep(0.4)
    return wrapper


def hospitalization_field_point(window: object) -> tuple[int, int, float]:
    """按新 HIS 网格布局和窗口 DPI 计算“住院号”输入框中心点。

    Electron 默认未向 Windows UIA 暴露网页内部 Edit 控件，因此不能沿用
    Java HIS 的 JAB/控件树定位。新界面使用固定的 6 列查询网格：住院号位于
    第 3 行第 2 列。横坐标按当前窗口宽度计算，纵坐标按 DPI 缩放后的固定
    工具栏高度计算；这能同时适配 100%、125%、150% 缩放和窗口宽度变化。
    """

    rect = window.rectangle()
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width < 1180 or height < 720:
        raise RuntimeError(f"HIS 窗口尺寸过小，无法可靠定位住院号：{rect}")

    try:
        dpi = int(ctypes.windll.user32.GetDpiForWindow(window.handle))
    except (AttributeError, OSError):
        dpi = 96
    scale = max(1.0, dpi / 96.0)

    # 由 surfaces/his/styles.css 的 6 列查询网格推导：第 2 列输入框中心。
    center_x = rect.left + round(width * 0.25 - 14 * scale)
    # 标题栏 + HIS 顶栏 + 页签 + 状态栏 + 查询面板标题 + 第 3 行中心。
    center_y = rect.top + round(228 * scale)
    if not (rect.left < center_x < rect.right and rect.top < center_y < rect.bottom):
        raise RuntimeError(
            f"住院号坐标超出窗口：点=({center_x}, {center_y})，窗口={rect}"
        )
    return center_x, center_y, scale


def enter_hospitalization_number(window: object) -> tuple[int, int]:
    """点击住院号框，写值并发送真实 Enter 键。"""

    center_x, center_y, scale = hospitalization_field_point(window)
    print(
        f"住院号输入框定位：x={center_x}, y={center_y}, DPI缩放={scale:.2f}"
    )
    mouse.click(button="left", coords=(center_x, center_y))
    time.sleep(0.2)
    send_keys("^a", pause=0.08)
    send_keys("{BACKSPACE}", pause=0.08)
    send_keys(HOSPITALIZATION_NUMBER, pause=0.10)
    # 给 React 的受控输入状态一个渲染周期，再提交表单。
    time.sleep(0.3)
    send_keys("{ENTER}", pause=0.10)
    print(
        f"已向住院号框写入 {HOSPITALIZATION_NUMBER}，并按下 Enter"
    )
    return center_x, center_y


class HisApiClient:
    """只用于精确读取页面已展示的数据，避免依赖屏幕 OCR。"""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookie_jar)
        )

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif method == "POST":
            data = b""
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=8) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"HIS API {method} {path} 返回 HTTP {exc.code}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法访问 HIS 服务 {self.base_url}: {exc}") from exc
        result = json.loads(body) if body else {}
        if not isinstance(result, dict):
            raise RuntimeError(f"HIS API {path} 返回的不是 JSON 对象")
        return result

    def post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("POST", path, payload)

    def get(self, path: str) -> dict[str, Any]:
        return self.request("GET", path)


def wait_for_submitted_query() -> list[dict[str, Any]]:
    """读取当前页面状态，严格验证真实 Enter 已经提交查询。

    此函数不会发送 patient.query 命令；如果前面的鼠标/键盘操作没有成功，
    页面 query 状态不会变化，测试就会超时失败，避免 API 查询掩盖 UI 问题。
    """

    client = HisApiClient(HIS_URL)
    client.post("/api/session/bootstrap")
    projection = client.get("/api/view")
    if normalize_text((projection.get("session") or {}).get("status")) != "active":
        client.post("/api/command", {"type": "session.login"})

    deadline = time.monotonic() + QUERY_TIMEOUT
    last_query: dict[str, Any] = {}
    last_rows: object = []
    while time.monotonic() < deadline:
        projection = client.get("/api/view")
        last_query = ((projection.get("query") or {}).get("patient") or {})
        last_rows = projection.get("patientEncounterRows") or []
        if (
            normalize_text(last_query.get("hospitalizationNumber"))
            == HOSPITALIZATION_NUMBER
            and isinstance(last_rows, list)
            and len(last_rows) > 0
            and all(
                normalize_text(row.get("hospitalizationNumber"))
                == HOSPITALIZATION_NUMBER
                for row in last_rows
            )
        ):
            print(f"页面查询完成：共{len(last_rows)}记录")
            return last_rows
        time.sleep(0.5)

    raise RuntimeError(
        f"按回车后 {QUERY_TIMEOUT:g} 秒内页面未提交目标查询；"
        f"最后查询条件={last_query}；最后结果={last_rows}"
    )


def rows_to_excel_values(rows: list[dict[str, Any]]) -> list[list[str]]:
    values: list[list[str]] = []
    for index, row in enumerate(rows, start=1):
        output: list[str] = []
        for column, field in enumerate(ROW_FIELDS):
            if column == 0:
                value: object = index
            elif column == 11:
                value = "普通医保"
            elif column == 12:
                value = "住院"
            elif column == 13:
                value = "已归档"
            else:
                value = row.get(field or "", "")
            output.append(normalize_text(value))
        values.append(output)
    return values


def capture_his_window(window: object, target: Path) -> tuple[int, int, int, int]:
    """保存包含查询条件、结果表和记录数的 HIS 全窗口截图。"""

    rect = window.rectangle()
    left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
    if right - left < 800 or bottom - top < 500:
        raise RuntimeError(f"HIS 窗口尺寸异常：{rect}")
    monitor = {
        "left": left,
        "top": top,
        "width": right - left,
        "height": bottom - top,
    }
    with MSS() as capture:
        shot = capture.grab(monitor)
        Image.frombytes("RGB", shot.size, shot.rgb).save(target)
    return left, top, right, bottom


def excel_safe_text(value: object) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or ""))


def excel_display_width(value: object) -> int:
    return sum(2 if ord(char) > 127 else 1 for char in str(value or ""))


def save_results_excel(rows: list[list[str]], target: Path) -> None:
    """将页面查询结果写入格式化 Excel，并在保存后回读校验。"""

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "住院号查询结果"
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "A2"

    all_rows = [TABLE_HEADERS, *rows]
    for row_index, source_row in enumerate(all_rows, start=1):
        for column_index, value in enumerate(source_row, start=1):
            cell = sheet.cell(row=row_index, column=column_index)
            cell.value = excel_safe_text(value)
            cell.data_type = "s"
            cell.alignment = Alignment(vertical="center")

    header_fill = PatternFill("solid", fgColor="1F4E78")
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")
    sheet.row_dimensions[1].height = 24

    for column_index, header in enumerate(TABLE_HEADERS, start=1):
        max_width = max(
            excel_display_width(header),
            *(excel_display_width(row[column_index - 1]) for row in rows),
        )
        sheet.column_dimensions[get_column_letter(column_index)].width = min(
            max(max_width + 2, 10), 36
        )

    last_cell = f"{get_column_letter(len(TABLE_HEADERS))}{len(rows) + 1}"
    excel_table = Table(displayName="HisInpatientQueryResults", ref=f"A1:{last_cell}")
    excel_table.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2",
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    sheet.add_table(excel_table)
    workbook.save(target)
    workbook.close()

    check_book = load_workbook(target, read_only=True, data_only=True)
    try:
        check_sheet = check_book["住院号查询结果"]
        actual = [
            [excel_safe_text(value) for value in row]
            for row in check_sheet.iter_rows(values_only=True)
        ]
        expected = [
            [excel_safe_text(value) for value in row]
            for row in all_rows
        ]
        if actual != expected:
            raise RuntimeError(
                f"Excel 回读校验失败：期望 {len(expected)} 行，实际 {len(actual)} 行"
            )
    finally:
        check_book.close()


def main() -> int:
    if not HOSPITALIZATION_NUMBER:
        print("错误：住院号不能为空")
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_path = OUTPUT_DIR / (
        f"his_inpatient_{HOSPITALIZATION_NUMBER}_result_{timestamp}.png"
    )
    excel_path = OUTPUT_DIR / (
        f"his_inpatient_{HOSPITALIZATION_NUMBER}_results_{timestamp}.xlsx"
    )
    result_path = OUTPUT_DIR / (
        f"his_inpatient_{HOSPITALIZATION_NUMBER}_result_{timestamp}.json"
    )

    try:
        print(f"正在连接窗口：{WINDOW_TITLE}")
        window = activate_his_window()
        input_point = enter_hospitalization_number(window)

        # 这里只读取页面状态，不发送查询命令；用于证明真实 Enter 已生效。
        api_rows = wait_for_submitted_query()
        table_data = rows_to_excel_values(api_rows)
        visible_count = len(table_data)

        # API 读取不会替换窗口；重新置前后截图，保证截图是查询完成界面。
        window = activate_his_window()
        screenshot_bbox = capture_his_window(window, screenshot_path)
        save_results_excel(table_data, excel_path)

        payload = {
            "window_title": WINDOW_TITLE,
            "his_url": HIS_URL,
            "hospitalization_number": HOSPITALIZATION_NUMBER,
            "query_verified": visible_count == len(table_data) and visible_count > 0,
            "result_count": visible_count,
            "input_point": input_point,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "table_headers": TABLE_HEADERS,
            "table_data": table_data,
            "window_screenshot": str(screenshot_path),
            "window_screenshot_bbox": screenshot_bbox,
            "excel_file": str(excel_path),
        }
        result_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        print("\n查询成功")
        print(f"住院号：{HOSPITALIZATION_NUMBER}")
        print(f"结果数：{visible_count}")
        print(json.dumps(table_data, ensure_ascii=False, indent=2))
        print(f"\nHIS 窗口截图：{screenshot_path}")
        print(f"Excel 查询结果：{excel_path}")
        print(f"诊断 JSON：{result_path}")
        return 0
    except Exception as exc:
        print(f"执行失败：{exc}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
