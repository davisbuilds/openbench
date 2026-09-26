import { Router, type Request, type Response } from 'express';
import { getFilterOptions } from '../db/queries.js';

export const filterOptionsRouter = Router();

// GET /api/filter-options - Distinct values for dashboard dropdowns
filterOptionsRouter.get('/', (_req: Request, res: Response) => {
  const options = getFilterOptions();
  res.json(options);
});
