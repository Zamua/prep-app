// Every route a user cell serves. The entry worker matches against the same
// list to tell a route that needs sign-in from one that does not exist.
import type { Route } from '../router.js';
import { apiRoutes } from './api.js';
import { jobRoutes } from './jobs.js';
import { pageRoutes } from './pages.js';

export const CELL_ROUTES: readonly Route[] = [...pageRoutes, ...jobRoutes, ...apiRoutes];
