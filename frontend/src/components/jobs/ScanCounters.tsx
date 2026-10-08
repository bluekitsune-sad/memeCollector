import { counterLabel, parseJobMessage } from "@/lib/jobMessage";
import { formatCount } from "@/lib/format";

/** Live scan counters parsed from `jobs.message` `key=value` pairs (PRD §5.2). */
export default function ScanCounters({ message }: { message: string | null }) {
  const parsed = parseJobMessage(message);

  if (parsed.counters.length === 0 && parsed.flags.length === 0) {
    return message ? <p className="job-message">{message}</p> : null;
  }

  return (
    <>
      {parsed.counters.length > 0 ? (
        <div className="counters">
          {parsed.counters.map((counter) => (
            <div className="counter" key={counter.key}>
              <div className="label">{counterLabel(counter.key)}</div>
              <div className="value">
                {counter.current !== undefined
                  ? counter.total !== undefined
                    ? `${formatCount(counter.current)} / ${formatCount(counter.total)}`
                    : formatCount(counter.current)
                  : counter.value}
              </div>
            </div>
          ))}
        </div>
      ) : null}

      {parsed.flags.length > 0 ? (
        <div className="chip-row" style={{ marginTop: 8 }}>
          {parsed.flags.map((flag) => (
            <span
              className={`badge${flag === "paused" ? " processing" : flag === "limit_reached" ? " failed" : ""}`}
              key={flag}
            >
              {flag.replace(/_/g, " ")}
            </span>
          ))}
        </div>
      ) : null}
    </>
  );
}
