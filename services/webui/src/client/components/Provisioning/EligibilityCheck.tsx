/**
 * Eligibility Check Component
 *
 * Shows ✓/✗ per candidate with human-readable elimination reasons.
 */

import React from 'react';

interface Requirement {
  name: string;
  satisfied: boolean;
  reason?: string;
}

interface EligibilityCheckProps {
  requirements: Requirement[];
  title?: string;
  compact?: boolean;
}

export const EligibilityCheck: React.FC<EligibilityCheckProps> = ({
  requirements,
  title,
  compact = false,
}) => {
  const allSatisfied = requirements.every((r) => r.satisfied);

  return (
    <div className={`${compact ? '' : 'bg-dark-900 border border-dark-700 rounded-lg'}`}>
      {title && (
        <div className={`${compact ? '' : 'px-4 py-3 border-b border-dark-700'}`}>
          <h3 className="font-medium text-white">{title}</h3>
        </div>
      )}

      <div className={compact ? '' : 'p-4'}>
        <div className="space-y-2">
          {requirements.map((req) => (
            <div key={req.name} className="flex items-start gap-3">
              {req.satisfied ? (
                <svg className="h-5 w-5 text-green-400 flex-shrink-0 mt-0.5" fill="currentColor" viewBox="0 0 20 20">
                  <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" />
                </svg>
              ) : (
                <svg className="h-5 w-5 text-red-400 flex-shrink-0 mt-0.5" fill="currentColor" viewBox="0 0 20 20">
                  <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clipRule="evenodd" />
                </svg>
              )}

              <div className="flex-1">
                <p className={`text-sm font-medium ${req.satisfied ? 'text-green-300' : 'text-red-300'}`}>
                  {req.name}
                </p>
                {req.reason && !req.satisfied && (
                  <p className="text-xs text-dark-400 mt-0.5">{req.reason}</p>
                )}
              </div>
            </div>
          ))}
        </div>

        {title && (
          <div className={`mt-4 pt-3 border-t border-dark-700 flex items-center gap-2 text-sm ${allSatisfied ? 'text-green-400' : 'text-red-400'}`}>
            {allSatisfied ? (
              <>
                <svg className="h-4 w-4" fill="currentColor" viewBox="0 0 20 20">
                  <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" />
                </svg>
                All requirements met
              </>
            ) : (
              <>
                <svg className="h-4 w-4" fill="currentColor" viewBox="0 0 20 20">
                  <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clipRule="evenodd" />
                </svg>
                Not eligible
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
};

export default EligibilityCheck;
