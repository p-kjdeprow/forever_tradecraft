# Private GNOME telemetry-region producer

This directory contains the reviewed GNOME Screenshot 41.0 source patch and
reproducible private build recipe for Crowley's default-off native telemetry
capture. The producer accepts one typed `--telemetry-region` request with
four integer coordinates, uses the ordinary GNOME Shell `ScreenshotArea`
backend with `flash=FALSE`, and builds with `-Dx11=disabled`. It does not
change the stock full-screen, window, or interactive paths.

`build-private.sh` must run in the approved isolated Debian Bookworm container,
with only a new private build cache mounted at its exact path. Copy the
verified upstream/Debian archives and this patch, recipe, manifest, README,
and offline harness into that cache before the container starts; the host
repository itself is not mounted into the build container.
The container invocation installs the recorded build dependencies, including
`patch` and `xz-utils` for the pinned source archives; it installs no host packages, writes
no service or desktop settings, and does not activate a screenshot. The stage contains only the verified GNOME archive,
Debian patch archive, build outputs, logs, source hashes, and private prefix.

The Python driver opens the configured binary, verifies its owner/mode/hash,
copies the exact bytes into a sealed Linux memfd, and executes that retained
image through the positive process-admission wrapper. The child sends a
nonce-bound primary-owner receipt over the inherited capability pipe and waits
for the parent ACK before calling `ScreenshotArea`. The stock executable is
selected whenever the profile capability is absent.

The container build leaves root-owned output. Before selecting the staged binary
in the production profile, install its ownership for the account running the
daemon and verify its checksum still matches `manifest.json`:

```sh
stage_dir=/path/to/private/gnome-telemetry-region-stage
producer_path="$stage_dir/prefix/bin/gnome-screenshot"
sudo chown -- "$(id -u):$(id -g)" "$producer_path"
chmod 0700 "$producer_path"
sha256sum "$producer_path"
```

This changes installation metadata only. A user-owned fixture copy does not
validate the configured cache binary's owner or permissions. Keep the driver's
private-executable check intact.

The private binary reserves a per-user screenshot invocation slot before
registering with GNOME's single-instance application. It opens the explicit
`XDG_RUNTIME_DIR` without following symlink components, requires a directory
owned by the effective user with mode `0700`, and holds a nonblocking `flock`
on a mode-`0600` regular file until exit. This keeps concurrent ordinary and
telemetry invocations from reparsing the primary process's shared options.
While a telemetry capture is active, the telemetry process also rejects
GApplication activation and every registered action route before option
mutation, capture, or UI dispatch.
Cold ordinary screenshot commands still use the stock GNOME path. The harmless
binary checks require a private runtime directory:

```text
runtime_dir=$(mktemp -d)
chmod 700 "$runtime_dir"
XDG_RUNTIME_DIR="$runtime_dir" gnome-screenshot --version
XDG_RUNTIME_DIR="$runtime_dir" gnome-screenshot --help
python3 tools/gnome-telemetry-region/offline_harness.py --self-check
```

The reproducible container command mounts only the new private stage directory
(read/write) and
uses the pinned
`debian@sha256:2c037a04925515fdd6ea85ea14a682d0e79931f5e9f5d07b6dbfc6ba12f9e858`
image. The exact package list is `appstream ca-certificates desktop-file-utils
file gettext gcc libglib2.0-dev libgtk-3-dev libhandy-1-dev libx11-dev
libxext-dev libxml2-utils meson ninja-build patch pkg-config python3 xz-utils`.
The command installs these packages inside the temporary container and invokes
the cache-side `build-private.sh`; the script fails unless
`GNOME_TELEMETRY_REGION_CONTAINER_DIGEST` equals the pinned digest and records
the installed package versions. Prepare only the verified archives, Debian
patch archive, source patch, recipe, manifest, README, and offline harness in
the private stage before starting the container. It does not mount the host
repository, home directory, display socket, Xauthority, service socket, or
credentials. The stage argument is mandatory so the caller chooses the
reviewed private build location explicitly.

The exact invocation is:

```sh
docker run --rm --network bridge \
  --mount type=bind,src="$stage_dir",dst=/stage \
  debian@sha256:2c037a04925515fdd6ea85ea14a682d0e79931f5e9f5d07b6dbfc6ba12f9e858 \
  bash -lc 'set -euxo pipefail; apt-get update; apt-get install -y --no-install-recommends appstream ca-certificates desktop-file-utils file gettext gcc libglib2.0-dev libgtk-3-dev libhandy-1-dev libx11-dev libxext-dev libxml2-utils meson ninja-build patch pkg-config python3 xz-utils; mkdir -m 700 -p /stage/build-runtime; export XDG_RUNTIME_DIR=/stage/build-runtime; GNOME_TELEMETRY_REGION_CONTAINER_DIGEST=sha256:2c037a04925515fdd6ea85ea14a682d0e79931f5e9f5d07b6dbfc6ba12f9e858 bash /stage/tools/gnome-telemetry-region/build-private.sh /stage'
```

No live display, Shell call, input, service activation, profile binding, or
native-100 run is part of this offline build.
