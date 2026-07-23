# openbench/tb-mid@1.0.0

Pinned-image Terminal-Bench 2 mid-band pack. See `PROVENANCE.md` and `images.json`.

## Build pinned images

Build the stock runtime first, then each committed native context (no pulls are
required beyond the Dockerfiles' pinned upstream inputs):

```bash
docker build -t openbench-harness:latest obench/docker
for context in data/packs/openbench-tb-mid/*/image-context; do
  task=$(basename "$(dirname "$context")")
  docker build -t "openbench-tb2-${task}:pinned" "$context"
done
```

`task.toml` selects the immutable digest recorded in `images.json`, while
`docker_image_tag` records the human/build tag. After building, compare
`docker image inspect` RepoDigests/Id with `images.json`; a mismatch is a hard
reproducibility failure and the task must not be run.
