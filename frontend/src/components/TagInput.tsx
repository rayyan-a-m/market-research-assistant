"use client";

import { useState } from "react";

interface Props {
  value: string[];
  onChange: (value: string[]) => void;
  placeholder?: string;
}

// Type + Enter or comma to add a pill; Backspace on an empty field removes the
// last one. Replaces "comma-separated" free text with discrete, editable items.
export function TagInput({ value, onChange, placeholder }: Props) {
  const [draft, setDraft] = useState("");

  function commit(raw: string) {
    const parts = raw
      .split(",")
      .map((s) => s.trim())
      .filter((s) => s && !value.includes(s));
    if (parts.length) onChange([...value, ...parts]);
    setDraft("");
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if ((e.key === "Enter" || e.key === ",") && draft.trim()) {
      e.preventDefault();
      commit(draft);
    } else if (e.key === "Backspace" && !draft && value.length) {
      onChange(value.slice(0, -1));
    }
  }

  return (
    <div className="flex flex-wrap gap-2 rounded-lg border border-border bg-background p-2 focus-within:outline focus-within:outline-2 focus-within:outline-accent">
      {value.map((tag, i) => (
        <span
          key={`${tag}-${i}`}
          className="inline-flex items-center gap-1.5 rounded-full bg-surface px-2.5 py-1 text-sm"
        >
          {tag}
          <button
            type="button"
            onClick={() => onChange(value.filter((_, j) => j !== i))}
            className="text-muted hover:text-foreground"
            aria-label={`Remove ${tag}`}
          >
            ×
          </button>
        </span>
      ))}
      <input
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={onKeyDown}
        onBlur={() => draft.trim() && commit(draft)}
        placeholder={value.length ? "" : placeholder}
        className="min-w-[8rem] flex-1 bg-transparent text-sm outline-none"
      />
    </div>
  );
}
