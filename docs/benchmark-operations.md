# Campaign launch and recovery

All multi-trial campaigns must run in a named tmux session on the execution
host. This applies to canonical Harbor suites and explicitly chosen native
compatibility experiments. It does not change either runner's scheduling,
resume or credential policy.

## Before launch

1. Verify `hostname -s`, read `agents.env` if present, and inspect existing
   benchmark processes and tmux sessions. Do not create a duplicate dispatcher.
2. Pin the clean, pushed commit, runtime versions, task/config hashes, result
   destination, credential lane, exact command and timeout/stop procedure.
   Keep local receipts and raw logs under ignored `results/`; never put secrets
   in commands or receipts. Keep this checkout unchanged while it executes.
3. Choose a host that can stay awake. Prefer the Mini for unattended work if
   the laptop will be closed. Deploy the pinned code and verify dependencies,
   authentication and controls there first; do not blindly move an active run
   or copy daily credentials between hosts.
4. On a MacBook, keep the lid open and power connected. Use `caffeinate -i`
   around the runner to prevent idle sleep for its lifetime. Check the actual
   assertion using `pmset -g assertions`. `tmux` provides terminal persistence;
   neither it nor this idle-sleep assertion guarantees execution while the lid
   is closed. [Apple documents closing the display as a sleep action](https://support.apple.com/en-sg/guide/mac-help/-mh10330/mac).

## Launch and observe

Create a private, run-specific directory and a launch script containing the
reviewed command. The following is a script template; replace the command and
paths before execution. On Linux use the host's established awake policy and
omit the macOS-only `caffeinate` wrapper.

```sh
#!/bin/sh
set -u
umask 077
cd /absolute/path/to/pinned-checkout || exit 1
run_dir=/absolute/path/to/new-run-directory
mkdir -p "$run_dir" || exit 1
# Exclusive creation refuses reuse of this launch receipt. Resume gets a new
# receipt directory and the runner's own verified resume arguments.
(set -C; : > "$run_dir/started.txt") || exit 1
date -u '+%Y-%m-%dT%H:%M:%SZ' > "$run_dir/started.txt"
/usr/bin/caffeinate -i /absolute/path/to/obench run /absolute/path/to/suite.toml \
  > "$run_dir/campaign.log" 2>&1
run_exit=$?
date -u '+%Y-%m-%dT%H:%M:%SZ' > "$run_dir/finished.txt"
printf '%s\n' "$run_exit" > "$run_dir/exit-code.txt"
exit "$run_exit"
```

Launch with a unique session name, passing the script path as an argument:

```sh
tmux new-session -d -s obench-study-YYYYMMDD -c /absolute/path/to/pinned-checkout \
  /bin/sh /absolute/path/to/launch.sh
tmux has-session -t obench-study-YYYYMMDD
tmux list-panes -t obench-study-YYYYMMDD -F '#{pane_pid} #{pane_current_command}'
pmset -g assertions
```

Inspect the actual runner process and first log/evidence activity as well as
the tmux session. Send the user the host, session name, log/results location and
next status-check plan. A tmux session alone does not prove a trial is making
progress. Use a persistent monitor when the session must report completion
without further interaction; record its log and completion result too.

The session exits when this script finishes; its receipts survive. A missing
`exit-code.txt` means completion is unknown, not success. Verify the runner's
ledger and intended result coverage before declaring completion. Retain failure
receipts. Do not launch a second copy simply because a session disappeared.

## Sleep, restart and interruption

- Closing a terminal or losing SSH should leave the tmux-hosted runner alive.
  Host sleep suspends local work and can disrupt API connections. Reboot ends
  tmux and its processes; neither is a checkpoint mechanism.
- After unexpected sleep, inspect process state, fresh transcript/tool events,
  queue state, result identities and `pmset -g log`. Preserve a local incident
  note, including sleep/wake times and potentially affected attempts.
- Recorded monotonic durations need not equal elapsed calendar time across
  sleep. Do not publish interrupted runs as uninterrupted latency measurements.
- Keep checker score, harness completion and timeout separate: a timed-out
  agent can leave an artifact that passes. Preserve that observation and its
  timeout, then decide whether a separately identified diagnostic repeat is
  needed. Do not silently replace results or call it a clean completed solve.
- Resume only after confirming no original worker remains and verifying the
  runner's exact resume identity/inputs. Stopping a tmux session is not proof
  of descendant cleanup; use the runner's stop procedure and verify processes.
