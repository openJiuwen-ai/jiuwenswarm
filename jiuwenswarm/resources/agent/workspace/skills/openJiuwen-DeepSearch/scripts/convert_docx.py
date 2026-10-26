
import os
import re
import base64
import logging
import tempfile
from pathlib import Path
from copy import deepcopy

import requests
import pypandoc

try:
    import yaml
    YAML_AVAILABLE = True
except Exception:
    YAML_AVAILABLE = False

try:
    from PIL import Image, ImageEnhance
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False


# =========================
# Pandoc
# =========================
def ensure_pandoc():
    try:
        pypandoc.get_pandoc_version()
    except OSError:
        pypandoc.download_pandoc()


# =========================
# Mermaid 配置与清洗
# =========================
DEFAULT_CONFIG = {
    "theme": "base",
    "look": "classic",
    "themeVariables": {
        "background": "#ffffff",
        "primaryTextColor": "#111827",
        "secondaryTextColor": "#111827",
        "tertiaryTextColor": "#111827",
        "lineColor": "#374151",
        "textColor": "#111827",
        "mainBkg": "#ffffff",
        "secondBkg": "#f9fafb",
        "tertiaryColor": "#ffffff",
        "xyChart": {
            "plotColorPalette": "#4338ca, #b91c1c, #047857, #b45309, #6d28d9"
        },
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = deepcopy(v)
    return result


def _extract_frontmatter(code: str):
    m = re.match(r"^\s*---\s*\n(.*?)\n---\s*\n?", code.strip(), flags=re.DOTALL)
    if m:
        return m.group(1), code.strip()[m.end():].strip()
    return "", code.strip()


def _dump_frontmatter(config_dict: dict) -> str:
    if YAML_AVAILABLE:
        text = yaml.safe_dump(
            {"config": config_dict},
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).strip()
        return f"---\n{text}\n---\n"
    # 最小兜底：手写一个稳定配置块
    tv = config_dict.get("themeVariables", {})
    xy = tv.get("xyChart", {})
    return (
        "---\n"
        "config:\n"
        f"  theme: {config_dict.get('theme', 'base')}\n"
        f"  look: {config_dict.get('look', 'classic')}\n"
        "  themeVariables:\n"
        f"    background: '{tv.get('background', '#ffffff')}'\n"
        f"    primaryTextColor: '{tv.get('primaryTextColor', '#111827')}'\n"
        f"    secondaryTextColor: '{tv.get('secondaryTextColor', '#111827')}'\n"
        f"    tertiaryTextColor: '{tv.get('tertiaryTextColor', '#111827')}'\n"
        f"    lineColor: '{tv.get('lineColor', '#374151')}'\n"
        f"    textColor: '{tv.get('textColor', '#111827')}'\n"
        f"    mainBkg: '{tv.get('mainBkg', '#ffffff')}'\n"
        f"    secondBkg: '{tv.get('secondBkg', '#f9fafb')}'\n"
        f"    tertiaryColor: '{tv.get('tertiaryColor', '#ffffff')}'\n"
        "    xyChart:\n"
        f"      plotColorPalette: '{xy.get('plotColorPalette', '#4338ca, #b91c1c, #047857, #b45309, #6d28d9')}'\n"
        "---\n"
    )


def _normalize_xychart_body(body: str) -> str:
    """
    针对 xychart 做保守优化：
    - 不再把 xychart-beta 改成 xychart，避免和服务端版本不兼容
    - 对极大数字的 y-axis 标题做轻量增强
    """
    text = body.strip()

    if not re.search(r"^\s*xychart(?:-beta)?\b", text, flags=re.MULTILINE):
        return text

    y_axis_match = re.search(
        r'^(\s*y-axis\s+)(".*?")\s+(.*?)\s*-->\s*(.*?)\s*$',
        text,
        flags=re.MULTILINE,
    )
    if y_axis_match:
        title = y_axis_match.group(2).strip('"').strip()
        ymax = y_axis_match.group(4)
        try:
            ymax_int = int(str(ymax).replace("_", "").strip())
        except Exception:
            ymax_int = None

        if ymax_int and ymax_int >= 10**12 and title.lower() in {"flop", "ops", "params", "tokens"}:
            better_title = f'"{title} (linear scale)"'
            text = re.sub(
                r'^(\s*y-axis\s+)(".*?")\s+(.*?)\s*-->\s*(.*?)\s*$',
                rf'\1{better_title} \3 --> \4',
                text,
                count=1,
                flags=re.MULTILINE,
            )

    return text


def _build_merged_frontmatter(frontmatter: str, body: str) -> str:
    body = _normalize_xychart_body(body)

    if not frontmatter:
        return _dump_frontmatter(DEFAULT_CONFIG) + body.strip()

    if not YAML_AVAILABLE:
        return f"---\n{frontmatter.strip()}\n---\n{body.strip()}"

    try:
        parsed = yaml.safe_load(frontmatter) or {}
        if not isinstance(parsed, dict):
            parsed = {}
    except Exception as e:
        return _dump_frontmatter(DEFAULT_CONFIG) + body.strip()

    if "config" in parsed and isinstance(parsed["config"], dict):
        existing_config = parsed["config"]
    else:
        # 少数情况下用户直接把 config 项写在根层，尽量兼容
        existing_config = parsed if isinstance(parsed, dict) else {}

    merged_config = _deep_merge(DEFAULT_CONFIG, existing_config)

    # 一些高对比默认值，如果用户没设则保留默认；用户设了就尊重用户
    merged_config.setdefault("theme", "base")
    merged_config.setdefault("look", "classic")
    merged_config.setdefault("themeVariables", {})
    merged_config["themeVariables"].setdefault(
        "xyChart", {"plotColorPalette": "#4338ca, #b91c1c, #047857, #b45309, #6d28d9"}
    )
    merged_config["themeVariables"]["xyChart"].setdefault(
        "plotColorPalette", "#4338ca, #b91c1c, #047857, #b45309, #6d28d9"
    )

    return _dump_frontmatter(merged_config) + body.strip()


def clean_mermaid_code(code: str) -> str:
    code = code.strip()
    frontmatter, body = _extract_frontmatter(code)
    return _build_merged_frontmatter(frontmatter, body).strip()


# =========================
# 图片增强
# =========================
def enhance_image(image_path: str):
    if not PIL_AVAILABLE:
        return

    try:
        img = Image.open(image_path)

        if img.mode in ("RGBA", "LA"):
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            bg.alpha_composite(img.convert("RGBA"))
            img = bg.convert("RGB")
        else:
            img = img.convert("RGB")

        img = img.resize((img.width * 2, img.height * 2), Image.Resampling.LANCZOS)
        img = ImageEnhance.Contrast(img).enhance(1.24)
        img = ImageEnhance.Sharpness(img).enhance(1.35)
        img = ImageEnhance.Color(img).enhance(1.15)

        img.save(image_path, format="PNG", optimize=True)
        img.close()
    except Exception as e:
        logging.error(f"Failed to enhance image: {image_path}")


# =========================
# Mermaid 渲染
# =========================
def _valid_image_response(resp: requests.Response) -> bool:
    content_type = (resp.headers.get("Content-Type") or "").lower()
    if resp.status_code != 200:
        return False
    if not resp.content or len(resp.content) < 100:
        return False
    if content_type and "image/" not in content_type:
        return False
    return True


def _save_failed_source(code: str, output_path: str, extra_text: str = ""):
    failed_src = Path(output_path).with_suffix(".mmd")
    failed_src.write_text(code, encoding="utf-8")
    if extra_text:
        failed_log = Path(output_path).with_suffix(".error.txt")
        failed_log.write_text(extra_text, encoding="utf-8")


def _error_excerpt(resp: requests.Response) -> str:
    content_type = (resp.headers.get("Content-Type") or "").lower()
    excerpt = f"status={resp.status_code}\ncontent-type={content_type}\n"
    try:
        if "text/" in content_type or "json" in content_type or "xml" in content_type:
            excerpt += resp.text[:2000]
        else:
            excerpt += f"binary response, {len(resp.content)} bytes"
    except Exception:
        excerpt += "unable to decode response body"
    return excerpt


def render_mermaid(code: str, output_path: str) -> bool:
    code = clean_mermaid_code(code)
    last_error = ""

    # 方案1：mermaid.ink（白底 + 放大）
    try:
        encoded = base64.urlsafe_b64encode(code.encode("utf-8")).decode("utf-8")
        url = f"https://mermaid.ink/img/{encoded}?bgColor=!white&type=png"
        r = requests.get(
            url,
            timeout=20,
            headers={"Accept": "image/png,image/*;q=0.9,*/*;q=0.8"},
        )
        if _valid_image_response(r):
            with open(output_path, "wb") as f:
                f.write(r.content)
            enhance_image(output_path)
            return True
        last_error = "mermaid.ink\n" + _error_excerpt(r)
    except Exception as e:
        last_error = f"mermaid.ink exception\n{e}"

    # 方案2：kroki
    try:
        url = "https://kroki.io/mermaid/png"
        r = requests.post(
            url,
            data=code.encode("utf-8"),
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Accept": "image/png,image/*;q=0.9,*/*;q=0.8",
            },
            timeout=25,
        )
        if _valid_image_response(r):
            with open(output_path, "wb") as f:
                f.write(r.content)
            enhance_image(output_path)
            return True
        last_error += "\n\nkroki\n" + _error_excerpt(r)
    except Exception as e:
        last_error += f"\n\nkroki exception\n{e}"

    _save_failed_source(code, output_path, last_error)
    return False


# =========================
# 标题修复（保守版）
# =========================
def normalize_headings(content: str) -> str:
    content = content.replace("\r\n", "\n").replace("\r", "\n")

    lines = content.split("\n")
    out = []
    in_code_block = False

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            in_code_block = not in_code_block
            out.append(line)
            continue

        if in_code_block:
            out.append(line)
            continue

        if not stripped:
            out.append("")
            continue

        m_hash = re.match(r"^(#{1,6})\s*(.+?)\s*$", stripped)
        if m_hash:
            hashes = m_hash.group(1)
            title = m_hash.group(2).strip()

            if out and out[-1] != "":
                out.append("")
            out.append(f"{hashes} {title}")
            out.append("")
            continue

        m_num = re.match(r"^(\d+(?:\.\d+)*)(?:\.\s+|\s+)(.+)$", stripped)
        if m_num:
            numbering = m_num.group(1)
            title = m_num.group(2).strip()

            if len(title) <= 80 and not re.search(r"[。！？!?]$", title):
                level = min(numbering.count(".") + 1, 6)
                heading = "#" * level + " " + f"{numbering} {title}"

                if out and out[-1] != "":
                    out.append("")
                out.append(heading)
                out.append("")
                continue

        out.append(line)

    result = "\n".join(out)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip() + "\n"


# =========================
# Mermaid 替换
# =========================
def replace_mermaid_blocks(content: str, tmp_dir: Path):
    pattern = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
    count = 0

    def repl(match):
        nonlocal count
        code = match.group(1).strip()
        img_name = f"mermaid_{count}.png"
        img_path = tmp_dir / img_name

        if render_mermaid(code, str(img_path)):
            count += 1
            return f"\n\n![diagram]({img_name})\n\n"
        else:
            count += 1
            return match.group(0)

    new_content = pattern.sub(repl, content)
    return new_content, count


# =========================
# 主流程
# =========================
def convert_md_to_docx(md_path: str, docx_path: str):
    ensure_pandoc()

    md_path = Path(md_path).resolve()
    docx_path = Path(docx_path).resolve()

    if not md_path.exists():
        raise FileNotFoundError(f"找不到 Markdown 文件: {md_path}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        content = md_path.read_text(encoding="utf-8")
        content = normalize_headings(content)
        content, mermaid_count = replace_mermaid_blocks(content, tmp_dir)

        temp_md = tmp_dir / "temp.md"
        temp_md.write_text(content, encoding="utf-8")

        old_cwd = os.getcwd()
        os.chdir(tmp_dir)
        try:
            pypandoc.convert_file(
                str(temp_md.name),
                "docx",
                outputfile=str(docx_path),
                extra_args=[
                    "--from=gfm",
                    "--resource-path=.",
                ],
            )
        finally:
            os.chdir(old_cwd)


if __name__ == "__main__":
    convert_md_to_docx("input.md", "output.docx")
