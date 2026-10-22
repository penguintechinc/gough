# WebUI Integration Notes

## New Pages & Components

### Pages Created
1. **BiomesPageExtended.tsx** — Replaces existing BiomesPage with extended filtering
2. **BiomeDetail.tsx** — New detailed view with 7 tabs
3. **CapacityPage.tsx** — New capacity planning & forecasting
4. **AuditLogPage.tsx** — New audit trail viewer (under `/audit` route)
5. **JoinerSecretsPage.tsx** — New secrets manager (under `/settings/joiner-secrets`)

### Components Created
- StateColorPill — Machine state badge with color coding
- HardwareTagFilter — Autocompleting tag selector
- EligibilityCheck — Requirement verification display
- SmartHealthBadge — Disk health indicator
- DiskPlanEditor — Partition editor with drag-reorder
- LicenseGatedFeature — License validation wrapper

### Type Definitions
- provisioning.ts — 16+ interfaces covering all entities

## Routing Integration

Add to your React Router config:

```tsx
import BiomesPageExtended from './pages/Provisioning/BiomesPageExtended';
import BiomeDetail from './pages/Provisioning/BiomeDetail';
import CapacityPage from './pages/Provisioning/CapacityPage';
import AuditLogPage from './pages/Audit/AuditLogPage';
import JoinerSecretsPage from './pages/Settings/JoinerSecretsPage';

const routes = [
  // Provisioning
  { path: '/provisioning/biomes', element: <BiomesPageExtended /> },
  { path: '/provisioning/biomes/:eggId', element: <BiomeDetail /> },
  { path: '/provisioning/capacity', element: <CapacityPage /> },
  
  // Audit
  { path: '/audit', element: <AuditLogPage /> },
  
  // Settings
  { path: '/settings/joiner-secrets', element: <JoinerSecretsPage /> },
];
```

## API Endpoints Expected

### Provisioning API
- GET `/provisioning/machines` — List machines
- GET `/provisioning/machines/:id` — Machine detail
- GET `/provisioning/biomes` — List biomes
- GET `/provisioning/biomes/:id` — Biome detail
- GET `/provisioning/capacity/forecast?horizon=7d` — Capacity forecast
- GET `/provisioning/capacity/migration-policy` — Migration policy
- PUT `/provisioning/capacity/migration-policy` — Update policy
- GET `/provisioning/capacity/migrations` — Migration events

### Audit API
- GET `/audit/events` — List events (supports pagination)
- GET `/audit/events/:id` — Event detail
- POST `/audit/verify` — Verify chain integrity

### Settings API
- GET `/settings/joiner-secrets` — List secrets
- POST `/settings/joiner-secrets/:id/rotate` — Rotate secret
- POST `/settings/joiner-secrets/:id/revoke` — Revoke secret
- POST `/settings/joiner-secrets/:id/ascertain` — Ascertain secret (admin-only)

### License API
- GET `/license/check?feature=waddleai` — Check license (returns 402 if not licensed)

## Styling

All components use:
- TailwindCSS v4
- Dark theme (dark-950, dark-900, dark-800 backgrounds)
- Gold accents (text-gold-500, bg-gold-600)
- State colors: green-400 (ready), red-400 (failed), yellow-400 (warning), blue-400 (deploying)

Custom color classes used (ensure defined in tailwind.config.js):
- dark-950 to dark-300 (grayscale)
- gold-400 to gold-600 (accents)
- green-400, red-400, yellow-400, blue-400 (status)

## Console Logging

All components follow the required format:
```typescript
console.log('[ComponentName] Action { key: value }');
// Examples:
console.log('[BiomesPageExtended] Fetch biomes { count: 42 }');
console.log('[DiskPlanEditor] Toggle dark drive { index: 2, isDark: true }');
```

Never log:
- Tokens, passwords, API keys
- PII (emails, phone numbers — log `email: "user@..."` instead)
- Full secret values
- MFA codes or CAPTCHA responses

## Testing

Playwright smoke tests created at `/tests/e2e/smoke-*.spec.ts`:
1. Login page (form validation, error handling)
2. Machines page (filtering, selection, navigation)
3. Biomes page (filtering by kind/phase, eligibility)
4. Biome detail (all 7 tabs)
5. Capacity page (forecast, policy editor, license gating)
6. Audit log (chain verification, filtering, pagination)
7. Joiner secrets (rotate/revoke/ascertain workflows)

Run tests:
```bash
npm run test:e2e
# or
npx playwright test tests/e2e/smoke-*.spec.ts
```

## Known Limitations

### No Jest Unit Tests
Jest tests (>.90% coverage) are specified but left for separate implementation.
Component structure is test-ready (pure functions, minimal side effects).

### No D3 Dependency Graph Visualization
BiomeDetail Dependencies tab structure is ready for D3 DAG. Add:
```tsx
import EggDependencyGraph from '../../components/Provisioning/EggDependencyGraph';

// In dependencies tab:
<EggDependencyGraph dependencies={biome.dependencies} />
```

### No Mock API Server
Tests assume real API endpoints. Mock setup can be added via:
- MSW (Mock Service Worker) for Playwright
- Storybook + Chromatic for component docs
- Vitest + vi.mock() for Jest unit tests

## Environment Variables

Ensure backend API is accessible:
- Dev: `http://localhost:8080/api/v1`
- Prod: Backend URL from environment

See `src/client/lib/api.ts` for axios instance configuration.

## Accessibility

All components implement:
- Keyboard navigation (Tab, Enter, Escape)
- ARIA labels on interactive elements
- WCAG AA contrast (4.5:1 body, 3:1 large text)
- Semantic HTML (button, select, table, etc.)
- prefers-reduced-motion respected

Test with:
```bash
axe DevTools browser extension
WAVE accessibility evaluator
keyboard-only navigation (Tab through all pages)
```

## Mobile Responsiveness

Tested breakpoints:
- 320px (mobile)
- 768px (tablet)
- 1024px+ (desktop)

All pages responsive with `grid-cols-1 md:grid-cols-2 lg:grid-cols-3` patterns.

## Future Extensions

1. **D3 Dependency Graph** — Add visual DAG in BiomeDetail
2. **Jest Tests** — ≥90% coverage on all components
3. **Storybook** — Document components + usage
4. **Permission Gating** — Operator vs Maintainer vs Admin UI differences
5. **Real-time Updates** — SSE integration for machine state changes
6. **Webhooks UI** — Webhook configuration & testing (from Settings spec)
7. **Bulk Actions Wizard** — Multi-step provisioning workflow
