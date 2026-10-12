import { useState } from "react";
function readable(name: string) {
  return name.replace(/_/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2");
}
export function ResultView({
  value,
  onIssuesStart,
  onResume,
}: {
  value: unknown;
  onIssuesStart?: (targets: string[]) => void;
  onResume?: (session: Record<string, unknown>) => void;
}) {
  const [selected, setSelected] = useState<string[]>([]);
  if (value === null || value === undefined) return <p>No results.</p>;
  if (typeof value !== "object")
    return (
      <span>
        {typeof value === "boolean" ? (value ? "Yes" : "No") : String(value)}
      </span>
    );
  if (
    !Array.isArray(value) &&
    "issues" in value &&
    Array.isArray(value.issues)
  ) {
    const errors =
      "errors" in value && Array.isArray(value.errors) ? value.errors : [];
    const incomplete =
      ("incomplete" in value && value.incomplete === true) || errors.length > 0;
    const targets = new Set(
      value.issues.map((row: Record<string, unknown>) =>
        String(row.url || `${row.repo}#${row.number}`),
      ),
    );
    const selectedTargets = selected.filter((target) => targets.has(target));
    return (
      <div className="issue-results">
        {incomplete && (
          <div role="status">
            <p>Issue results are incomplete.</p>
            {errors.length > 0 && (
              <ul>
                {errors.map((error: Record<string, unknown>, index: number) => (
                  <li key={index}>
                    {String(error.repo || "Registered repository")}:{" "}
                    {String(
                      error.message || "Repository issues could not be read.",
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {value.issues.length === 0 ? (
          <p>
            {incomplete ? "No issues could be verified." : "No issues found."}
          </p>
        ) : (
          value.issues.map((row: Record<string, unknown>) => {
            const target = String(row.url || `${row.repo}#${row.number}`);
            return (
              <label className="issue-result" key={target}>
                <input
                  type="checkbox"
                  checked={selected.includes(target)}
                  onChange={(e) =>
                    setSelected((v) =>
                      e.target.checked
                        ? [...v, target]
                        : v.filter((x) => x !== target),
                    )
                  }
                />
                <span>
                  <strong>
                    #{String(row.number)} {String(row.title)}
                  </strong>
                  <small>
                    {String(row.repo)} · {String(row.state)}
                  </small>
                </span>
              </label>
            );
          })
        )}
        {selectedTargets.length > 0 && onIssuesStart && (
          <button
            className="primary"
            onClick={() => onIssuesStart(selectedTargets)}
          >
            Start {selectedTargets.length} selected{" "}
            {selectedTargets.length === 1 ? "issue" : "issues"}
          </button>
        )}
      </div>
    );
  }
  if (
    !Array.isArray(value) &&
    "results" in value &&
    Array.isArray(value.results)
  )
    return (
      <div className="search-results">
        {value.results.length ? (
          value.results.map((row: Record<string, unknown>, index: number) => (
            <article key={String(row.sid || index)}>
              <strong>
                {String(row.title || row.summary || row.sid || "Saved session")}
              </strong>
              <p>
                {String(row.repo || "")} ·{" "}
                {String(row.branch || row.slot || "")} · {String(row.ago || "")}
              </p>
              {Array.isArray(row.excerpts) &&
                row.excerpts.map((excerpt, index) => (
                  <blockquote key={index}>{String(excerpt)}</blockquote>
                ))}
              {onResume && (
                <button className="secondary" onClick={() => onResume(row)}>
                  Resume this conversation
                </button>
              )}
            </article>
          ))
        ) : (
          <p>No matching conversations.</p>
        )}
      </div>
    );
  if (Array.isArray(value))
    return (
      <div className="data-list">
        {value.length ? (
          value.map((item, index) => (
            <div key={index}>
              <ResultView value={item} />
            </div>
          ))
        ) : (
          <p>None</p>
        )}
      </div>
    );
  return (
    <dl className="data-result">
      {Object.entries(value)
        .filter(
          ([key]) =>
            ![
              "attachment",
              "attachments",
              "fingerprint",
              "operationId",
            ].includes(key),
        )
        .map(([key, item]) => (
          <div key={key}>
            <dt>{readable(key)}</dt>
            <dd>
              <ResultView value={item} />
            </dd>
          </div>
        ))}
    </dl>
  );
}
