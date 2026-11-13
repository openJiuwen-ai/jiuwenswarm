# Replacing a local plugin with a marketplace plugin

When a marketplace package has the same plugin ID as an existing local package, installation first asks for confirmation. Cancel preserves the local package. Replace local version overwrites it with the marketplace package, including local edits, regardless of version ordering.

Replacement is rejected while the previous plugin is executing or closing resources. Retry and confirm again when it is idle. Replacement preserves the plugin ID and experience data outside the package; a failed commit restores the previous package, installation records, and the previous plugin selections in live sessions. Matching display names alone do not authorize replacement.

Ordinary local and built-in plugin installs use existing definitions without querying the marketplace, so they remain available when the marketplace is offline. When a streaming consumer task ends or is cancelled, the host closes any abandoned streams and releases their plugin reservations after resource cleanup finishes.

If a confirmation-required error appears without a dialog, ensure the frontend build is up to date and hard-refresh the browser page.
