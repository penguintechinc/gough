/**
 * WebTerminal Component - xterm.js based terminal for shell access.
 *
 * Connects to WebSocket for real-time shell sessions with support for:
 * - SSH, kubectl, docker, and cloud CLI sessions
 * - Terminal resize
 * - Copy/paste
 * - Link detection
 */

import React, { useEffect, useRef, useState, useCallback } from 'react';
import api from '../../lib/api';

interface WebTerminalProps {
  resourceType: string;
  resourceId: string;
  sessionType?: 'ssh' | 'kubectl' | 'docker' | 'cloud_cli';
  principals?: string[];
  onClose?: () => void;
  onError?: (error: string) => void;
}

type ConnectionStatus = 'connecting' | 'connected' | 'disconnected' | 'error';

/**
 * WebTerminal provides a web-based terminal interface using xterm.js.
 */
export const WebTerminal: React.FC<WebTerminalProps> = ({
  resourceType,
  resourceId,
  sessionType = 'ssh',
  principals = ['ubuntu'],
  onClose,
  onError,
}) => {
  const terminalRef = useRef<HTMLDivElement>(null);
  const terminalInstance = useRef<any>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const fitAddonRef = useRef<any>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [status, setStatus] = useState<ConnectionStatus>('connecting');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // Initialize terminal and connect
  useEffect(() => {
    let mounted = true;

    const initializeTerminal = async () => {
      if (!terminalRef.current || !mounted) return;

      try {
        // Dynamically import xterm.js (may not be installed yet)
        const { Terminal } = await import('@xterm/xterm');
        const { FitAddon } = await import('@xterm/addon-fit');
        const { WebLinksAddon } = await import('@xterm/addon-web-links');

        // Import CSS
        await import('@xterm/xterm/css/xterm.css');

        // Create terminal instance
        const term = new Terminal({
          cursorBlink: true,
          fontSize: 14,
          fontFamily: 'Menlo, Monaco, "Courier New", monospace',
          theme: {
            background: '#1a1a2e',
            foreground: '#d4d4d4',
            cursor: '#d4af37',
            cursorAccent: '#1a1a2e',
            selectionBackground: 'rgba(212, 175, 55, 0.3)',
          },
          rows: 30,
          cols: 120,
        });

        const fitAddon = new FitAddon();
        const webLinksAddon = new WebLinksAddon();

        term.loadAddon(fitAddon);
        term.loadAddon(webLinksAddon);
        term.open(terminalRef.current);
        fitAddon.fit();

        terminalInstance.current = term;
        fitAddonRef.current = fitAddon;

        // Create shell session
        const response = await api.post('/shell/sessions', {
          resource_type: resourceType,
          resource_id: resourceId,
          session_type: sessionType,
          principals,
        });

        if (!mounted) return;

        const { session_id, websocket_url } = response.data;
        setSessionId(session_id);

        // Connect WebSocket
        const wsUrl = websocket_url.replace('wss://gough.local', '');
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const fullWsUrl = `${protocol}//${window.location.host}${wsUrl}`;

        const ws = new WebSocket(fullWsUrl);
        wsRef.current = ws;

        ws.onopen = () => {
          if (!mounted) return;
          setStatus('connected');
          term.writeln('\x1b[1;32mConnected to remote shell\x1b[0m\r\n');
        };

        ws.onmessage = (event) => {
          try {
            const message = JSON.parse(event.data);
            if (message.type === 'output' && message.data) {
              term.write(message.data);
            } else if (message.type === 'connected') {
              term.writeln('\x1b[1;33mShell ready\x1b[0m\r\n');
            } else if (message.type === 'disconnect_message') {
              term.writeln(`\r\n\x1b[1;31m${message.reason}\x1b[0m\r\n`);
              setStatus('disconnected');
            }
          } catch {
            // Raw output
            term.write(event.data);
          }
        };

        ws.onerror = () => {
          if (!mounted) return;
          setStatus('error');
          setErrorMessage('WebSocket connection error');
          term.writeln('\x1b[1;31mConnection error\x1b[0m\r\n');
          onError?.('WebSocket connection error');
        };

        ws.onclose = () => {
          if (!mounted) return;
          setStatus('disconnected');
          term.writeln('\r\n\x1b[1;31mConnection closed\x1b[0m\r\n');
        };

        // Handle terminal input
        term.onData((data: string) => {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'input', input: data }));
          }
        });

        // Handle terminal resize
        term.onResize(({ cols, rows }: { cols: number; rows: number }) => {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'resize', cols, rows }));
          }
        });

        // Handle window resize
        const handleResize = () => fitAddon.fit();
        window.addEventListener('resize', handleResize);

        return () => {
          window.removeEventListener('resize', handleResize);
        };

      } catch (err: any) {
        if (!mounted) return;
        const message = err.response?.data?.error || err.message || 'Failed to connect';
        setStatus('error');
        setErrorMessage(message);
        onError?.(message);
      }
    };

    initializeTerminal();

    return () => {
      mounted = false;
      if (wsRef.current) {
        wsRef.current.close();
      }
      if (terminalInstance.current) {
        terminalInstance.current.dispose();
      }
    };
  }, [resourceType, resourceId, sessionType, principals, onError]);

  // Handle close
  const handleClose = useCallback(async () => {
    if (sessionId) {
      try {
        await api.delete(`/shell/sessions/${sessionId}`);
      } catch {
        // Ignore errors on close
      }
    }
    if (wsRef.current) {
      wsRef.current.close();
    }
    onClose?.();
  }, [sessionId, onClose]);

  // Status indicator color
  const statusColors: Record<ConnectionStatus, string> = {
    connecting: 'bg-yellow-500',
    connected: 'bg-green-500',
    disconnected: 'bg-gray-500',
    error: 'bg-red-500',
  };

  return (
    <div className="flex flex-col h-full bg-dark-900 rounded-lg overflow-hidden border border-dark-700">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2 bg-dark-800 border-b border-dark-700">
        <div className="flex items-center gap-3">
          <span className={`w-2.5 h-2.5 rounded-full ${statusColors[status]}`} />
          <span className="text-sm text-dark-200">
            {resourceType}/{resourceId}
          </span>
          <span className="text-xs text-dark-400 bg-dark-700 px-2 py-0.5 rounded">
            {sessionType}
          </span>
        </div>
        <button
          onClick={handleClose}
          className="px-3 py-1 text-sm text-dark-300 hover:text-white
                     hover:bg-dark-700 rounded transition-colors"
        >
          Close
        </button>
      </div>

      {/* Terminal */}
      <div className="flex-1 p-2">
        {errorMessage ? (
          <div className="flex items-center justify-center h-full text-red-400">
            <div className="text-center">
              <p className="text-lg mb-2">Connection Failed</p>
              <p className="text-sm text-dark-400">{errorMessage}</p>
              <button
                onClick={handleClose}
                className="mt-4 px-4 py-2 bg-dark-700 hover:bg-dark-600 
                           rounded transition-colors"
              >
                Close
              </button>
            </div>
          </div>
        ) : (
          <div ref={terminalRef} className="w-full h-full" />
        )}
      </div>
    </div>
  );
};

export default WebTerminal;
