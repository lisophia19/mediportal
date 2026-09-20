import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { fetchCalls } from "../api";
import { useApiRequest } from "../useApiRequest";
import { formatStatusLabel, formatDateTime } from "../format";
import StatusBadge from "../components/StatusBadge.jsx";

const STATUS_OPTIONS = [
  "",
  "scheduled",
  "no_match",
  "no_slots",
  "abandoned",
  "failed",
  "in_progress",
];

function callerLabel(call) {
  if (call.patient) {
    return `${call.patient.first_name} ${call.patient.last_name}`;
  }
  return call.caller_phone || "Unknown caller";
}

function appointmentLabel(call) {
  if (!call.appointment) return "—";
  const { doctor, start_time: startTime } = call.appointment;
  return `${doctor} · ${formatDateTime(startTime)}`;
}

export default function CallList() {
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const navigate = useNavigate();

  const { data, loading, error } = useApiRequest(
    () => fetchCalls({ status, q, page }),
    [status, q, page],
  );
  const calls = data?.calls || [];
  const totalPages = data?.total_pages || 1;

  return (
    <div className="page">
      <h1>Calls</h1>
      <div className="filters">
        <select
          value={status}
          onChange={(e) => {
            setPage(1);
            setStatus(e.target.value);
          }}
        >
          {STATUS_OPTIONS.map((option) => (
            <option key={option} value={option}>
              {option === "" ? "All statuses" : formatStatusLabel(option)}
            </option>
          ))}
        </select>
        <input
          type="text"
          placeholder="Search patient name or complaint…"
          value={q}
          onChange={(e) => {
            setPage(1);
            setQ(e.target.value);
          }}
        />
      </div>

      {loading && <div className="loading-state">Loading calls…</div>}
      {error && <div className="error-text">{error}</div>}

      {!loading && !error && calls.length === 0 && (
        <div className="empty-state">No calls match these filters.</div>
      )}

      {!loading && !error && calls.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Caller / Patient</th>
              <th>Complaint</th>
              <th>Routed to</th>
              <th>Status</th>
              <th>Appointment</th>
            </tr>
          </thead>
          <tbody>
            {calls.map((call) => (
              <tr
                key={call.id}
                className="clickable"
                onClick={() => navigate(`/calls/${call.id}`)}
              >
                <td>{formatDateTime(call.started_at)}</td>
                <td>{callerLabel(call)}</td>
                <td>{(call.raw_complaint || "—").slice(0, 60)}</td>
                <td>{call.matched_term || "—"}</td>
                <td>
                  <StatusBadge status={call.status} />
                </td>
                <td>{appointmentLabel(call)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {totalPages > 1 && (
        <div style={{ marginTop: 12, display: "flex", gap: 8 }}>
          <button disabled={page <= 1} onClick={() => setPage(page - 1)}>
            Previous
          </button>
          <span>
            Page {page} of {totalPages}
          </span>
          <button disabled={page >= totalPages} onClick={() => setPage(page + 1)}>
            Next
          </button>
        </div>
      )}
    </div>
  );
}
