import os
from pathlib import Path

from openjiuwen.core.foundation.tool import Tool, ToolCard


class ArchiveTool(Tool):
    """文档打包压缩工具：ZIP打包/解包。"""

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="archive_tool",
                name="archive_tool",
                description=(
                    "文档打包压缩工具：将多个文件打包成ZIP，"
                    "或解压ZIP文件。"
                    "当用户需要打包发送文件或解压收到的压缩包时调用。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["create", "extract"],
                            "description": "操作类型：create(打包) 或 extract(解压)",
                        },
                        "file_paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "create操作时要打包的文件路径列表",
                        },
                        "archive_path": {
                            "type": "string",
                            "description": "extract操作时要解压的ZIP文件路径",
                        },
                        "output_filename": {
                            "type": "string",
                            "description": "create操作时的输出文件名（不含扩展名）",
                        },
                        "output_subdir": {
                            "type": "string",
                            "description": "输出子目录名，默认为 archive_output",
                        },
                    },
                    "required": ["operation"],
                },
            )
        )

    async def invoke(self, inputs, **kwargs):
        operation = inputs.get("operation", "")
        output_subdir = inputs.get("output_subdir", "archive_output")

        if not operation:
            return {"success": False, "error": "缺少 operation 参数"}

        from openjiuwen.core.sys_operation.cwd import get_cwd

        base_dir = Path(get_cwd()) / output_subdir
        base_dir.mkdir(parents=True, exist_ok=True)

        try:
            if operation == "create":
                result = self._create_archive(
                    inputs.get("file_paths", []),
                    inputs.get("output_filename", "archive"),
                    str(base_dir),
                )
            elif operation == "extract":
                archive_path = inputs.get("archive_path", "")
                if not archive_path or not os.path.isfile(
                    archive_path
                ):
                    return {
                        "success": False,
                        "error": f"压缩包不存在: {archive_path}",
                    }
                result = self._extract_archive(
                    archive_path, str(base_dir)
                )
            else:
                return {
                    "success": False,
                    "error": f"不支持的操作: {operation}",
                }

            if "error" in result:
                return {"success": False, "error": result["error"]}
            return {
                "success": True,
                "operation": operation,
                **result,
            }
        except Exception as e:
            return {"success": False, "error": f"操作失败: {e}"}

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)

    @staticmethod
    def _create_archive(
        file_paths: list, output_filename: str, output_dir: str
    ) -> dict:
        import zipfile

        valid_paths = [p for p in file_paths if os.path.isfile(p)]
        if not valid_paths:
            return {"error": "没有有效的文件可打包"}

        output_path = str(
            Path(output_dir) / f"{output_filename}.zip"
        )
        with zipfile.ZipFile(
            output_path, "w", zipfile.ZIP_DEFLATED
        ) as zf:
            for fp in valid_paths:
                arcname = Path(fp).name
                zf.write(fp, arcname)

        return {
            "total_files": len(valid_paths),
            "path": output_path,
            "exists": os.path.isfile(output_path),
            "size_bytes": (
                os.path.getsize(output_path)
                if os.path.isfile(output_path)
                else 0
            ),
        }

    @staticmethod
    def _extract_archive(
        archive_path: str, output_dir: str
    ) -> dict:
        import zipfile

        extract_dir = str(
            Path(output_dir) / Path(archive_path).stem
        )
        Path(extract_dir).mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(extract_dir)
            file_list = zf.namelist()

        return {
            "total_files": len(file_list),
            "extracted_to": extract_dir,
            "files": file_list,
        }
