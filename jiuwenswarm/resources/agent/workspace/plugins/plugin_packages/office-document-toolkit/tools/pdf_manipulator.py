import os
from pathlib import Path

from openjiuwen.core.foundation.tool import Tool, ToolCard

from pdf_font_utils import select_pdf_font


class PDFManipulator(Tool):
    """PDF专项操控工具：合并、拆分、压缩、加密、加水印、提取指定页/图片。"""

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="pdf_manipulator",
                name="pdf_manipulator",
                description=(
                    "PDF专项操控工具：合并、拆分、压缩、加密、加水印、"
                    "提取指定页/图片。当用户需要操控PDF文件时调用。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": [
                                "merge",
                                "split",
                                "compress",
                                "encrypt",
                                "watermark",
                                "extract_pages",
                                "extract_images",
                            ],
                            "description": "PDF操作类型",
                        },
                        "file_path": {
                            "type": "string",
                            "description": "主PDF文件路径",
                        },
                        "file_paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "merge操作时的多个PDF文件路径列表",
                        },
                        "options": {
                            "type": "object",
                            "description": (
                                "操作参数：split(page_ranges)、"
                                "encrypt(password)、"
                                "watermark(watermark_text/font_size)、"
                                "extract_pages(page_numbers)"
                            ),
                        },
                        "output_subdir": {
                            "type": "string",
                            "description": "输出子目录名，默认为 pdf_output",
                        },
                    },
                    "required": ["operation"],
                },
            )
        )

    async def invoke(self, inputs, **kwargs):
        operation = inputs.get("operation", "")
        file_path = inputs.get("file_path", "")
        file_paths = inputs.get("file_paths", [])
        options = inputs.get("options", {})
        output_subdir = inputs.get("output_subdir", "pdf_output")

        if not operation:
            return {"success": False, "error": "缺少 operation 参数"}

        from openjiuwen.core.sys_operation.cwd import get_cwd

        base_dir = Path(get_cwd()) / output_subdir
        base_dir.mkdir(parents=True, exist_ok=True)

        try:
            if operation == "merge":
                result = self._merge_pdfs(file_paths, str(base_dir))
            elif operation == "split":
                if not file_path or not os.path.isfile(file_path):
                    return {
                        "success": False,
                        "error": f"文件不存在: {file_path}",
                    }
                result = self._split_pdf(
                    file_path, options, str(base_dir)
                )
            elif operation == "compress":
                if not file_path or not os.path.isfile(file_path):
                    return {
                        "success": False,
                        "error": f"文件不存在: {file_path}",
                    }
                result = self._compress_pdf(file_path, str(base_dir))
            elif operation == "encrypt":
                if not file_path or not os.path.isfile(file_path):
                    return {
                        "success": False,
                        "error": f"文件不存在: {file_path}",
                    }
                result = self._encrypt_pdf(
                    file_path, options, str(base_dir)
                )
            elif operation == "watermark":
                if not file_path or not os.path.isfile(file_path):
                    return {
                        "success": False,
                        "error": f"文件不存在: {file_path}",
                    }
                result = self._watermark_pdf(
                    file_path, options, str(base_dir)
                )
            elif operation == "extract_pages":
                if not file_path or not os.path.isfile(file_path):
                    return {
                        "success": False,
                        "error": f"文件不存在: {file_path}",
                    }
                result = self._extract_pages(
                    file_path, options, str(base_dir)
                )
            elif operation == "extract_images":
                if not file_path or not os.path.isfile(file_path):
                    return {
                        "success": False,
                        "error": f"文件不存在: {file_path}",
                    }
                result = self._extract_images(file_path, str(base_dir))
            else:
                return {
                    "success": False,
                    "error": f"不支持的操作: {operation}",
                }

            if "error" in result:
                return {"success": False, "error": result["error"]}
            return {"success": True, "operation": operation, **result}
        except ImportError as e:
            return {
                "success": False,
                "error": f"依赖库缺失: {e}. 请安装对应依赖后重试。",
            }
        except Exception as e:
            return {"success": False, "error": f"PDF操作失败: {e}"}

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)

    @staticmethod
    def _merge_pdfs(file_paths: list, output_dir: str) -> dict:
        from pypdf import PdfReader, PdfWriter

        if not file_paths:
            return {"error": "merge操作需要 file_paths 参数"}
        valid_paths = [p for p in file_paths if os.path.isfile(p)]
        if not valid_paths:
            return {"error": "没有有效的PDF文件"}

        writer = PdfWriter()
        for path in valid_paths:
            reader = PdfReader(path)
            for page in reader.pages:
                writer.add_page(page)

        output_path = str(Path(output_dir) / "merged.pdf")
        with open(output_path, "wb") as f:
            writer.write(f)
        return {
            "total_files": len(valid_paths),
            "total_pages": len(writer.pages),
            "path": output_path,
            "exists": os.path.isfile(output_path),
            "size_bytes": (
                os.path.getsize(output_path)
                if os.path.isfile(output_path)
                else 0
            ),
        }

    @staticmethod
    def _split_pdf(
        file_path: str, options: dict, output_dir: str
    ) -> dict:
        from pypdf import PdfReader, PdfWriter

        page_ranges = options.get("page_ranges", [])
        reader = PdfReader(file_path)
        total_pages = len(reader.pages)

        if not page_ranges:
            page_ranges = [
                [i + 1, i + 1] for i in range(total_pages)
            ]

        output_files = []
        for i, (start, end) in enumerate(page_ranges):
            writer = PdfWriter()
            for page_num in range(
                start - 1, min(end, total_pages)
            ):
                writer.add_page(reader.pages[page_num])
            output_path = str(
                Path(output_dir)
                / f"split_{i + 1}_pages_{start}-{end}.pdf"
            )
            with open(output_path, "wb") as f:
                writer.write(f)
            output_files.append(
                {
                    "file": Path(output_path).name,
                    "pages": f"{start}-{end}",
                    "size_bytes": os.path.getsize(output_path),
                }
            )

        return {
            "total_pages": total_pages,
            "split_files": len(output_files),
            "files": output_files,
        }

    @staticmethod
    def _compress_pdf(file_path: str, output_dir: str) -> dict:
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(file_path)
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        for page in writer.pages:
            page.compress_content_streams()

        output_path = str(Path(output_dir) / "compressed.pdf")
        with open(output_path, "wb") as f:
            writer.write(f)

        original_size = os.path.getsize(file_path)
        compressed_size = os.path.getsize(output_path)
        return {
            "original_size": original_size,
            "compressed_size": compressed_size,
            "reduction": (
                f"{(1 - compressed_size / original_size) * 100:.1f}%"
                if original_size > 0
                else "0%"
            ),
            "path": output_path,
            "exists": True,
            "size_bytes": compressed_size,
        }

    @staticmethod
    def _encrypt_pdf(
        file_path: str, options: dict, output_dir: str
    ) -> dict:
        from pypdf import PdfReader, PdfWriter

        password = options.get("password", "")
        if not password:
            return {"error": "encrypt操作需要 password 参数"}

        reader = PdfReader(file_path)
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        writer.encrypt(password)

        output_path = str(Path(output_dir) / "encrypted.pdf")
        with open(output_path, "wb") as f:
            writer.write(f)
        return {
            "path": output_path,
            "exists": os.path.isfile(output_path),
            "size_bytes": (
                os.path.getsize(output_path)
                if os.path.isfile(output_path)
                else 0
            ),
            "encrypted": True,
        }

    @staticmethod
    def _watermark_pdf(
        file_path: str, options: dict, output_dir: str
    ) -> dict:
        from fpdf import FPDF
        from pypdf import PdfReader, PdfWriter

        watermark_text = options.get("watermark_text", "WATERMARK")
        font_size = options.get("font_size", 50)

        reader = PdfReader(file_path)
        page = reader.pages[0]
        page_width = float(page.mediabox.width)
        page_height = float(page.mediabox.height)

        wm_pdf = FPDF(unit="pt", format=(page_width, page_height))
        wm_pdf.add_page()
        font_name = select_pdf_font(wm_pdf)
        wm_pdf.set_font(font_name, "", font_size)
        wm_pdf.set_text_color(200, 200, 200)
        wm_pdf.set_xy(0, page_height / 2)
        wm_pdf.cell(page_width, font_size, watermark_text, align="C")

        wm_path = str(Path(output_dir) / "_watermark_temp.pdf")
        wm_pdf.output(wm_path)

        wm_reader = PdfReader(wm_path)
        wm_page = wm_reader.pages[0]
        writer = PdfWriter()
        for page in reader.pages:
            page.merge_page(wm_page)
            writer.add_page(page)

        output_path = str(Path(output_dir) / "watermarked.pdf")
        with open(output_path, "wb") as f:
            writer.write(f)

        if os.path.isfile(wm_path):
            os.remove(wm_path)

        return {
            "watermark_text": watermark_text,
            "total_pages": len(writer.pages),
            "path": output_path,
            "exists": os.path.isfile(output_path),
            "size_bytes": (
                os.path.getsize(output_path)
                if os.path.isfile(output_path)
                else 0
            ),
        }

    @staticmethod
    def _extract_pages(
        file_path: str, options: dict, output_dir: str
    ) -> dict:
        from pypdf import PdfReader, PdfWriter

        page_numbers = options.get("page_numbers", [])
        reader = PdfReader(file_path)
        total_pages = len(reader.pages)

        if not page_numbers:
            return {"error": "extract_pages需要 page_numbers 参数"}

        writer = PdfWriter()
        for num in page_numbers:
            if 1 <= num <= total_pages:
                writer.add_page(reader.pages[num - 1])

        output_path = str(Path(output_dir) / "extracted_pages.pdf")
        with open(output_path, "wb") as f:
            writer.write(f)
        return {
            "total_pages": total_pages,
            "extracted_pages": len(writer.pages),
            "page_numbers": page_numbers,
            "path": output_path,
            "exists": os.path.isfile(output_path),
            "size_bytes": (
                os.path.getsize(output_path)
                if os.path.isfile(output_path)
                else 0
            ),
        }

    @staticmethod
    def _extract_images(file_path: str, output_dir: str) -> dict:
        import pypdf

        reader = pypdf.PdfReader(file_path)
        extracted = []
        img_count = 0

        for page_num, page in enumerate(reader.pages):
            resources = page.get("/Resources", {})
            if "/XObject" in resources:
                x_object = resources["/XObject"].get_object()
                for obj_name in x_object:
                    obj = x_object[obj_name]
                    if obj.get("/Subtype") == "/Image":
                        img_count += 1
                        img_data = obj.get_data()
                        ext = obj.get("/Filter", "")
                        if ext == "/DCTDecode":
                            img_ext = ".jpg"
                        elif ext == "/FlateDecode":
                            img_ext = ".png"
                        else:
                            img_ext = ".bin"
                        img_path = str(
                            Path(output_dir)
                            / f"page{page_num + 1}_img{img_count}{img_ext}"
                        )
                        with open(img_path, "wb") as f:
                            f.write(img_data)
                        extracted.append(
                            {
                                "page": page_num + 1,
                                "image": Path(img_path).name,
                                "size_bytes": os.path.getsize(img_path),
                            }
                        )

        return {
            "total_images": len(extracted),
            "images": extracted,
        }
