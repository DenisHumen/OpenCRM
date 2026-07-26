import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { Icon } from "../components/Icon";
import { Chip, ConfirmModal, EmptyState, ScreenLoading, Toggle } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { formatDateTime, formatDuration } from "../lib/format";

export function BoardEditor() {
  const { id } = useParams();
  const { t, locale, toast, toastError } = useApp();
  const navigate = useNavigate();
  const [board, setBoard] = useState<any>(null);
  const [clients, setClients] = useState<any[]>([]);
  const [confirm, setConfirm] = useState<null | "regenerate" | "deleteBoard" | "deleteShare" | number>(null);
  const [copied, setCopied] = useState(false);
  const [expiryOpen, setExpiryOpen] = useState(false);
  const [pinOpen, setPinOpen] = useState(false);
  const [pinDraft, setPinDraft] = useState("");
  const [dragId, setDragId] = useState<number | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const pollTimer = useRef<number>();

  const load = useCallback(async () => {
    try {
      setBoard(await api.get(`/boards/${id}`));
    } catch (e) {
      toastError(e);
      navigate("/boards");
    }
  }, [id, toastError, navigate]);

  useEffect(() => {
    void load();
    api.get("/clients?per_page=200").then((d) => setClients(d.items)).catch(() => undefined);
  }, [load]);

  // поллинг обрабатывающихся работ
  useEffect(() => {
    const processing = board?.works?.filter((w: any) => w.status === "processing") ?? [];
    if (processing.length === 0) return;
    pollTimer.current = window.setTimeout(async () => {
      for (const work of processing) {
        try {
          const fresh = await api.get(`/boards/${id}/works/${work.id}`);
          if (fresh.status !== "processing") {
            setBoard((prev: any) => ({
              ...prev,
              works: prev.works.map((w: any) => (w.id === fresh.id ? fresh : w)),
            }));
          }
        } catch {
          /* повторим на следующем тике */
        }
      }
      setBoard((prev: any) => ({ ...prev })); // перезапуск эффекта
    }, 1500);
    return () => window.clearTimeout(pollTimer.current);
  }, [board, id]);

  if (!board) return <ScreenLoading />;

  const share = board.shares.find((s: any) => s.is_active) ?? board.shares[0] ?? null;

  const patchBoard = async (patch: any) => {
    try {
      const updated = await api.patch(`/boards/${id}`, patch);
      setBoard((prev: any) => ({ ...prev, ...updated }));
    } catch (e) {
      toastError(e);
    }
  };

  const patchShare = async (patch: any) => {
    if (!share) return;
    try {
      const updated = await api.patch(`/shares/${share.id}`, patch);
      setBoard((prev: any) => ({
        ...prev,
        shares: prev.shares.map((s: any) => (s.id === updated.id ? updated : s)),
      }));
    } catch (e) {
      toastError(e);
    }
  };

  const createShare = async () => {
    try {
      const link = await api.post(`/boards/${id}/shares`, {});
      setBoard((prev: any) => ({ ...prev, shares: [link, ...prev.shares] }));
    } catch (e) {
      toastError(e);
    }
  };

  const regenerate = async () => {
    if (!share) return;
    try {
      const link = await api.post(`/shares/${share.id}/regenerate`);
      await load();
      void navigator.clipboard.writeText(link.url).catch(() => undefined);
      toast(t("copied"));
    } catch (e) {
      toastError(e);
    }
  };

  const copyLink = async () => {
    if (!share) return;
    try {
      await navigator.clipboard.writeText(share.url);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      toast(share.url);
    }
  };

  const uploadFiles = async (list: FileList | null) => {
    if (!list) return;
    for (const file of Array.from(list)) {
      try {
        const work = await api.upload(`/boards/${id}/works`, file);
        setBoard((prev: any) => ({ ...prev, works: [...prev.works, work], works_count: prev.works_count + 1 }));
      } catch (e) {
        toastError(e);
      }
    }
  };

  const deleteWork = async (workId: number) => {
    try {
      await api.del(`/boards/${id}/works/${workId}`);
      setBoard((prev: any) => ({
        ...prev,
        works: prev.works.filter((w: any) => w.id !== workId),
        cover_work_id: prev.cover_work_id === workId ? null : prev.cover_work_id,
      }));
    } catch (e) {
      toastError(e);
    }
  };

  const reorder = async (sourceId: number, targetId: number) => {
    if (sourceId === targetId) return;
    const ids = board.works.map((w: any) => w.id);
    const from = ids.indexOf(sourceId);
    const to = ids.indexOf(targetId);
    ids.splice(from, 1);
    ids.splice(to, 0, sourceId);
    const reordered = ids.map((wid: number) => board.works.find((w: any) => w.id === wid));
    setBoard((prev: any) => ({ ...prev, works: reordered }));
    try {
      await api.put(`/boards/${id}/works/order`, { work_ids: ids });
    } catch (e) {
      toastError(e);
      void load();
    }
  };

  const expiryLabel = (s: any) => {
    if (!s?.expires_at) return t("off");
    const days = Math.ceil((new Date(s.expires_at + "Z").getTime() - Date.now()) / 86400000);
    return days > 0 ? `${days}d` : t("off");
  };

  const setExpiry = (days: number | null) => {
    void patchShare({ expires_at: days ? new Date(Date.now() + days * 86400000).toISOString() : null });
  };

  return (
    <div className="page page-wide" style={{ paddingTop: 36 }}>
      <Link to="/boards" style={{ display: "inline-flex", alignItems: "center", gap: 6, color: "var(--muted)", fontSize: 13, marginBottom: 20 }}>
        <Icon name="arrowLeft" size={14} />
        {t("boards")}
      </Link>
      <div className="page-head" style={{ marginBottom: 28 }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <h1 className="page-title" style={{ fontSize: 22 }}>
              {board.title}
            </h1>
            {board.is_published ? (
              <Chip variant="success">{t("published")}</Chip>
            ) : (
              <Chip>
                <span className="dot" />
                {t("draft")}
              </Chip>
            )}
          </div>
          <div className="page-sub">
            {board.client_id && clients.length > 0 && (
              <>
                {t("forClient")}{" "}
                <Link to={`/clients/${board.client_id}`} style={{ color: "var(--muted)", textDecoration: "underline", textUnderlineOffset: 2 }}>
                  {clients.find((c) => c.id === board.client_id)?.name ?? "…"}
                </Link>
                {" · "}
              </>
            )}
            {board.works.length} {t("works")} · {t("updated")} {formatDateTime(board.updated_at, locale)}
          </div>
        </div>
        <div
          style={{ display: "flex", alignItems: "center", gap: 9, cursor: "pointer", padding: "8px 12px", border: "1px solid var(--border)", borderRadius: 8 }}
          onClick={() => void patchBoard({ is_published: !board.is_published })}
        >
          <Toggle on={board.is_published} onToggle={() => undefined} />
          <span style={{ fontSize: 13, fontWeight: 500 }}>{t("published")}</span>
        </div>
      </div>

      <div className="editor-layout">
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="works-grid">
            {board.works.map((work: any) => (
              <div
                key={work.id}
                className={"work-card" + (dragId !== null && dragId !== work.id ? "" : "")}
                draggable
                onDragStart={() => setDragId(work.id)}
                onDragEnd={() => setDragId(null)}
                onDragOver={(e) => {
                  e.preventDefault();
                  e.currentTarget.classList.add("drag-over");
                }}
                onDragLeave={(e) => e.currentTarget.classList.remove("drag-over")}
                onDrop={(e) => {
                  e.preventDefault();
                  e.currentTarget.classList.remove("drag-over");
                  if (dragId !== null) void reorder(dragId, work.id);
                }}
              >
                <div className="work-media">
                  {work.media?.card || work.media?.poster ? (
                    <img src={work.media.card ?? work.media.poster} alt="" />
                  ) : (
                    <div style={{ aspectRatio: "16/10", display: "grid", placeItems: "center", color: "var(--faint)" }}>
                      <Icon name={work.kind === "video" ? "play" : "image"} size={22} />
                    </div>
                  )}
                  {board.cover_work_id === work.id && (
                    <div className="badge-floating">
                      <Icon name="star" size={11} fill="currentColor" />
                      {t("cover")}
                    </div>
                  )}
                  {work.status === "processing" && (
                    <div className="work-overlay">
                      <div className="spinner" />
                      <span style={{ color: "var(--muted)", fontSize: 11.5 }}>{t("processing")}</span>
                    </div>
                  )}
                  {work.status === "ready" && board.cover_work_id !== work.id && work.kind === "image" && (
                    <button className="cover-btn" title={t("cover")} onClick={() => void patchBoard({ cover_work_id: work.id })}>
                      <Icon name="star" size={13} />
                    </button>
                  )}
                  {work.duration_sec && (
                    <div className="badge-floating" style={{ top: "auto", left: "auto", bottom: 8, right: 8, color: "var(--text)", fontWeight: 500, fontSize: 10.5 }}>
                      <Icon name="play" size={9} fill="currentColor" />
                      {formatDuration(work.duration_sec)}
                    </div>
                  )}
                </div>
                <div className="work-foot">
                  <Icon name="grip" size={13} />
                  <WorkTitle work={work} boardId={board.id} onSaved={(updated) =>
                    setBoard((prev: any) => ({ ...prev, works: prev.works.map((w: any) => (w.id === updated.id ? updated : w)) }))
                  } />
                  <button className="text-link" style={{ display: "flex", color: "var(--faint)" }} onClick={() => setConfirm(work.id)}>
                    <Icon name="trash" size={13} />
                  </button>
                </div>
              </div>
            ))}
            <div
              className="dropzone"
              style={{ minHeight: 200, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 8 }}
              onClick={() => fileInput.current?.click()}
              onDragOver={(e) => {
                e.preventDefault();
                e.currentTarget.classList.add("drag-over");
              }}
              onDragLeave={(e) => e.currentTarget.classList.remove("drag-over")}
              onDrop={(e) => {
                e.preventDefault();
                e.currentTarget.classList.remove("drag-over");
                void uploadFiles(e.dataTransfer.files);
              }}
            >
              <Icon name="upload" size={20} />
              <span style={{ fontSize: 12.5, textAlign: "center", lineHeight: 1.5, whiteSpace: "pre-line" }}>{t("dropWorks")}</span>
              <input
                ref={fileInput}
                type="file"
                accept="image/jpeg,image/png,image/gif,image/webp,image/svg+xml,video/mp4,video/webm"
                multiple
                hidden
                onChange={(e) => {
                  void uploadFiles(e.target.files);
                  e.target.value = "";
                }}
              />
            </div>
          </div>
        </div>

        <div className="editor-rail">
          <div className="rail-card">
            <div className="rail-title">{t("boardSettings")}</div>
            <label className="label">{t("boardTitle")}</label>
            <BlurInput value={board.title} onSave={(v) => void patchBoard({ title: v })} style={{ marginBottom: 12, height: 32 }} />
            <label className="label">{t("description")}</label>
            <BlurInput value={board.description} onSave={(v) => void patchBoard({ description: v })} textarea style={{ marginBottom: 12 }} />
            <label className="label">{t("client")}</label>
            <select
              className="select"
              style={{ height: 32 }}
              value={board.client_id ?? ""}
              onChange={(e) => void patchBoard({ client_id: e.target.value ? Number(e.target.value) : null })}
            >
              <option value="">{t("noClient")}</option>
              {clients.map((client) => (
                <option key={client.id} value={client.id}>
                  {client.name}
                </option>
              ))}
            </select>
            <div style={{ color: "var(--faint)", fontSize: 11.5, marginTop: 10, lineHeight: 1.5 }}>{t("coverHint")}</div>
          </div>

          <div className="rail-card">
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14 }}>
              <div style={{ fontSize: 14, fontWeight: 600 }}>{t("share")}</div>
              {share && (
                <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
                  <span style={{ color: "var(--muted)", fontSize: 12 }}>
                    {share.is_active ? t("linkActive") : t("linkOff")}
                  </span>
                  <Toggle on={share.is_active} onToggle={() => void patchShare({ is_active: !share.is_active })} />
                </div>
              )}
            </div>
            {!share ? (
              <button className="copy-btn" onClick={() => void createShare()}>
                <Icon name="link" size={15} />
                {t("createLink")}
              </button>
            ) : (
              <>
                <button className={"copy-btn" + (copied ? " copied" : "")} onClick={() => void copyLink()}>
                  <Icon name={copied ? "check" : "copy"} size={15} stroke={copied ? 2 : 1.5} />
                  {copied ? t("copied") : t("copyLink")}
                </button>
                <div style={{ display: "flex", alignItems: "center", gap: 6, margin: "10px 2px 14px", color: "var(--faint)", fontSize: 12 }}>
                  <Icon name="link" size={12} />
                  <span style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                    {share.url.replace(/^https?:\/\//, "")}
                  </span>
                </div>
                <div>
                  <div className="share-row" onClick={() => setExpiryOpen((o) => !o)}>
                    <div className="share-row-label">
                      <Icon name="clock" size={14} />
                      {t("expiry")}
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      <span style={{ fontSize: 12.5 }}>{expiryLabel(share)}</span>
                      <Icon name="chevronDown" size={13} className="" />
                    </div>
                  </div>
                  {expiryOpen && (
                    <div style={{ display: "flex", gap: 6, padding: "2px 2px 12px" }}>
                      {[
                        { label: t("off"), days: null },
                        { label: t("days7"), days: 7 },
                        { label: t("days30"), days: 30 },
                        { label: t("days90"), days: 90 },
                      ].map((option) => (
                        <button
                          key={option.label}
                          className={"option-chip" + ((option.days === null) === !share.expires_at && option.days === null ? " active" : "")}
                          onClick={() => setExpiry(option.days)}
                        >
                          {option.label}
                        </button>
                      ))}
                    </div>
                  )}
                  <div className="share-row" onClick={() => setPinOpen((o) => !o)}>
                    <div className="share-row-label">
                      <Icon name="lock" size={14} />
                      {t("pinCode")}
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      <span style={{ fontSize: 12.5, letterSpacing: "0.1em" }}>{share.has_pin ? "••••" : t("off")}</span>
                      <Icon name="chevronDown" size={13} />
                    </div>
                  </div>
                  {pinOpen && (
                    <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "2px 2px 12px" }}>
                      {share.has_pin ? (
                        <button className="option-chip" onClick={() => void patchShare({ pin: null })}>
                          {t("pinRemove")}
                        </button>
                      ) : (
                        <>
                          <input
                            className="input"
                            style={{ width: 90, height: 30, letterSpacing: "0.2em" }}
                            maxLength={8}
                            inputMode="numeric"
                            placeholder="4821"
                            value={pinDraft}
                            onChange={(e) => setPinDraft(e.target.value.replace(/\D/g, ""))}
                          />
                          <button
                            className="option-chip"
                            onClick={() => {
                              if (pinDraft.length >= 4) {
                                void patchShare({ pin: pinDraft });
                                setPinDraft("");
                              }
                            }}
                          >
                            {t("pinSet")}
                          </button>
                        </>
                      )}
                      <span style={{ color: "var(--faint)", fontSize: 11.5, lineHeight: 1.4 }}>{t("pinHint")}</span>
                    </div>
                  )}
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "12px 2px 0", borderTop: "1px solid var(--surface-2)" }}>
                    <button className="btn-danger-link" onClick={() => setConfirm("regenerate")}>
                      {t("regenerate")}
                    </button>
                    <span style={{ color: "var(--faint)", fontSize: 11.5 }}>{t("regenerateHint")}</span>
                  </div>
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "10px 2px 0" }}>
                    <button className="btn-danger-link" onClick={() => setConfirm("deleteShare")}>
                      {t("deleteLink")}
                    </button>
                    <span style={{ color: "var(--faint)", fontSize: 11.5 }}>{t("deleteLinkHint")}</span>
                  </div>
                </div>
              </>
            )}
          </div>

          {share && (
            <div className="rail-card" style={{ padding: "16px 18px" }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <div>
                  <div style={{ fontSize: 13.5, fontWeight: 500 }}>{t("viewsCount", { n: share.views_count })}</div>
                  <div style={{ color: "var(--faint)", fontSize: 12, marginTop: 3 }}>
                    {share.last_viewed_at
                      ? t("lastView", { t: formatDateTime(share.last_viewed_at, locale) })
                      : t("noViewsYet")}
                  </div>
                </div>
                <span style={{ color: "var(--muted)", fontSize: 12.5 }}>
                  {t("uniqueVisitors", { n: share.unique_views_count })}
                </span>
              </div>
            </div>
          )}

          {share && (
            <a className="btn btn-secondary" href={`/b/${share.token}`} target="_blank" rel="noreferrer">
              <Icon name="eye" size={15} />
              {t("previewAsClient")}
            </a>
          )}

          <button className="btn-danger-link" style={{ alignSelf: "flex-start" }} onClick={() => setConfirm("deleteBoard")}>
            {t("deleteBoard")}
          </button>
        </div>
      </div>

      {confirm === "regenerate" && (
        <ConfirmModal text={t("regenerateConfirm")} confirmLabel={t("confirm")} danger onConfirm={() => void regenerate()} onClose={() => setConfirm(null)} />
      )}
      {confirm === "deleteShare" && share && (
        <ConfirmModal
          text={t("deleteLinkConfirm")}
          confirmLabel={t("delete")}
          danger
          onConfirm={async () => {
            try {
              await api.del(`/shares/${share.id}`);
              await load();
            } catch (e) {
              toastError(e);
            }
          }}
          onClose={() => setConfirm(null)}
        />
      )}
      {confirm === "deleteBoard" && (
        <ConfirmModal
          text={t("deleteBoardConfirm")}
          confirmLabel={t("delete")}
          danger
          onConfirm={async () => {
            try {
              await api.del(`/boards/${id}`);
              navigate("/boards");
            } catch (e) {
              toastError(e);
            }
          }}
          onClose={() => setConfirm(null)}
        />
      )}
      {typeof confirm === "number" && (
        <ConfirmModal
          text={t("deleteWorkConfirm")}
          confirmLabel={t("delete")}
          danger
          onConfirm={() => void deleteWork(confirm)}
          onClose={() => setConfirm(null)}
        />
      )}
    </div>
  );
}

