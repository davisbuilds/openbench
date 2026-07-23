# GHCR Distribution — tb-mid Pinned Images

## Overview

The 6 Terminal-Bench 2 mid-band (tb-mid) task images are distributed from
**GitHub Container Registry (GHCR)** under the
`ghcr.io/minghinmatthewlam/` namespace. Each image is pinned by digest for
reproducibility.

## Image Registry References

Each image's full registry reference is recorded in `images.json` as the
`registry_ref` field. Two copies are kept in sync:

- `.openbench/packs/openbench/tb-mid/1.0.0/images.json` (installed pack)
- `data/packs/openbench-tb-mid/images.json` (published data)

| Task                          | Registry Ref                                                                                    |
|-------------------------------|-------------------------------------------------------------------------------------------------|
| `adaptive-rejection-sampler`  | `ghcr.io/minghinmatthewlam/openbench-tb2-adaptive-rejection-sampler@sha256:b5df6...`            |
| `merge-diff-arc-agi-task`     | `ghcr.io/minghinmatthewlam/openbench-tb2-merge-diff-arc-agi-task@sha256:11dc5...`              |
| `overfull-hbox`               | `ghcr.io/minghinmatthewlam/openbench-tb2-overfull-hbox@sha256:b3072...`                        |
| `query-optimize`              | `ghcr.io/minghinmatthewlam/openbench-tb2-query-optimize@sha256:9e847...`                       |
| `sanitize-git-repo`           | `ghcr.io/minghinmatthewlam/openbench-tb2-sanitize-git-repo@sha256:d972c...`                    |
| `winning-avg-corewars`        | `ghcr.io/minghinmatthewlam/openbench-tb2-winning-avg-corewars@sha256:61446...`                  |

## Pull by Digest

Always pull by digest (not tag) to guarantee reproducibility:

```bash
docker pull ghcr.io/minghinmatthewlam/openbench-tb2-adaptive-rejection-sampler@sha256:b5df629425a80e7ced5d68628bd2ee4d16523a6ec9c09f42b5fea900c0e1a575
```

Each image also carries a `:pinned` tag (e.g.,
`openbench-tb2-adaptive-rejection-sampler:pinned`) but the tag is a
convenience alias — the digest is the authoritative reference.

## Local-Build Fallback

If GHCR is unreachable or the registry_ref digest is not available, each
image can be built locally from its `image-context/` directory:

```bash
# From the task pack root:
cd .openbench/packs/openbench/tb-mid/1.0.0/adaptive-rejection-sampler/
docker build -t openbench-tb2-adaptive-rejection-sampler:pinned image-context/
```

The `task.toml` `docker_image` field contains the authoritative digest
reference; the `docker_image_tag` field provides the pinned tag for local
builds. Both are valid — digest for remote pull, tag for local build.

## Pushing an Update

### Prerequisites

1. **GitHub token with `write:packages` scope** — this is required to push
   to GHCR. The regular `gh` CLI token typically has only read scopes.

2. **Docker authenticated to ghcr.io** — authenticate with your GitHub
   username and a personal access token (classic) that has `write:packages`
   scope:

   ```bash
   echo <PAT_WITH_WRITE_PACKAGES> | docker login ghcr.io -u minghinmatthewlam --password-stdin
   ```

   Or use the `gh` CLI token if it has the right scope (see below).

### One-Time Setup: Add `write:packages` Scope

The current `gh` auth token has scopes: `admin:public_key`, `gist`,
`read:org`, `repo`. It does **not** have `write:packages`, which is required
for pushing container images.

**Manual step — do this once:**

1. Go to https://github.com/settings/tokens
2. Create a new **classic** personal access token (or update an existing one)
3. Enable the **`write:packages`** scope (under `admin:write:packages` → this
   grants both read and write)
4. Use this token for Docker authentication:

   ```bash
   echo <TOKEN> | docker login ghcr.io -u minghinmatthewlam --password-stdin
   ```

### Push Command

For each image you want to push:

```bash
# Pull the existing image reference from images.json to get the ref
REGISTRY_REF="ghcr.io/minghinmatthewlam/openbench-tb2-adaptive-rejection-sampler@sha256:b5df629425a80e7ced5d68628bd2ee4d16523a6ec9c09f42b5fea900c0e1a575"

# Tag the local image with the registry reference
docker tag openbench-tb2-adaptive-rejection-sampler:pinned "$REGISTRY_REF"

# Push by digest
docker push "$REGISTRY_REF"
```

Or push all 6 at once using the `images.json` data:

```bash
python3 -c "
import json
with open('.openbench/packs/openbench/tb-mid/1.0.0/images.json') as f:
    data = json.load(f)
for name, entry in data.items():
    ref = entry['registry_ref']
    print(f'Pushing {name}: {ref}')
    import subprocess
    subprocess.run(['docker', 'push', ref], check=True)
"
```

### Verify

After pushing, verify the image is available:

```bash
docker pull ghcr.io/minghinmatthewlam/openbench-tb2-adaptive-rejection-sampler@sha256:b5df629425a80e7ced5d68628bd2ee4d16523a6ec9c09f42b5fea900c0e1a575
docker run --rm ghcr.io/minghinmatthewlam/openbench-tb2-adaptive-rejection-sampler@sha256:b5df629425a80e7ced5d68628bd2ee4d16523a6ec9c09f42b5fea900c0e1a575 python3 -c "print('ok')"
```

## Auth Single-Writer Rule

**Each benchmark machine does its own `pi /login` (or equivalent). Never
copy `auth.json` between machines.** This applies independently of GHCR
authentication — GHCR tokens are for image distribution; harness tokens are
for API authentication during benchmark runs.
