r"""独立流程配置器使用的 HIS Windows 自动化适配器。

运行方式（Windows CMD）：
    python his_automation.py

脚本只操作已经打开的“医院信息系统（HIS）”窗口：精确定位“住院号”
输入框，写入 ZY26031245，按下 Enter，等待查询结果后点击首条结果。
进入“医嘱费用查询”页面后截图；如果页面出现横向滚动条，先截当前
位置，再拖到最右端截第二张。
"""

from __future__ import annotations

import ctypes
import http.cookiejar
import json
import os
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
from PIL import Image
from pywinauto import Desktop, mouse
from pywinauto.controls.hwndwrapper import HwndWrapper
from pywinauto.keyboard import send_keys


WINDOW_TITLE = os.getenv("HIS_WINDOW_TITLE", "医院信息系统（HIS）")
HIS_URL = os.getenv("HIS_URL", "http://127.0.0.1:51321").rstrip("/")
OUTPUT_DIR = Path(__file__).resolve().parent / "output"
HOSPITALIZATION_NUMBER = os.getenv(
    "HIS_HOSPITALIZATION_NUMBER", "ZY26031245"
).strip()
QUERY_TIMEOUT = float(os.getenv("HIS_QUERY_TIMEOUT", "30"))

def normalize_text(value: object) -> str:
    return str(value or "").strip()


def activate_his_window() -> object:
    """按标题连接并置前顶层窗口，不要求其 UIA 类型必须为 Window。"""

    desktop = Desktop(backend="uia")
    deadline = time.monotonic() + 5.0
    available: list[str] = []
    wrapper: object | None = None

    while True:
        top_level_items = desktop.windows()
        available = sorted(
            {
                normalize_text(item.window_text())
                for item in top_level_items
                if normalize_text(item.window_text())
            }
        )
        matches = [
            item
            for item in top_level_items
            if normalize_text(item.window_text()) == WINDOW_TITLE
        ]
        if matches:
            # 普通应用通常暴露为 Window；完全自绘、没有 UIA 控件树的
            # 顶层窗口可能只暴露为 Pane。两者都属于可连接的顶层对象。
            type_priority = {"Window": 0, "Pane": 1}
            wrapper = min(
                matches,
                key=lambda item: type_priority.get(
                    str(getattr(item.element_info, "control_type", "") or ""),
                    2,
                ),
            )
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)

    if wrapper is None:
        raise RuntimeError(
            f"找不到窗口 {WINDOW_TITLE!r}；当前顶层窗口={available}"
        )

    hwnd = int(getattr(wrapper, "handle", 0) or 0)
    if not hwnd:
        raise RuntimeError(f"窗口 {WINDOW_TITLE!r} 没有可用的顶层句柄")

    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.5)

    # UIAWrapper.set_focus() 对没有 UIA Provider 的顶层 Pane 只尝试 UIA
    # SetFocus，可能没有异常却也不改变窗口 Z 序。这里始终通过真实 HWND
    # 执行顶层窗口置前，同时仍返回 UIA wrapper 供后续控件查找使用。
    try:
        HwndWrapper(hwnd).set_focus()
    except Exception:
        user32.ShowWindow(hwnd, 5)  # SW_SHOW
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)

    time.sleep(0.4)
    if int(user32.GetForegroundWindow() or 0) != hwnd:
        raise RuntimeError(f"已连接窗口 {WINDOW_TITLE!r}，但未能将其置于前台")
    return wrapper


