export const EXIT_SUCCESS = 0;
const EXIT_RUNTIME_FAILURE = 1;
const EXIT_INVALID_USAGE = 2;
const EXIT_UNAVAILABLE = 3;
const EXIT_NOT_FOUND = 4;
const EXIT_PARTIAL_SUCCESS = 5;

export type CliExitCode =
  | typeof EXIT_SUCCESS
  | typeof EXIT_RUNTIME_FAILURE
  | typeof EXIT_INVALID_USAGE
  | typeof EXIT_UNAVAILABLE
  | typeof EXIT_NOT_FOUND
  | typeof EXIT_PARTIAL_SUCCESS;

export class CliError extends Error {
  readonly exitCode: CliExitCode;

  constructor(message: string, exitCode: CliExitCode = EXIT_RUNTIME_FAILURE) {
    super(message);
    this.name = 'CliError';
    this.exitCode = exitCode;
  }
}

export function invalidUsage(message: string): CliError {
  return new CliError(message, EXIT_INVALID_USAGE);
}

export function unavailable(message: string): CliError {
  return new CliError(message, EXIT_UNAVAILABLE);
}

export function notFound(message: string): CliError {
  return new CliError(message, EXIT_NOT_FOUND);
}

export function partialSuccess(message: string): CliError {
  return new CliError(message, EXIT_PARTIAL_SUCCESS);
}

export function exitCodeForError(error: unknown): CliExitCode {
  if (error instanceof CliError) return error.exitCode;
  return EXIT_RUNTIME_FAILURE;
}

export function messageForError(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return String(error);
}
