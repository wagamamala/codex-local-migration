# Codex Local Migration

[中文说明](README.zh-CN.md)

Move selected local Codex projects and original chats from one Mac to another. The skill guides project and media transfer, export of original session data, isolated recovery, and verification that the original chats open under the expected projects.

This is a community skill. The first release focuses on Mac-to-Mac migration of local Codex Desktop projects. The included Python helper exports and verifies private bundles. The recovery workflow uses version-specific evidence before any merge into the destination's normal Codex store.

## Install in Codex

For a full migration, **install the skill on both Macs** so Codex can work with each Mac's local files and session store. On each Mac, copy the entire [`skills/codex-local-migration`](skills/codex-local-migration) directory to `~/.agents/skills/` for all projects, or to the relevant repository's `.agents/skills/` for project-only use. Then start a new Codex session. The helper requires Python 3.9+. Exporting a Zstandard-compressed rollout also requires a trusted `zstd` executable. [OpenAI skill installation guidance](https://developers.openai.com/api/reference/cli)

### Old Mac — Export

> Use `$codex-local-migration` to export my selected local projects and original chats. Include their project files, screenshots, and attachments. Verify the bundles and tell me which files to transfer to the new Mac.

Transfer the resulting private bundles to your new Mac.

### New Mac — Verify and restore

After receiving the ZIPs, extract them and move the entire extracted project package from Downloads to a long-term location such as **Documents → Codex Projects**. In Codex, choose **Projects → + → Local** and select the **inner project folder** containing the working files as the primary source. If you already added a project using a Downloads path or the outer package folder, use **⋯ → Edit project** to set the primary source to the relocated inner folder and keep the same sidebar project.

> Use `$codex-local-migration` to verify the received bundles and rehearse restoring the original chats in an isolated store. Check the history, media, and destination project for each chat. Show me the rehearsal results before importing into my everyday Codex sidebar.

### One-time use without installing

Download and extract the repository ZIP on each Mac where you need to run a step. Keep the entire extracted skill directory together. In a local Codex chat, provide the path to the extracted `skills/codex-local-migration/SKILL.md` and ask Codex to read it and follow the **Old Mac** or **New Mac** instructions above. Provide the project names on the old Mac and the bundle locations on the new Mac. If your export is already complete, you only need the extracted skill folder on the receiving Mac.

## What the skill covers

- **Project files** including referenced deliverables, uncommitted work, and the stable primary folder on the new Mac.
- **Chat and media archives** separated by project, with usable links to images and attachments.
- **Native data** including original rollout bytes, scoped SQLite history and metadata, and a checksum manifest. Same-ID log candidates remain separate and get an offset compatibility report.
- **Recovery checks** for original identity, readable history and images, desktop project grouping, and ability to continue chatting.

Start with the [skill](skills/codex-local-migration/SKILL.md). Follow the [export guide](skills/codex-local-migration/references/export.md) and [destination runbook](skills/codex-local-migration/references/restore.md) as needed.

## Repository and private data

This repository contains the skill, its helper, and synthetic tests. Keep generated bundles and all recovered chat data in a private folder outside your Git checkout. Native sessions can contain sensitive material. For AirDrop or another private transfer, confirm the specific destination device.

Run the synthetic test suite from the repository root:

```sh
python3 -B -m unittest discover -s tests
```

For an optional integration check against your installed Codex CLI:

```sh
python3 -B tests/verify_app_server.py
```

This check was run against `codex-cli 0.171.0-alpha.13`. It creates synthetic source and destination stores, verifies a same-ID history conflict against real SQLite turn offsets, checks embedded user and tool images, and confirms original UUID, history, destination work directory, and project binding after an app-server restart. The destination's pre-existing test chat stays readable. It does not exercise the signed-in desktop sidebar or send a model message. The check requires the local CLI to expose the experimental project and resume operations it calls.
