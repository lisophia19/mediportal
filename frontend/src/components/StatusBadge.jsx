import { formatStatusLabel } from "../format";

export default function StatusBadge({ status }) {
  return <span className={`badge badge-${status}`}>{formatStatusLabel(status)}</span>;
}
