import { HashRouter, Routes, Route, Navigate, Outlet } from 'react-router-dom'
import { AuthProvider, useAuth } from './AuthContext'
import { ThemeProvider } from './ThemeContext'
import Login from './pages/Login'
import Topbar from './components/Topbar'
import Dashboard from './pages/Dashboard'
import Vehicles from './pages/Vehicles'
import VehicleDetail from './pages/VehicleDetail'
import Timetable from './pages/Timetable'
import Observation from './pages/Observation'
import Maps from './pages/Maps'
import VehiclesLayout from './pages/VehiclesLayout'
import './App.css'

function ProtectedLayout() {
  const { token } = useAuth();
  if (!token) return <Navigate to="/login" replace />;
  return (
    <>
      <Topbar />
      <Outlet />
    </>
  );
}

function App() {
  return (
    <ThemeProvider>
      <AuthProvider>
        <HashRouter>
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route element={<ProtectedLayout />}>
              <Route path="/" element={<Dashboard />} />
              <Route path="/vehicles" element={<VehiclesLayout />}>
                <Route index element={<Navigate to="/vehicles/timetable" replace />} />
                <Route path="timetable" element={<Timetable />} />
                <Route path="observation" element={<Observation />} />
                <Route path="all" element={<Vehicles />} />
              </Route>
              <Route path="/vehicle/:vehicleNo" element={<VehicleDetail />} />
              <Route path="/maps" element={<Maps />} />
            </Route>
          </Routes>
        </HashRouter>
      </AuthProvider>
    </ThemeProvider>
  )
}

export default App
