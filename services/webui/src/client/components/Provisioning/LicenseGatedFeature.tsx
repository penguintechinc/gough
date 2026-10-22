/**
 * License Gated Feature Component
 *
 * Wraps gated controls with license status banner.
 */

import React from 'react';

interface LicenseGatedFeatureProps {
  featureName: string;
  licensed: boolean;
  licenseError?: string;
  httpStatus?: number;
  children: React.ReactNode;
}

export const LicenseGatedFeature: React.FC<LicenseGatedFeatureProps> = ({
  featureName,
  licensed,
  licenseError,
  httpStatus,
  children,
}) => {
  if (!licensed) {
    return (
      <div className="space-y-4">
        <div className={`p-4 rounded-lg border ${httpStatus === 402 ? 'bg-red-900/20 border-red-700' : 'bg-yellow-900/20 border-yellow-700'}`}>
          <div className="flex items-start gap-3">
            <svg
              className={`h-5 w-5 flex-shrink-0 mt-0.5 ${httpStatus === 402 ? 'text-red-400' : 'text-yellow-400'}`}
              fill="currentColor"
              viewBox="0 0 20 20"
            >
              <path fillRule="evenodd" d="M18 5v8a2 2 0 01-2 2h-5l-5 4v-4H4a2 2 0 01-2-2V5a2 2 0 012-2h12a2 2 0 012 2zm-11-1a1 1 0 100-2 1 1 0 000 2z" clipRule="evenodd" />
            </svg>
            <div className="flex-1">
              <h3 className={`font-semibold ${httpStatus === 402 ? 'text-red-300' : 'text-yellow-300'}`}>
                {featureName} Not Available
              </h3>
              {licenseError && (
                <p className={`text-sm mt-1 ${httpStatus === 402 ? 'text-red-400' : 'text-yellow-400'}`}>
                  {licenseError}
                </p>
              )}
            </div>
          </div>
        </div>
        <div className="opacity-50 pointer-events-none">{children}</div>
      </div>
    );
  }

  return <>{children}</>;
};

export default LicenseGatedFeature;
