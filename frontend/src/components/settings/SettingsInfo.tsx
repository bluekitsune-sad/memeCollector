import type { SettingsResponse } from "@/lib/types";

/** Read-only sections of Settings: storage, ranking weights, notices (PRD §42–§43). */
export default function SettingsInfo({ settings }: { settings: SettingsResponse }) {
  const { storage, search } = settings;

  return (
    <>
      <section className="panel">
        <h2>Storage (read-only)</h2>
        <dl className="readonly-list">
          <dt>Media</dt>
          <dd>{storage.media_directory}</dd>
          <dt>Thumbnails</dt>
          <dd>{storage.thumbnail_directory}</dd>
          <dt>Previews</dt>
          <dd>{storage.preview_directory}</dd>
          <dt>Database</dt>
          <dd>{storage.database_path}</dd>
        </dl>
      </section>

      <section className="panel">
        <h2>Search ranking weights (read-only)</h2>
        <dl className="readonly-list">
          <dt>Semantic</dt>
          <dd>{Math.round(search.semantic_weight * 100)}%</dd>
          <dt>Keyword</dt>
          <dd>{Math.round(search.keyword_weight * 100)}%</dd>
          <dt>Tags</dt>
          <dd>{Math.round(search.tag_weight * 100)}%</dd>
          <dt>Metadata</dt>
          <dd>{Math.round(search.metadata_weight * 100)}%</dd>
        </dl>
        <p className="inline-msg">Configured in config/config.yaml — shown here for transparency.</p>
      </section>

      <section className="panel">
        <h2>Responsible use</h2>
        <p className="notice">
          {settings.notices?.copyright ??
            "You are responsible for ensuring your collection and use of this media complies with " +
              "the source sites' terms and applicable copyright law. MemeVault keeps source " +
              "attribution and is intended for personal archival use only."}
        </p>
        <p className="inline-msg" style={{ marginTop: 8 }}>
          MemeVault is a personal archival tool: it keeps source attribution for everything it
          collects and never redistributes content automatically.
        </p>
      </section>

      <section className="panel">
        <h2>About</h2>
        <dl className="readonly-list">
          <dt>Server</dt>
          <dd>
            {settings.server.host}:{settings.server.port}
          </dd>
          <dt>Mode</dt>
          <dd>Local-first (PRD §42)</dd>
        </dl>
      </section>
    </>
  );
}
