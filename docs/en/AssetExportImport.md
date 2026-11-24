# Asset Export & Import

> **Objective**: Explain the unified export and import of expert and expert-team asset packages: how to export a package, import it in another environment, and handle error messages.
>
> **中文版：** [资产导入导出](../zh/资产导入导出.md)

---

## 1. Overview

Unified asset export/import packs local **experts (agent_template)** and **expert teams (agent_group)** into standard `.zip` asset packages for backup, migration, and distribution across environments (development machines, staging, production deployments).

### 1.1 What Is an Asset Package

An asset package is a `.zip` archive with a **single root directory**: the root directory name is the asset id, and the `manifest.json` inside it declares the asset type, name, and version.

```text
expert.demo.zip
└── expert.demo/
    ├── manifest.json
    ├── persona/…
    └── …
```

- Export produces this structure automatically. The download file name is `<name>.zip`, or `<name>-<version>.zip` when the manifest declares a version; `<name>` is the display name shown in the UI, falling back to the asset id. The top-level directory inside the package is always the asset id, independent of the download file name;
- Import unpacks the archive safely first, then validates and commits it based on the root manifest.

### 1.2 Supported Asset Types

| Asset type | Export | Import | Behavior after import |
|-----------|--------|--------|----------------------|
| Expert (agent_template) | Yes | Yes | The page chains into the install flow automatically (see 3.3) |
| Expert team (agent_group) | Yes | Yes | Import equals install; the team appears in "Mine" directly |

> 💡 **Note**: For skills, plugins, and other asset types, keep using the existing upload/install entries on their own pages.

### 1.3 Typical Scenarios

- Migrate a tuned expert or expert team from a development environment to production;
- Back up locally created assets before a system upgrade or reinstall;
- Distribute an expert team to colleagues — they import it and start using it.

---

## 2. Exporting Assets

### 2.1 Entry Point and Visibility

Open the detail page of an expert or expert team and click **Export** in the action area. The system builds the asset package and triggers the download:

- **Desktop app**: a save dialog opens; choose a local directory;
- **Browser**: the download starts directly, or you are asked for a save location.

Button visibility matches what the server can actually export:

| Asset | Export button is shown when |
|-------|----------------------------|
| Expert | The package was created locally, or it is an installed built-in/Hub copy |
| Expert team | Always, except for Hub assets that are not installed (uninstalled local/built-in packages included) |

> ⚠️ **Note**: Uninstalled shelf and Hub assets have no local materials to pack, so the export button is hidden for them.

### 2.2 What Export Does to the Content

Export goes through the shared safe-packing layer, which automatically:

- **Drops redundant directories**: `.git`, `.archive`, `__pycache__`, `.pytest_cache`, `node_modules`, `out`, `.venv`, plus miscellaneous files such as `.DS_Store`;
- **Drops sensitive files**: private keys (`id_rsa`, `id_ed25519`), `credentials.json`, `token.json` / `tokens.json`, `*.key` / `*.p12` / `*.pfx`, and real `.env` files. Only `.env.example` / `.env.template` may travel with the package, and their content must not contain plaintext secrets or URLs with embedded credentials;
- **Enforces size limits**: archive ≤50MB, single file ≤64MB, extracted total ≤512MB, file count ≤10000, directory depth ≤32. Exceeding a limit fails the export with an error.

The download link produced by an export is **short-lived** (about 10 minutes); complete the save promptly.

---

## 3. Importing Assets

### 3.1 Entry Point

Import reuses the existing **Upload file to create** entry — no extra menu item is added. On the **Expert Management** page, open the **Create** menu and choose **Upload file to create**; in the expert view it imports expert packages, and the same entry in the expert-team view imports team packages.

The dialog accepts a `.zip` asset package by **click-to-choose**; the desktop app also supports dragging the file into the window. Other file formats are rejected.

### 3.2 Import Flow

1. After the file is chosen, the server reads it in place and unpacks it safely: the package structure and manifest are verified, and symlinks and path-traversal entries are rejected;
2. Once validation passes, the package is committed and the asset id is returned;
3. The page shows the result and switches to the "Mine" list.

### 3.3 Behavior After Import

The two asset types behave differently:

- **Expert team**: import equals install — the team lands in the expert-team "Mine" list; if the install step fails, the import is kept and the failure reason is shown, and the install can be retried from the detail page;
- **Expert**: the install flow starts automatically after the import; a failed install **rolls the import back** so no half-imported expert is left behind, and the dialog stays open with the failure reason for an immediate retry. If the expert depends on connectors that need authorization, the authorization guide pops up.

If connectors declared by the asset are not ready in the current environment, the install flow provides the authorization guide. Complete the connector authorization on the **Extensions** page before using the related capabilities.

### 3.4 Import Never Overwrites

- An asset with the same id already exists locally → rejected; delete it or rename first;
- The id collides with a system built-in asset → rejected; built-ins cannot be overwritten;
- An expert team's display name duplicates an existing team → rejected as a name conflict.

---

## 4. Error Message Reference

The messages below are the unified error texts of the export chain; when an import fails, the dialog shows the corresponding failure reason in place, with the same meanings and remedies.

| Message | Meaning | What to do |
|---------|---------|-----------|
| The asset or the requested version does not exist | The export target was deleted, or the requested version does not exist | Confirm the asset exists and is installed, then export again |
| Uninstalled or restricted assets cannot be exported | The asset type is not wired to export yet, or it is uninstalled with no local materials | Install it first, then export |
| Invalid asset package: missing manifest or bad structure | The zip is not a single-root package, `manifest.json` is missing, or the type cannot be identified | Use a package produced by unified export; do not modify the structure by hand |
| The package contains symlinks or unsafe paths and was rejected | The package contains symlinks or path-traversal entries | Remove those entries and repack |
| Plaintext credentials detected in package files; remove them and retry | File content hit the credential scan | Clean up the plaintext secrets and retry |
| File count, single file, or archive size exceeds the limit | A size limit was exceeded (see 2.2) | Trim the package content and retry |
| An asset with the same id already exists; overwriting is not supported | A local asset with the same name exists | Delete the existing asset, or change the id inside the package |
| Conflicts with a built-in asset; overwriting is not allowed | Same id as a built-in asset | Change the id inside the package |

---

## 5. FAQ

### 5.1 The detail page has no Export button

**Symptom**: No "Export" button in the action area of an expert or expert-team detail page.

**Solution**: Check the visibility rules in 2.1 — for experts, only locally created packages or installed copies can be exported; Hub expert teams that are not installed cannot be exported. Install first, then return to the detail page.

### 5.2 Import says "Invalid asset package"

**Symptom**: After uploading a .zip, the import reports an invalid package or an unrecognizable type.

**Solution**: Import only accepts packages **produced by unified export** with a `manifest.json`. Do not zip arbitrary folders by hand; to migrate a skill package, use the existing upload entry on the skills page.

### 5.3 The target environment already has the same asset

**Symptom**: Import reports that an asset with the same id already exists.

**Solution**: Import does not overwrite. To replace an asset, delete the existing one in the target environment first and import again; alternatively, change the asset id in the source environment and re-export.

### 5.4 Connectors still need authorization after a successful import

**Symptom**: The import succeeded but the install flow reports connectors pending authorization.

**Solution**: Complete the connector authorization on the **Extensions** page. A failed expert install rolls the import back — re-import once authorization is done; an expert team is already installed by the import — its members can call the connectors once authorization is done.

---

*Simplified Chinese: [资产导入导出](../zh/资产导入导出.md)*
