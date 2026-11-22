export type SpeechWarning = { start: number; end: number; text: string; code: string; reason: string };
export type SpeechGate = { code: "speech_ready"; confirmation: string; warnings: SpeechWarning[] };

export function speechGate(detail: unknown): SpeechGate | null {
  if (detail && typeof detail === "object" && "code" in detail && detail.code === "speech_ready" &&
      "confirmation" in detail && typeof detail.confirmation === "string" &&
      "warnings" in detail && Array.isArray(detail.warnings)) {
    return { code: "speech_ready", confirmation: detail.confirmation, warnings: detail.warnings };
  }
  return null;
}

export function SpeechWarnings({ warnings, locate }: {
  warnings: SpeechWarning[];
  locate: (warning: SpeechWarning) => void;
}) {
  return <ul className="speech-warnings">
    {warnings.map((warning) => <li key={warning.start}>
      <button className="subtle" type="button" onClick={() => locate(warning)}>
        <strong>{warning.text}</strong><span>{warning.reason}</span>
      </button>
    </li>)}
  </ul>;
}
