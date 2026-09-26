import { Router, type Request, type Response } from 'express';
import { getStats, getToolAnalytics, getCostOverTime, getCostByProject, getCostByModel, getProviderQuotas } from '../db/queries.js';

export const statsRouter = Router();

// GET /api/stats - Aggregated statistics
statsRouter.get('/', (req: Request, res: Response) => {
  const stats = getStats({
    agentType: req.query.agent_type as string | undefined,
    since: req.query.since as string | undefined,
  });
  const quota_monitor = getProviderQuotas();

  // Keep usage_monitor as a compatibility alias while the Monitor surface migrates.
  res.json({ ...stats, quota_monitor, usage_monitor: quota_monitor });
});

// GET /api/stats/tools - Tool analytics
statsRouter.get('/tools', (req: Request, res: Response) => {
  const tools = getToolAnalytics({
    agentType: req.query.agent_type as string | undefined,
    since: req.query.since as string | undefined,
  });
  res.json({ tools });
});

// GET /api/stats/cost - Cost breakdowns
statsRouter.get('/cost', (req: Request, res: Response) => {
  const costFilters = {
    agentType: req.query.agent_type as string | undefined,
    since: req.query.since as string | undefined,
  };
  const [timeline, byProject, byModel] = [
    getCostOverTime(costFilters),
    getCostByProject(Number(req.query.limit) || 10, costFilters),
    getCostByModel(costFilters),
  ];
  res.json({ timeline, by_project: byProject, by_model: byModel });
});

// GET /api/stats/usage-monitor - Compatibility alias for provider quota snapshots
statsRouter.get('/usage-monitor', (_req: Request, res: Response) => {
  res.json(getProviderQuotas());
});
