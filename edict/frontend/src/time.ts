export interface FormatTimeOptions {
  seconds?: boolean;
}

function pad2(value: number): string {
  return String(value).padStart(2, '0');
}

function hasTimezone(value: string): boolean {
  return /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(value);
}

function normalizeTimestamp(value: string): string {
  let normalized = value.trim();
  if (!normalized) return normalized;
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(normalized)) {
    normalized = normalized.replace(' ', 'T');
  }
  normalized = normalized.replace(/\.(\d{3})\d+(?=(?:[zZ]|[+-]\d{2}:?\d{2})?$)/, '.$1');
  return normalized;
}

function rawFallback(value: number | string | undefined, seconds: boolean, dateTime: boolean): string {
  if (typeof value !== 'string') return '';
  const normalized = value.trim().replace('T', ' ');
  if (!normalized) return '';
  if (dateTime) return normalized.slice(0, seconds ? 19 : 16);
  return normalized.slice(11, seconds ? 19 : 16) || normalized.slice(0, seconds ? 8 : 5);
}

export function parseTimestamp(value: number | string | undefined | null): Date | null {
  if (value == null || value === '') return null;
  if (typeof value === 'number') {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  const normalized = normalizeTimestamp(value);
  if (!normalized) return null;

  let date = new Date(normalized);
  if (!Number.isNaN(date.getTime())) return date;

  if (!hasTimezone(normalized) && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(normalized)) {
    date = new Date(`${normalized}Z`);
    if (!Number.isNaN(date.getTime())) return date;
  }

  return null;
}

export function formatLocalDateTime(value: number | string | undefined | null, options: FormatTimeOptions = {}): string {
  const seconds = options.seconds !== false;
  const date = parseTimestamp(value);
  if (!date) return rawFallback(value ?? undefined, seconds, true);
  const datePart = `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
  const timePart = `${pad2(date.getHours())}:${pad2(date.getMinutes())}${seconds ? `:${pad2(date.getSeconds())}` : ''}`;
  return `${datePart} ${timePart}`;
}

export function formatLocalTime(value: number | string | undefined | null, options: FormatTimeOptions = {}): string {
  const seconds = options.seconds !== false;
  const date = parseTimestamp(value);
  if (!date) return rawFallback(value ?? undefined, seconds, false);
  return `${pad2(date.getHours())}:${pad2(date.getMinutes())}${seconds ? `:${pad2(date.getSeconds())}` : ''}`;
}
