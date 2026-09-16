# Migration

## 0.1.x → 0.2.0

The 0.2.0 rewrite is in progress. The manual conversion procedure is not yet available;
continue using the existing release and home together until the guide is complete.
Development checks use new scratch homes only.

Version-1 `config.json` files, 0.1.x databases, and home-level `jobs/` directories are refused
with the message "This Enso home predates 0.2.0; see the migration guide:" and a link to
this page. Do not change only the version number:
0.2.0 also changes the home layout and operational database. It provides no compatibility
parser or automatic conversion. [Configuration](configuration.md#configuration-ownership-in-020)
describes the implemented schema and the remaining layout changes.

The new database uses SQLite `user_version = 1` and a separate application identifier.
An old 0.1.x binary cannot be relied on to reject it: never point old code at a 0.2.0 home.
Restoring a pre-conversion backup would discard any work accepted since that backup.

Keep existing installations and backups intact. Conversion of an existing home is a
separate, explicitly authorized operation; this notice is not a conversion procedure.
