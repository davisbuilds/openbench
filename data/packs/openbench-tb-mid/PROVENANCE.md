# Pack provenance

Terminal-Bench 2 commit `2fd12b88aafdd04a52c298e3940bcb189f9766d6`, Apache-2.0. Each task uses its own pinned image; see `images.json`.

## Selection outcome

Six of eight approved tasks survived the no-network polarity gate. `protein-assembly` was dropped because its reference solution requires live RCSB/PubChem/FPbase queries and therefore could not be made a faithful no-network checker oracle without pre-materializing scientific answers. `configure-git-webserver` was dropped because the upstream verifier installs `expect` at check time and its service/SSH workflow did not pass under `--network none` without materially rewriting the verifier.
