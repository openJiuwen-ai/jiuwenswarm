from openjiuwen.core.foundation.tool import Tool, ToolCard


class CitationFormatter(Tool):
    """Format citation entries according to GB/T 7714, APA, MLA, or simple inline style."""

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="citation_formatter",
                name="citation_formatter",
                description=(
                    "Format citation entries according to specified style "
                    "(GB/T 7714, APA, MLA, or simple inline). "
                    "Use when adding citations to a manuscript and needing "
                    "standardized, error-free formatting. Supports journal "
                    "articles, books, web pages, reports, and conference papers."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "citations": {
                            "type": "array",
                            "description": "Citation entries to format",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "author": {
                                        "type": "string",
                                        "description": "Author name(s), e.g., '张三' or 'Smith, J.'",
                                    },
                                    "title": {
                                        "type": "string",
                                        "description": "Work title",
                                    },
                                    "year": {
                                        "type": "string",
                                        "description": "Publication year",
                                    },
                                    "source": {
                                        "type": "string",
                                        "description": "Journal name, website name, or report source",
                                    },
                                    "volume": {
                                        "type": "string",
                                        "description": "Volume number (journals)",
                                    },
                                    "issue": {
                                        "type": "string",
                                        "description": "Issue number (journals)",
                                    },
                                    "pages": {
                                        "type": "string",
                                        "description": "Page range, e.g., '12-25'",
                                    },
                                    "publisher": {
                                        "type": "string",
                                        "description": "Publisher name (books)",
                                    },
                                    "place": {
                                        "type": "string",
                                        "description": "Publication place (books)",
                                    },
                                    "url": {
                                        "type": "string",
                                        "description": "URL (web sources)",
                                    },
                                    "access_date": {
                                        "type": "string",
                                        "description": "Access date for web sources (YYYY-MM-DD)",
                                    },
                                    "source_type": {
                                        "type": "string",
                                        "description": "Source type: journal, book, web, report, conference",
                                    },
                                },
                                "required": ["author", "title", "year"],
                            },
                        },
                        "style": {
                            "type": "string",
                            "description": "Citation style: gbt7714, apa, mla, or inline",
                        },
                    },
                    "required": ["citations", "style"],
                },
            )
        )

    async def invoke(self, inputs, **kwargs):
        try:
            citations = inputs.get("citations", [])
            style = inputs.get("style", "inline")

            valid_styles = ("gbt7714", "apa", "mla", "inline")
            if style not in valid_styles:
                return {
                    "success": False,
                    "error": f"Unknown style: {style}. Supported: {', '.join(valid_styles)}",
                }

            results = []
            for i, cite in enumerate(citations, 1):
                formatted = self._format_single(cite, style, i)
                results.append(formatted)

            return {
                "success": True,
                "formatted_citations": results,
                "style": style,
                "count": len(results),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)

    # ------------------------------------------------------------------ #
    #  Dispatch
    # ------------------------------------------------------------------ #

    def _format_single(self, cite, style, index):
        author = cite.get("author", "")
        title = cite.get("title", "")
        year = cite.get("year", "")
        source = cite.get("source", "")
        volume = cite.get("volume", "")
        issue = cite.get("issue", "")
        pages = cite.get("pages", "")
        publisher = cite.get("publisher", "")
        place = cite.get("place", "")
        url = cite.get("url", "")
        access_date = cite.get("access_date", "")
        source_type = cite.get("source_type", "")

        if style == "gbt7714":
            return self._fmt_gbt7714(cite, index)
        elif style == "apa":
            return self._fmt_apa(cite)
        elif style == "mla":
            return self._fmt_mla(cite)
        else:
            return self._fmt_inline(author, title, year, source)

    # ------------------------------------------------------------------ #
    #  GB/T 7714
    # ------------------------------------------------------------------ #

    def _fmt_gbt7714(self, cite, index):
        author = cite.get("author", "")
        title = cite.get("title", "")
        year = cite.get("year", "")
        source = cite.get("source", "")
        volume = cite.get("volume", "")
        issue = cite.get("issue", "")
        pages = cite.get("pages", "")
        publisher = cite.get("publisher", "")
        place = cite.get("place", "")
        url = cite.get("url", "")
        access_date = cite.get("access_date", "")
        source_type = cite.get("source_type", "")
        type_map = {
            "journal": "[J]",
            "book": "[M]",
            "web": "[EB/OL]",
            "report": "[R]",
            "conference": "[C]",
        }
        type_marker = type_map.get(source_type, "")

        parts = [f"[{index}] {author}. {title}"]

        if type_marker:
            parts.append(type_marker)

        if source_type == "journal":
            seg = source or ""
            if year:
                seg += f", {year}" if seg else year
            if volume:
                seg += f", {volume}" if seg else volume
            if issue:
                seg += f"({issue})"
            if seg:
                parts.append(seg)
            if pages:
                parts.append(f": {pages}")
        elif source_type in ("book", "report", "conference"):
            seg = ""
            if place:
                seg = place
            if publisher:
                seg += f": {publisher}" if seg else publisher
            if year:
                seg += f", {year}" if seg else year
            if seg:
                parts.append(seg)
        elif source_type == "web":
            if url:
                parts.append(url)
            if access_date:
                parts.append(f"[{access_date}]")
        else:
            if year:
                parts.append(year)
            if source:
                parts.append(source)

        return ". ".join(parts) + "."

    # ------------------------------------------------------------------ #
    #  APA
    # ------------------------------------------------------------------ #

    def _fmt_apa(self, cite):
        author = cite.get("author", "")
        title = cite.get("title", "")
        year = cite.get("year", "")
        source = cite.get("source", "")
        volume = cite.get("volume", "")
        issue = cite.get("issue", "")
        pages = cite.get("pages", "")
        publisher = cite.get("publisher", "")
        url = cite.get("url", "")
        access_date = cite.get("access_date", "")
        source_type = cite.get("source_type", "")
        if source_type == "journal":
            r = f"{author} ({year}). {title}. {source}"
            if volume:
                r += f", {volume}"
                if issue:
                    r += f"({issue})"
            if pages:
                r += f", {pages}"
            return r + "."
        elif source_type == "book":
            r = f"{author} ({year}). {title}."
            if publisher:
                r += f" {publisher}."
            return r
        elif source_type == "web":
            r = f"{author} ({year}). {title}."
            if source:
                r += f" {source}."
            if url:
                r += f" {url}"
            if access_date:
                r += f" Retrieved {access_date}"
            return r
        else:
            r = f"{author} ({year}). {title}."
            if source:
                r += f" {source}."
            return r

    # ------------------------------------------------------------------ #
    #  MLA
    # ------------------------------------------------------------------ #

    def _fmt_mla(self, cite):
        author = cite.get("author", "")
        title = cite.get("title", "")
        year = cite.get("year", "")
        source = cite.get("source", "")
        volume = cite.get("volume", "")
        issue = cite.get("issue", "")
        pages = cite.get("pages", "")
        publisher = cite.get("publisher", "")
        url = cite.get("url", "")
        access_date = cite.get("access_date", "")
        source_type = cite.get("source_type", "")
        if source_type == "journal":
            r = f'"{title}." {source}'
            if volume:
                r += f", vol. {volume}"
            if issue:
                r += f", no. {issue}"
            if year:
                r += f", {year}"
            if pages:
                r += f", pp. {pages}"
            return r + "."
        elif source_type == "book":
            r = f"{author}. {title}."
            if publisher:
                r += f" {publisher}"
            if year:
                r += f", {year}"
            return r + "."
        elif source_type == "web":
            r = f'"{title}."'
            if source:
                r += f" {source}"
            if year:
                r += f", {year}"
            if url:
                r += f", {url}"
            if access_date:
                r += f". Accessed {access_date}"
            return r + "."
        else:
            r = f'"{title}."'
            if source:
                r += f" {source}"
            if year:
                r += f", {year}"
            return r + "."

    # ------------------------------------------------------------------ #
    #  Inline (simple)
    # ------------------------------------------------------------------ #

    def _fmt_inline(self, author, title, year, source):
        parts = []
        if author:
            parts.append(author)
        if title:
            parts.append(title)
        if year:
            parts.append(year)
        if source:
            parts.append(source)
        return " —— ".join(parts)