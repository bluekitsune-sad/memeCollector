/** Pure form-state and payload building for the Settings page (PRD §42–§43).

Kept out of the component so validation and mapping are unit-testable and the
form component only renders and wires events.
*/

import type { SettingsPatch, SettingsResponse } from "./types";

/** The crawl-limit fields, stored as strings so a partial edit never shows "undefined". */
export interface CrawlForm {
  delay_seconds: string;
  concurrency: string;
  max_pages: string;
  download_limit: string;
  max_file_size_mb: string;
}

/** The editable AI fields (provider/model/embedding), stored as strings. */
export interface AiForm {
  provider: string;
  model: string;
  embedding_model: string;
}

/** Numeric settings render as text so a missing/invalid value never shows "undefined". */
function num(value: number | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : "";
}

/** Server values, overlaid with whatever the user has typed but not saved yet. */
export function crawlFormFrom(
  settings: SettingsResponse,
  override: Partial<CrawlForm>,
): CrawlForm {
  const base: CrawlForm = {
    delay_seconds: num(settings.crawler.delay_seconds),
    concurrency: num(settings.crawler.concurrency),
    max_pages: num(settings.crawler.max_pages),
    download_limit: num(settings.crawler.download_limit),
    max_file_size_mb: num(settings.crawler.max_file_size_mb),
  };
  return { ...base, ...override };
}

/** Server values, overlaid with whatever the user has typed but not saved yet. */
export function aiFormFrom(
  settings: SettingsResponse,
  override: Partial<AiForm>,
): AiForm {
  const base: AiForm = {
    provider: settings.ai.provider,
    model: settings.ai.model,
    embedding_model: settings.ai.embedding_model,
  };
  return { ...base, ...override };
}

/** `null` when every crawl limit is a non-negative number, else the error message. */
export function crawlError(form: CrawlForm): string | null {
  const values = Object.values(form);
  if (values.some((value) => value.trim() === "" || !Number.isFinite(Number(value)) || Number(value) < 0)) {
    return "Every crawl limit must be a non-negative number.";
  }
  return null;
}

/** Typed PATCH body built from the validated form values. */
export function settingsPatch(form: CrawlForm, ai: AiForm): SettingsPatch {
  return {
    crawler: {
      delay_seconds: Number(form.delay_seconds),
      concurrency: Number(form.concurrency),
      max_pages: Number(form.max_pages),
      download_limit: Number(form.download_limit),
      max_file_size_mb: Number(form.max_file_size_mb),
    },
    ai: {
      provider: ai.provider.trim(),
      model: ai.model.trim(),
      embedding_model: ai.embedding_model.trim(),
    },
  };
}
