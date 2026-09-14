import { Navigate, Route, Routes, Link, useNavigate } from "react-router-dom";
import { getToken, clearToken } from "./api";
import Login from "./pages/Login.jsx";
import CallList from "./pages/CallList.jsx";
import CallDetail from "./pages/CallDetail.jsx";

function RequireAuth({ children }) {
  const token = getToken();
  if (!token) return <Navigate to="/login" replace />;
  return children;
}

function TopBar() {
  const navigate = useNavigate();
  const token = getToken();
  if (!token) return null;

  function handleLogout() {
    clearToken();
    navigate("/login");
  }

  return (
    <div className="top-bar">
      <Link to="/calls">Mediportal Call Review</Link>
      <button onClick={handleLogout}>Log out</button>
    </div>
  );
}

export default function App() {
  return (
    <>
      <TopBar />
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          path="/calls"
          element={
            <RequireAuth>
              <CallList />
            </RequireAuth>
          }
        />
        <Route
          path="/calls/:id"
          element={
            <RequireAuth>
              <CallDetail />
            </RequireAuth>
          }
        />
        <Route path="*" element={<Navigate to="/calls" replace />} />
      </Routes>
    </>
  );
}
