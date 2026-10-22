/**
 * State Color Pill Component
 *
 * Displays machine state with color-coded styling.
 */

import React from 'react';

interface StateColorPillProps {
  state: string;
  size?: 'sm' | 'md' | 'lg';
}

export const StateColorPill: React.FC<StateColorPillProps> = ({ state, size = 'md' }) => {
  const getStateColor = (s: string) => {
    switch (s.toLowerCase()) {
      case 'ready':
        return 'text-green-400 bg-green-900/30 border-green-700';
      case 'probed':
        return 'text-blue-400 bg-blue-900/30 border-blue-700';
      case 'new':
        return 'text-yellow-400 bg-yellow-900/30 border-yellow-700';
      case 'allocated':
        return 'text-cyan-400 bg-cyan-900/30 border-cyan-700';
      case 'deploying':
        return 'text-amber-400 bg-amber-900/30 border-amber-700';
      case 'failed':
        return 'text-red-400 bg-red-900/30 border-red-700';
      case 'retired':
        return 'text-gray-400 bg-gray-900/30 border-gray-700';
      default:
        return 'text-dark-400 bg-dark-800 border-dark-700';
    }
  };

  const sizeClass = {
    sm: 'px-1.5 py-0.5 text-xs',
    md: 'px-2 py-0.5 text-sm',
    lg: 'px-3 py-1 text-base',
  }[size];

  return (
    <span className={`inline-flex items-center gap-1 ${sizeClass} rounded border ${getStateColor(state)}`}>
      <svg className="h-2 w-2 rounded-full fill-current" viewBox="0 0 8 8">
        <circle cx="4" cy="4" r="4" />
      </svg>
      {state}
    </span>
  );
};

export default StateColorPill;
