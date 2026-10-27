import { listCommands, registerCommand } from './commands.js';
import { registerDatabaseCommands } from './commands/database.js';
import { registerHookCommands } from './commands/hooks.js';
import { registerMaintenanceCommands } from './commands/maintenance.js';
import { registerReportingCommands } from './commands/reporting.js';
import { registerRuntimeCommands } from './commands/runtime.js';
import { registerSessionLiveCommands } from './commands/sessions-live.js';
import { registerWarehouseCommands } from './commands/warehouse.js';
import { commandHelp, rootHelp } from './help.js';
import { writeStdout } from './output.js';

let registered = false;

export function registerAllCommands(): void {
  if (registered) return;
  registered = true;

  registerRuntimeCommands();
  registerDatabaseCommands();
  registerMaintenanceCommands();
  registerSessionLiveCommands();
  registerReportingCommands();
  registerHookCommands();
  registerWarehouseCommands();

  registerCommand({
    name: 'help',
    group: 'Meta Commands',
    summary: 'Show root help or command help',
    usage: 'help [command]',
    handler(ctx, args) {
      if (args.length === 0) {
        writeStdout(ctx, rootHelp(ctx.commandName, listCommands()));
        return;
      }
      writeStdout(ctx, commandHelp(ctx.commandName, args.join(' '), 'Command help is available with --help.'));
    },
  });
}
