import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { BoardCard } from "../components/BoardCard";
import { Icon } from "../components/Icon";
import { Avatar, Chip, ConfirmModal, EmptyState, ScreenLoading } from "../components/ui";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import {
  fileExt,
  fileSize,
  formatDate,
  formatDateTime,
  formatMoney,
  initials,
  relativeDay,
} from "../lib/format";

const NOTE_ICONS: Record<string, string> = { note: "note", call: "call", meeting: "meeting", email: "email" };

export function ClientCard() {
  const { id } = useParams();
  const { t, locale, user, toast, toastError } = useApp();
  const navigate = useNavigate();
  const [client, setClient] = useState<any>(null);
  const [notes, setNotes] = useState<any[]>([]);
  const [files, setFiles] = useState<any[]>([]);
  const [boards, setBoards] = useState<any[]>([]);
  const [deals, setDeals] = useState<any[]>([]);
  const [tab, setTab] = useState<"history" | "files" | "boards" | "deals">("history");
  const [draft, setDraft] = useState("");
  const [draftKind, setDraftKind] = useState("note");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const data = await api.get(`/clients/${id}`);
      setClient(data);
      setFiles(data.files);
      const notesData = await api.get(`/clients/${id}/notes?per_page=100`);
      setNotes(notesData.items);
      const boardsData = await api.get(`/boards?client_id=${id}`);
      setBoards(boardsData.items);
      // Заявки приходят вместе с карточкой — отдельный запрос не нужен.
      setDeals(data.deals ?? []);
    } catch (e) {
      toastError(e);
      navigate("/clients");
    }
  }, [id, toastError, navigate]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!client) return <ScreenLoading />;

  const addNote = async () => {
    const body = draft.trim();
    if (!body) return;
    try {
      const note = await api.post(`/clients/${id}/notes`, { kind: draftKind, body });
      setNotes((prev) => [note, ...prev]);
      setDraft("");
    } catch (e) {
      toastError(e);
    }
  };

  const deleteNote = async (noteId: number) => {
    try {
      await api.del(`/clients/${id}/notes/${noteId}`);
      setNotes((prev) => prev.filter((n) => n.id !== noteId));
    } catch (e) {
      toastError(e);
    }
  };

  const uploadFiles = async (list: FileList | null) => {
    if (!list) return;
    for (const file of Array.from(list)) {
      try {
        const record = await api.upload(`/clients/${id}/files`, file);
        setFiles((prev) => [record, ...prev]);
      } catch (e) {
        toastError(e);
      }
    }
  };

  const saveContact = async (field: string, value: string) => {
    try {
      const updated = await api.patch(`/clients/${id}`, { [field]: value });
      setClient((prev: any) => ({ ...prev, ...updated }));
    } catch (e) {
      toastError(e);
      void load();
    }
  };

  const contacts = [
    { field: "phone", label: t("phone"), value: client.phone },
    { field: "email", label: t("email"), value: client.email },
    { field: "messenger", label: t("telegram"), value: client.messenger },
    { field: "company", label: t("company"), value: client.company },
  ];

  return (
    <div className="page">
      <Link to="/clients" style={{ display: "inline-flex", alignItems: "center", gap: 6, color: "var(--muted)", fontSize: 13, marginBottom: 20 }}>
        <Icon name="arrowLeft" size={14} />
        {t("clients")}
      </Link>
      <div className="page-head" style={{ alignItems: "flex-start", marginBottom: 24 }}>
        <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
          <Avatar text={initials(client.name)} large />
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <h1 className="page-title" style={{ fontSize: 22 }}>
                {client.name}
              </h1>
              {client.tags.map((tag: string) => (
                <Chip key={tag}>{tag}</Chip>
              ))}
            </div>
            <div className="page-sub" style={{ marginTop: 5 }}>
              {client.company && <>{client.company} · </>}
              {t("added")} {formatDate(client.created_at, locale)}
            </div>
          </div>
        </div>
        <div style={{ display: "flex", gap: 10, position: "relative" }}>
          <button
            className="btn btn-primary"
            onClick={async () => {
              try {
                const board = await api.post("/boards", { title: t("newBoard"), client_id: client.id });
                navigate(`/boards/${board.id}`);
              } catch (e) {
                toastError(e);
              }
            }}
          >
            <Icon name="plus" stroke={2} />
            {t("newBoard")}
          </button>
          <button className="btn-icon" onClick={() => setMenuOpen((open) => !open)}>
            <Icon name="dots" />
          </button>
          {menuOpen && (
            <div className="user-menu" style={{ position: "absolute", top: 42, right: 0, bottom: "auto", left: "auto", width: 200 }}>
              <button
                className="user-menu-item"
                style={{ color: "var(--danger)" }}
                onClick={() => {
                  setMenuOpen(false);
                  setConfirmDelete(true);
                }}
              >
                <Icon name="trash" size={14} />
                {t("deleteClient")}
              </button>
            </div>
          )}
        </div>
      </div>

      <div className="card contact-grid" style={{ marginBottom: 28 }}>
        {contacts.map((contact) => (
          <EditableContact key={contact.field} {...contact} onSave={saveContact} />
        ))}
      </div>

      <div className="tabs">
        <button className={"tab" + (tab === "history" ? " active" : "")} onClick={() => setTab("history")}>
          {t("history")}
        </button>
        <button className={"tab" + (tab === "files" ? " active" : "")} onClick={() => setTab("files")}>
          {t("files")}
          {files.length > 0 && <span className="count">{files.length}</span>}
        </button>
        <button className={"tab" + (tab === "boards" ? " active" : "")} onClick={() => setTab("boards")}>
          {t("boards")}
          {boards.length > 0 && <span className="count">{boards.length}</span>}
        </button>
        {/* Заявки клиента: за год их бывает пять, и «что мы для него делали»
            должно быть вопросом к системе, а не к памяти. */}
        <button className={"tab" + (tab === "deals" ? " active" : "")} onClick={() => setTab("deals")}>
          {t("deals")}
          {deals.length > 0 && <span className="count">{deals.length}</span>}
        </button>
      </div>

      {tab === "history" && (
        <>
          <div className="card" style={{ padding: "14px 16px", marginBottom: 20 }}>
            <input
              style={{ width: "100%", background: "none", border: "none", outline: "none", color: "var(--text)", fontSize: 13.5, fontFamily: "var(--sans)", padding: "2px 0 10px" }}
              placeholder={t("addNotePlaceholder")}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void addNote();
              }}
            />
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <div style={{ display: "flex", gap: 6 }}>
                {(["note", "call", "meeting", "email"] as const).map((kind) => (
                  <button
                    key={kind}
                    className={"option-chip" + (draftKind === kind ? " active" : "")}
                    onClick={() => setDraftKind(kind)}
                  >
                    <Icon name={NOTE_ICONS[kind]} size={13} />
                    {t(kind === "email" ? "emailNote" : kind)}
                  </button>
                ))}
              </div>
              <button className="btn btn-primary" style={{ height: 28, padding: "0 12px", fontSize: 12.5 }} onClick={() => void addNote()}>
                {t("add")}
              </button>
            </div>
          </div>
          <div>
            {notes.map((note) => (
              <div className="feed-item" key={note.id}>
                <div className="feed-icon">
                  <Icon name={NOTE_ICONS[note.kind] ?? "note"} size={14} />
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 4 }}>
                    <span style={{ color: "var(--muted)", fontSize: 12.5, fontWeight: 500 }}>
                      {t(note.kind === "email" ? "emailNote" : note.kind)}
                    </span>
                    <span style={{ color: "var(--faint)", fontSize: 12 }}>{formatDateTime(note.happened_at, locale)}</span>
                    {(user?.role === "root" || note.author_id === user?.id) && (
                      <button className="text-link" style={{ marginLeft: "auto", fontSize: 11.5 }} onClick={() => void deleteNote(note.id)}>
                        {t("delete")}
                      </button>
                    )}
                  </div>
                  <div style={{ color: "var(--text)", fontSize: 13.5, lineHeight: 1.55 }}>{note.body}</div>
                </div>
              </div>
            ))}
            {notes.length === 0 && <EmptyState title={t("addNotePlaceholder")} />}
          </div>
        </>
      )}

      {tab === "files" && (
        <>
          <div
            className="dropzone"
            style={{ marginBottom: 20 }}
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
            {t("dropFiles")} <span style={{ color: "var(--accent)", textDecoration: "underline", textUnderlineOffset: 2 }}>{t("browse")}</span>{" "}
            {t("filesInternal")}
            <input ref={fileInput} type="file" multiple hidden onChange={(e) => void uploadFiles(e.target.files)} />
          </div>
          <div className="list-card">
            {files.map((file) => (
              <div key={file.id} className="list-row hoverable" style={{ height: 52 }}>
                <div className="file-ext">{fileExt(file.original_name)}</div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>{file.original_name}</div>
                  <div style={{ color: "var(--faint)", fontSize: 12 }}>
                    {fileSize(file.size_bytes)} · {formatDate(file.created_at, locale)}
                  </div>
                </div>
                <a href={file.download_url} style={{ color: "var(--muted)", display: "flex" }}>
                  <Icon name="download" />
                </a>
                <button
                  className="text-link"
                  style={{ display: "flex", color: "var(--faint)" }}
                  onClick={async () => {
                    try {
                      await api.del(`/clients/${id}/files/${file.id}`);
                      setFiles((prev) => prev.filter((f) => f.id !== file.id));
                    } catch (e) {
                      toastError(e);
                    }
                  }}
                >
                  <Icon name="trash" />
                </button>
              </div>
            ))}
            {files.length === 0 && <EmptyState title={t("dropFiles") + " " + t("browse")} />}
          </div>
        </>
      )}

      {tab === "boards" && (
        <div className="board-grid">
          {boards.map((board) => (
            <BoardCard key={board.id} board={board} />
          ))}
          {boards.length === 0 && (
            <div className="card" style={{ gridColumn: "1 / -1" }}>
              <EmptyState title={t("noBoardsYet")} />
            </div>
          )}
        </div>
      )}

      {tab === "deals" && (
        <div className="list-card">
          {deals.map((deal) => (
            <Link to={`/deals/${deal.id}`} key={deal.id} className="list-row hoverable">
              <div className="list-row-text">
                <div className="truncate" style={{ color: "var(--text)", fontSize: 13.5, fontWeight: 500 }}>
                  {deal.title}
                </div>
                <div className="truncate" style={{ color: "var(--faint)", fontSize: 12 }}>
                  {deal.manager_name || t("nobody")}
                </div>
              </div>
              {deal.amount !== null && (
                <span style={{ color: "var(--muted)", fontSize: 12.5, flexShrink: 0, fontVariantNumeric: "tabular-nums" }}>
                  {formatMoney(deal.amount, client.currency || "USD", locale)}
                </span>
              )}
              <span style={{ width: 90, textAlign: "right", color: "var(--faint)", fontSize: 12, flexShrink: 0 }}>
                {relativeDay(deal.created_at, locale)}
              </span>
            </Link>
          ))}
          {deals.length === 0 && <EmptyState title={t("noDeals")} />}
        </div>
      )}

      {confirmDelete && (
        <ConfirmModal
          text={t("deleteClientConfirm")}
          confirmLabel={t("delete")}
          danger
          onConfirm={async () => {
            try {
              await api.del(`/clients/${id}`);
              toast(t("deleteClient") + " ✓");
              navigate("/clients");
            } catch (e) {
              toastError(e);
            }
          }}
          onClose={() => setConfirmDelete(false)}
        />
      )}
    </div>
  );
}

function EditableContact({
  field,
  label,
  value,
  onSave,
}: {
  field: string;
  label: string;
  value: string;
  onSave: (field: string, value: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);

  useEffect(() => setDraft(value), [value]);

  return (
    <div className="contact-cell" onClick={() => !editing && setEditing(true)} style={{ cursor: editing ? "auto" : "text" }}>
      <div className="contact-label">{label}</div>
      {editing ? (
        <input
          className="contact-input"
          value={draft}
          autoFocus
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => {
            setEditing(false);
            if (draft !== value) onSave(field, draft);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
            if (e.key === "Escape") {
              setDraft(value);
              setEditing(false);
            }
          }}
        />
      ) : (
        <div className="contact-value">{value || "—"}</div>
      )}
    </div>
  );
}
