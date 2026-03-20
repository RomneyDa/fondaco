const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Format a Date to "Mon Mar 2, 3:50 AM" */
function fmt(d: Date): string {
  const dow = DAYS[d.getDay()];
  const mon = MONTHS[d.getMonth()];
  const hr = d.getHours() % 12 || 12;
  const mins = String(d.getMinutes()).padStart(2, "0");
  const ampm = d.getHours() < 12 ? "AM" : "PM";
  return `${dow} ${mon} ${d.getDate()}, ${hr}:${mins} ${ampm}`;
}

/** Format an ISO string to local time: "Mon Mar 2, 3:50 AM" */
export function fmtTime(iso: string | undefined | null): string {
  if (!iso) return "\u2014";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return fmt(d);
  } catch {
    return iso;
  }
}

/** Format a UTC filename like "2026-02-20T03-45-00Z.json" to local time.
 *  Today → "3:50 AM", older → "Mar 10, 3:50 AM" (no weekday). */
export function fmtSessionName(file: string): string {
  const name = file.replace(".json", "");
  const match = name.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})Z$/);
  if (match) {
    const iso = `${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:${match[6]}Z`;
    const d = new Date(iso);
    if (!isNaN(d.getTime())) {
      const now = new Date();
      const isToday =
        d.getFullYear() === now.getFullYear() &&
        d.getMonth() === now.getMonth() &&
        d.getDate() === now.getDate();
      const hr = d.getHours() % 12 || 12;
      const mins = String(d.getMinutes()).padStart(2, "0");
      const ampm = d.getHours() < 12 ? "AM" : "PM";
      if (isToday) return `${hr}:${mins} ${ampm}`;
      const mon = MONTHS[d.getMonth()];
      return `${mon} ${d.getDate()}, ${hr}:${mins} ${ampm}`;
    }
  }
  return name;
}

/** Format just the time portion: "3:50:12 AM" */
export function fmtTimeOnly(iso: string | undefined | null): string {
  if (!iso) return "\u2014";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const hr = d.getHours() % 12 || 12;
    const mins = String(d.getMinutes()).padStart(2, "0");
    const ampm = d.getHours() < 12 ? "AM" : "PM";
    return `${hr}:${mins} ${ampm}`;
  } catch {
    return iso;
  }
}

/** Format just the date portion: "Mar 2" */
export function fmtDateOnly(iso: string | undefined | null): string {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    return `${MONTHS[d.getMonth()]} ${d.getDate()}`;
  } catch {
    return "";
  }
}
