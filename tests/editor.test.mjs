import { takeStatus } from "../web/take-status.ts";
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
