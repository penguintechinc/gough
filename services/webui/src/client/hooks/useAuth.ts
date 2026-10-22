import { create } from 'zustand';
import api from '../lib/api';
import type { User, LoginCredentials, AuthState } from '../types';

interface AuthStore extends AuthState {
  login: (credentials: LoginCredentials) => Promise<void>;
  logout: () => Promise<void>;
  fetchUser: () => Promise<void>;
  checkAuth: () => Promise<boolean>;
  setUser: (user: User | null) => void;
}

// No `persist` middleware here on purpose — auth state lives in the
// HttpOnly `gough_access`/`gough_refresh` cookies the backend manages.
// Persisting anything auth-related to localStorage (directly or via
// zustand's storage) would reintroduce the XSS-exfiltration risk this
// migration removes.
export const useAuthStore = create<AuthStore>()((set) => ({
  user: null,
  isAuthenticated: false,
  isLoading: true,

  login: async (credentials: LoginCredentials) => {
    try {
      // The response body still includes tokens for programmatic/API
      // clients, but the web UI ignores them — the browser already has
      // the HttpOnly cookies from the Set-Cookie headers on this response.
      const response = await api.post('/auth/login', credentials);
      const { user } = response.data;

      set({
        user,
        isAuthenticated: true,
        isLoading: false,
      });
    } catch (error) {
      set({
        user: null,
        isAuthenticated: false,
        isLoading: false,
      });
      throw error;
    }
  },

  logout: async () => {
    try {
      // Backend clears gough_access/gough_refresh/gough_csrf cookies.
      await api.post('/auth/logout');
    } catch {
      // Ignore logout errors
    } finally {
      set({
        user: null,
        isAuthenticated: false,
        isLoading: false,
      });
    }
  },

  fetchUser: async () => {
    try {
      const response = await api.get('/auth/me');
      set({ user: response.data, isLoading: false });
    } catch {
      set({ user: null, isLoading: false });
    }
  },

  checkAuth: async () => {
    // Cookies are HttpOnly, so presence can't be checked client-side —
    // ask the backend directly, which reads gough_access from the cookie.
    try {
      const response = await api.get('/auth/me');
      set({
        user: response.data,
        isAuthenticated: true,
        isLoading: false,
      });
      return true;
    } catch {
      set({
        user: null,
        isAuthenticated: false,
        isLoading: false,
      });
      return false;
    }
  },

  setUser: (user: User | null) => {
    set({ user });
  },
}));

// Hook for components
export const useAuth = () => {
  const store = useAuthStore();

  return {
    user: store.user,
    isAuthenticated: store.isAuthenticated,
    isLoading: store.isLoading,
    login: store.login,
    logout: store.logout,
    checkAuth: store.checkAuth,
    hasRole: (roles: string[]) => {
      if (!store.user) return false;
      return roles.includes(store.user.role);
    },
    isAdmin: () => store.user?.role === 'admin',
    isMaintainer: () => store.user?.role === 'maintainer',
    isViewer: () => store.user?.role === 'viewer',
  };
};
