Create a Python function

```python
async def run_tasks(tasks: list[Callable[[], Awaitable[None]]], max_concurrent: int) -> None
```

where each task is an async job to be run and `max_concurrent` is the maximum
number of tasks that may run concurrently. Put the function in a file called
`run.py` in the working directory so that it can be imported using
`from run import run_tasks`.

Use the system Python (standard library is sufficient). Sometimes runs are
cancelled via a keyboard interrupt (SIGINT), but the tasks' cleanup code (the
`finally` blocks) must still run to completion for every task that has started —
including when a cancellation arrives. After cancellation begins, tasks that have
not yet started must not be started; only started tasks' cleanups must run.
