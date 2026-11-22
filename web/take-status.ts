import type { QC } from "./qc";
import type { Job } from "./audio";

type Decision = "pending" | "accepted" | "rejected";

export function takeStatus(state?: Job["state"], decision?: Decision, qc?: QC, recovered = false) {
  if (!state) return { label: "Без озвучки", tone: "neutral", problem: false };
  if (state === "queued" || state === "running") return { label: "Озвучивается", tone: "neutral", problem: false };
  if (state !== "ready") return {
    label: state === "failed" ? "Не удалось озвучить" : state === "cancelled" ? "Остановлен" : "Прерван",
    tone: "warning", problem: true,
  };
  if (decision === "rejected") return { label: "Отклонён", tone: "warning", problem: true };
  if (decision === "accepted") return { label: "Принят вручную", tone: "positive", problem: false };
  if (qc?.state === "queued" || qc?.state === "running") return { label: "Проверяется", tone: "neutral", problem: false };
  if (qc?.state === "failed" || qc?.state === "unavailable") return { label: "Проверка недоступна", tone: "warning", problem: true };
  if (qc?.state === "complete") {
    if (qc.pronunciation_integrity?.status === "review") return { label: "Проверьте точность произнесения", tone: "warning", problem: true };
    if (qc.report?.status === "likely_defect") return { label: "Возможен пропуск речи", tone: "warning", problem: true };
    if (recovered || qc.report?.status === "suspect" || qc.pronunciation_integrity?.status !== "clear") {
      return { label: "Проверить на слух", tone: "warning", problem: true };
    }
    if (qc.report?.status === "verified") return { label: "Полнота подтверждена", tone: "positive", problem: false };
  }
  return { label: "Не проверен", tone: "neutral", problem: false };
}
