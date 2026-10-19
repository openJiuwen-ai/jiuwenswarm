from __future__ import annotations

import html
import json
from pathlib import Path


def generate_html(tree_dict: dict, output_path: Path) -> None:
    payload = _normalize_tree(tree_dict)
    stats = _tree_stats(payload)
    html_text = _render_document(payload, stats)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_text, encoding="utf-8")


def _normalize_tree(tree_dict: dict) -> dict:
    if "children" in tree_dict or "skills" in tree_dict:
        return _normalize_recursive_node(tree_dict, depth=0)
    return _normalize_legacy_tree(tree_dict)


def _normalize_recursive_node(node_dict: dict, *, depth: int) -> dict:
    # Compatibility: workflow tree payloads may encode leaf skills as
    # children nodes (type=leaf) rather than under a "skills" list.
    if _is_leaf_skill_node(node_dict):
        return _skill_leaf_from_node(node_dict)

    children = [_normalize_recursive_node(child, depth=depth + 1) for child in node_dict.get("children", [])]
    skill_children = [_skill_leaf(skill) for skill in node_dict.get("skills", [])]
    node_type = "root" if depth == 0 else "category"
    return {
        "name": node_dict.get("name", node_dict.get("id", "category")),
        "id": node_dict.get("id", ""),
        "description": node_dict.get("description", ""),
        "type": node_type,
        "children": children + skill_children,
    }


def _is_leaf_skill_node(node_dict: dict) -> bool:
    node_type = str(node_dict.get("type", "") or "").strip().lower()
    if node_type in {"leaf", "skill"}:
        return True
    has_children = bool(list(node_dict.get("children", []) or []))
    has_worker_id = bool(str(node_dict.get("worker_id", "") or "").strip())
    return (not has_children) and has_worker_id


def _skill_leaf_from_node(node_dict: dict) -> dict:
    worker_id = str(node_dict.get("worker_id", "") or "").strip()
    node_id = str(node_dict.get("id", "") or node_dict.get("cid", "") or "").strip()
    return {
        "name": node_dict.get("name", worker_id or node_id or "skill"),
        "id": worker_id or node_id,
        "description": node_dict.get("description", ""),
        "type": "skill",
        "meta": {
            "github_url": node_dict.get("github_url", ""),
            "stars": node_dict.get("stars", 0),
            "author": node_dict.get("author", ""),
        },
        "children": [],
    }


def _normalize_legacy_tree(tree_dict: dict) -> dict:
    root = {"name": "Skills", "id": "root", "description": "", "type": "root", "children": []}
    for domain_id, domain_data in tree_dict.get("domains", {}).items():
        domain_node = {
            "name": domain_data.get("name", domain_id),
            "id": domain_id,
            "description": domain_data.get("description", ""),
            "type": "domain",
            "children": [],
        }
        for type_id, type_data in domain_data.get("types", {}).items():
            type_node = {
                "name": type_data.get("name", type_id),
                "id": type_id,
                "description": type_data.get("description", ""),
                "type": "category",
                "children": [_skill_leaf(skill) for skill in type_data.get("skills", [])],
            }
            domain_node["children"].append(type_node)
        root["children"].append(domain_node)
    return root


def _skill_leaf(skill: dict) -> dict:
    return {
        "name": skill.get("name", skill.get("id", "skill")),
        "id": skill.get("id", ""),
        "description": skill.get("description", ""),
        "type": "skill",
        "meta": {
            "github_url": skill.get("github_url", ""),
            "stars": skill.get("stars", 0),
            "author": skill.get("author", ""),
        },
        "children": [],
    }


def _tree_stats(root: dict) -> dict:
    category_count = 0
    skill_count = 0
    max_depth = 1

    def walk(node: dict, depth: int) -> None:
        nonlocal category_count, skill_count, max_depth
        max_depth = max(max_depth, depth)
        if node.get("type") == "skill":
            skill_count += 1
            return
        if depth > 1:
            category_count += 1
        for child in node.get("children", []):
            walk(child, depth + 1)

    walk(root, 1)
    return {"categories": category_count, "skills": skill_count, "depth": max_depth}


def _render_document(root: dict, stats: dict) -> str:
    tree_json = json.dumps(root, ensure_ascii=False)
    head = _html_head()
    hero = _hero_markup(root, stats)
    tree_section = f'<section class="tree">{_render_node_children(root.get("children", []))}</section>'
    raw_section = f'<pre class="raw">{html.escape(json.dumps(root, ensure_ascii=False, indent=2))}</pre>'
    return f"""<!DOCTYPE html>
<html lang="en">
{head}
<body>
  <main>
    {hero}
    {tree_section}
    {raw_section}
  </main>
</body>
</html>"""


