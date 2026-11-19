import { Browser } from "@wailsio/runtime";
import { useEffect, useState } from "react";
import type { MouseEvent, ReactNode } from "react";
import { desktopUpdate } from "../bridge/update.ts";
import { stageLabels } from "../components/UpdateCenter.tsx";
import type { SelfUpdate } from "../components/UpdateCenter.tsx";
import "./AboutPage.css";

const projectUrl = "https://github.com/Ricori/nonoka-sub-x";
const acknowledgementUrl = "https://github.com/caca2331/finesub";

function ExternalLink({ href, children }: { href: string; children: string }) {
  const open = (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    void Browser.OpenURL(href).catch(() => window.open(href, "_blank", "noopener,noreferrer"));
  };

  return (
    <a href={href} target="_blank" rel="noreferrer" onClick={open}>
      <span>{children}</span>
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M14 5h5v5m0-5-8 8M19 14v4a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h4" />
      </svg>
    </a>
  );
}

function Row({ label, note, children }: { label: string; note?: string; children: ReactNode }) {
  return (
    <div className="about-row">
      <div>
        <span>{label}</span>
        {children}
      </div>
      {note && <p>{note}</p>}
    </div>
  );
}

/** The running build, read from the updater service. It is a constant compiled
    into the binary, so one read per mount is enough. */
function useAppVersion(): string {
  const [version, setVersion] = useState("");
  useEffect(() => {
    void desktopUpdate.currentVersion().then(setVersion).catch(() => undefined);
  }, []);
  return version;
}

/** Mirrors the topbar affordance: a staged build installs on exit and only
    needs a restart to arrive sooner, and any earlier stage is worth naming so
    the version below does not look stale. */
function UpdateHint({ update }: { update?: SelfUpdate }) {
  const status = update?.status;
  if (!status || status.mandatory) return null;
  if (status.ready && status.stage !== "installing") {
    return (
      <button className="about-update" type="button" onClick={update?.install}>
        重启安装 v{status.version}
      </button>
    );
  }
  const label = stageLabels[status.stage];
  return label ? <span className="about-update-note">{label}</span> : null;
}

export function AboutPage({ update }: { update?: SelfUpdate }) {
  const version = useAppVersion();

  return (
    <section className="about-layout">
      <article className="panel about-hero">
        <div className="about-hero-copy">
          <div className="about-heading">
            <span className="eyebrow">Nonoka Sub X</span>
            <h2>烤肉就交给 Nonoka</h2>
            <p>领先的 AI 工作流字幕工作台</p>
          </div>
          <div className="about-chips">
            <span className="about-chip about-chip-accent">{version ? `v${version}` : "读取版本…"}</span>
            <span className="about-chip">GPL-3.0</span>
            <span className="about-chip">© 2026 Ricori</span>
          </div>
        </div>
        <div className="about-mascot-wrapper" aria-hidden="true">
          <img
            className="about-mascot-img"
            src="./assets/nonoka.png"
            alt="Nonoka"
            draggable={false}
          />
        </div>
      </article>

      <article className="panel about-details">
        <Row label="当前版本">
          <div className="about-version">
            <strong>{version ? `v${version}` : "—"}</strong>
            <UpdateHint update={update} />
          </div>
        </Row>
        <Row label="作者">
          <strong>伊波千果</strong>
        </Row>
        <Row label="Github">
          <ExternalLink href={projectUrl}>github.com/Ricori/nonoka-sub-x</ExternalLink>
        </Row>
        <Row label="鸣谢" >
          <ExternalLink href={acknowledgementUrl}>caca2331/finesub</ExternalLink>
        </Row>
      </article>
    </section>
  );
}
