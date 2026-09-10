r"""独立流程配置器的 OCR 表格列标注模块。

本文件随流程配置器发布，不依赖 HIS 项目目录中的 Python 文件。
执行顺序：

1. 检查 HIS 窗口是否最大化，未最大化则先最大化；
2. 调用原脚本完成住院号查询、点击结果和左右两张截图；
3. 用 RapidOCR 按配置的表格标题或目标列定位表头；
4. 给截图中实际出现的六个目标列画红框，另存为 ``*_marked.png``。

首次使用需要在当前虚拟环境安装 OCR 依赖：
    pip install rapidocr onnxruntime
"""

from __future__ import annotations

import re
import time
import traceback
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Sequence

from PIL import Image, ImageDraw

import his_automation as base


TARGET_HEADERS: tuple[str, ...] = (
    "医嘱名称",
    "单次剂量",
    "剂量单位",
    "频次",
    "总量",
    "总量单位",
)

MARK_COLOR = (235, 32, 32)
MARK_WIDTH = 4
OCR_MIN_SCORE = 0.45


@dataclass(frozen=True)
class OcrItem:
    text: str
    score: float
    left: int
    top: int
    right: int
    bottom: int
    raw_text: str = ""

    @property
    def center_x(self) -> float:
        return (self.left + self.right) / 2

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass(frozen=True)
class HeaderMatch:
    target: str
    item: OcrItem
    match_score: float


def maximize_his_window() -> None:
    """在所有业务操作之前检查并最大化 HIS 窗口。"""

    print(f"正在检查 HIS 窗口是否最大化：{base.WINDOW_TITLE}")
    window = base.activate_his_window()
    try:
        is_maximized = bool(window.is_maximized())
    except Exception:
        # 个别窗口包装器不支持 is_maximized；调用 maximize 本身是幂等操作。
        is_maximized = False

    if is_maximized:
        print("HIS 窗口已经最大化")
        return

    print("HIS 窗口未最大化，正在最大化")
    window.maximize()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            if window.is_maximized():
                break
        except Exception:
            break
        time.sleep(0.15)
    time.sleep(0.6)  # 等待网页跟随新窗口尺寸完成重排。


def create_ocr_engine() -> Any:
    """创建 RapidOCR 引擎；缺少依赖时给出可直接执行的安装命令。"""

    try:
        from rapidocr import RapidOCR
    except ImportError as exc:
        raise RuntimeError(
            "缺少 OCR 依赖。请先在运行本脚本的虚拟环境中执行：\n"
            "    pip install rapidocr onnxruntime"
        ) from exc

    # 默认配置已经包含中英文检测和识别模型。
    return RapidOCR()


def normalize_ocr_text(text: object) -> str:
    """去掉 OCR 常见空白和标点，只保留便于匹配表头的字符。"""

    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", str(text or ""))


def box_to_rect(box: Any, scale: float = 1.0) -> tuple[int, int, int, int]:
    points = list(box)
    xs = [float(point[0]) / scale for point in points]
    ys = [float(point[1]) / scale for point in points]
    return (
        max(0, round(min(xs))),
        max(0, round(min(ys))),
        max(0, round(max(xs))),
        max(0, round(max(ys))),
    )


def parse_ocr_result(result: Any, scale: float) -> list[OcrItem]:
    """兼容 RapidOCR 新版对象返回值及旧版列表返回值。"""

    items: list[OcrItem] = []
    boxes = getattr(result, "boxes", None)
    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)

    if boxes is not None and texts is not None and scores is not None:
        rows: Iterable[tuple[Any, Any, Any]] = zip(boxes, texts, scores)
    else:
        raw = result
        if isinstance(result, tuple) and len(result) == 2:
            raw = result[0]
        rows = [] if raw is None else (
            (row[0], row[1], row[2])
            for row in raw
            if isinstance(row, (list, tuple)) and len(row) >= 3
        )

    for box, text, score in rows:
        clean = normalize_ocr_text(text)
        confidence = float(score)
        if not clean or confidence < OCR_MIN_SCORE:
            continue
        left, top, right, bottom = box_to_rect(box, scale)
        if right <= left or bottom <= top:
            continue
        items.append(
            OcrItem(
                text=clean,
                score=confidence,
                left=left,
                top=top,
                right=right,
                bottom=bottom,
                raw_text=str(text or "").strip(),
            )
        )
    return items