def find_hospitalization_field(window: object, timeout: float = 5.0) -> object | None:
    """按 UIA 控件类型和 aria-label 查找“住院号”输入框。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            matches = [
                item
                for item in window.descendants(control_type="Edit")
                if normalize_text(getattr(item.element_info, "name", ""))
                == "住院号"
                and item.is_visible()
                and item.is_enabled()
            ]
        except Exception:
            matches = []
        if len(matches) == 1:
            return matches[0]
        time.sleep(0.25)
    return None


def control_value(field: object) -> str | None:
    """读取 UIA ValuePattern；控件不支持读取时返回 None。"""

    try:
        return normalize_text(field.get_value())
    except Exception:
        return None


def type_and_submit() -> None:
    """在当前获得焦点的输入框内覆盖写值并发送 Enter。"""

    send_keys("^a", pause=0.08)
    send_keys("{BACKSPACE}", pause=0.08)
    send_keys(HOSPITALIZATION_NUMBER, pause=0.10)
    # 给 React 的受控输入状态一个渲染周期，再提交表单。
    time.sleep(0.3)
    send_keys("{ENTER}", pause=0.10)


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


def enter_hospitalization_number(window: object) -> tuple[tuple[int, int], str]:
    """优先按 UIA 控件定位；不可用时才按布局坐标兜底。"""

    field = find_hospitalization_field(window)
    if field is not None:
        rect = field.rectangle()
        center_x = rect.left + (rect.right - rect.left) // 2
        center_y = rect.top + (rect.bottom - rect.top) // 2
        print(
            f"按控件定位住院号输入框：x={center_x}, y={center_y}, "
            "name='住院号', type='Edit'"
        )
        field.click_input()
        time.sleep(0.2)
        send_keys("^a", pause=0.08)
        send_keys("{BACKSPACE}", pause=0.08)
        send_keys(HOSPITALIZATION_NUMBER, pause=0.10)
        time.sleep(0.3)
        actual = control_value(field)
        if actual is None or actual == HOSPITALIZATION_NUMBER:
            send_keys("{ENTER}", pause=0.10)
            print(
                f"已向住院号控件写入 {HOSPITALIZATION_NUMBER}，并按下 Enter"
            )
            return (center_x, center_y), "uia-control"
        print(
            f"控件值校验未通过（实际={actual!r}），改用动态坐标兜底"
        )

    center_x, center_y, scale = hospitalization_field_point(window)
    print(
        f"动态坐标定位住院号输入框：x={center_x}, y={center_y}, "
        f"DPI缩放={scale:.2f}"
    )
    mouse.click(button="left", coords=(center_x, center_y))
    time.sleep(0.2)
    type_and_submit()
    print(
        f"已向住院号框写入 {HOSPITALIZATION_NUMBER}，并按下 Enter"
    )
    return (center_x, center_y), "coordinate-fallback"


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


def find_query_result_control(window: object, timeout: float = 3.0) -> object | None:
    """尝试按 UIA 名称找到包含目标住院号的患者结果行。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            matches = []
            for item in window.descendants():
                name = normalize_text(getattr(item.element_info, "name", ""))
                control_type = normalize_text(
                    getattr(item.element_info, "control_type", "")
                )
                rect = item.rectangle()
                if (
                    HOSPITALIZATION_NUMBER in name
                    and control_type in {"Button", "DataItem"}
                    and rect.right - rect.left >= 300
                    and item.is_visible()
                    and item.is_enabled()
                ):
                    matches.append(item)
            if len(matches) == 1:
                return matches[0]
        except Exception:
            pass
        time.sleep(0.25)
    return None


def query_result_row_point(window: object) -> tuple[int, int, float]:
    """按 HIS 查询表布局计算首条查询结果行内的安全点击点。"""

    rect = window.rectangle()
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width < 1180 or height < 720:
        raise RuntimeError(f"HIS 窗口尺寸过小，无法可靠点击查询结果：{rect}")

    scale = window_dpi_scale(window)
    logical_height = height / scale
    # max-height:820px 时查询面板会压缩约 23px；外层窗口还包含标题栏。
    row_center_dip = 321 if logical_height <= 850 else 344
    center_x = rect.left + round(300 * scale)
    center_y = rect.top + round(row_center_dip * scale)
    return center_x, center_y, scale


