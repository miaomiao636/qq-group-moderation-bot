"""图像基础能力（T-201）：感知哈希、GIF代表帧、二维码编解码。

零预算方案（决策 D-014）：全部本地计算，不依赖付费服务。
- dHash 8x8 感知哈希：纯 Pillow 实现，用于相同/相似图片判定与缓存键；
- GIF 代表帧：最多抽取 8 帧（PROJECT_CONTEXT 多模态要求）；
- 二维码：zxing-cpp 解码（含生成，供测试）；解码失败返回空——
  微信小程序码通常不可被通用解码器解析，属预期行为，不作为处罚依据。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image

MAX_GIF_FRAMES = 8


def load_image(source: str | bytes | Path) -> Image.Image:
    """从路径或字节加载图片（校验魔数，拒绝非图片内容）。"""
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    if (
        not data[:8] == b"\x89PNG\r\n\x1a\n"
        and data[:3] not in (b"\xff\xd8\xff", b"GIF")
        and data[:2] != b"BM"
    ):
        raise ValueError("非受支持的图片格式（仅接受 JPEG/PNG/GIF/BMP）")
    img = Image.open(BytesIO(data))
    img.load()
    return img


def dhash(img: Image.Image, size: int = 8) -> str:
    """差异感知哈希：缩放到 (size+1)xsize 灰度，比较相邻像素梯度。"""
    gray = img.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    pixels = list(gray.tobytes())
    bits: list[int] = []
    for row in range(size):
        for col in range(size):
            bits.append(
                1 if pixels[row * (size + 1) + col] > pixels[row * (size + 1) + col + 1] else 0
            )
    return "".join(str(b) for b in bits)


def hamming_distance(hash_a: str, hash_b: str) -> int:
    """两个等长哈希的汉明距离；<=10 视为相似图。"""
    if len(hash_a) != len(hash_b):
        raise ValueError("哈希长度不一致")
    return sum(a != b for a, b in zip(hash_a, hash_b, strict=True))


def similar(hash_a: str, hash_b: str, threshold: int = 10) -> bool:
    return hamming_distance(hash_a, hash_b) <= threshold


def extract_frames(img: Image.Image, max_frames: int = MAX_GIF_FRAMES) -> list[Image.Image]:
    """抽取代表帧：静态图返回原图；动图均匀抽样最多 max_frames 帧。"""
    is_animated = getattr(img, "is_animated", False)
    if is_animated:
        total = int(getattr(img, "n_frames", 1))
        if total <= max_frames:
            indexes = list(range(total))
        else:
            step = total / max_frames
            indexes = [min(int(i * step), total - 1) for i in range(max_frames)]
        frames = []
        for idx in indexes:
            img.seek(idx)
            frames.append(img.copy())
        img.seek(0)
        return frames
    return [img]


def image_frames_from_source(source: str | bytes | Path) -> list[Image.Image]:
    return extract_frames(load_image(source))


def decode_qr_codes(img: object) -> list[str]:
    """解码图中的二维码/条码，返回载荷列表；不支持或无码返回空。

    接受 PIL Image 或 zxingcpp 自有 Image 类型。
    """
    try:
        import zxingcpp
    except ImportError:  # pragma: no cover - 环境缺失时降级
        return []
    try:
        results = zxingcpp.read_barcodes(img)
    except Exception:  # noqa: BLE001 - 解码库对异常输入可能抛错
        return []
    return [str(r.text) for r in results]


def encode_qr(payload: str) -> object:
    """生成二维码图片（供测试与白名单基准生成）；返回 zxingcpp 自有 Image。"""
    import zxingcpp

    return zxingcpp.create_barcode(payload, zxingcpp.BarcodeFormat.QRCode)
