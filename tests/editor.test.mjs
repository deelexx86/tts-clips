import assert from "node:assert/strict";
import test from "node:test";
import { editorRange } from "../web/text.ts";

test("speech finding offsets select the exact textarea fragment after emoji and combining marks", () => {
  const prefix = "🚉 Новая площа́дка. ";
  const fragment = "2\u202f450,5 м²";
  const text = prefix + fragment + " у станции.";
  const start = Array.from(prefix).length;
  const end = start + Array.from(fragment).length;
  const range = editorRange(Array.from(text), start, end);
  assert.deepEqual(range, [prefix.length, prefix.length + fragment.length]);
  assert.equal(text.slice(...range), fragment);
});
