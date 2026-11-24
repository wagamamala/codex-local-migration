# Source export and private transfer

## Locate the right data

- Ask which projects and chats matter. Confirm the stable thread UUID, displayed title, current project folder, archival status, and any older working folder from the session metadata. A message ID, UI title, or timestamp is insufficient for identification.
- Identify the active Codex home and SQLite directory. Honor a custom `CODEX_HOME` or `CODEX_SQLITE_HOME`. When multiple state or history database versions exist, identify the files the running app uses and pass the exact paths. Record application and bundled CLI versions on both Macs.
- Inventory the project's working tree, uncommitted changes, hidden project files, and referenced deliverables outside the project. Preserve required Git data where appropriate. Review symlinks and dependencies that refer to the old Mac. Record missing files and distinguish cloud-linked content from files on disk.

## Export native data

Run the bundled Python 3.9+ helper once for each project's selected chats. Replace the example IDs with IDs verified on the old Mac. The output parent directory should already exist.

```sh
python3 scripts/native_bundle.py export \
  --thread-id 00000000-0000-4000-8000-000000000001 \
  --thread-id 00000000-0000-4000-8000-000000000002 \
  --output ./research-native.zip

python3 scripts/native_bundle.py verify ./research-native.zip
```

Run these commands from the skill folder or pass the script's full path. Use `--codex-home`, `--sqlite-home`, `--state-db`, or `--history-db` when the local layout differs. The ZIP contains untouched selected logs under their original relative paths, any same-ID candidates found in the session directories, scoped state and history rows, relevant table definitions and migration metadata, scoped desktop mappings, and SHA-256 plus size for each data member. It reads committed WAL data using SQLite read-only transactions and checks for concurrent changes before publishing the output. If the history database or an index entry is absent, record the gap. Review `registered_rollout_missing` and `unhandled_scoped_tables` before transfer. An unhandled table may be unrelated to recovery or hold required data for the installed version. Inspect its schema and export only the required rows for the selected IDs. Use a private local directory as the output parent.

Some older sessions are stored as `.jsonl.zst` even while SQLite names the logical `.jsonl` path. The helper uses a trusted `zstd` binary from PATH or `--zstd` to expand a temporary copy. It preserves the original compressed file under `native/` and puts the uncompressed bytes under `decoded/`. The source archive stays intact.

The helper implements the observed local JSONL and SQLite layout. For another layout, inspect the installed version and use the read-only principles above. An unknown schema, a non-terminal turn, or a detected credential pattern requires local review before transfer. Credential pattern checks are a limited heuristic. Native logs can include personal information, tool results, and secrets even when the checks pass. Share generated bundles only with the intended device. Keep authentication stores, API keys, keychain material, browser profiles, and unselected chats outside the export.

## Recover and map media

- Inspect message attachments and stored tool results. An old temporary path can be gone while the actual bytes are still present in a user message or tool output in the native rollout. For local images, first match the message or tool item ID and path. Use a verified per-call image order if the runtime stores images in a batch.
- Match the media to archived bytes with a digest. Reuse identical files already present in the project copy or an earlier private supplement. When the original bytes exist only on the source, include those files with the chat they belong to. Preserve the association `thread ID → message/tool item ID → old path → archive member or destination path → SHA-256`. An embedded image can have no original path.
- When changing the number or association of images would be guesswork, keep the recoverable images in a per-chat gallery and report the missing association. Keep duplicate references when separate messages point to the same asset.
- Keep each project supplement self-contained with its markdown or event records, media, and file references. Preserve the relative paths, or rewrite links and validate every new target. Keep a separate list of missing original documents, expired remote URLs, and unavailable external services.
- The export helper preserves media already embedded in native records. It leaves external file discovery, readable transcript export, and attachment path mapping to this workflow because a raw log can reference files outside the user's chosen scope.

For native log locations and configuration, see [Codex troubleshooting](https://learn.chatgpt.com/docs/reference/troubleshooting#feedback-and-logs) and [Codex advanced configuration](https://learn.chatgpt.com/docs/config-file/config-advanced).
