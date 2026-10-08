"use client";

interface SettingFieldProps {
  id: string;
  label: string;
  type: "number" | "text";
  value: string;
  onChange: (value: string) => void;
  min?: number;
  max?: number;
  step?: number;
}

/** One labelled settings input (shared by the crawl + AI sections). */
export default function SettingField({ id, label, type, value, onChange, min, max, step }: SettingFieldProps) {
  const numeric = type === "number" ? { min, max, step } : {};

  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        className="input"
        type={type}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        {...numeric}
      />
    </div>
  );
}