def run_ocr(engine: Any, image_path: Path) -> list[OcrItem]:
    """放大截图后 OCR，最后把坐标换算回原图尺寸。"""

    scale = 2.0
    with Image.open(image_path) as source:
        image = source.convert("RGB")
        enlarged = image.resize(
            (round(image.width * scale), round(image.height * scale)),
            Image.Resampling.LANCZOS,
        )

    # RapidOCR 支持 PIL/ndarray 的版本不完全一致；临时文件方式兼容性最高。
    temporary_path = image_path.with_name(f".{image_path.stem}_ocr_input.png")
    enlarged.save(temporary_path)
    try:
        result = engine(str(temporary_path))
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
    return parse_ocr_result(result, scale)


def locate_table_title(
    items: Sequence[OcrItem],
    table_title: str = "医嘱明细",
    match_threshold: float = 0.72,
) -> OcrItem | None:
    """按配置文字查找表格标题，不再把“医嘱明细”写死。"""

    target = normalize_ocr_text(table_title)
    if not target:
        return None

    exact = [item for item in items if target in item.text]
    if exact:
        return min(exact, key=lambda item: (item.left, item.top))

    fuzzy = [
        item
        for item in items
        if SequenceMatcher(None, item.text, target).ratio() >= match_threshold
    ]
    return min(fuzzy, key=lambda item: (item.left, item.top)) if fuzzy else None


def target_match_score(text: str, target: str) -> float:
    """计算表头匹配分数，并避免把“总量单位”错当成“总量”。"""

    if target == "总量" and "单位" in text:
        return 0.0
    if text == target:
        return 1.0
    if target in text:
        return 0.96
    if text in target and len(text) >= 2:
        return 0.82
    return SequenceMatcher(None, text, target).ratio()


def narrow_merged_item(item: OcrItem, target: str) -> OcrItem:
    """OCR 合并相邻表头时，按目标文本在整行中的比例估算目标子框。"""

    text = item.text
    index = text.find(target)
    if index < 0 or len(text) <= len(target):
        return item
    width = item.right - item.left
    left = item.left + round(width * index / len(text))
    right = item.left + round(width * (index + len(target)) / len(text))
    return OcrItem(target, item.score, left, item.top, right, item.bottom)


def match_header_candidates(
    candidates: Sequence[OcrItem],
    target_headers: Sequence[str],
) -> list[HeaderMatch]:
    """在同一候选区域内匹配配置的列名。"""

    matches: list[HeaderMatch] = []
    used: set[int] = set()
    for target in target_headers:
        ranked: list[tuple[float, int, OcrItem]] = []
        for index, item in enumerate(candidates):
            score = target_match_score(item.text, target)
            combined = score * 0.9 + item.score * 0.1
            ranked.append((combined, index, item))
        if not ranked:
            continue
        combined, index, item = max(ranked, key=lambda entry: entry[0])
        if combined < 0.72 or (index in used and target not in item.text):
            continue
        used.add(index)
        matches.append(
            HeaderMatch(
                target=target,
                item=narrow_merged_item(item, target),
                match_score=combined,
            )
        )
    return matches


def find_best_header_row(
    items: Sequence[OcrItem],
    target_headers: Sequence[str],
    search_top: int,
    search_bottom: int,
    table_right: int,
) -> list[HeaderMatch]:
    """按同一水平行聚类，避免把查询表单里的同名标签误认为表头。"""

    candidates = [
        item
        for item in items
        if search_top <= item.center_y <= search_bottom and item.left < table_right
    ]
    possible = [
        item
        for item in candidates
        if any(target_match_score(item.text, target) >= 0.70 for target in target_headers)
    ]
    best_matches: list[HeaderMatch] = []
    best_key: tuple[int, float, float] = (0, 0.0, 0.0)
    for anchor in possible:
        row = [item for item in candidates if abs(item.center_y - anchor.center_y) <= 22]
        matches = match_header_candidates(row, target_headers)
        key = (
            len(matches),
            sum(match.match_score for match in matches),
            -anchor.center_y,
        )
        if key > best_key:
            best_key = key
            best_matches = matches
    return best_matches


