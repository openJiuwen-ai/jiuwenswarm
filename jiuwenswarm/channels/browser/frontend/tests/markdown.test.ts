import { describe, it, expect } from "vitest";
import { renderMarkdown } from "../src/sidepanel/markdown";

describe("renderMarkdown", () => {
  it("escapes HTML so agent output cannot inject markup", () => {
    const out = renderMarkdown("<script>alert('x')</script>");
    expect(out).toContain("&lt;script&gt;");
    expect(out).not.toContain("<script>");
  });

  it("renders bold and italic", () => {
    expect(renderMarkdown("**bold** and *italic*")).toContain("<strong>bold</strong>");
    expect(renderMarkdown("**bold** and *italic*")).toContain("<em>italic</em>");
  });

  it("renders headings", () => {
    expect(renderMarkdown("## A heading")).toContain("<h2>A heading</h2>");
  });

  it("renders fenced code blocks and inline code", () => {
    const out = renderMarkdown("before\n```js\nconst a = 1;\n```\nafter");
    expect(out).toContain("<pre><code>");
    expect(out).toContain("const a = 1;");
  });

  it("renders unordered and ordered lists", () => {
    const ul = renderMarkdown("- one\n- two");
    expect(ul).toContain("<ul>");
    expect(ul).toContain("<li>one</li>");
    expect(ul).toContain("<li>two</li>");
    const ol = renderMarkdown("1. first\n2. second");
    expect(ol).toContain("<ol>");
    expect(ol).toContain("<li>first</li>");
    expect(ol).toContain("<li>second</li>");
  });

  it("renders blockquotes and paragraphs", () => {
    expect(renderMarkdown("> a quote")).toContain("<blockquote>a quote</blockquote>");
    expect(renderMarkdown("hello world")).toContain("<p>hello world</p>");
  });
});