function BlurInput({
  value,
  onSave,
  textarea,
  style,
}: {
  value: string;
  onSave: (value: string) => void;
  textarea?: boolean;
  style?: React.CSSProperties;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  const commit = () => {
    if (draft !== value) onSave(draft);
  };
  return textarea ? (
    <textarea className="textarea" style={style} value={draft} onChange={(e) => setDraft(e.target.value)} onBlur={commit} />
  ) : (
    <input
      className="input"
      style={style}
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
    />
  );
}

function WorkTitle({ work, boardId, onSaved }: { work: any; boardId: number; onSaved: (w: any) => void }) {
  const { toastError } = useApp();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(work.title || work.original_name);

  useEffect(() => setDraft(work.title || work.original_name), [work.title, work.original_name]);

  if (!editing) {
    return (
      <button className="work-name" onClick={() => setEditing(true)} title={draft}>
        {draft}
      </button>
    );
  }
  return (
    <input
      className="work-name"
      style={{ color: "var(--text)" }}
      value={draft}
      autoFocus
      onChange={(e) => setDraft(e.target.value)}
      onBlur={async () => {
        setEditing(false);
        if (draft !== (work.title || work.original_name)) {
          try {
            onSaved(await api.patch(`/boards/${boardId}/works/${work.id}`, { title: draft }));
          } catch (e) {
            toastError(e);
          }
        }
      }}
      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
    />
  );
}
