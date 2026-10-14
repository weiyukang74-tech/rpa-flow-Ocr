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

import numpy as np
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
OCR_WEAK_MIN_SCORE = 0.20


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
            "OCR 运行库加载失败。此 EXE 已内置 OCR，无需安装 Python 或执行 pip。\n"
            f"底层错误：{type(exc).__name__}: {exc}"
        ) from exc

    # RapidOCR 2.0.x passes pathlib.Path objects to older ONNX Runtime
    # releases. Those releases only accept strings, so normalize the model
    # path before the engine is constructed. This keeps the Windows 7 build
    # on the last ONNX Runtime generation that does not require newer APIs.
    try:
        from rapidocr.inference_engine import onnxruntime as rapidocr_ort

        original_session = rapidocr_ort.InferenceSession
        if not getattr(original_session, "_rpa_path_compatible", False):
            def compatible_session(path_or_bytes: Any, *args: Any, **kwargs: Any) -> Any:
                if isinstance(path_or_bytes, Path):
                    path_or_bytes = str(path_or_bytes)
                return original_session(path_or_bytes, *args, **kwargs)

            compatible_session._rpa_path_compatible = True
            rapidocr_ort.InferenceSession = compatible_session
    except (ImportError, AttributeError):
        pass

    # 默认配置已经包含中英文检测和识别模型。
    try:
        return RapidOCR(
            params={
                "Global.text_score": OCR_WEAK_MIN_SCORE,
                "Global.use_cls": False,
            }
        )
    except (TypeError, ValueError):
        # Older compatible releases may not accept the structured parameter
        # dictionary; call-time filtering in run_ocr remains available.
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


def parse_ocr_result(
    result: Any,
    scale: float,
    min_score: float = OCR_MIN_SCORE,
) -> list[OcrItem]:
    """兼容 RapidOCR 新版对象返回值及旧版列表返回值。"""

    items: list[OcrItem] = []
    boxes = getattr(result, "boxes", None)
    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)

    if boxes is not None and texts is not None and scores is not None:
        rows: Iterable[tuple[Any, Any, Any]] = zip(boxes, texts, scores)
    elif any(value is not None for value in (boxes, texts, scores)) or hasattr(
        result, "to_json"
    ):
        # RapidOCR 3.x returns a RapidOCROutput dataclass.  An empty OCR
        # result legitimately has one or more of boxes/txts/scores set to
        # None; it must be treated as no rows rather than iterated as a list.
        rows = ()
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
        if not clean or confidence < min_score:
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


