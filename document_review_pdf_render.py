"""Bounded local PDF rasterization for OCR; original documents are never changed.

PDFium's native API is not thread-safe, including across documents. Every call
and explicit close is protected by the same lock. Gray PNG encoding uses the
standard library, so portable builds need neither Pillow nor NumPy.
"""
from contextlib import contextmanager
import math
import struct
import threading
import zlib


class PDFRenderError(ValueError):
    pass


class PDFRendererUnavailable(PDFRenderError):
    pass


_PDFIUM_LOCK = threading.RLock()


def _dimensions(width, height, scale, max_pixels, max_dimension):
    if any(not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0
           for value in (width, height, scale)):
        raise PDFRenderError("PDF 页面尺寸无效，无法安全渲染")
    if not math.isfinite(width * scale) or not math.isfinite(height * scale):
        raise PDFRenderError("PDF 页面尺寸超过安全渲染范围")
    pixels = (math.ceil(width * scale), math.ceil(height * scale))
    if pixels[0] > max_dimension or pixels[1] > max_dimension or pixels[0] * pixels[1] > max_pixels:
        raise PDFRenderError("PDF 页面超过 OCR 安全渲染尺寸或像素上限，请缩小页面后重试")
    return pixels


def _gray_png(buffer, width, height, stride):
    if stride < width or len(buffer) < stride * height:
        raise PDFRenderError("PDF 渲染位图结构无效")
    view = memoryview(buffer).cast("B")
    compressor = zlib.compressobj(3)
    compressed = bytearray()
    for row in range(height):
        compressed.extend(compressor.compress(b"\x00"))
        compressed.extend(compressor.compress(view[row * stride:row * stride + width]))
    compressed.extend(compressor.flush())

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", bytes(compressed)) + chunk(b"IEND", b""))


def renderer_backend():
    try:
        import pypdfium2
        return "pypdfium2", pypdfium2
    except (ImportError, OSError):
        pass
    try:
        import fitz
        return "pymupdf", fitz
    except (ImportError, OSError):
        return None


class _Renderer:
    def __init__(self, name, library, document, max_pixels, max_dimension):
        self.name, self.library, self.document = name, library, document
        self.max_pixels, self.max_dimension = max_pixels, max_dimension
        self.page_count = len(document) if name == "pypdfium2" else document.page_count

    def render_page(self, page_number, *, scale=2.0):
        try:
            return self._render_page(page_number, scale=scale)
        except PDFRenderError:
            raise
        except Exception as error:
            raise PDFRenderError("PDF 页面渲染失败；未接受不完整的 OCR 输入") from error

    def _render_page(self, page_number, *, scale):
        if type(page_number) is not int or not 1 <= page_number <= self.page_count:
            raise PDFRenderError("PDF 渲染页码无效")
        if self.name == "pypdfium2":
            with _PDFIUM_LOCK:
                page = self.document[page_number - 1]
                bitmap = None
                try:
                    _dimensions(*page.get_size(), scale, self.max_pixels, self.max_dimension)
                    bitmap = page.render(scale=scale, grayscale=True, limit_image_cache=True)
                    if bitmap.n_channels != 1:
                        raise PDFRenderError("PDF 渲染器未返回预期的灰度位图")
                    _dimensions(bitmap.width, bitmap.height, 1, self.max_pixels, self.max_dimension)
                    return _gray_png(bitmap.buffer, bitmap.width, bitmap.height, bitmap.stride)
                finally:
                    try:
                        if bitmap is not None:
                            bitmap.close()
                    finally:
                        page.close()
        page = self.document[page_number - 1]
        _dimensions(page.rect.width, page.rect.height, scale, self.max_pixels, self.max_dimension)
        pixmap = page.get_pixmap(matrix=self.library.Matrix(scale, scale), colorspace=self.library.csGRAY, alpha=False)
        try:
            _dimensions(pixmap.width, pixmap.height, 1, self.max_pixels, self.max_dimension)
            return pixmap.tobytes("png")
        finally:
            # PyMuPDF pixmaps own native memory and have no public close().
            del pixmap


@contextmanager
def open_pdf_renderer(data, *, max_pages=250, max_pixels=16_000_000, max_dimension=8192):
    if any(type(value) is not int or value <= 0 for value in (max_pages, max_pixels, max_dimension)):
        raise PDFRenderError("PDF 安全渲染限额必须是正整数")
    backend = renderer_backend()
    if backend is None:
        raise PDFRendererUnavailable("扫描 PDF 缺少页面渲染组件；请安装 pypdfium2 或使用包含该组件的便携包")
    name, library = backend
    document = None
    try:
        try:
            if name == "pypdfium2":
                with _PDFIUM_LOCK:
                    document = library.PdfDocument(data)
                    renderer = _Renderer(name, library, document, max_pixels, max_dimension)
            else:
                document = library.open(stream=data, filetype="pdf")
                if document.is_encrypted:
                    raise PDFRenderError("加密 PDF 未提供密码，不能进行 OCR 渲染")
                renderer = _Renderer(name, library, document, max_pixels, max_dimension)
            if not 0 < renderer.page_count <= max_pages:
                raise PDFRenderError("PDF 页数超过安全渲染限制或没有页面")
        except PDFRenderError:
            raise
        except Exception as error:
            raise PDFRenderError("PDF 页面渲染器无法打开文件") from error
        yield renderer
    finally:
        if document is not None:
            if name == "pypdfium2":
                with _PDFIUM_LOCK:
                    document.close()
            else:
                document.close()


__all__ = ["PDFRenderError", "PDFRendererUnavailable", "open_pdf_renderer", "renderer_backend"]
