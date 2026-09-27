# Backend development

This is a backend slice. Dependencies are already installed offline. Use:

```sh
pnpm exec tsc --noEmit
node --import tsx --test tests/*.test.ts
# Additional scratch TypeScript tests can use the same runner:
node --import tsx --test tests/my-check.test.ts
git status --short
git diff
```

The supplied public regression tests cover usage separation and its HTTP route.
They predate the repair. Add your own focused tests while investigating; passing
these public tests does not imply passing the hidden behavioral grader.

The original package.json and lockfile are retained for context. Its full-app
build, frontend, lint and other scripts refer to files outside this backend
slice; use the commands above. No dependency installation or network is needed.
The runtime provides TypeScript 6.0.3, tsx 4.23.5 and the backend packages/types.
Git contains one synthetic initial snapshot and no original history or remotes.
Only implementation under src/ is submitted. Test files, scratch artifacts and
Git metadata are development aids. Keep new tests outside src/. Use `rm -r` or
Python's shutil for disposable directories; the harness may reject `rm -f`.
