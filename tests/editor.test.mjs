import { takeStatus } from "../web/take-status.ts";
import assert from "node:assert/strict";
import test from "node:test";
import { editorRange, editorText, replacement, sourceEdit } from "../web/text.ts";

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

test("the textarea maps source CRLF, lone CR and Unicode ranges both ways", () => {
  const prefix = "🚉 По́езд.\r\n\rРаз.";
  const fragment = "Ночь.\r\nСвет.";
  const text = prefix + fragment + "\r\nУтро.";
  const start = Array.from(prefix).length;
  const end = start + Array.from(fragment).length;
  const view = editorText(text);
  const range = editorRange(Array.from(text), start, end);
  assert.equal(view.slice(...range), "Ночь.\nСвет.");
  const next = view.slice(0, range[0]) + "Туман." + view.slice(range[1]);
  const edit = sourceEdit(text, replacement(view, next, range));
  assert.deepEqual(edit, { start, end, text: "Туман." });
  const loneCr = Array.from(editorText(prefix)).lastIndexOf("\n");
  const split = sourceEdit(text, { start: loneCr, end: loneCr + 1, text: "" });
  assert.equal(Array.from(text).slice(split.start, split.end).join(""), "\r");
});

test("take status distinguishes manual decisions, completeness and pronunciation", () => {
  const checked = { state: "complete", report: { status: "verified" },
    pronunciation_integrity: { status: "review" } };
  assert.equal(takeStatus("ready", "pending", checked).label, "Проверьте точность произнесения");
  assert.equal(takeStatus("ready", "accepted", checked).label, "Принят вручную");
  assert.equal(takeStatus("ready", "rejected", checked).problem, true);
  const clear = { ...checked, pronunciation_integrity: { status: "clear" } };
  assert.equal(takeStatus("ready", "pending", clear).label, "Полнота подтверждена");
  assert.equal(takeStatus("ready", "pending", clear, true).label, "Проверить на слух");
  assert.equal(takeStatus("ready", "pending", { ...clear, state: "running" }).label, "Проверяется");
  assert.equal(takeStatus("ready", "pending", { ...clear, state: "failed" }).problem, true);
  assert.equal(takeStatus("cancelled").label, "Остановлен");
});
