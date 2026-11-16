# Deployed configuration

The host installer validates private host.toml and renders scanner.toml and automation.toml
through their owning packages. scanner.toml contains game/account/character transport paths,
server URL and polling/refresh intervals. automation.toml pins independent native identity,
process/window facts and its canvas crop. Neither component loads the host contract at runtime.

The host example is [host.example.toml](../../apps/host/src/crowley_host/ops/package_data/config/host.example.toml).
Actual window/capture bounds are
validated by desktop adapters; unused outer-window calibration fields have been removed.
Credentials remain only in private secrets.env, mode 0600. Production configuration and
package-owned addon copies never point into a source checkout. Review install --dry-run
and doctor before the coordinated cutover.
