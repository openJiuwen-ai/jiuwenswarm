import os

from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.sys_operation.cwd import get_cwd


class DocumentExporter(Tool):
    """Export a structured document with TOC, citation list, and metadata to a formatted Markdown file."""

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="document_exporter",
                name="document_exporter",
                description=(
                    "Export a structured document with auto-generated table of contents, "
                    "citation list, and YAML metadata to a formatted Markdown file. "
                    "Use when finishing a manuscript and needing a deliverable file with "
                    "proper structure. Section content can include Mermaid blocks, SVG, "
                    "and Markdown tables — they are preserved as-is in the output."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Document title",
                        },
                        "sections": {
                            "type": "array",
                            "description": "Document sections, each with heading, level, and content",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "heading": {
                                        "type": "string",
                                        "description": "Section heading text",
                                    },
                                    "level": {
                                        "type": "integer",
                                        "description": "Heading level (1-6, where 1 is #). Defaults to 2.",
                                    },
                                    "content": {
                                        "type": "string",
                                        "description": (
                                            "Section body text in Markdown. "
                                            "Can include Mermaid code blocks, SVG source, "
                                            "and Markdown tables — preserved as-is."
                                        ),
                                    },
                                },
                                "required": ["heading", "content"],
                            },
                        },
                        "citations": {
                            "type": "array",
                            "description": "Formatted citation strings for the reference list",
                            "items": {"type": "string"},
                        },
                        "metadata": {
                            "type": "object",
                            "description": "Optional document metadata for YAML front matter",
                            "properties": {
                                "author": {"type": "string", "description": "Author name"},
                                "date": {"type": "string", "description": "Date string"},
                                "summary": {"type": "string", "description": "Document summary"},
                                "tags": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Document tags",
                                },
                            },
                        },
                        "output_filename": {
                            "type": "string",
                            "description": (
                                "Output filename (e.g., 'my-article.md'). "
                                "If omitted, a title-based name is generated."
                            ),
                        },
                    },
                    "required": ["title", "sections"],
                },
            )
        )

    async def invoke(self, inputs, **kwargs):
        try:
            title = inputs.get("title", "Untitled")
            sections = inputs.get("sections", [])
            citations = inputs.get("citations", [])
            metadata = inputs.get("metadata", {})
            output_filename = inputs.get("output_filename")

            lines = []

            # --- YAML front matter ---
            if metadata:
                lines.append("---")
                for key, value in metadata.items():
                    if isinstance(value, list):
                        lines.append(f"{key}: [{', '.join(value)}]")
                    else:
                        lines.append(f"{key}: {value}")
                lines.append("---")
                lines.append("")

            # --- Title ---
            lines.append(f"# {title}")
            lines.append("")

            # --- Table of contents ---
            if sections:
                lines.append("## 目录")
                lines.append("")
                for section in sections:
                    heading = section.get("heading", "")
                    level = section.get("level", 2)
                    indent = "  " * max(0, level - 1)
                    anchor = self._make_anchor(heading)
                    lines.append(f"{indent}- [{heading}](#{anchor})")
                lines.append("")

            # --- Sections ---
            for section in sections:
                heading = section.get("heading", "")
                level = section.get("level", 2)
                content = section.get("content", "")
                prefix = "#" * max(1, min(level, 6))
                lines.append(f"{prefix} {heading}")
                lines.append("")
                lines.append(content)
                lines.append("")

            # --- Reference list ---
            if citations:
                lines.append("## 参考文献")
                lines.append("")
                for i, citation in enumerate(citations, 1):
                    lines.append(f"{i}. {citation}")
                lines.append("")

            document = "\n".join(lines)

            # --- Write file ---
            cwd = get_cwd()
            output_dir = os.path.join(str(cwd), "exports")
            os.makedirs(output_dir, exist_ok=True)

            if not output_filename:
                safe_title = self._sanitize_filename(title)[:50]
                output_filename = f"{safe_title}.md"

            if not output_filename.endswith(".md"):
                output_filename += ".md"

            filepath = os.path.join(output_dir, output_filename)

            with open(filepath, "w", encoding="utf-8") as f:
                f.write(document)

            # --- Stats ---
            char_count = len(document.replace(" ", "").replace("\n", ""))
            section_count = len(sections)
            citation_count = len(citations)

            return {
                "success": True,
                "filepath": filepath,
                "stats": {
                    "char_count": char_count,
                    "section_count": section_count,
                    "citation_count": citation_count,
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)

    @staticmethod
    def _make_anchor(heading: str) -> str:
        anchor = heading.lower().replace(" ", "-")
        return "".join(c for c in anchor if c.isalnum() or c == "-")

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        safe = name.replace(" ", "-")
        return "".join(c for c in safe if c.isalnum() or c == "-")