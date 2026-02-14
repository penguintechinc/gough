/**
 * ShellAccessButton - Button to open shell access to a resource.
 *
 * Checks permissions and opens terminal modal for shell sessions.
 */

import React, { useState, useCallback } from 'react';
import api from '../../lib/api';
import { WebTerminal } from '../Terminal/WebTerminal';

interface ShellAccessButtonProps {
  resourceType: string;
  resourceId: string;
  resourceName?: string;
  sessionType?: 'ssh' | 'kubectl' | 'docker' | 'cloud_cli';
  principals?: string[];
  className?: string;
  disabled?: boolean;
}

interface ShellAccessInfo {
  allowed: boolean;
  reason?: string;
  sessionType: string;
  principals: string[];
}

/**
 * Button component that checks shell access permissions and opens terminal.
 */
export const ShellAccessButton: React.FC<ShellAccessButtonProps> = ({
  resourceType,
  resourceId,
  resourceName,
  sessionType = 'ssh',
  principals = ['ubuntu'],
  className = '',
  disabled = false,
}) => {
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [shellInfo, setShellInfo] = useState<ShellAccessInfo | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Check access permissions
  const checkAccess = useCallback(async () => {
    try {
      const response = await api.post('/shell/check-access', {
        resource_type: resourceType,
        resource_id: resourceId,
        session_type: sessionType,
      });
      return response.data as ShellAccessInfo;
    } catch (err: any) {
      const message = err.response?.data?.error || 'Failed to check access';
      throw new Error(message);
    }
  }, [resourceType, resourceId, sessionType]);

  // Handle button click
  const handleClick = useCallback(async () => {
    if (disabled || isLoading) return;

    setIsLoading(true);
    setError(null);

    try {
      const info = await checkAccess();
      setShellInfo(info);

      if (info.allowed) {
        setIsModalOpen(true);
      } else {
        setError(info.reason || 'Access denied');
      }
    } catch (err: any) {
      setError(err.message);
    } finally {
      setIsLoading(false);
    }
  }, [disabled, isLoading, checkAccess]);

  // Handle modal close
  const handleClose = useCallback(() => {
    setIsModalOpen(false);
    setShellInfo(null);
  }, []);

  // Handle terminal error
  const handleError = useCallback((errorMessage: string) => {
    setError(errorMessage);
  }, []);

  // Button disabled state
  const buttonDisabled = disabled || isLoading;

  return (
    <>
      {/* Shell Access Button */}
      <button
        onClick={handleClick}
        disabled={buttonDisabled}
        className={`inline-flex items-center gap-2 px-3 py-1.5 text-sm
                   bg-gold-600 hover:bg-gold-500 text-dark-900 font-medium
                   rounded transition-colors disabled:opacity-50
                   disabled:cursor-not-allowed ${className}`}
        title={disabled ? 'Shell access not available' : 'Open shell session'}
      >
        {isLoading ? (
          <>
            <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24">
              <circle
                className="opacity-25"
                cx="12"
                cy="12"
                r="10"
                stroke="currentColor"
                strokeWidth="4"
                fill="none"
              />
              <path
                className="opacity-75"
                fill="currentColor"
                d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
              />
            </svg>
            <span>Connecting...</span>
          </>
        ) : (
          <>
            <svg
              className="h-4 w-4"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"
              />
            </svg>
            <span>Shell</span>
          </>
        )}
      </button>

      {/* Error Toast */}
      {error && (
        <div
          className="fixed bottom-4 right-4 bg-red-600 text-white px-4 py-2
                     rounded-lg shadow-lg flex items-center gap-2 z-50"
        >
          <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
            />
          </svg>
          <span>{error}</span>
          <button
            onClick={() => setError(null)}
            className="ml-2 hover:text-red-200"
          >
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M6 18L18 6M6 6l12 12"
              />
            </svg>
          </button>
        </div>
      )}

      {/* Terminal Modal */}
      {isModalOpen && shellInfo && (
        <div className="fixed inset-0 bg-black/80 flex items-center justify-center z-50 p-4">
          <div className="w-full max-w-6xl h-[80vh] bg-dark-900 rounded-lg overflow-hidden">
            <WebTerminal
              resourceType={resourceType}
              resourceId={resourceId}
              sessionType={shellInfo.sessionType as any}
              principals={shellInfo.principals}
              onClose={handleClose}
              onError={handleError}
            />
          </div>
        </div>
      )}
    </>
  );
};

export default ShellAccessButton;
