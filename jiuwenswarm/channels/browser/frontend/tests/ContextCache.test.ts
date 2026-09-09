import { describe, it, expect } from "vitest";
import { ContextCache } from "../src/background/ContextCache";
import { PageContext } from "../src/shared/types";

function makeCtx(title: string, text: string): PageContext {
  return {
    url: `https://example.com/${title}`,
    title,
    pageType: "article",
    capturedAt: new Date().toISOString(),
    text,
    originalLength: text.length,
  };
}

describe("ContextCache", () => {
  it("set/get/delete round-trip", () => {
    const c = new ContextCache();
    const ctx = makeCtx("a", "hello");
    c.set(1, ctx);
    expect(c.get(1)).toBe(ctx);
    expect(c.get(2)).toBeUndefined();
    c.delete(1);
    expect(c.get(1)).toBeUndefined();
  });

  it("aggregates multiple contexts in order with separators", () => {
    const c = new ContextCache();
    c.set(1, makeCtx("One", "alpha"));
    c.set(2, makeCtx("Two", "beta"));
    const out = c.aggregate([1, 2], 10000);
    expect(out).toContain("### One");
    expect(out).toContain("### Two");
    expect(out).toContain("alpha");
    expect(out).toContain("beta");
    expect(out).toContain("---");
  });

  it("skips unknown tab ids", () => {
    const c = new ContextCache();
    c.set(1, makeCtx("One", "alpha"));
    expect(c.aggregate([1, 999], 10000)).not.toContain("undefined");
  });

  it("truncates when total exceeds maxChars", () => {
    const c = new ContextCache();
    c.set(1, makeCtx("One", "x".repeat(1000)));
    const out = c.aggregate([1], 400);
    expect(out.length).toBeLessThanOrEqual(500);
    expect(out).toContain("truncated");
  });
});
