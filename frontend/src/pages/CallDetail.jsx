import { Link, useParams } from "react-router-dom";
import { fetchCallDetail } from "../api";
import { useApiRequest } from "../useApiRequest";
import { formatDateTime } from "../format";
import StatusBadge from "../components/StatusBadge.jsx";

export default function CallDetail() {
  const { id } = useParams();
  const { data: call, loading, error } = useApiRequest(() => fetchCallDetail(id), [id]);

  return (
    <div className="page">
      <Link className="back-link" to="/calls">
        ← Back to calls
      </Link>

      {loading && <div className="loading-state">Loading call…</div>}
      {error && <div className="error-text">{error}</div>}

      {call && (
        <>
          <div className="section">
            <h2>Outcome</h2>
            <p>
              <StatusBadge status={call.status} /> · started{" "}
              {formatDateTime(call.started_at)}
              {call.ended_at && ` · ended ${formatDateTime(call.ended_at)}`}
            </p>
            <p>Caller phone: {call.caller_phone || "—"}</p>
            <p className="vogent-call-id">Vogent call ID: {call.vogent_call_id || "—"}</p>
          </div>

          <div className="section">
            <h2>Patient</h2>
            {call.patient ? (
              <p>
                {call.patient.first_name} {call.patient.last_name} · DOB{" "}
                {call.patient.date_of_birth} · ZIP{" "}
                {call.patient.home_zip || "—"}
              </p>
            ) : (
              <p>Not identified</p>
            )}
          </div>

          <div className="section">
            <h2>Routing</h2>
            <p>
              <strong>Said:</strong> {call.raw_complaint || "—"}
            </p>
            <p>
              <strong>Matched to:</strong> {call.matched_term?.term || "—"}
              {call.matched_term?.body_part &&
                ` (${call.matched_term.body_part}, ${call.matched_term.category})`}
            </p>
          </div>

          <div className="section">
            <h2>Appointment</h2>
            {call.appointment ? (
              <p>
                {call.appointment.doctor} at {call.appointment.practice} ·{" "}
                {call.appointment.when} · {call.appointment.appointment_type}
              </p>
            ) : (
              <p>No appointment booked.</p>
            )}
          </div>

          <div className="section">
            <h2>Transcript</h2>
            {(call.transcript || []).length === 0 && <p>No transcript captured.</p>}
            {(call.transcript || []).map((line, index) => (
              <div className="transcript-line" key={index}>
                <span className="transcript-speaker">{line.speaker}:</span>
                {line.text}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
