import { useCallback, useEffect, useMemo, useState } from "react";

import { Icon } from "../components/Icon";
import {
  Avatar,
  Chip,
  ConfirmModal,
  EmptyState,
  LoadFailed,
  Modal,
  ScreenLoading,
} from "../components/ui";
import { api, type Role } from "../lib/api";
import { useApp } from "../lib/app";
import { useLiveTopic } from "../lib/live";
import { useFailure } from "../lib/failure";
import { useGuard } from "../lib/guard";
import { davnost, formatDate, formatDateTime, initials } from "../lib/format";
import { can } from "../lib/permissions";
import { useReference } from "../lib/reference";
import { nazvanieRoli } from "../lib/roli";

/** Как показать список: таблицей, доской по состоянию или плоским перечнем. */
type Vid = "table" | "board" | "list";

/** Отбор по состоянию. Один активный, а не набор галок: состояний всего три, и
 *  «активные И отключённые» — это просто «все». */
type Otbor = "all" | "active" | "pending" | "disabled";

/** По сколько строк на странице. Пятнадцать — столько влезает в экран ноутбука
 *  без прокрутки страницы; остальные значения на случай крупного штата. */
const NA_STRANITSE = [15, 30, 50, 100];

export function Staff() {
  const { t, locale, user, toast, toastError } = useApp();
  const [items, setItems] = useState<any[] | null>(null);
  const [tempPassword, setTempPassword] = useState<{ name: string; password: string } | null>(null);
  const [confirmDisable, setConfirmDisable] = useState<number | null>(null);
  const [confirmRole, setConfirmRole] = useState<{ id: number; name: string; role: "root" | "manager" } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<{ id: number; name: string } | null>(null);
  /** Пачку спрашивают отдельно: одного отключить — вопрос, пятнадцать — тот же
   *  вопрос, только цена ошибки в пятнадцать раз выше. */
  const [confirmPachka, setConfirmPachka] = useState(false);
  /** Отклонённая заявка не откладывается, а стирает учётную запись насовсем.
   *  Кнопка стояла вплотную к «Одобрить» и не спрашивала ничего. */
  const [confirmOtkaz, setConfirmOtkaz] = useState<{ id: number; name: string } | null>(null);

  const [vid, setVid] = useState<Vid>("table");
  const [otbor, setOtbor] = useState<Otbor>("all");
  const [poRoli, setPoRoli] = useState<string>("");
  const [poisk, setPoisk] = useState("");
  const [stranitsa, setStranitsa] = useState(1);
  const [naStranitse, setNaStranitse] = useState(NA_STRANITSE[0]);
  const [vybrany, setVybrany] = useState<number[]>([]);
  const [zayavkiSkryty, setZayavkiSkryty] = useState(false);
  /** Карточка сотрудника шторкой — только на телефоне: таблицы там нет. */
  const [kartochka, setKartochka] = useState<any | null>(null);

  // Засов на сброс пароля. Не запись, но цена та же: каждое нажатие ставит
  // НОВЫЙ временный пароль, а окно показывает тот ответ, который пришёл
  // последним. Два нажатия — и на экране пароль от одного сброса, а в базе от
  // другого; человек диктует его сотруднику, и тот не входит.
  const resetGuard = useGuard();
  // Засов на пачку: массовое действие идёт по одному запросу на человека, и
  // второе нажатие пустило бы вторую очередь по тем же строкам.
  const pachkaGuard = useGuard();

  // Должности раздаёт не всякий, кто видит список сотрудников: завести человека
  // и решить, что ему можно, — разные по весу решения. Без права список ролей
  // не спрашиваем вовсе, иначе на каждой загрузке стучались бы в закрытую дверь.
  const managesRoles = can(user, "roles.manage");
  // Управление людьми — своё право, и сервер спрашивает именно его
  // (`staff.manage` во всех маршрутах одобрения, отключения и удаления). Маршрут
  // экрана закрыт только `staff.view`, поэтому смотрящий получал полный набор
  // кнопок, включая «Удалить навсегда», и каждая отвечала отказом.
  const managesStaff = can(user, "staff.manage");

  // Список должностей. `null` тут не мелочь, а защита от порчи данных: пока
  // отказ сводился к пустому массиву, выпадающий список рисовался из одного
  // пункта «Без роли», выглядел исправным — и выбор этого единственного пункта
  // СНИМАЛ должность по-настоящему. Не знаем набора — не показываем то, чем
  // назначают: показываем должность как есть и даём повторить загрузку.
  const roles = useReference<Role>(managesRoles ? "/roles" : null);

  const { failure, fail, clear } = useFailure();

  const load = useCallback(() => {
    clear();
    api.get("/staff").then((d) => setItems(d.items)).catch(fail);
  }, [fail, clear]);
  // Новый сотрудник или смена должности — сразу, а не через минуту таймера.
  useLiveTopic(["staff", "roles"], load);

  // Присутствие меняется со временем, и список освежается сам — но ТОЛЬКО в
  // видимой вкладке: пять запросов раз в минуту из свёрнутой это триста в час
  // за список, на который никто не смотрит.
  useEffect(() => {
    const vidno = () => document.visibilityState === "visible";
    load();
    const timer = window.setInterval(() => {
      if (vidno()) load();
    }, 60_000);
    const vernulis = () => {
      if (vidno()) load();
    };
    document.addEventListener("visibilitychange", vernulis);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", vernulis);
    };
  }, [load]);

  // Отбор и поиск возвращают на первую страницу: остаться на пятой, когда строк
  // осталось три, значит увидеть пустоту и решить, что сломалось. Отметки при
  // этом снимаются: «15 выбрано» над одной видимой строкой — ловушка, из неё
  // «Деактивировать» уходит на четырнадцать человек, которых на экране нет.
  useEffect(() => {
    setStranitsa(1);
    setVybrany([]);
  }, [otbor, poRoli, poisk, naStranitse]);

  const vse = items ?? [];
  const zayavki = useMemo(() => vse.filter((u) => u.status === "pending"), [vse]);
  const schyot = useMemo(
    () => ({
      all: vse.length,
      active: vse.filter((u) => u.status === "active").length,
      pending: zayavki.length,
      disabled: vse.filter((u) => u.status === "disabled").length,
    }),
    [vse, zayavki],
  );

  // Отбор, поиск и порядок считаются здесь, а не запросом: сервер отдаёт штат
  // целиком, и у списка сотрудников есть естественный потолок — фирма, а не
  // база клиентов. Лишний круг к серверу на каждую букву ничего не купил бы.
  const otobrannye = useMemo(() => {
    const slovo = poisk.trim().toLowerCase();
    return vse.filter((u) => {
      if (otbor !== "all" && u.status !== otbor) return false;
      if (poRoli === "root" && u.role !== "root") return false;
      if (poRoli && poRoli !== "root" && String(u.role_id ?? "") !== poRoli) return false;
      if (!slovo) return true;
      return [u.name, u.email, u.role_name].some((x) => (x ?? "").toLowerCase().includes(slovo));
    });
  }, [vse, otbor, poRoli, poisk]);

  const stranits = Math.max(1, Math.ceil(otobrannye.length / naStranitse));
  const tekushchaya = Math.min(stranitsa, stranits);
  const ot = (tekushchaya - 1) * naStranitse;
  const stroki = otobrannye.slice(ot, ot + naStranitse);

  const rootCount = vse.filter((u) => u.status === "active" && u.role === "root").length;

  if (!items) return <ScreenLoading error={failure} onRetry={load} />;

  const action = async (path: string) => {
    try {
      await api.post(path);
      load();
    } catch (e) {
      toastError(e);
    }
  };

  const changeRole = async (id: number, role: "root" | "manager") => {
    try {
      await api.post(`/staff/${id}/role`, { role });
      load();
    } catch (e) {
      toastError(e);
    }
  };

  const assignRole = async (id: number, roleId: number | null) => {
    try {
      await api.post(`/roles/assign/${id}`, { role_id: roleId });
      load();
      toast(t("roleAssigned"));
    } catch (e) {
      // Отказ здесь осмысленный: себе роль менять нельзя, и последнего, кто
      // раздаёт права, снять тоже. Показываем причину сервера, а не «ошибка».
      toastError(e);
    }
  };

  const removeUser = async (id: number) => {
    try {
      await api.del(`/staff/${id}`);
      load();
    } catch (e) {
      toastError(e);
    }
  };

  const sbrosPorolya = async (person: any) => {
    if (!resetGuard.take()) return;
    try {
      const result = await api.post(`/staff/${person.id}/reset-password`);
      setTempPassword({ name: person.name, password: result.temp_password });
    } catch (e) {
      toastError(e);
    } finally {
      resetGuard.free();
    }
  };

  /** Пачка идёт ПО ОДНОМУ запросу на человека, а не одной массовой ручкой.
   *
   *  Не от лени: отключение считает инвариант «есть кому раздавать права», и
   *  массовая ручка обязана была бы считать его на «как будет после всей
   *  пачки». Пока такой ручки нет, честнее послать те же запросы, что делает
   *  рука: каждый пересчитает инвариант заново, а первый отказ остановит
   *  очередь и назовёт причину.
   */
  const pachkoy = async (chto: { vid: "role"; roleId: number | null } | { vid: "off" }) => {
    if (!pachkaGuard.take()) return;
    try {
      for (const id of vybrany) {
        if (chto.vid === "role") {
          await api.post(`/roles/assign/${id}`, { role_id: chto.roleId });
        } else {
          await api.post(`/staff/${id}/disable`);
        }
      }
      setVybrany([]);
      load();
    } catch (e) {
      toastError(e);
      load();
    } finally {
      pachkaGuard.free();
    }
  };

  const prisutstvie = (person: any) =>
    person.is_online
      ? t("online")
      : person.last_seen_at
        ? davnost(person.last_seen_at, locale)
        : t("offline");

  const sostoyanie = (person: any) => {
    if (person.status === "active") return <Chip variant="success">{t("stActive")}</Chip>;
    if (person.status === "pending") return <Chip variant="warning">{t("stPending")}</Chip>;
    return <Chip variant="danger">{t("stOff")}</Chip>;
  };

  const perekluchit = (id: number) =>
    setVybrany((bylo) => (bylo.includes(id) ? bylo.filter((x) => x !== id) : [...bylo, id]));

  /** Кого пачка тронуть не может: себя (ни роль сменить, ни отключить) и
   *  последнего root. Галка у них была бы приглашением в отказ сервера, а
   *  первый отказ ещё и останавливал бы очередь на остальных. */
  const mozhnoVPachku = (person: any) => person.id !== user?.id && !(person.role === "root" && rootCount <= 1);

  const dostupnye = stroki.filter(mozhnoVPachku);
  const vseVybrany = dostupnye.length > 0 && dostupnye.every((u) => vybrany.includes(u.id));

  /** Что можно сделать со строкой. Считается один раз: те же правила нужны и
   *  таблице, и мобильной карточке, а разойдясь, они дали бы кнопку, которой
   *  сервер откажет. */
  const pravila = (person: any) => {
    const svoy = person.id === user?.id;
    const root = person.role === "root";
    return {
      svoy,
      root,
      // Последнего root не снять и не удалить — иначе управлять станет некому.
      posledniyRoot: root && rootCount <= 1,
      mozhnoSbros: !root && managesStaff && !svoy,
      mozhnoOtklyuchit: !root && managesStaff && !svoy && person.status === "active",
      mozhnoVernut: managesStaff && person.status === "disabled",
      // Блок заявок закрывается кнопкой «Разобрать позже», и без этого одобрить
      // человека до перезагрузки экрана было бы нечем: в строке такого действия
      // не было вовсе.
      mozhnoOdobrit: managesStaff && person.status === "pending",
    };
  };

  const deystviya = (person: any) => {
    const p = pravila(person);
    if (p.svoy) return <span className="staff-you">{t("you")}</span>;
    return (
      <div className="staff-actions">
        {p.mozhnoSbros && (
          <button
            type="button"
            className="btn-icon staff-act"
            title={t("resetPassword")}
            aria-label={t("resetPassword")}
            disabled={resetGuard.busy}
            onClick={() => void sbrosPorolya(person)}
          >
            <Icon name="refresh" size={14} />
          </button>
        )}
        {p.mozhnoOtklyuchit && (
          <button
            type="button"
            className="btn-icon staff-act"
            title={t("deactivate")}
            aria-label={t("deactivate")}
            onClick={() => setConfirmDisable(person.id)}
          >
            <Icon name="logout" size={14} />
          </button>
        )}
        {p.mozhnoVernut && (
          <button
            type="button"
            className="btn-icon staff-act"
            title={t("restore")}
            aria-label={t("restore")}
            onClick={() => void action(`/staff/${person.id}/enable`)}
          >
            <Icon name="check" size={14} />
          </button>
        )}
        {p.mozhnoOdobrit && (
          <button
            type="button"
            className="btn-icon staff-act"
            title={t("approve")}
            aria-label={t("approve")}
            onClick={() => void action(`/staff/${person.id}/approve`)}
          >
            <Icon name="check" size={14} />
          </button>
        )}
        {!p.posledniyRoot && managesRoles && (
          <button
            type="button"
            className="btn-icon staff-act"
            title={p.root ? t("makeManager") : t("makeRoot")}
            aria-label={p.root ? t("makeManager") : t("makeRoot")}
            onClick={() =>
              setConfirmRole({ id: person.id, name: person.name, role: p.root ? "manager" : "root" })
            }
          >
            <Icon name="star" size={14} />
          </button>
        )}
        {!p.posledniyRoot && managesStaff && (
          <button
            type="button"
            className="btn-icon staff-act staff-act-danger"
            title={t("deletePermanently")}
            aria-label={t("deletePermanently")}
            onClick={() => setConfirmDelete({ id: person.id, name: person.name })}
          >
            <Icon name="trash" size={14} />
          </button>
        )}
      </div>
    );
  };

  /** Должность: чип у root, список у того, кому её вправе менять. */
  const dolzhnost = (person: any) => {
    const p = pravila(person);
    if (p.root) return <Chip variant="brand" title={t("rootHasEverything")}>{t("root")}</Chip>;
    if (managesRoles && !p.svoy && roles.items) {
      return (
        <select
          className="input input-sm staff-role-select"
          value={person.role_id ?? ""}
          aria-label={t("assignRole")}
          onChange={(e) => void assignRole(person.id, e.target.value ? Number(e.target.value) : null)}
        >
          <option value="">{t("noRole")}</option>
          {roles.items.map((role) => (
            <option key={role.id} value={role.id}>
              {nazvanieRoli(t, role.name)}
            </option>
          ))}
        </select>
      );
    }
    return (
      <Chip title={p.svoy ? t("cannotChangeOwnRole") : undefined}>
        {nazvanieRoli(t, person.role_name) || t("noRole")}
      </Chip>
    );
  };

  const CHIPY: { klyuch: Otbor; podpis: string }[] = [
    { klyuch: "all", podpis: t("all") },
    { klyuch: "active", podpis: t("stActive") },
    { klyuch: "pending", podpis: t("stPending") },
    { klyuch: "disabled", podpis: t("stOff") },
  ];

  return (
    <div className="page page-wide">
      <div className="staff-head">
        <div>
          <h1 className="page-title">{t("staff")}</h1>
          <div className="page-sub">{t("staffSub")}</div>
        </div>
        <a className="btn btn-secondary" href="/api/v1/staff/export.csv">
          <Icon name="download" size={13} />
          {t("exportCsv")}
        </a>
      </div>

      {zayavki.length > 0 && !zayavkiSkryty && (
        // Заявки отдельным блоком над таблицей, а не строками в ней: их
        // разбирают, а не просматривают, и в общем списке они терялись бы
        // между активными.
        <div className="staff-pending">
          <div className="staff-pending-head">
            <Icon name="alert" size={14} />
            <h2 className="staff-pending-title">{t("signupRequests")}</h2>
            <span className="staff-pending-count">{zayavki.length}</span>
            <button type="button" className="staff-pending-later" onClick={() => setZayavkiSkryty(true)}>
              {t("laterAll")}
            </button>
          </div>
          {zayavki.map((person) => (
            <div key={person.id} className="staff-pending-row">
              <Avatar text={initials(person.name)} src={person.avatar_url} />
              <div className="staff-pending-who">
                <div className="truncate staff-pending-name">{person.name}</div>
                <div className="truncate staff-pending-mail">
                  {person.email} · {t("requested")} {formatDateTime(person.created_at, locale)}
                </div>
              </div>
              {managesStaff && (
                <>
                  <button
                    className="btn btn-primary btn-sm"
                    onClick={() => void action(`/staff/${person.id}/approve`)}
                  >
                    <Icon name="check" size={13} stroke={2} />
                    {t("approve")}
                  </button>
                  <button
                    className="btn btn-secondary btn-sm staff-reject"
                    onClick={() => setConfirmOtkaz({ id: person.id, name: person.name })}
                  >
                    {t("reject")}
                  </button>
                </>
              )}
            </div>
          ))}
        </div>
      )}

      <div className="staff-bar">
        <div className="staff-views">
          {([
            { klyuch: "table", znachok: "receipt", podpis: t("viewTable") },
            { klyuch: "board", znachok: "boards", podpis: t("viewBoard") },
            { klyuch: "list", znachok: "docs", podpis: t("viewList") },
          ] as const).map((v) => (
            <button
              key={v.klyuch}
              type="button"
              className={`staff-view ${vid === v.klyuch ? "staff-view-on" : ""}`}
              aria-pressed={vid === v.klyuch}
              onClick={() => setVid(v.klyuch)}
            >
              <Icon name={v.znachok} size={14} />
              {v.podpis}
            </button>
          ))}
        </div>
        <div className="staff-search">
          <Icon name="search" size={14} />
          <input
            type="text"
            value={poisk}
            placeholder={t("searchStaff")}
            aria-label={t("searchStaff")}
            onChange={(e) => setPoisk(e.target.value)}
          />
        </div>
      </div>

      <div className="staff-filters">
        {CHIPY.map((c) => (
          <button
            key={c.klyuch}
            type="button"
            className={`staff-chip ${otbor === c.klyuch ? "staff-chip-on" : ""}`}
            aria-pressed={otbor === c.klyuch}
            onClick={() => setOtbor(c.klyuch)}
          >
            {c.podpis}
            <span className="staff-chip-count">{schyot[c.klyuch]}</span>
          </button>
        ))}
        {roles.items && roles.items.length > 0 && (
          <select
            className="input input-sm staff-filter-role"
            value={poRoli}
            aria-label={t("colRole")}
            onChange={(e) => setPoRoli(e.target.value)}
          >
            <option value="">{t("colRole")}</option>
            <option value="root">{t("root")}</option>
            {roles.items.map((role) => (
              <option key={role.id} value={String(role.id)}>
                {nazvanieRoli(t, role.name)}
              </option>
            ))}
          </select>
        )}
        <span className="staff-shown">{t("shownOf", { a: otobrannye.length, b: vse.length })}</span>
      </div>

      {/* Строка стоит над списком, а не в каждой строке сотрудника: беда одна
          на весь экран, и повторять её двадцать раз значит спрятать. */}
      {roles.failure !== null && <LoadFailed error={roles.failure} onRetry={roles.reload} />}

      {otobrannye.length === 0 ? (
        <EmptyState title={t("nothingFound")} />
      ) : vid === "table" ? (
        <div className="staff-table-card">
          <div className="staff-scroll">
            <div className="staff-grid">
              <div className="staff-th staff-th-pick">
                <input
                  type="checkbox"
                  aria-label={t("selectAll")}
                  checked={vseVybrany}
                  onChange={() => setVybrany(vseVybrany ? [] : dostupnye.map((u) => u.id))}
                />
              </div>
              <div className="staff-th staff-th-name">{t("colName")}</div>
              <div className="staff-th">{t("colEmail")}</div>
              <div className="staff-th">{t("colRole")}</div>
              <div className="staff-th">{t("colStatus")}</div>
              <div className="staff-th">{t("colPresence")}</div>
              <div className="staff-th staff-num">{t("colDeals")}</div>
              <div className="staff-th">{t("colJoined")}</div>
              <div className="staff-th">{t("colLastLogin")}</div>
              <div className="staff-th">{t("colActions")}</div>

              {stroki.map((person) => (
                <div key={person.id} className="staff-tr" role="row">
                  <div className="staff-td staff-td-pick">
                    <input
                      type="checkbox"
                      aria-label={person.name}
                      checked={vybrany.includes(person.id)}
                      disabled={!mozhnoVPachku(person)}
                      onChange={() => perekluchit(person.id)}
                    />
                  </div>
                  <div className="staff-td staff-td-name">
                    <Avatar text={initials(person.name)} src={person.avatar_url} online={person.is_online} />
                    <span className="truncate staff-name">{person.name}</span>
                  </div>
                  <div className="staff-td">
                    <a className="truncate staff-mail" href={`mailto:${person.email}`}>{person.email}</a>
                  </div>
                  <div className="staff-td">{dolzhnost(person)}</div>
                  <div className="staff-td">{sostoyanie(person)}</div>
                  <div className="staff-td staff-quiet">{prisutstvie(person)}</div>
                  <div className="staff-td staff-num staff-quiet">
                    {person.deals_open > 0 ? person.deals_open : "—"}
                  </div>
                  <div className="staff-td staff-quiet">{formatDate(person.created_at, locale)}</div>
                  <div className="staff-td staff-quiet" title={person.last_login_at ? undefined : t("neverOnline")}>
                    {person.last_login_at ? formatDateTime(person.last_login_at, locale) : "—"}
                  </div>
                  <div className="staff-td">{deystviya(person)}</div>
                </div>
              ))}
            </div>
          </div>

          {vybrany.length > 0 && (
            <div className="staff-bulk">
              <span className="staff-bulk-count">{vybrany.length}</span>
              <span className="staff-bulk-word">{t("selected")}</span>
              <span className="staff-bulk-line" />
              {managesRoles && roles.items && (
                <select
                  className="input input-sm staff-bulk-role"
                  value=""
                  aria-label={t("bulkRole")}
                  disabled={pachkaGuard.busy}
                  onChange={(e) => {
                    if (!e.target.value) return;
                    // «Без роли» — своё значение, а не пустое: два пункта с
                    // одинаковым value отличались бы только порядком, и
                    // управляемый список возвращал бы выбор в заголовок.
                    const chey = e.target.value === "none" ? null : Number(e.target.value);
                    void pachkoy({ vid: "role", roleId: chey });
                  }}
                >
                  <option value="">{t("bulkRole")}</option>
                  <option value="none">{t("noRole")}</option>
                  {roles.items.map((role) => (
                    <option key={role.id} value={String(role.id)}>
                      {nazvanieRoli(t, role.name)}
                    </option>
                  ))}
                </select>
              )}
              {managesStaff && (
                <button
                  type="button"
                  className="btn btn-secondary btn-sm staff-reject"
                  disabled={pachkaGuard.busy}
                  onClick={() => setConfirmPachka(true)}
                >
                  {t("bulkOff")}
                </button>
              )}
              <button
                type="button"
                className="btn-icon staff-bulk-close"
                aria-label={t("close")}
                onClick={() => setVybrany([])}
              >
                <Icon name="x" size={14} />
              </button>
            </div>
          )}

          <div className="staff-foot">
            <span className="staff-quiet">{t("rowsPerPage")}</span>
            <select
              className="input input-sm staff-per-page"
              value={naStranitse}
              aria-label={t("rowsPerPage")}
              onChange={(e) => setNaStranitse(Number(e.target.value))}
            >
              {NA_STRANITSE.map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
            <span className="staff-quiet staff-num-text">
              {t("pageRange", {
                a: otobrannye.length === 0 ? 0 : ot + 1,
                b: Math.min(ot + naStranitse, otobrannye.length),
                n: otobrannye.length,
              })}
            </span>
            <span className="staff-foot-spacer" />
            <div className="staff-pager">
              <button
                type="button"
                className="staff-page"
                disabled={tekushchaya <= 1}
                aria-label={t("pagePrev")}
                onClick={() => setStranitsa(tekushchaya - 1)}
              >
                ‹
              </button>
              {Array.from({ length: stranits }, (_, i) => i + 1).map((n) => (
                <button
                  key={n}
                  type="button"
                  className={`staff-page ${n === tekushchaya ? "staff-page-on" : ""}`}
                  aria-current={n === tekushchaya ? "page" : undefined}
                  aria-label={t("pageNumber", { n })}
                  onClick={() => setStranitsa(n)}
                >
                  {n}
                </button>
              ))}
              <button
                type="button"
                className="staff-page"
                disabled={tekushchaya >= stranits}
                aria-label={t("pageNext")}
                onClick={() => setStranitsa(tekushchaya + 1)}
              >
                ›
              </button>
            </div>
          </div>
        </div>
      ) : vid === "board" ? (
        <div className="staff-board">
          {([
            { klyuch: "online", podpis: t("online"), kto: (u: any) => u.is_online },
            { klyuch: "active", podpis: t("stActive"), kto: (u: any) => !u.is_online && u.status === "active" },
            { klyuch: "pending", podpis: t("stPending"), kto: (u: any) => u.status === "pending" },
            { klyuch: "off", podpis: t("stOff"), kto: (u: any) => u.status === "disabled" },
          ] as const).map((kolonka) => {
            const svoi = otobrannye.filter(kolonka.kto);
            return (
              <div key={kolonka.klyuch} className="staff-col">
                <div className="staff-col-head">
                  <span className={`staff-dot staff-dot-${kolonka.klyuch}`} />
                  <span className="staff-col-name">{kolonka.podpis}</span>
                  <span className="staff-col-count">{svoi.length}</span>
                </div>
                <div className="staff-col-body">
                  {svoi.map((person) => (
                    <div key={person.id} className="staff-card">
                      <div className="staff-card-top">
                        <Avatar text={initials(person.name)} src={person.avatar_url} online={person.is_online} />
                        <div className="staff-card-who">
                          <div className="truncate staff-name">{person.name}</div>
                          <div className="truncate staff-quiet staff-card-mail">{person.email}</div>
                        </div>
                      </div>
                      <div className="staff-card-bottom">
                        <Chip>{person.role === "root" ? t("root") : nazvanieRoli(t, person.role_name) || t("noRole")}</Chip>
                        <span className="staff-card-deals">
                          {person.deals_open > 0 ? t("staffDealsOpen", { n: person.deals_open }) : "—"}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="staff-flat">
          {otobrannye.map((person) => (
            <div key={person.id} className="staff-flat-row">
              <Avatar text={initials(person.name)} src={person.avatar_url} />
              <span className="truncate staff-flat-name">{person.name}</span>
              <span className="truncate staff-quiet staff-flat-mail">{person.email}</span>
              <span className="staff-quiet staff-flat-role">
                {person.role === "root" ? t("root") : nazvanieRoli(t, person.role_name) || t("noRole")}
              </span>
              <span className="staff-quiet staff-flat-seen">{prisutstvie(person)}</span>
            </div>
          ))}
        </div>
      )}

      {/* Телефон: таблицы нет намеренно — десять колонок в 390 пикселях
          нечитаемы. Те же строки идут списком, карточка открывается шторкой. */}
      <div className="staff-mobile">
        {otobrannye.map((person) => (
          <button key={person.id} type="button" className="staff-m-row" onClick={() => setKartochka(person)}>
            <Avatar text={initials(person.name)} src={person.avatar_url} online={person.is_online} />
            <span className="staff-m-who">
              <span className="truncate staff-name">{person.name}</span>
              <span className="truncate staff-quiet staff-m-role">
                {person.role === "root" ? t("root") : nazvanieRoli(t, person.role_name) || t("noRole")}
              </span>
            </span>
            <Icon name="chevronRight" size={15} />
          </button>
        ))}
      </div>

      {kartochka && (
        <Modal title={kartochka.name} onClose={() => setKartochka(null)}>
          <div className="staff-m-chips">
            {sostoyanie(kartochka)}
            <Chip>{kartochka.role === "root" ? t("root") : nazvanieRoli(t, kartochka.role_name) || t("noRole")}</Chip>
          </div>
          <div className="staff-m-fields">
            <div className="staff-m-field">
              <span className="staff-quiet">{t("colEmail")}</span>
              <a className="staff-mail" href={`mailto:${kartochka.email}`}>{kartochka.email}</a>
            </div>
            <div className="staff-m-field">
              <span className="staff-quiet">{t("colPresence")}</span>
              <span>{prisutstvie(kartochka)}</span>
            </div>
            <div className="staff-m-field">
              <span className="staff-quiet">{t("colJoined")}</span>
              <span>{formatDate(kartochka.created_at, locale)}</span>
            </div>
            <div className="staff-m-field">
              <span className="staff-quiet">{t("colLastLogin")}</span>
              <span>{kartochka.last_login_at ? formatDateTime(kartochka.last_login_at, locale) : t("neverOnline")}</span>
            </div>
            <div className="staff-m-field">
              <span className="staff-quiet">{t("colDeals")}</span>
              <span>{kartochka.deals_open > 0 ? kartochka.deals_open : "—"}</span>
            </div>
          </div>
          <div className="staff-m-actions">{deystviya(kartochka)}</div>
        </Modal>
      )}

      {tempPassword && (
        <Modal title={t("resetPassword")} onClose={() => setTempPassword(null)}>
          <div style={{ fontSize: 13.5, marginBottom: 12 }}>{t("tempPasswordIs", { name: tempPassword.name })}</div>
          <div
            className="card"
            style={{ padding: "12px 16px", fontFamily: "monospace", fontSize: 16, letterSpacing: "0.05em", textAlign: "center", marginBottom: 12, userSelect: "all" }}
          >
            {tempPassword.password}
          </div>
          <div style={{ color: "var(--faint)", fontSize: 12.5 }}>{t("tempPasswordHint")}</div>
        </Modal>
      )}
      {confirmOtkaz && (
        <ConfirmModal
          text={t("rejectConfirm", { name: confirmOtkaz.name })}
          confirmLabel={t("reject")}
          danger
          onConfirm={() => void action(`/staff/${confirmOtkaz.id}/reject`)}
          onClose={() => setConfirmOtkaz(null)}
        />
      )}
      {confirmPachka && (
        <ConfirmModal
          text={t("bulkOffConfirm", { n: vybrany.length })}
          confirmLabel={t("bulkOff")}
          danger
          onConfirm={() => void pachkoy({ vid: "off" })}
          onClose={() => setConfirmPachka(false)}
        />
      )}
      {confirmDisable !== null && (
        <ConfirmModal
          text={t("deactivateConfirm")}
          confirmLabel={t("deactivate")}
          danger
          onConfirm={() => void action(`/staff/${confirmDisable}/disable`)}
          onClose={() => setConfirmDisable(null)}
        />
      )}
      {confirmRole && (
        <ConfirmModal
          text={
            confirmRole.role === "root"
              ? t("makeRootConfirm", { name: confirmRole.name })
              : t("makeManagerConfirm", { name: confirmRole.name })
          }
          confirmLabel={confirmRole.role === "root" ? t("makeRoot") : t("makeManager")}
          danger={confirmRole.role === "root"}
          onConfirm={() => void changeRole(confirmRole.id, confirmRole.role)}
          onClose={() => setConfirmRole(null)}
        />
      )}
      {confirmDelete && (
        <ConfirmModal
          text={t("deleteUserConfirm", { name: confirmDelete.name })}
          confirmLabel={t("deletePermanently")}
          danger
          onConfirm={() => void removeUser(confirmDelete.id)}
          onClose={() => setConfirmDelete(null)}
        />
      )}
    </div>
  );
}
