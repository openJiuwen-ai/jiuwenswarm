---
name: equations
description: Read before inserting or editing an equation in a Word document.
---

**Insert real equations.** An equation is an Office Math object, inserted as OOXML (an `m:oMath` element inside a paragraph) with `insertOoxml` in `execute_office_js`. Never fake one with plain text, Unicode superscripts or an image, unless the user asks for that.

**Build the OOXML carefully.** Wrap the math in a complete package with the `m` (math) and `w` (wordprocessing) namespaces declared. Use `m:oMathPara` for a display equation on its own line and a bare `m:oMath` inside a run's paragraph for an inline one. Escape `&`, `<` and `>` in text. Structures: fractions `m:f`, superscripts and subscripts `m:sSup`, `m:sSub`, `m:sSubSup`, radicals `m:rad`, n-ary operators such as sums and integrals `m:nary`, delimiters `m:d`, matrices `m:m`.

**Place it.** Insert after or inside the paragraph the user means, not by replacing a paragraph that has other content. Keep numbering consistent with the document's existing equations (for example a right-aligned "(3)" in a table or with a tab stop), and update cross-references to it.

**Check it.** Read back the paragraph and confirm the equation is there and the surrounding text is intact; when the layout matters, run `verify_doc_visual`. When insertion fails, report the error and do not fall back to plain text without telling the user.
