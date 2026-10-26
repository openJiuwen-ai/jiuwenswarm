from __future__ import annotations


_TOP1_TEMPLATE = """\
# Role
- You are a retriever.
- Select exactly one executable worker from the candidate tree.
- Do not explain your reasoning.

# Goal
- Choose the single best worker for the user request.
- The result must be an executable worker id from the tree.

# Output Rules
- Output exactly 1 line.
- Output exactly 1 worker id from the tree.
- Do not output category ids.
- Do not output explanations, numbering, JSON, or Markdown.

Node Name Hierarchy:
{tree_cid_hierarchy}
"""


_TOPK_TEMPLATE = """\
# Role
- You are a retriever.
- Rank candidate workers for the current user query.
- Do not explain your reasoning.

# Goal
- Select the best {top_k} executable workers for the user request.

# Output Rules
- Output up to {top_k} lines.
- Each line must contain exactly 1 worker id from the tree.
- Do not output category ids.
- Do not output explanations, numbering, JSON, or Markdown.

Node Name Hierarchy:
{tree_cid_hierarchy}
"""


def build_retriever_system_prompt(*, tree_cid_hierarchy: str, top_k: int) -> str:
    resolved_top_k = max(1, int(top_k or 1))
    template = _TOPK_TEMPLATE if resolved_top_k > 1 else _TOP1_TEMPLATE
    return template.format(
        top_k=resolved_top_k,
        tree_cid_hierarchy=str(tree_cid_hierarchy or "").strip() or "(no candidates)",
    )


def build_retriever_catalog_prompt(*, choices, top_k: int) -> str:
    resolved_top_k = max(1, int(top_k or 1))
    lines = [
        "# Role",
        "- You are a retriever.",
        "- Rank candidate workers for the current user query.",
        "- Do not explain your reasoning.",
        "",
        "# Goal",
        f"- Select the best {resolved_top_k} workers from the candidate list.",
        "",
        "# Output Rules",
        f"- Output up to {resolved_top_k} lines.",
        "- Each line must contain exactly 1 worker id from the candidate list.",
        "- Do not output explanations, numbering, JSON, or Markdown.",
        "",
        "# Candidates",
    ]
    has_choice = False
    for choice in choices:
        choice_id = str(getattr(choice, "choice_id", "") or "").strip()
        if not choice_id:
            continue
        description = " ".join(str(getattr(choice, "description", "") or "").split())
        line = f"- {choice_id}"
        if description:
            line += f": {description}"
        lines.append(line)
        has_choice = True
    if not has_choice:
        lines.append("- (no candidates)")
    return "\n".join(lines)


__all__ = ["build_retriever_catalog_prompt", "build_retriever_system_prompt"]
