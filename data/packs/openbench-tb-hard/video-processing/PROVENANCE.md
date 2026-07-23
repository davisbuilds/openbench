# Provenance

- **Imported by**: obench import harbor (obench 0.1.0)
- **Import date**: 2026-07-21
- **Source path**: `/tmp/tb-hard-candidates/video-processing`
- **Harbor schema_version seen**: 1.1

## License

**You must verify the upstream dataset / task license before publishing
or redistributing this imported task.** The importer does not inspect or
grant rights. Harbor itself is Apache-2.0; individual Harbor registry
datasets and Terminal-Bench tasks carry their own terms.

## Auto-converted

- `instruction.md` copied from Harbor (agent-facing text unchanged).
- `workspace/` staged from `environment/Dockerfile` COPY/ADD sources
  (build context = `environment/`).
- `checker_data/harbor-tests/` holds Harbor `tests/` (verifier-owned;
  not visible as agent workspace).
- `checker.sh` wraps Harbor `tests/test.sh`, maps `reward.txt` /
  `reward.json` → OpenBench exit 0 / `SCORE:` / fail
  (same field preference as `obench export harbor`: JSON keys
  `reward`, `score`, `accuracy`, else first numeric).
- Solution handling:
  - materialized 1 file(s) by running solve.sh under env -i (no network)

## Needs attention

- **DOCKER-REQUIRED**: local `--exec local` may fail.
  - RUN directive: RUN apt-get update && apt-get install -y libgl1-mesa-glx libglib2.0-0 libsm6 lib
  - RUN directive: RUN pip install "opencv-contrib-python>=4.11.0.86"
  - Prefer `--exec docker` with an image that matches the Harbor base image / packages noted in `REQUIREMENTS.md`.

## Published frontier solve rate

- **Rate:** 1/13 = **7.7%**
- **Source:** published Terminal-Bench 2.0 leaderboard submissions in the frontier panel documented in the pack-level `PROVENANCE.md`; snapshot and calculation method are pinned there.