def find_header_matches(
    items: Sequence[OcrItem],
    image_width: int,
    target_headers: Sequence[str] = TARGET_HEADERS,
    table_title: str | None = "医嘱明细",
    table_right_ratio: float = 0.74,
    header_search_height: int = 90,
) -> tuple[list[HeaderMatch], OcrItem]:
    if not 0.2 <= table_right_ratio <= 1.0:
        raise RuntimeError("主表右边界比例必须在 0.2～1.0 之间")
    if not 30 <= header_search_height <= 2000:
        raise RuntimeError("表头向下查找范围必须在 30～2000 像素之间")

    main_table_right = round(image_width * table_right_ratio)
    configured_title = normalize_ocr_text(table_title or "")
    if configured_title:
        title = locate_table_title(items, configured_title)
        if title is None:
            raise RuntimeError(
                f"OCR 未找到配置的表格标题“{table_title}”；可修改标题，或把定位方式改为按目标列自动定位"
            )
        search_top = max(0, title.bottom - 5)
        search_bottom = title.bottom + header_search_height
    else:
        # 没有独立标题的表格，直接在整张图片里寻找目标列最集中的水平行。
        title = OcrItem("自动定位", 1.0, 0, 0, main_table_right, 0)
        search_top = 0
        search_bottom = 100_000

    matches = find_best_header_row(
        items,
        target_headers,
        search_top,
        search_bottom,
        main_table_right,
    )
    if not matches:
        title_hint = f"表格标题“{table_title}”下方" if configured_title else "整张截图中"
        raise RuntimeError(
            f"OCR 在{title_hint}未找到目标列：{', '.join(target_headers)}"
        )
    return matches, title


