---
name: codex-local-migration
description: Use when moving selected local Codex projects or original chats between Macs, recovering a Codex sidebar project, or checking whether transferred chat history, screenshots, and attachments can be resumed. 适用于换 Mac、迁移 Codex 本地项目和原会话、恢复侧栏分组。
license: MIT
---

# Codex Local Migration

Find out whether the user needs project files, readable chat archives, original resumable chats, or all three. Track these separately. This workflow concerns local Codex data. For a cloud task, first check access from the destination account and workspace.

## Source Mac

1. Confirm the selected project folders and chat IDs. Use the desktop project list and actual thread metadata. Project assignment can be stored in desktop state even when a SQLite thread has a null project ID. Wait for work on the selected chats to finish before exporting.
2. Copy the selected project folders, uncommitted files, and referenced deliverables outside the project root. Keep paths inside each copied folder intact. Use separate packages per project when people want independent sidebar entries. Expand [export details](references/export.md) when there are screenshots, attachments, or older working directories.
3. For original chats, run [native_bundle.py](scripts/native_bundle.py) once per project with the selected IDs. The export keeps the original rollout bytes, selected index and database records, and a checksum manifest. Inspect coverage and record anything missing. Bundles are private, including the native logs and screenshots.
4. Transfer by a user-approved private route. For AirDrop, identify the exact receiving Mac. If the picker shows identical contact names or the destination is uncertain, let the user select it. Confirm receipt and checksums on the receiving Mac. A sender-side “Sent” establishes only the sender-side result.

## Destination Mac

1. Verify and extract the received bundles. Move the extracted project package as a unit from Downloads to a long-term location, such as **Documents → Codex Projects**. Then use **Projects → + → Local** and choose the inner folder containing the working files as the primary source. If the project already exists and points to Downloads or to the outer package folder, use **⋯ → Edit project** to update its primary source to the inner folder at the new location. Reuse the existing sidebar project. Check its saved path before assigning restored chats. Verification and isolated rehearsal can proceed during setup. Add a separate chat and media archive as an extra source if desired.
2. For original chats, follow [the recovery runbook](references/restore.md). Match each log to its history offsets. Rehearse with the same original IDs in a separate Codex home and SQLite directory. Validate reading, project binding, media, and reload. Check tool availability and version-specific behavior on the destination itself.
3. For a live merge, rehearse the actual backup, import, and verification on a closed destination copy, then obtain authorization for the destination change. Prefer a supported import. If the merge requires Codex to quit, start the tested import process on the destination before asking the user to quit. Confirm it can finish independently and tell the user when to reopen. A manual launcher is a fallback when the destination tools cannot start an independent process. Follow the [live merge steps](references/restore.md#apply-after-rehearsal).
4. After restart, open each original chat from the intended sidebar project and verify its original ID. Report sidebar visibility, readable history and media, and continuation separately. For an authorized continuation test, have the user ask about a specific prior detail in the recovered chat and check that the reply uses that detail. Report any untested or missing part by chat.

When the remote Mac provides an officially supported handoff for the matching Git project, use that path if it fits the user's goal. If the agent cannot control Codex's own interface, provide the exact manual click and then verify what the available read tools can see.
