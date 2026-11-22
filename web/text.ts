export type Edit = { start: number; end: number; text: string };
export type Range = [number, number];

export function editorText(text: string): string {
  return text.replace(/\r\n?/g, "\n");
}

// Server offsets count Unicode code points. Textarea selection counts UTF-16 units.
export function editorRange(points: string[], start: number, end: number): Range {
  const offset = (index: number) => editorText(points.slice(0, index).join("")).length;
  return [offset(start), offset(end)];
}

// A displayed newline may occupy two code points in the saved source.
export function sourceEdit(source: string, edit: Edit): Edit {
  if (!source.includes("\r\n")) return edit;
  const units = source.match(/\r\n|[\s\S]/gu) ?? [];
  const offset = (index: number) => index + units.slice(0, index).filter((unit) => unit === "\r\n").length;
  return { ...edit, start: offset(edit.start), end: offset(edit.end) };
}

export function replacement(before: string, after: string, selected?: Range): Edit {
  if (selected) {
    const [start, end] = selected;
    const prefix = before.slice(0, start);
    const suffix = before.slice(end);
    if (0 <= start && start <= end && end <= before.length &&
        after.length >= prefix.length + suffix.length &&
        after.startsWith(prefix) && after.endsWith(suffix)) {
      return {
        start: Array.from(prefix).length,
        end: Array.from(before.slice(0, end)).length,
        text: after.slice(prefix.length, after.length - suffix.length),
      };
    }
  }

  const oldPoints = Array.from(before);
  const newPoints = Array.from(after);
  const shorter = Math.min(oldPoints.length, newPoints.length);
  let prefix = 0;
  let suffix = 0;
  while (prefix < shorter && oldPoints[prefix] === newPoints[prefix]) prefix++;
  while (suffix < shorter && oldPoints[oldPoints.length - suffix - 1] ===
         newPoints[newPoints.length - suffix - 1]) suffix++;

  // Include every plausible position when the browser didn't report the edit range.
  const overlap = Math.max(0, prefix + suffix - shorter);
  const start = prefix - overlap;
  const tail = suffix - overlap;
  return {
    start,
    end: oldPoints.length - tail,
    text: newPoints.slice(start, newPoints.length - tail).join(""),
  };
}
