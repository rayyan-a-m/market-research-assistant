"use client";

interface Props {
  value: string[];
  onChange: (value: string[]) => void;
}

function isValid(u: string): boolean {
  return u.trim() === "" || /^https:\/\/.+/i.test(u.trim());
}

// A row per source URL (add / remove), with inline https validation — replaces
// the newline-separated textarea.
export function UrlList({ value, onChange }: Props) {
  const rows = value.length ? value : [""];

  function setRow(i: number, v: string) {
    onChange(rows.map((r, j) => (j === i ? v : r)));
  }
  function addRow() {
    onChange([...rows, ""]);
  }
  function removeRow(i: number) {
    const next = rows.filter((_, j) => j !== i);
    onChange(next.length ? next : [""]);
  }

  return (
    <div className="flex flex-col gap-2">
      {rows.map((url, i) => (
        <div key={i} className="flex flex-col gap-1">
          <div className="flex items-center gap-2">
            <input
              value={url}
              onChange={(e) => setRow(i, e.target.value)}
              placeholder="https://competitor.com/blog"
              className="input"
              inputMode="url"
            />
            {rows.length > 1 && (
              <button
                type="button"
                onClick={() => removeRow(i)}
                className="px-2 text-muted hover:text-foreground"
                aria-label="Remove URL"
              >
                ×
              </button>
            )}
          </div>
          {!isValid(url) && <span className="text-xs text-red-600">Must start with https://</span>}
        </div>
      ))}
      <button type="button" onClick={addRow} className="w-fit text-sm text-accent hover:opacity-70">
        + Add URL
      </button>
    </div>
  );
}
