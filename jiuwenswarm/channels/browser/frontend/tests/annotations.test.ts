import { describe, it, expect } from "vitest";
import { normalizeUrl } from "../src/shared/url";

describe("normalizeUrl", () => {
  it("strips the hash fragment", () => {
    expect(normalizeUrl("https://example.com/page#section")).toBe("https://example.com/page");
  });

  it("strips a trailing slash", () => {
    expect(normalizeUrl("https://example.com/page/")).toBe("https://example.com/page");
  });

  it("returns the input unchanged for non-URL strings", () => {
    expect(normalizeUrl("not a url")).toBe("not a url");
  });

  it("keeps a normal URL unchanged", () => {
    expect(normalizeUrl("https://example.com/a/b")).toBe("https://example.com/a/b");
  });
});
