export function clock(ns: number): string {
  return new Date(ns / 1e6).toISOString().slice(11, 23);
}

export function ms(value: number): string {
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${value.toFixed(1)} ms`;
}

export function compactJson(value: unknown): string {
  const text = JSON.stringify(value);
  return text === "{}" ? "" : text;
}
