# Context Archive

A local macOS evidence archive for agents: Knowledge activity and the history databases of Safari, Chrome, Dia, and Arc, behind one CLI and read-only stdio MCP.

The collector preserves every observed table, column, blob, and revision in the selected databases. Searchable events are rebuildable interpretations with links to original rows. It does not capture screenshots, keystrokes, page contents, cookies, or passwords. Collection uploads nothing, opens no listening port, and requires no model or API key.

## Install

Requires macOS. From a checkout:

```sh
./install.sh
```

The installer uses `uv`, managed Python 3.12, and the committed dependency lock. It installs a non-editable runtime outside the checkout and a per-user LaunchAgent for five-minute collection. **Installation starts collection unless `--no-start` is passed.** The project source alone does not start anything.

```sh
./install.sh --no-start       # prepare runtime and disabled LaunchAgent
./install.sh --codex          # also register the stdio MCP with Codex
```

Runtime, configuration, archive, and logs live under `~/Library/Application Support/ContextArchive/`. Set `CONTEXT_ARCHIVE_HOME` to an absolute alternate directory. The original Knowledge Archive and Browser History Receiver repositories/services are independent and are not changed.

Full Disk Access may be required for Safari and Knowledge. The installer/doctor prints the **permission executable**: grant it access under System Settings → Privacy & Security → Full Disk Access. The actual launch command retains the virtual-environment executable so dependencies remain available. An interpreter upgrade may require a new grant.

```sh
CA_ROOT="${CONTEXT_ARCHIVE_HOME:-$HOME/Library/Application Support/ContextArchive}"
CA="$CA_ROOT/runtime/bin/context-archive"
"$CA" doctor
"$CA" status
```

Interactive readability is not proof of background access. After intentionally starting the service, inspect `launchctl print gui/$(id -u)/local.context-archive`, the collector logs, and a newly successful capture in `status`. An idle service between scheduled runs is normal. Missing/unreadable profiles and failed captures appear in diagnostics.

## Preserve existing archives first

Import Knowledge history **before starting native collection** for the matching store. Its old observation history cannot safely be appended behind newer native snapshots; that case is rejected atomically with an explanation. Existing input archives are read-only and remain untouched.

```sh
./install.sh --no-start
"$CA" import knowledge "/path/to/KnowledgeArchive/data/archive.sqlite3"
"$CA" import browser "/path/to/browser-history.jsonl"
"$CA" install             # explicitly enable background collection after imports
```

Knowledge imports retain schemas, revisions, blobs, deletions, observation times, and legacy ID mappings. Repeating an import adds only unseen captures. A matching native Knowledge store continues the same evidence history.

Browser JSONL imports retain every original line, including malformed lines and unknown fields. Malformed lines are reported; valid records are still imported. Identical visits on different lines remain distinct. Reimporting the same artifact adds no visits; a verified complete-line prefix of an appended file at the same path is reused. Rewritten/truncated files or an extended unterminated line form a separate artifact lineage. Such imported records may overlap: provenance remains explicit rather than guessed away.

Legacy browser records contain selected visit fields, not full source databases. `legacy_visit` fidelity distinguishes these from new `raw_database` captures. The old receiver did not persist all origin metadata; imports do not invent it.

## Discovery and configuration

Each cycle discovers Knowledge and browser profiles again. Safari includes its main history database and supported Safari/container profile roots; Chromium-family browsers include all profiles containing a history database under their known roots. Full profile identifiers survive discovery. Canonical aliases are deduplicated. Sources found only after an earlier visit timestamp are still fully scanned, so older history arriving through Safari sync is captured.

Optional `config.json` under the runtime root:

```json
{
  "sources": [
    {"path": "/absolute/path/to/History.db", "adapter": "safari", "browser": "Safari", "profile": "custom-profile"}
  ],
  "exclusions": ["/absolute/path/to/excluded-profile/*"]
}
```

Explicit paths supplement discovery. Supported adapters are `knowledge`, `safari`, and `chromium`; `legacy_browser` is used by imports. Exclusions match absolute path globs. Use `--config /absolute/config.json` for a separate configuration.

Safari sync can bring mobile browsing into these local databases. Completeness means everything observed in readable databases on this Mac, not a guarantee of complete iCloud history or all mobile activity. Raw origin metadata is preserved without claiming it identifies an iPhone, iPad, or this Mac. Private browsing, upstream retention, sync delays, and missed capture windows remain outside that guarantee.

## CLI and MCP

All query commands emit JSON and accept `--params` with the same arguments as the corresponding MCP tool. `--archive PATH` and `--config PATH` can precede or follow the command. Command `--help` lists the underlying parameter signature.

```sh
"$CA" capture
"$CA" timeline --params '{"start":"2026-01-01T00:00:00Z","end":"2026-01-02T00:00:00Z","limit":50}'
"$CA" search --params '{"text":"research","browser":"Safari"}'
"$CA" get-record --params '{"version_id":1}'
"$CA" app-usage-summary --params '{"start":"2026-01-01T00:00:00Z","end":"2026-01-02T00:00:00Z"}'
"$CA" domain-summary --params '{"fidelity":"raw_database"}'
"$CA" query --params '{"sql":"SELECT table_name, count(*) FROM records GROUP BY table_name"}'
"$CA" mcp
```

