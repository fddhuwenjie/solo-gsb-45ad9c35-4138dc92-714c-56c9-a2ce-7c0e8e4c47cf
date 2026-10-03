"""颜色转换工具：HEX <-> HSL、相对亮度、WCAG 对比度。

JS 前端 (static/app.js) 中保留了一份逐行等价的实现，
两端对同一组颜色必须算出相同的 HSL / 对比度结果。
"""
from __future__ import annotations


def normalize_hex(value: str) -> str:
    """把用户输入规范化为 #rrggbb，非法时抛 ValueError。"""
    if not isinstance(value, str):
        raise ValueError("颜色必须是字符串")
    s = value.strip().lower()
    if s.startswith("#"):
        s = s[1:]
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        raise ValueError(f"无法识别的十六进制颜色：{value!r}")
    int(s, 16)  # 校验十六进制字符
    return "#" + s


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    s = normalize_hex(hex_color).lstrip("#")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def rgb_to_hex(r: float, g: float, b: float) -> str:
    def ch(x: float) -> str:
        # floor(v+0.5)，与前端 JS Math.round 的半值向上取整一致
        x = max(0, min(255, int(x + 0.5)))
        return f"{x:02x}"

    return f"#{ch(r)}{ch(g)}{ch(b)}"


def _hue_to_channel(p: float, q: float, t: float) -> float:
    # 运算顺序刻意与前端 app.js 的 hueToChannel 保持逐操作一致，
    # 避免 42.5 这类取整边界上的浮点尾差导致两端重建色相差 1。
    if t < 0:
        t += 1
    if t > 1:
        t -= 1
    if t < 1 / 6:
        return p + (q - p) * 6 * t
    if t < 1 / 2:
        return q
    if t < 2 / 3:
        return p + (q - p) * 6 * (2 / 3 - t)
    return p


def hsl_to_rgb(h: float, s: float, l: float) -> tuple[float, float, float]:
    """h/s/l 均为 0..1。返回 0..255 的浮点 RGB。"""
    # 刻意复刻前端 ((h % 1) + 1) % 1 的双重取模：第二次取模会引入同一个
    # ulp 尾差，保证两端在 42.5 这类取整边界上得到相同的通道值。
    h = (h % 1.0 + 1.0) % 1.0
    if s == 0:
        v = l * 255
        return v, v, v
    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q
    r = _hue_to_channel(p, q, h + 1 / 3)
    g = _hue_to_channel(p, q, h)
    b = _hue_to_channel(p, q, h - 1 / 3)
    return r * 255, g * 255, b * 255


def rgb_to_hsl(r: float, g: float, b: float) -> tuple[float, float, float]:
    """输入 0..255，返回 0..1 的 h/s/l。"""
    r, g, b = r / 255, g / 255, b / 255
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2
    if mx == mn:
        return 0.0, 0.0, l
    d = mx - mn
    s = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
    if mx == r:
        h = (g - b) / d + (6 if g < b else 0)
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    return h / 6, s, l


def hex_to_hsl(hex_color: str) -> tuple[int, int, int]:
    """返回整数形式 H(0-359)、S/L(0-100)，用于界面展示与求解网格。

    取整用 floor(v+0.5)（而非 Python 默认的银行家舍入），
    与前端 JS 的 Math.round 在 .5 边界上保持一致。
    """
    h, s, l = rgb_to_hsl(*hex_to_rgb(hex_color))
    return (
        min(359, int(h * 360 + 0.5)),
        min(100, int(s * 100 + 0.5)),
        min(100, int(l * 100 + 0.5)),
    )


def hsl_to_hex(h: float, s: float, l: float) -> str:
    """h 0..360，s/l 0..100。"""
    return rgb_to_hex(*hsl_to_rgb(h / 360, s / 100, l / 100))


def with_lightness(hex_color: str, lightness: int) -> str:
    """保持色相/饱和度，仅替换明度（整数 0..100）。"""
    h, s, _ = hex_to_hsl(hex_color)
    return hsl_to_hex(h, s, lightness)


def _srgb_channel_to_linear(c: float) -> float:
    c = c / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = (_srgb_channel_to_linear(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def luminance_at(hex_color: str, lightness: int) -> float:
    return relative_luminance(hex_to_rgb(with_lightness(hex_color, lightness)))


def contrast_ratio(rgb1: tuple[int, int, int], rgb2: tuple[int, int, int]) -> float:
    l1, l2 = relative_luminance(rgb1), relative_luminance(rgb2)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def contrast_of_hex(c1: str, c2: str) -> float:
    return contrast_ratio(hex_to_rgb(c1), hex_to_rgb(c2))