def click_query_result(window: object) -> str:
    """点击目标查询结果行，使 HIS 进入医嘱费用查询页面。"""

    row = find_query_result_control(window)
    if row is not None:
        rect = row.rectangle()
        center_x = rect.left + (rect.right - rect.left) // 2
        center_y = rect.top + (rect.bottom - rect.top) // 2
        print(
            f"按控件点击查询结果：x={center_x}, y={center_y}, "
            f"住院号={HOSPITALIZATION_NUMBER}"
        )
        row.click_input()
        return "uia-control"

    center_x, center_y, scale = query_result_row_point(window)
    print(
        f"按动态坐标点击首条查询结果：x={center_x}, y={center_y}, "
        f"DPI缩放={scale:.2f}"
    )
    mouse.click(button="left", coords=(center_x, center_y))
    return "coordinate-fallback"


def wait_for_result_navigation() -> int:
    """等待点击结果完成，并验证已选择目标住院次及加载医嘱数据。"""

    client = HisApiClient(HIS_URL)
    client.post("/api/session/bootstrap")
    projection = client.get("/api/view")
    if normalize_text((projection.get("session") or {}).get("status")) != "active":
        client.post("/api/command", {"type": "session.login"})

    deadline = time.monotonic() + QUERY_TIMEOUT
    last_selected: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        projection = client.get("/api/view")
        selected = projection.get("selectedEncounter")
        last_selected = selected if isinstance(selected, dict) else None
        if (
            last_selected is not None
            and normalize_text(last_selected.get("hospitalizationNumber"))
            == HOSPITALIZATION_NUMBER
        ):
            orders = projection.get("orders") or []
            time.sleep(0.8)  # 等待前端从患者查询切换到医嘱费用查询。
            print(f"已进入医嘱费用查询页面：共{len(orders)}条医嘱")
            return len(orders)
        time.sleep(0.5)

    raise RuntimeError(
        f"点击结果后 {QUERY_TIMEOUT:g} 秒内未进入目标住院次；"
        f"最后所选住院次={last_selected}"
    )


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


def window_dpi_scale(window: object) -> float:
    try:
        dpi = int(ctypes.windll.user32.GetDpiForWindow(window.handle))
    except (AttributeError, OSError):
        dpi = 96
    return max(1.0, dpi / 96.0)


def longest_neutral_run(image: Image.Image, y: int) -> tuple[int, int] | None:
    """查找一行中符合 Windows/Chromium 灰色滚动滑块的最长色块。"""

    width = image.width
    pixels = image.load()
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for x in range(width + 1):
        matched = False
        if x < width:
            red, green, blue = pixels[x, y]
            brightness = (red + green + blue) / 3
            matched = (
                165 <= brightness <= 205
                and max(red, green, blue) - min(red, green, blue) <= 22
            )
        if matched and start is None:
            start = x
        elif not matched and start is not None:
            runs.append((start, x - 1))
            start = None

    minimum = max(80, round(width * 0.05))
    maximum = round(width * 0.96)
    candidates = [
        run for run in runs if minimum <= run[1] - run[0] + 1 <= maximum
    ]
    return max(candidates, key=lambda run: run[1] - run[0]) if candidates else None