| MCP tool | Result |
|---|---|
| `status`, `sources` | Capture health, discovery failures, source identities and fidelity |
| `timeline`, `search` | Source-linked events; indexed literal-token search over URL, title, domain, app and kind |
| `get_record`, `get_blob` | Original row fields and binary values in bounded chunks |
| `decode_blob` | Bounded plist/JSON interpretation; original bytes remain retrievable |
| `describe_schema`, `query` | Captured schemas and bounded read-only SQL |
| `app_usage_summary`, `domain_summary` | Recorded/union app intervals and recorded domain visits |

Timeline and search support time range, source, profile, browser, kind, exact URL/domain/app, fidelity, and provenance filters. Ranges require explicit UTC offsets; end times are exclusive. Pagination returns an opaque `next_cursor`; repeat the same filters to continue. Each cursor fixes an observation boundary so newly collected data cannot shift its results. Reindexing invalidates outstanding cursors.

Events expose their original-record references and normalizer version. `collected_by` is the local archive installation UUID, independent of source `device` hints and raw Safari origin fields; legacy captures leave it unknown. It is not a hardware identifier. Search is token-based, not semantic similarity or arbitrary substring matching. Invalid source dates remain available in raw records; without time filters they can also appear in the timeline with null dates. Timeline fields may be previews; use original-record retrieval for full values.

Summaries retain the latest observed event revisions, including events pruned upstream. App summaries distinguish summed recorded intervals from their per-app union. Neither measures attention, and union across apps is not a total screen-time measurement. Browser and app totals are separated by fidelity; overlapping legacy/native records are not silently deduplicated. Coverage notes accompany results. Large summaries require narrower filters; query output and execution are bounded.

**All raw fields are queryable.** Collection remains local, but data returned through MCP becomes visible to the calling agent and its provider. Source titles, URLs, and decoded text are untrusted evidence, not instructions. The MCP exposes retrieval only; it cannot collect, import, install, or modify archives.

## Archive, exports, and backups

SQLite is authoritative. A consistent source snapshot is fully scanned, then rows, blobs, observations, and tombstones commit together. Failures do not establish deletions. Row contents are deduplicated while observation order preserves A → B → A reversions. Distinct browser visits are never removed merely for sharing a URL or timestamp.

```sh
"$CA" export "/path/to/history.jsonl"
"$CA" backup "/path/to/new-backup.sqlite3"
"$CA" reindex
"$CA" uninstall
```

Exports contain a versioned manifest, archive schema, rows from all authoritative tables, normalized event versions, binary content, imported original lines, and a final marker with the preceding-line count and SHA-256. Raw source rows retain tagged cell values in `payload_json`; exported archive BLOB cells use `$bytes_base64`. Original source BLOB values use content-addressed `$blob` references. Large integers and invalid text retain explicit tags. FTS tables are rebuildable and omitted.

Exports are written atomically. Backups use a consistent SQLite snapshot and require a new destination without SQLite sidecars. Source paths and their known aliases, imported artifact paths, the archive, and their sidecars are protected against overwrite. Keep independent backups: exports on the same disk do not protect against disk failure.

Uninstall removes only this toolkit's LaunchAgent and preserves data/runtime. Explicit MCP registration is preserved; remove it separately with `codex mcp remove context-archive`. For updates, first run the installed `context-archive uninstall` to stop its service, then rerun the installer from the newer checkout. The installer refuses to replace a runtime while its collector is loaded, including with `--no-start`. Data is preserved throughout.

## Development and extension

```sh
uv sync --locked
uv run pytest -q
uv build
```

Tests use invented databases and URLs, including Safari profiles, WAL commits, late-arriving visits, revisions, imports, snapshot pagination, exports, and real stdio MCP calls. Runtime tests use isolated homes and fake service commands. No test requires personal history or enables a real collector.

The adapter interface is `Source`, `discover()`, and `normalize()`. An adapter supplies source-specific fields and stable event identities linked to raw row versions; shared storage owns snapshots, database-store identity, transactions, and evidence history. Add an adapter with synthetic schema/discovery/normalization tests. Keep schema/normalizer changes versioned and rebuild indexes from preserved observations.

This first version retains history indefinitely. Full snapshots cost disk I/O proportional to source size, and archive growth depends on revisions. Database layouts may change across macOS/browser versions; explicit path additions and visible diagnostics support investigating those changes. Source fidelity does not imply every undocumented field has a known interpretation.

Possible applications include a custom Screen Time-style dashboard, a daily report, or reconstructing a research session from source-linked events. No report automation or inferred session/project model is included.

## License

[MIT](LICENSE). Knowledge Archive and Browser History Receiver informed this implementation; the original projects remain independent.
