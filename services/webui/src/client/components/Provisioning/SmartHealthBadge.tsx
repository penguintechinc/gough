/**
 * SMART Health Badge Component
 *
 * Displays disk health status from SMART data.
 */

import React from 'react';

interface SmartHealthBadgeProps {
  status: 'healthy' | 'warning' | 'critical' | 'unknown';
  temperature?: number;
  powerOnHours?: number;
  predictedFailure?: boolean;
}

export const SmartHealthBadge: React.FC<SmartHealthBadgeProps> = ({
  status,
  temperature,
  powerOnHours,
  predictedFailure,
}) => {
  const getStatusColor = (s: string) => {
    switch (s) {
      case 'healthy':
        return { bg: 'bg-green-900/30', border: 'border-green-700', text: 'text-green-300', icon: '✓' };
      case 'warning':
        return { bg: 'bg-yellow-900/30', border: 'border-yellow-700', text: 'text-yellow-300', icon: '!' };
      case 'critical':
        return { bg: 'bg-red-900/30', border: 'border-red-700', text: 'text-red-300', icon: '⚠' };
      default:
        return { bg: 'bg-gray-900/30', border: 'border-gray-700', text: 'text-gray-300', icon: '?' };
    }
  };

  const colors = getStatusColor(status);

  return (
    <div className={`inline-flex flex-col gap-1 px-2 py-1 ${colors.bg} border ${colors.border} rounded`}>
      <div className={`flex items-center gap-1 text-sm font-medium ${colors.text}`}>
        <span>{colors.icon}</span>
        {status.charAt(0).toUpperCase() + status.slice(1)}
      </div>

      <div className="text-xs text-dark-400 space-y-0.5">
        {temperature !== undefined && (
          <div>Temp: {temperature}°C</div>
        )}
        {powerOnHours !== undefined && (
          <div>Hours: {powerOnHours.toLocaleString()}</div>
        )}
        {predictedFailure && (
          <div className="text-red-400 font-medium">Predicted failure</div>
        )}
      </div>
    </div>
  );
};

export default SmartHealthBadge;
