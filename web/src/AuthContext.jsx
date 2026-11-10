import { createContext, useContext, useState } from 'react';
import { postJson } from './api';

const AuthContext = createContext(null);

function readSession() {
  try {
    const token = localStorage.getItem('balaji_token');
    const user = localStorage.getItem('balaji_user');
    return token ? { token, user: user ? JSON.parse(user) : null } : null;
  } catch {
    return null;
  }
}

export function AuthProvider({ children }) {
  const [session, setSession] = useState(readSession);

  async function login(username, password) {
    const data = await postJson('/auth/login', { username, password });
    const s = { token: data.token, user: { username: data.username, role: data.role } };
    localStorage.setItem('balaji_token', s.token);
    localStorage.setItem('balaji_user', JSON.stringify(s.user));
    setSession(s);
  }

  async function register(username, password) {
    const data = await postJson('/auth/register', { username, password });
    return data.role;
  }

  function logout() {
    localStorage.removeItem('balaji_token');
    localStorage.removeItem('balaji_user');
    setSession(null);
  }

  return (
    <AuthContext.Provider value={{ ...session, login, register, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
