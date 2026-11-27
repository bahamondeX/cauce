# OCI runtime verification

`scripts/test-oci-runtime.py` loads a temporary Docker archive built
from a selected manifest's exact OCI config and layer blobs. It
checks the imported config digest and rootfs diff IDs before launch,
then checks the in-container binary SHA-256 values against the OCI
ledger. It cleans up its own temporary tags, containers and files.

The Milestone 10 and Milestone 11 Full and Core Linux ARM64 layouts
ran in the existing Colima Docker runtime. They used the packaged entrypoint and
validated the packaged TOML. Both ran as UID/GID 10001 with writable
service directories and a read-only certificate fixture. The host
client verified the local certificate and received the expected HTTPS
response. An independent process in the container network namespace
checked `/ready`. Both containers exited successfully on SIGTERM.
The test covers idle shutdown. Existing host socket tests carry the
active-session drain evidence.

These runtime cases use ARM64 on a local VM. The AMD64 evidence is
separately marked as user-mode emulation. Native comparative hardware
qualification remains open.

[Milestone 10 image runtime evidence](../bench/program/evidence/milestone-11/oci-m10-arm64-runtime.json)
[Milestone 11 image runtime evidence](../bench/program/evidence/milestone-11/oci-arm64-runtime.json).
