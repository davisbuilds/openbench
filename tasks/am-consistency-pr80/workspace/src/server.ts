import { installRuntimeSignalHandlers, startAgentMonitorRuntime } from './runtime.js';

const runtime = startAgentMonitorRuntime();
installRuntimeSignalHandlers(runtime);
await runtime;