def _html_head() -> str:
    return """<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Capability Tree</title>
  <style>
    :root {
      --bg: #f5efe4;
      --ink: #1b2a2f;
      --panel: rgba(255,255,255,0.86);
      --line: rgba(27,42,47,0.12);
      --accent: #0f766e;
      --accent-2: #9a3412;
      --skill: #164e63;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(15,118,110,0.12), transparent 30%),
        radial-gradient(circle at top right, rgba(154,52,18,0.10), transparent 28%),
        linear-gradient(180deg, #fbf7ef, var(--bg));
      min-height: 100vh;
    }
    main {
      max-width: 1100px;
      margin: 0 auto;
      padding: 40px 20px 72px;
    }
    .hero {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 28px 28px 24px;
      box-shadow: 0 22px 60px rgba(27,42,47,0.08);
      backdrop-filter: blur(8px);
    }
    h1 {
      margin: 0 0 10px;
      font-size: clamp(2rem, 4vw, 3.3rem);
      line-height: 1.05;
    }
    .lede {
      margin: 0;
      max-width: 720px;
      font-size: 1.05rem;
      line-height: 1.6;
      color: rgba(27,42,47,0.82);
    }
    .stats {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 12px;
      margin-top: 22px;
    }
    .stat {
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 14px 16px;
      background: rgba(255,255,255,0.72);
    }
    .stat strong {
      display: block;
      font-size: 1.5rem;
      color: var(--accent);
    }
    .tree {
      margin-top: 26px;
      display: grid;
      gap: 10px;
    }
    details {
      background: rgba(255,255,255,0.78);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 0 14px 10px;
    }
    summary {
      list-style: none;
      cursor: pointer;
      padding: 14px 0;
      font-weight: 700;
      display: flex;
      align-items: baseline;
      gap: 10px;
    }
    summary::-webkit-details-marker { display: none; }
    .node-meta {
      font-size: 0.83rem;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: rgba(27,42,47,0.55);
    }
    .node-desc {
      margin: 0 0 8px;
      color: rgba(27,42,47,0.84);
      line-height: 1.55;
    }
    .children {
      margin-left: 14px;
      padding-left: 14px;
      border-left: 2px solid var(--line);
      display: grid;
      gap: 10px;
    }
    .skill {
      border: 1px solid rgba(22,78,99,0.16);
      background: rgba(240,249,255,0.7);
      border-radius: 16px;
      padding: 12px 14px;
    }
    .skill h4 {
      margin: 0 0 6px;
      color: var(--skill);
      font-size: 1rem;
    }
    .skill p {
      margin: 0;
      line-height: 1.5;
      color: rgba(27,42,47,0.82);
    }
    .meta-row {
      margin-top: 8px;
      font-size: 0.88rem;
      color: rgba(27,42,47,0.66);
    }
    .raw {
      margin-top: 28px;
      background: rgba(16,24,40,0.92);
      color: #dce7ea;
      border-radius: 22px;
      padding: 18px;
      overflow: auto;
      font: 13px/1.5 Consolas, monospace;
    }
  </style>
</head>"""


def _hero_markup(root: dict, stats: dict) -> str:
    title = html.escape(root.get("name", "Capability Tree"))
    stat_cards = "".join(
        [
            f'<div class="stat"><strong>{stats["categories"]}</strong><span>Categories</span></div>',
            f'<div class="stat"><strong>{stats["skills"]}</strong><span>Skills</span></div>',
            f'<div class="stat"><strong>{stats["depth"]}</strong><span>Tree Depth</span></div>',
        ]
    )
    return (
        '<section class="hero">'
        f"<h1>{title}</h1>"
        '<p class="lede">Interactive browse view for the generated capability tree. '
        "Expand categories to inspect subtrees and leaf skills.</p>"
        f'<div class="stats">{stat_cards}</div>'
        "</section>"
    )


def _render_node_children(nodes: list[dict]) -> str:
    return "".join(_render_node(node) for node in nodes)


def _render_node(node: dict) -> str:
    node_type = html.escape(str(node.get("type", "category")))
    name = html.escape(str(node.get("name", "Unknown")))
    node_id = html.escape(str(node.get("id", "")))
    description = html.escape(str(node.get("description", "") or ""))
    children = node.get("children", [])

    if node.get("type") == "skill":
        meta = node.get("meta", {})
        meta_bits = []
        if meta.get("author"):
            meta_bits.append(f"author: {html.escape(str(meta['author']))}")
        if meta.get("stars"):
            meta_bits.append(f"stars: {html.escape(str(meta['stars']))}")
        if meta.get("github_url"):
            url = html.escape(str(meta["github_url"]))
            meta_bits.append(f'<a href="{url}" target="_blank" rel="noreferrer">source</a>')
        meta_row = f'<div class="meta-row">{" | ".join(meta_bits)}</div>' if meta_bits else ""
        return f'<article class="skill"><h4>{name}</h4><p>{description}</p>{meta_row}</article>'

    open_attr = " open" if node.get("type") in {"root", "domain"} else ""
    description_html = f'<p class="node-desc">{description}</p>' if description else ""
    children_html = f'<div class="children">{_render_node_children(children)}</div>' if children else ""
    return (
        f"<details{open_attr}>"
        f'<summary><span>{name}</span><span class="node-meta">{node_type} {node_id}</span></summary>'
        f"{description_html}"
        f"{children_html}"
        f"</details>"
    )
