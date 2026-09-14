export function formatStatusLabel(status) {
  return (status || "unknown").replace(/_/g, " ");
}

export function formatDateTime(value) {
  return value ? new Date(value).toLocaleString() : "—";
}