def visual_text_segments(
    image: Image.Image,
    item: OcrItem,
) -> list[tuple[int, int, int, int]]:
    """Split a suspiciously wide OCR line at large visual text gaps."""

    text = normalize_ocr_text(item.text)
    width = item.right - item.left
    height = item.bottom - item.top
    if len(text) < 6 or height < 8 or width < max(80, height * 5):
        return []

    padding_y = max(2, round(height * 0.15))
    crop_left = max(0, item.left)
    crop_top = max(0, item.top - padding_y)
    crop_right = min(image.width, item.right)
    crop_bottom = min(image.height, item.bottom + padding_y)
    if crop_right <= crop_left or crop_bottom <= crop_top:
        return []

    pixels = np.asarray(
        image.crop((crop_left, crop_top, crop_right, crop_bottom)).convert("RGB"),
        dtype=np.int16,
    )
    background = np.median(pixels.reshape(-1, 3), axis=0)
    color_distance = np.max(np.abs(pixels - background), axis=2)
    active_columns = (
        np.count_nonzero(color_distance >= 28, axis=0)
        >= max(2, round(pixels.shape[0] * 0.10))
    )
    active_indexes = np.flatnonzero(active_columns)
    if active_indexes.size < 2:
        return []

    first_active = int(active_indexes[0])
    last_active = int(active_indexes[-1]) + 1
    minimum_gap = max(7, round(height * 0.45))
    gaps: list[tuple[int, int]] = []
    gap_start: int | None = None
    for column in range(first_active, last_active):
        if not active_columns[column] and gap_start is None:
            gap_start = column
        elif active_columns[column] and gap_start is not None:
            if column - gap_start >= minimum_gap:
                gaps.append((gap_start, column))
            gap_start = None
    if gap_start is not None and last_active - gap_start >= minimum_gap:
        gaps.append((gap_start, last_active))
    if not gaps:
        return []

    cuts = [(left + right) // 2 for left, right in gaps]
    local_ranges: list[tuple[int, int]] = []
    start = first_active
    for cut in cuts:
        local_ranges.append((start, cut))
        start = cut
    local_ranges.append((start, last_active))
    minimum_segment_width = max(14, round(height * 1.15))
    if (
        len(local_ranges) < 2
        or len(local_ranges) > 8
        or any(right - left < minimum_segment_width for left, right in local_ranges)
    ):
        return []

    return [
        (
            crop_left + left,
            item.top,
            crop_left + right,
            item.bottom,
        )
        for left, right in local_ranges
    ]


def partition_ocr_text(item: OcrItem, widths: Sequence[int]) -> list[str]:
    raw_parts = [
        normalize_ocr_text(part)
        for part in re.split(r"\s+", item.raw_text.strip())
        if normalize_ocr_text(part)
    ]
    if len(raw_parts) == len(widths):
        return raw_parts

    text = normalize_ocr_text(item.text)
    if len(text) < len(widths):
        return []
    total_width = max(1, sum(widths))
    boundaries = [0]
    for index in range(1, len(widths)):
        target = round(sum(widths[:index]) * len(text) / total_width)
        target = max(boundaries[-1] + 1, target)
        target = min(len(text) - (len(widths) - index), target)
        boundaries.append(target)
    boundaries.append(len(text))
    return [
        text[boundaries[index] : boundaries[index + 1]]
        for index in range(len(widths))
    ]


def merge_segment_items(items: Sequence[OcrItem]) -> OcrItem:
    ordered = sorted(items, key=lambda item: (item.left, item.top))
    merged_text = normalize_ocr_text(ordered[0].text)
    merged_raw = ordered[0].raw_text or ordered[0].text
    for item in ordered[1:]:
        next_text = normalize_ocr_text(item.text)
        overlap = 0
        for size in range(min(len(merged_text), len(next_text)), 0, -1):
            if merged_text[-size:] == next_text[:size]:
                overlap = size
                break
        merged_text += next_text[overlap:]
        merged_raw += next_text[overlap:]
    return OcrItem(
        text=merged_text,
        score=min(item.score for item in ordered),
        left=min(item.left for item in ordered),
        top=min(item.top for item in ordered),
        right=max(item.right for item in ordered),
        bottom=max(item.bottom for item in ordered),
        raw_text=merged_raw,
    )


def split_merged_ocr_items(
    engine: Any,
    image: Image.Image,
    items: Sequence[OcrItem],
    min_score: float,
    use_cls: bool,
) -> list[OcrItem]:
    split_items: list[OcrItem] = []
    for item in items:
        segments = visual_text_segments(image, item)
        if not segments:
            split_items.append(item)
            continue

        recognized: list[OcrItem] = []
        for left, top, right, bottom in segments:
            padding_x = max(1, round((bottom - top) * 0.12))
            crop_left = max(0, left - padding_x)
            crop_right = min(image.width, right + padding_x)
            crop_top = max(0, top - 2)
            crop_bottom = min(image.height, bottom + 2)
            crop = image.crop((crop_left, crop_top, crop_right, crop_bottom))
            local_items = run_ocr(
                engine,
                crop,
                min_score=min_score,
                use_cls=use_cls,
                split_merged=False,
            )
            if not local_items:
                recognized = []
                break
            recognized.append(
                merge_segment_items(
                    [
                        OcrItem(
                            text=local.text,
                            score=local.score,
                            left=local.left + crop_left,
                            top=local.top + crop_top,
                            right=local.right + crop_left,
                            bottom=local.bottom + crop_top,
                            raw_text=local.raw_text,
                        )
                        for local in local_items
                    ]
                )
            )

        original_text = normalize_ocr_text(item.text)
        recognized_text = "".join(
            normalize_ocr_text(candidate.text) for candidate in recognized
        )
        if (
            len(recognized) >= 2
            and SequenceMatcher(None, original_text, recognized_text).ratio() >= 0.55
        ):
            split_items.extend(recognized)
            continue

        widths = [right - left for left, _top, right, _bottom in segments]
        parts = partition_ocr_text(item, widths)
        if len(parts) != len(segments):
            split_items.append(item)
            continue
        split_items.extend(
            OcrItem(
                text=part,
                score=item.score,
                left=left,
                top=top,
                right=right,
                bottom=bottom,
                raw_text=part,
            )
            for part, (left, top, right, bottom) in zip(parts, segments)
        )
    return split_items


def run_ocr(
    engine: Any,
    image_source: Path | Image.Image | np.ndarray,
    min_score: float = OCR_MIN_SCORE,
    use_cls: bool = False,
    split_merged: bool = True,
) -> list[OcrItem]:
    """Run OCR in memory with an adaptive scale for desktop UI text."""

    if isinstance(image_source, np.ndarray):
        image = Image.fromarray(image_source).convert("RGB")
    elif isinstance(image_source, Image.Image):
        image = image_source.convert("RGB")
    else:
        with Image.open(image_source) as source:
            image = source.convert("RGB")

    # RapidOCR itself limits oversized inputs. Targeting a roughly 2K long
    # edge avoids enlarging a 1920px desktop to 4K only to shrink it again,
    # while smaller windows still receive enough magnification for tiny text.
    longest_side = max(1, image.width, image.height)
    scale = min(2.0, max(1.0, 2000.0 / longest_side))
    if scale > 1.02:
        enlarged = image.resize(
            (round(image.width * scale), round(image.height * scale)),
            Image.Resampling.LANCZOS,
        )
    else:
        enlarged = image
        scale = 1.0
    input_array = np.asarray(enlarged)
    try:
        result = engine(
            input_array,
            text_score=min_score,
            use_cls=use_cls,
        )
    except TypeError:
        # Compatibility with older RapidOCR releases that accept ndarray input
        # but do not expose call-time thresholds.
        result = engine(input_array)
    items = parse_ocr_result(result, scale, min_score)
    if not split_merged:
        return items
    return split_merged_ocr_items(
        engine,
        image,
        items,
        min_score,
        use_cls,
    )


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