def neutral_grid_score(image: Image.Image, x: int, top: int, bottom: int) -> float:
    """计算某条 x 位置作为浅灰色表格竖线的可能性。"""

    pixels = image.load()
    hits = 0
    samples = 0
    step = max(1, (bottom - top) // 180)
    for y in range(top, bottom + 1, step):
        red, green, blue = pixels[x, y]
        brightness = (red + green + blue) / 3
        samples += 1
        if 175 <= brightness <= 242 and max(red, green, blue) - min(red, green, blue) <= 18:
            hits += 1
    return hits / max(1, samples)


def find_vertical_grid_lines(
    image: Image.Image,
    header_top: int,
    data_bottom: int,
    main_table_right: int,
) -> list[int]:
    """从表头到末行的连续浅灰竖线中提取列边界。"""

    raw: list[int] = []
    top = max(0, header_top)
    bottom = min(image.height - 1, data_bottom)
    for x in range(5, min(image.width - 5, main_table_right)):
        if neutral_grid_score(image, x, top, bottom) >= 0.58:
            raw.append(x)

    groups: list[list[int]] = []
    for x in raw:
        if not groups or x - groups[-1][-1] > 2:
            groups.append([x])
        else:
            groups[-1].append(x)
    return [round(sum(group) / len(group)) for group in groups]


def infer_data_bottom(
    items: Sequence[OcrItem],
    header_bottom: int,
    image_height: int,
    main_table_right: int,
) -> int:
    """以最后一行 OCR 数据的底部确定红框高度。"""

    data_items = [
        item
        for item in items
        if item.top > header_bottom
        and item.left < main_table_right
        and item.bottom < image_height - 105
    ]
    if not data_items:
        return min(image_height - 110, header_bottom + 42)
    last_text_bottom = max(item.bottom for item in data_items)
    # HIS 默认行高约 34px；向下补齐到该行底部，但不进入分页栏。
    return min(image_height - 110, last_text_bottom + 15)


def column_edges(
    item: OcrItem,
    grid_lines: Sequence[int],
    main_table_right: int,
) -> tuple[int, int]:
    """用表头文字两侧最近的表格竖线确定完整列宽。"""

    # 网格线候选已经要求在整段表头/数据区内保持连续，因此这里直接采用
    # OCR 文字框外侧最近的两条线。不能再按“文字距边框至少 4px”过滤：
    # HIS 的窄列标题本来就可能只离边框 2~3px，过滤后会错误跳到上一列，
    # 造成“单次剂量”连同“停止时间”等相邻列一起被框住。
    left_lines = [x for x in grid_lines if x <= item.left]
    right_lines = [x for x in grid_lines if x >= item.right]
    left = max(left_lines) if left_lines else max(0, item.left - 10)
    right = min(right_lines) if right_lines else min(main_table_right, item.right + 10)
    return left, right


def annotate_screenshot(
    engine: Any,
    source_path: Path,
    target_headers: Sequence[str] = TARGET_HEADERS,
    table_title: str | None = "医嘱明细",
    table_right_ratio: float = 0.74,
    header_search_height: int = 90,
) -> tuple[Path, list[str]]:
    """识别可见目标列，给整列可见数据区画框并另存图片。"""

    items = run_ocr(engine, source_path)
    with Image.open(source_path) as source:
        image = source.convert("RGB")

    matches, _title = find_header_matches(
        items,
        image.width,
        target_headers,
        table_title,
        table_right_ratio,
        header_search_height,
    )
    main_table_right = round(image.width * table_right_ratio)
    header_top = max(0, min(match.item.top for match in matches) - 8)
    header_bottom = max((match.item.bottom for match in matches), default=header_top + 25) + 7
    data_bottom = infer_data_bottom(items, header_bottom, image.height, main_table_right)
    grid_lines = find_vertical_grid_lines(
        image, header_top, data_bottom, main_table_right
    )

    draw = ImageDraw.Draw(image)
    found: list[str] = []
    for match in sorted(matches, key=lambda entry: entry.item.left):
        left, right = column_edges(match.item, grid_lines, main_table_right)
        draw.rectangle(
            (left + 1, header_top, right - 1, data_bottom),
            outline=MARK_COLOR,
            width=MARK_WIDTH,
        )
        found.append(match.target)

    marked_path = source_path.with_name(f"{source_path.stem}_marked.png")
    image.save(marked_path)
    return marked_path, found


def is_raw_run_screenshot(path: Path) -> bool:
    name = path.name
    number = re.escape(base.HOSPITALIZATION_NUMBER)
    return bool(
        re.fullmatch(
            rf"his_inpatient_{number}_(?:current|rightmost)_\d{{8}}_\d{{6}}\.png",
            name,
            flags=re.IGNORECASE,
        )
    )


def new_run_screenshots(before: set[Path]) -> list[Path]:
    """只返回本次原脚本新生成的原始截图。"""

    after = {
        path.resolve()
        for path in base.OUTPUT_DIR.glob("*.png")
        if is_raw_run_screenshot(path)
    }
    created = after - before
    order = {"current": 0, "rightmost": 1}

    def sort_key(path: Path) -> tuple[int, str]:
        position = "rightmost" if "_rightmost_" in path.name else "current"
        return order[position], path.name

    return sorted(created, key=sort_key)


def main() -> int:
    try:
        # 按用户要求：脚本一开始先处理最大化，再做任何查询或截图。
        maximize_his_window()
        engine = create_ocr_engine()

        base.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        before = {
            path.resolve()
            for path in base.OUTPUT_DIR.glob("*.png")
            if is_raw_run_screenshot(path)
        }

        result = base.main()
        if result != 0:
            return result

        screenshots = new_run_screenshots(before)
        if not screenshots:
            raise RuntimeError("原流程执行成功，但没有找到本次新生成的截图")

        all_found: set[str] = set()
        print("\n开始 OCR 识别并标框")
        for screenshot in screenshots:
            marked_path, found = annotate_screenshot(engine, screenshot)
            all_found.update(found)
            print(f"原始截图：{screenshot}")
            print(f"标框截图：{marked_path}")
            print(f"本图识别字段：{', '.join(found) if found else '无'}")

        missing = [target for target in TARGET_HEADERS if target not in all_found]
        if missing:
            print(
                "提示：以下字段未出现在当前/最右端两张截图的可见区域，"
                f"因此未标框：{', '.join(missing)}"
            )
        else:
            print("六个目标字段均已识别并标框")
        return 0
    except Exception as exc:
        print(f"执行失败：{exc}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