def detect_horizontal_scrollbar(
    window: object,
    screenshot_path: Path,
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """从首张截图检测结果区横向滚动条及滑块拖动起止点。"""

    with Image.open(screenshot_path) as source:
        image = source.convert("RGB")
    scale = window_dpi_scale(window)
    width, height = image.size
    search_top = max(0, height - round(75 * scale))
    search_bottom = max(search_top + 1, height - round(25 * scale))

    candidates: list[tuple[int, int, int]] = []
    for y in range(search_top, search_bottom):
        run = longest_neutral_run(image, y)
        if run is not None:
            candidates.append((run[0], run[1], y))

    # 真正的圆角滑块会在相邻多行保持近似相同的左右边界；页面分隔线
    # 通常只有 1 像素高，因此要求至少 4 行相互支持，避免误判。
    best: tuple[int, int, int] | None = None
    best_support = 0
    for candidate in candidates:
        left, right, _y = candidate
        support = sum(
            1
            for other_left, other_right, _other_y in candidates
            if abs(other_left - left) <= 8 and abs(other_right - right) <= 8
        )
        if support > best_support or (
            support == best_support
            and best is not None
            and right - left > best[1] - best[0]
        ):
            best = candidate
            best_support = support

    if best is None or best_support < 4:
        return None

    thumb_left, thumb_right, thumb_y = best
    thumb_width = thumb_right - thumb_left + 1
    current_center_x = (thumb_left + thumb_right) // 2
    track_right = width - round(17 * scale)
    rightmost_center_x = track_right - thumb_width // 2
    rightmost_center_x = max(current_center_x, rightmost_center_x)

    rect = window.rectangle()
    start = (rect.left + current_center_x, rect.top + thumb_y)
    end = (rect.left + rightmost_center_x, rect.top + thumb_y)
    return start, end


def drag_scrollbar(start: tuple[int, int], end: tuple[int, int]) -> None:
    """平滑拖动滚动条滑块，降低快速跳动导致鼠标脱离滑块的概率。"""

    if start == end:
        return
    mouse.move(coords=start)
    mouse.press(button="left", coords=start)
    try:
        for step in range(1, 13):
            x = round(start[0] + (end[0] - start[0]) * step / 12)
            y = round(start[1] + (end[1] - start[1]) * step / 12)
            mouse.move(coords=(x, y))
            time.sleep(0.03)
    finally:
        mouse.release(button="left", coords=end)


def main() -> int:
    if not HOSPITALIZATION_NUMBER:
        print("错误：住院号不能为空")
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    current_screenshot_path = OUTPUT_DIR / (
        f"his_inpatient_{HOSPITALIZATION_NUMBER}_current_{timestamp}.png"
    )
    right_screenshot_path = OUTPUT_DIR / (
        f"his_inpatient_{HOSPITALIZATION_NUMBER}_rightmost_{timestamp}.png"
    )

    try:
        print(f"正在连接窗口：{WINDOW_TITLE}")
        window = activate_his_window()
        _input_point, locator_mode = enter_hospitalization_number(window)

        # 这里只读取页面状态，不发送查询命令；用于证明真实 Enter 已生效。
        api_rows = wait_for_submitted_query()
        visible_count = len(api_rows)

        # 查询完成后点击唯一结果行，等待进入医嘱费用查询页面。
        window = activate_his_window()
        result_locator_mode = click_query_result(window)
        order_count = wait_for_result_navigation()

        # 第一张始终保存跳转页面的当前位置。
        window = activate_his_window()
        capture_his_window(window, current_screenshot_path)

        # 检测到横向滚动条时，拖到最右端保存第二张，并恢复原位置，
        # 避免影响下一条批量任务的“当前位置”截图。
        scrollbar = detect_horizontal_scrollbar(window, current_screenshot_path)
        saved_right_screenshot = False
        if scrollbar is not None:
            original_position, rightmost_position = scrollbar
            print(
                f"检测到横向滚动条：{original_position} -> {rightmost_position}"
            )
            drag_scrollbar(original_position, rightmost_position)
            try:
                time.sleep(0.5)
                capture_his_window(window, right_screenshot_path)
                saved_right_screenshot = True
            finally:
                # 拖到最右端后实际滑块会被轨道边界夹住，因此重新检测
                # 第二张截图中的滑块位置，再恢复到最初位置。
                if right_screenshot_path.is_file():
                    right_state = detect_horizontal_scrollbar(
                        window, right_screenshot_path
                    )
                    if right_state is not None:
                        drag_scrollbar(right_state[0], original_position)
                        time.sleep(0.4)
        else:
            print("未检测到横向滚动条，只保存当前位置截图")

        print("\n查询成功")
        print(f"住院号：{HOSPITALIZATION_NUMBER}")
        print(f"患者查询结果数：{visible_count}")
        print(f"医嘱结果数：{order_count}")
        print(f"输入框定位方式：{locator_mode}")
        print(f"结果行定位方式：{result_locator_mode}")
        print(f"当前位置截图：{current_screenshot_path}")
        if saved_right_screenshot:
            print(f"最右端截图：{right_screenshot_path}")
        return 0
    except Exception as exc:
        print(f"执行失败：{exc}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
